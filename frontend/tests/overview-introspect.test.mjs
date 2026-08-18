// 概览页「自省」按钮测试（issue #187）：每个仓库卡片右上角新增「自省」
// 按钮，点击后调用后端同步审查接口 POST /api/repos/{repo_id}/introspect
// ——AI agent 审查该仓库的功能与实现情况，把改进建议写入该仓库的 issue
// （分配人 = 仓库 owner）；请求中禁用按钮防重复点击，成功后刷新开放
// issue 列表并展示已创建 issue 的跳转链接。
//
// 断言：
// 1. 渲染：每个仓库卡片头右上角渲染「自省」按钮，与「对账」「添加
//    Issue」按钮并排成组（.issue-repo-actions）；
// 2. 点击：POST /api/repos/{repo_id}/introspect 参数正确（repo_id 对
//    应被点击仓库）；请求中按钮禁用并显示「自省中…」；
// 3. 成功：显示「已创建自省 issue」+ issue 编号链接，并刷新开放 issue
//    列表（再次请求 /api/issues/overview）；
// 4. 失败：接口异常显示错误信息。
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
// overview-repo-reconcile.test.mjs 一致）。api 也经 vite 加载，与
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

function introspectBtns(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('introspect-btn'))
}

// 拼接渲染树全部文本（与对账测试一致，不引入 JSON 分隔符）
function treeText(renderer) {
  const walk = (n) => {
    if (n == null) return ''
    if (typeof n === 'string' || typeof n === 'number') return String(n)
    if (Array.isArray(n)) return n.map(walk).join('')
    return walk(n.children)
  }
  return walk(renderer.toJSON())
}

async function clickIntrospect(renderer, index = 0, postImpl) {
  const btns = introspectBtns(renderer)
  assert.ok(btns.length > index, `找不到第 ${index} 个自省按钮`)
  mock.method(api, 'post', postImpl)
  await TestRenderer.act(async () => {
    btns[index].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
}

// ---- 源码与样式断言 ----

test('源码含「自省」按钮、审查接口调用与结果提示', () => {
  assert.match(overviewSrc, /introspect-btn/, '应有自省按钮类名')
  assert.match(overviewSrc, /tr\('overview\.introspect'\)/, '自省按钮应经 t() 国际化')
  assert.equal(zhCN['overview.introspect'], '自省', '中文「自省」文案应保留')
  assert.match(
    overviewSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.repo_id\}\/introspect`\)/,
    '点击后应调 POST /api/repos/{repo_id}/introspect',
  )
  assert.match(
    overviewSrc,
    /disabled=\{introspectResults\[r\.repo_id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(overviewSrc, /tr\('overview\.introspecting'\)/, '「自省中…」应经 t() 国际化')
  assert.equal(zhCN['overview.introspecting'], '自省中…', '中文「自省中…」文案应保留')
  assert.match(overviewSrc, /IntrospectResult/, '应渲染自省结果组件')
  assert.match(overviewSrc, /tr\('overview\.introspectCreated'\)/, '「已创建自省 issue」应经 t() 国际化')
  assert.equal(zhCN['overview.introspectCreated'], '已创建自省 issue', '中文文案应保留')
})

test('styles.css：自省按钮与结果样式', () => {
  const btn = styles.match(/\.introspect-btn\s*\{([^}]*)\}/)
  assert.ok(btn, 'styles.css 应有 .introspect-btn 规则')
  assert.match(btn[1], /white-space:\s*nowrap/, '自省按钮不应换行')
  assert.ok(styles.includes('.introspect-result'), 'styles.css 应有 .introspect-result 规则')
})

// ---- 渲染与点击 ----

test('渲染：每个仓库卡片头右上角渲染「自省」按钮，与对账/添加按钮并排成组', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = introspectBtns(renderer)
    assert.equal(btns.length, 2, '两个仓库卡片各应有一个自省按钮')
    const actions = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('issue-repo-actions'))
    assert.equal(actions.length, 2, '每个仓库卡片应有一个操作组容器')
    const groupCls = actions[0].findAll((n) => n.type === 'button')
      .map((b) => String(b.props.className || ''))
    assert.ok(groupCls.some((c) => c.includes('introspect-btn')), '操作组应含自省按钮')
    assert.ok(groupCls.some((c) => c.includes('reconcile-btn')), '操作组应含对账按钮')
    assert.ok(groupCls.some((c) => c.includes('add-issue-btn')), '操作组应含添加 Issue 按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击：POST /api/repos/{repo_id}/introspect 参数正确（对应对被点击仓库）', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    const postCalls = []
    await clickIntrospect(renderer, 1, async (pathname) => {
      postCalls.push(pathname)
      return { review: '报告', issue: { iid: 77, web_url: 'https://gitlab.example.com/x/-/issues/77' } }
    })
    assert.deepEqual(postCalls, ['/api/repos/2/introspect'],
                     '应只调用被点击仓库（repo_id=2）的自省接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('请求中：按钮禁用并显示「自省中…」防重复点击，完成后恢复', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    let resolvePost = null
    mock.method(api, 'post', () => new Promise((resolve) => { resolvePost = resolve }))
    const btns = introspectBtns(renderer)
    await TestRenderer.act(async () => {
      btns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const loading = introspectBtns(renderer)
    assert.equal(loading[0].props.disabled, true, '请求中应禁用按钮防重复点击')
    assert.ok(treeText(renderer).includes('自省中'), '请求中应显示「自省中…」')
    // 请求完成：结果展示 + 按钮恢复可点击
    await TestRenderer.act(async () => {
      resolvePost({ review: '报告', issue: { iid: 77, web_url: 'https://gitlab.example.com/x/-/issues/77' } })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const after = introspectBtns(renderer)
    assert.equal(Boolean(after[0].props.disabled), false, '请求完成后应恢复可点击')
    assert.ok(treeText(renderer).includes('已创建自省 issue'), '完成后应展示自省结果')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('成功：显示已创建自省 issue 链接并刷新开放 issue 列表', async () => {
  const { renderer, renderError, getCalls } = await renderOverview()
  try {
    assert.equal(renderError, null)
    const issuesBefore = getCalls.filter((p) => p === '/api/issues/overview').length
    await clickIntrospect(renderer, 0, async () => ({
      review: '审查报告',
      issue: { iid: 88, web_url: 'https://gitlab.example.com/x/-/issues/88' },
    }))
    const text = treeText(renderer)
    assert.ok(text.includes('已创建自省 issue'), '应显示已创建自省 issue')
    assert.ok(text.includes('#88'), '应显示新建 issue 编号')
    // href 是属性不是文本，treeText 取不到，改在渲染树里找跳转链接
    const links = renderer.root.findAll((n) => n.type === 'a')
    assert.ok(links.some((a) => String(a.props.href || '').includes('/issues/88')),
              '应提供跳转 GitLab 的 issue 链接')
    const issuesAfter = getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(issuesAfter > issuesBefore, '成功后应刷新开放 issue 列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('失败：接口异常显示错误信息且按钮恢复', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickIntrospect(renderer, 0, async () => {
      throw new Error('AI 审查失败: 模型不可用')
    })
    assert.ok(treeText(renderer).includes('AI 审查失败: 模型不可用'),
              '失败应显示后端错误信息')
    const btns = introspectBtns(renderer)
    assert.equal(Boolean(btns[0].props.disabled), false, '失败后按钮应恢复可点击')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
