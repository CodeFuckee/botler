// 概览页「开放 Issue」板块测试（issue #64）：聚合读取所有已启用仓库的
// 开放（opened）issue，外层按仓库优先级排序（后端保证），仓库内按最后
// 更新时间降序；板块 15 秒轮询（与流水线板块同频），点击 issue 跳 GitLab。
//
// 断言：
// 1. Overview.jsx 轮询 GET /api/issues/overview，渲染仓库分组卡片；
// 2. 分组卡片显示仓库名与优先级徽章，组内每条 issue 渲染 #iid、标题链接
//    （web_url 新窗口）、最后更新时间；
// 3. 空状态（无仓库 / 全部仓库无开放 issue）显示占位文案；
// 4. 单仓库查询失败（errors 非空）与整体请求失败均不崩溃；
// 5. issue 字段缺失兜底（无标题 / 无更新时间）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, ISSUE_POLL_MS } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('概览页轮询 GET /api/issues/overview 聚合开放 issue', () => {
  assert.match(overview, /api\.get\('\/api\/issues\/overview'\)/,
               '应调用 /api/issues/overview 聚合接口')
  assert.match(overview, /ISSUE_POLL_MS/, '应使用 ISSUE_POLL_MS 轮询常量')
  assert.equal(ISSUE_POLL_MS, 15000, 'issue 板块轮询间隔应为 15 秒')
})

test('板块渲染仓库分组：仓库名、优先级徽章、issue 链接', () => {
  assert.match(overview, /repo_name/, '分组卡片应显示仓库名')
  assert.match(overview, /priority/, '分组卡片应显示仓库优先级徽章')
  assert.match(overview, /web_url/, 'issue 应使用 web_url 跳转 GitLab')
  assert.match(overview, /fmtAgo/, '应使用 fmtAgo 显示最后更新时间')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询 tasks / pipelines / issues 三个端点，
// mock 按路径分流；issues 数据可注入。
async function renderOverview(issuesPayload) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
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
  return { renderer, renderError }
}

const ISSUES_PAYLOAD = {
  repos: [
    {
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        {
          iid: 64, title: '概览页面增加读取已启用的仓库issue',
          updated_at: '2026-08-14 10:20:00',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
          labels: [
            { name: 'feature', color: '428BCA', text_color: 'FFFFFF' },
            { name: 'ui', color: '69D100', text_color: 'FFFFFF' },
          ],
          milestone: 'v1.0',
          assignees: [
            { name: 'Agent', username: 'agent',
              avatar_url: 'https://gitlab.example.com/avatar/agent.png' },
          ],
          user_notes_count: 3,
        },
        {
          iid: 63, title: '对账遇 token 失效时兜底',
          updated_at: '2026-08-14 08:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/63',
          labels: [{ name: 'bug', color: null, text_color: null }],
          milestone: null,
          assignees: [],
          user_notes_count: 0,
        },
      ],
    },
    {
      repo_id: 2, repo_name: 'shipyard', priority: 20,
      issues: [
        {
          iid: 7, title: '修复登录问题',
          updated_at: '2026-08-13 01:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/shipyard/-/issues/7',
        },
      ],
    },
  ],
  errors: [],
  total: 3,
}

