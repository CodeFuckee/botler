// 概览页「发掘」按钮测试（issue #189）：每个仓库卡片右上角新增「发掘」
// 按钮，点击后调用后端同步发掘接口 POST /api/repos/{repo_id}/discover
// ——AI agent 根据该仓库实现的功能去 GitHub 搜索类似仓库、翻找用户需求
// issue，整理成若干条需求写入该仓库的 issue（分配人 = 仓库 owner，一条
// 需求一个 issue）；请求中禁用按钮防重复点击，成功后刷新开放 issue 列表
// 并展示已创建发掘 issue 的跳转链接列表。
//
// issue #301：无论是否找到用户需求 issue，都把相似仓库列出——响应始终
// 携带 similar_repos；未找到用户需求 issue（count=0）时显示「未找到用户
// 需求 issue」提示，有创建时显示「已创建 N 个发掘 issue」+ 编号链接，
// 两种情况下方均列出相似仓库（名称链接 + star + 描述）。
//
// 断言：
// 1. 渲染：每个仓库卡片头右上角渲染「发掘」按钮，与「自省」「对账」
//    「添加 Issue」按钮并排成组（.issue-repo-actions）；
// 2. 点击：POST /api/repos/{repo_id}/discover 参数正确（repo_id 对
//    应被点击仓库）；请求中按钮禁用并显示「发掘中…」；
// 3. 成功：显示「已创建 N 个发掘 issue」+ issue 编号链接列表 + 相似
//    仓库列表，并刷新开放 issue 列表（再次请求 /api/issues/overview）；
// 4. 无需求 issue：count=0 时显示「未找到用户需求 issue」+ 相似仓库列表；
// 5. 失败：接口异常显示错误信息。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-introspect.test.mjs 一致）。api 也经 vite 加载，与
// Overview 组件内 import 的是同一模块实例，可对 api 做 method mock。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const ISSUES_PAYLOAD = {
  repos: [
    { repo_id: 1, repo_name: 'botler', priority: 10, issues: [
      { iid: 11, title: '已有 issue',
        updated_at: '2026-08-15 01:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/11' },
    ] },
    { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
  ],
  errors: [], total: 1,
}

// 挂载 Overview：api.get 按路径分流，返回 get 调用记录供刷新断言
async function renderOverview({ issuesPayload = ISSUES_PAYLOAD } = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
    if (pathname === '/api/settings') return { gitlab: { owner_token_masked: 'glpat-***' } }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError, getCalls }
}

function discoverBtns(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('discover-btn'))
}

// 拼接渲染树全部文本（与自省测试一致，不引入 JSON 分隔符）
function treeText(renderer) {
  const walk = (n) => {
    if (n == null) return ''
    if (typeof n === 'string' || typeof n === 'number') return String(n)
    if (Array.isArray(n)) return n.map(walk).join('')
    return walk(n.children)
  }
  return walk(renderer.toJSON())
}

async function clickDiscover(renderer, index = 0, postImpl) {
  const btns = discoverBtns(renderer)
  assert.ok(btns.length > index, `找不到第 ${index} 个发掘按钮`)
  mock.method(api, 'post', postImpl)
  await TestRenderer.act(async () => {
    btns[index].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
}

// ---- 源码与样式断言 ----

test('源码含「发掘」按钮、发掘接口调用与结果提示', () => {
  assert.match(overviewSrc, /discover-btn/, '应有发掘按钮类名')
  assert.match(overviewSrc, /tr\('overview\.discover'\)/, '发掘按钮应经 t() 国际化')
  assert.equal(zhCN['overview.discover'], '发掘', '中文「发掘」文案应保留')
  assert.match(
    overviewSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.repo_id\}\/discover`\)/,
    '点击后应调 POST /api/repos/{repo_id}/discover',
  )
  assert.match(
    overviewSrc,
    /disabled=\{discoverResults\[r\.repo_id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(overviewSrc, /tr\('overview\.discovering'\)/, '「发掘中…」应经 t() 国际化')
  assert.equal(zhCN['overview.discovering'], '发掘中…', '中文「发掘中…」文案应保留')
  assert.match(overviewSrc, /DiscoverResult/, '应渲染发掘结果组件')
  assert.match(overviewSrc, /tr\('overview\.discoverCreated', \{ n: createdCount \}\)/,
               '「已创建 N 个发掘 issue」应经 t() 国际化并插值数量')
  assert.ok(zhCN['overview.discoverCreated'].includes('个发掘 issue'), '中文文案应保留')
  // issue #301：无论是否找到用户需求 issue，都展示相似仓库列表
  assert.match(overviewSrc, /result\.similar_repos/, '应读取响应中的相似仓库列表')
  assert.match(overviewSrc, /tr\('overview\.discoverRepos'\)/, '相似仓库标题应经 t() 国际化')
  assert.equal(zhCN['overview.discoverRepos'], '相似仓库', '中文「相似仓库」文案应保留')
  assert.match(overviewSrc, /tr\('overview\.discoverNoIssue'\)/, '「未找到用户需求 issue」应经 t() 国际化')
  assert.ok(zhCN['overview.discoverNoIssue'].includes('未找到用户需求 issue'), '中文文案应保留')
  assert.match(overviewSrc, /tr\('overview\.openSimilarRepo'\)/, '相似仓库链接应经 t() 国际化')
  assert.ok(zhCN['overview.openSimilarRepo'].includes('相似仓库'), '中文文案应保留')
  assert.match(overviewSrc, /compass/, '发掘按钮应使用 compass 图标')
})

test('styles.css：发掘按钮与结果样式', () => {
  const btn = styles.match(/\.discover-btn\s*\{([^}]*)\}/)
  assert.ok(btn, 'styles.css 应有 .discover-btn 规则')
  assert.match(btn[1], /white-space:\s*nowrap/, '发掘按钮不应换行')
  assert.ok(styles.includes('.discover-result'), 'styles.css 应有 .discover-result 规则')
  assert.ok(styles.includes('.discover-repos-title'), 'styles.css 应有 .discover-repos-title 规则')
  assert.ok(styles.includes('.discover-repo'), 'styles.css 应有 .discover-repo 规则')
})

// ---- 渲染与点击 ----

test('渲染：每个仓库卡片头右上角渲染「发掘」按钮，与自省/对账/添加按钮并排成组', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = discoverBtns(renderer)
    assert.equal(btns.length, 2, '两个仓库卡片各应有一个发掘按钮')
    const actions = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('issue-repo-actions'))
    assert.equal(actions.length, 2, '每个仓库卡片应有一个操作组容器')
    const groupCls = actions[0].findAll((n) => n.type === 'button')
      .map((b) => String(b.props.className || ''))
    assert.ok(groupCls.some((c) => c.includes('discover-btn')), '操作组应含发掘按钮')
    assert.ok(groupCls.some((c) => c.includes('introspect-btn')), '操作组应含自省按钮')
    assert.ok(groupCls.some((c) => c.includes('reconcile-btn')), '操作组应含对账按钮')
    assert.ok(groupCls.some((c) => c.includes('add-issue-btn')), '操作组应含添加 Issue 按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击：POST /api/repos/{repo_id}/discover 参数正确（对应对被点击仓库）', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    const postCalls = []
    await clickDiscover(renderer, 1, async (pathname) => {
      postCalls.push(pathname)
      return { issues: [{ iid: 77, web_url: 'https://gitlab.example.com/x/-/issues/77' }], count: 1,
               similar_repos: [{ full_name: 'a/b', html_url: 'https://github.com/a/b',
                                description: '相似项目', stars: 100 }] }
    })
    assert.deepEqual(postCalls, ['/api/repos/2/discover'],
                     '应只调用被点击仓库（repo_id=2）的发掘接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('请求中：按钮禁用并显示「发掘中…」防重复点击，完成后恢复', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    let resolvePost = null
    mock.method(api, 'post', () => new Promise((resolve) => { resolvePost = resolve }))
    const btns = discoverBtns(renderer)
    await TestRenderer.act(async () => {
      btns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const loading = discoverBtns(renderer)
    assert.equal(loading[0].props.disabled, true, '请求中应禁用按钮防重复点击')
    assert.ok(treeText(renderer).includes('发掘中'), '请求中应显示「发掘中…」')
    // 请求完成：结果展示 + 按钮恢复可点击
    await TestRenderer.act(async () => {
      resolvePost({ issues: [{ iid: 77, web_url: 'https://gitlab.example.com/x/-/issues/77' }], count: 1,
                   similar_repos: [{ full_name: 'a/b', html_url: 'https://github.com/a/b',
                                     description: '相似项目', stars: 100 }] })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const after = discoverBtns(renderer)
    assert.equal(Boolean(after[0].props.disabled), false, '请求完成后应恢复可点击')
    assert.ok(treeText(renderer).includes('已创建 1 个发掘 issue'), '完成后应展示发掘结果')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('成功：显示已创建发掘 issue 链接列表并刷新开放 issue 列表', async () => {
  const { renderer, renderError, getCalls } = await renderOverview()
  try {
    assert.equal(renderError, null)
    const issuesBefore = getCalls.filter((p) => p === '/api/issues/overview').length
    await clickDiscover(renderer, 0, async () => ({
      issues: [
        { iid: 88, web_url: 'https://gitlab.example.com/x/-/issues/88' },
        { iid: 89, web_url: 'https://gitlab.example.com/x/-/issues/89' },
      ],
      count: 2,
      similar_repos: [
        { full_name: 'a/b', html_url: 'https://github.com/a/b', description: '相似项目', stars: 100 },
        { full_name: 'c/d', html_url: 'https://github.com/c/d', description: null, stars: 50 },
      ],
    }))
    const text = treeText(renderer)
    assert.ok(text.includes('已创建 2 个发掘 issue'), '应显示已创建发掘 issue 数量')
    assert.ok(text.includes('#88') && text.includes('#89'), '应显示新建 issue 编号')
    // href 是属性不是文本，treeText 取不到，改在渲染树里找跳转链接
    const links = renderer.root.findAll((n) => n.type === 'a')
    assert.ok(links.some((a) => String(a.props.href || '').includes('/issues/88')),
              '应提供跳转 GitLab 的 issue 链接')
    assert.ok(links.some((a) => String(a.props.href || '').includes('/issues/89')),
              '应提供第二个发掘 issue 的跳转链接')
    const issuesAfter = getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(issuesAfter > issuesBefore, '成功后应刷新开放 issue 列表')
    // issue #301：相似仓库列表随结果展示（名称链接 + star）
    assert.ok(text.includes('相似仓库'), '应展示相似仓库标题')
    assert.ok(text.includes('a/b'), '应展示第一个相似仓库名')
    assert.ok(text.includes('c/d'), '应展示第二个相似仓库名')
    assert.ok(text.includes('100'), '应展示相似仓库 star')
    const repoLinks = renderer.root.findAll(
      (n) => n.type === 'a' && String(n.props.href || '').includes('github.com/'))
    assert.ok(repoLinks.some((a) => String(a.props.href) === 'https://github.com/a/b'),
              '应提供相似仓库跳转链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无需求 issue：count=0 时显示「未找到用户需求 issue」并列出相似仓库', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickDiscover(renderer, 0, async () => ({
      issues: [],
      count: 0,
      similar_repos: [
        { full_name: 'x/y', html_url: 'https://github.com/x/y', description: '无需求仓库', stars: 7 },
      ],
    }))
    const text = treeText(renderer)
    assert.ok(text.includes('未找到用户需求 issue'), 'count=0 应提示未找到用户需求 issue')
    assert.ok(text.includes('相似仓库'), '应展示相似仓库标题')
    assert.ok(text.includes('x/y'), '应列出相似仓库名')
    assert.ok(text.includes('无需求仓库'), '应列出相似仓库描述')
    assert.ok(!text.includes('已创建'), '未创建需求时不应显示「已创建」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('失败：接口异常显示错误信息且按钮恢复', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickDiscover(renderer, 0, async () => {
      throw new Error('GitHub 搜索类似仓库失败: 限流')
    })
    assert.ok(treeText(renderer).includes('GitHub 搜索类似仓库失败: 限流'),
              '失败应显示后端错误信息')
    const btns = discoverBtns(renderer)
    assert.equal(Boolean(btns[0].props.disabled), false, '失败后按钮应恢复可点击')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