test('渲染开放 issue 板块：仓库分组、优先级徽章、issue 链接与时间', async () => {
  const { renderer, renderError } = await renderOverview(ISSUES_PAYLOAD)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('botler'), '应显示仓库名 botler')
    assert.ok(text.includes('shipyard'), '应显示仓库名 shipyard')
    assert.ok(text.includes('优先级'), '应显示优先级徽章')
    assert.ok(text.includes('概览页面增加读取已启用的仓库issue'), '应显示 issue 标题')
    assert.ok(text.includes('修复登录问题'), '应显示第二个仓库的 issue 标题')
    // 每条 issue 渲染为指向 GitLab 的新窗口链接
    const links = root.findAllByType('a').filter((a) => a.props.href?.includes('/-/issues/'))
    assert.equal(links.length, 3, '三条 issue 均应渲染为 GitLab 链接')
    assert.ok(links.every((a) => a.props.target === '_blank'), '链接应新窗口打开')
    assert.ok(
      links.some((a) => a.props.href === ISSUES_PAYLOAD.repos[0].issues[0].web_url),
      '链接 href 应为 issue web_url',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无仓库时显示空状态', async () => {
  const { renderer, renderError } = await renderOverview({ repos: [], errors: [], total: 0 })
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无开放 issue'), '应显示空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('仓库存在但均无开放 issue 时显示空状态', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues: [] }],
    errors: [], total: 0,
  })
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无开放 issue'), '全部仓库无 issue 时应显示空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('单仓库查询失败（errors 非空）时正常渲染不崩溃', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [
      {
        repo_id: 1, repo_name: 'botler', priority: 10,
        issues: [{ iid: 1, title: '正常', updated_at: '2026-08-14 02:00:00',
                   web_url: 'https://gitlab.example.com/x/-/issues/1' }],
      },
      { repo_id: 2, repo_name: 'broken', priority: 20, issues: [] },
    ],
    errors: ['仓库 broken: 模拟 GitLab API 故障'],
    total: 1,
  })
  try {
    assert.equal(renderError, null, 'errors 非空时渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('正常'), '正常仓库的 issue 应照常显示')
    assert.ok(text.includes('broken'), '失败仓库分组照常显示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('整体请求失败时显示错误提示而不崩溃', async () => {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    throw new Error('网络错误')
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
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('网络错误'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('issue 字段缺失兜底：无标题显示占位、无更新时间不崩', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 9, title: null, updated_at: null,
          web_url: 'https://gitlab.example.com/x/-/issues/9' },
      ],
    }],
    errors: [], total: 1,
  })
  try {
    assert.equal(renderError, null, '字段缺失时渲染不应崩溃')
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    // JSX 中 `#{i.iid} — {title}` 是分离的文本节点，拼接 span/a 文本断言
    const issueText = root
      .findAll((n) => n.type === 'span' || n.type === 'a')
      .flatMap((n) => n.props.children || [])
      .join('')
    assert.ok(issueText.includes('#9'), `应显示 issue 编号（实际: ${issueText}）`)
    assert.ok(issueText.includes('—'), '标题缺失应显示占位符')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- issue #71：参考 GitLab issue 页面美化——标签胶囊 / 里程碑 / 头像 / 评论数 ----

test('渲染标签彩色胶囊：名称与 GitLab 标签色背景/文字色', async () => {
  const { renderer, renderError } = await renderOverview(ISSUES_PAYLOAD)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const pills = root.findAll((n) => n.props.className === 'label-pill')
    assert.equal(pills.length, 3, '三个标签均应渲染为胶囊（feature/ui/bug）')
    const byName = Object.fromEntries(pills.map((p) => [p.props.children, p.props.style]))
    assert.deepEqual(byName.feature,
                     { background: '#428BCA', color: '#FFFFFF' },
                     '有颜色标签应使用 GitLab 标签背景色与文字色')
    assert.deepEqual(byName.ui, { background: '#69D100', color: '#FFFFFF' })
    assert.equal(byName.bug, undefined, '无颜色标签不应带内联颜色（走中性样式）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染里程碑胶囊与评论数', async () => {
  const { renderer, renderError } = await renderOverview(ISSUES_PAYLOAD)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('v1.0'), '应显示里程碑标题')
    assert.ok(text.includes('💬'), '应显示评论数图标')
    const milestone = root.findAll((n) => n.props.className === 'milestone-chip')
    assert.equal(milestone.length, 1, '仅一条 issue 有里程碑时应只渲染一个里程碑胶囊')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染 assignee 头像（avatar_url + name）', async () => {
  const { renderer, renderError } = await renderOverview(ISSUES_PAYLOAD)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const imgs = root.findAll((n) => n.type === 'img' && n.props.className === 'assignee-avatar')
    assert.equal(imgs.length, 1, '仅一条 issue 有 assignee 时应渲染一个头像')
    assert.equal(imgs[0].props.src, 'https://gitlab.example.com/avatar/agent.png')
    assert.equal(imgs[0].props.alt, 'Agent', '头像 alt 应为 assignee 姓名')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('美化字段整体缺失（旧接口返回）时渲染不崩溃', async () => {
  // shipyard 的 issue 不带任何美化字段——旧版本后端/缓存数据兜底
  const { renderer, renderError } = await renderOverview(ISSUES_PAYLOAD)
  try {
    assert.equal(renderError, null, '美化字段缺失时渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('修复登录问题'), '无美化字段的 issue 应照常显示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('assignee 无 avatar_url 时渲染首字符占位，不渲染无 src 的 img', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [{
        iid: 5, title: '无头像',
        updated_at: '2026-08-14 02:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/5',
        labels: [], milestone: null,
        assignees: [{ name: 'Chen', username: 'chen', avatar_url: null }],
        user_notes_count: null,
      }],
    }],
    errors: [], total: 1,
  })
  try {
    assert.equal(renderError, null, 'avatar_url 缺失时渲染不应崩溃')
    const root = renderer.root
    const fallback = root.findAll(
      (n) => String(n.props.className || '').includes('avatar-fallback'))
    assert.equal(fallback.length, 1, '无头像应渲染首字符占位')
    assert.equal(fallback[0].props.children, 'C', '占位应为首字符大写')
    const imgs = root.findAll((n) => n.type === 'img')
    assert.equal(imgs.length, 0, 'avatar_url 缺失不应渲染 img（避免空 src 请求）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
