// 概览页「开放 Issue」板块 bot 终态标签分组测试（issue #80）：issue
// 按 bot 状态分组为三组——bot-failed（处理失败）/ bot-done（已完成待
// 确认）/ 其他（两种标签都不带），组标题带计数、只渲染非空组；带终态
// 标签的 issue 在标题旁显示醒目状态徽章（绿=done、红=failed），且不再
// 重复渲染为普通标签胶囊。
//
// 断言：
// 1. groupIssuesByBotLabel 纯函数：三组正确归类、组内保持原始相对顺序
//    （后端已按 updated_at 降序）、bot-done 与 bot-failed 并存时归 done
//    （失败后重试成功两标签并存，成功为最终态）；
// 2. 边界：labels 缺失/null/非数组、label 元素为 null/缺 name、空数组、
//    单元素、100 条混合数据均不崩且归类正确；
// 3. 渲染：组标题顺序 bot-failed → bot-done → 其他（用户指定顺序）、
//    计数、空组不渲染、状态徽章、终态标签不再渲染为 label-pill、
//    徽章位于 issue-link 按钮内。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, groupIssuesByBotLabel, botStatusKey,
        ISSUE_GROUPS, BOT_STATUS_META } =
  await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('Overview.jsx 导出分组函数与组定义，渲染使用分组逻辑', () => {
  assert.equal(typeof groupIssuesByBotLabel, 'function',
               '应导出 groupIssuesByBotLabel 分组函数')
  assert.equal(typeof botStatusKey, 'function', '应导出 botStatusKey 判定函数')
  assert.match(overview, /groupIssuesByBotLabel/,
               '渲染应调用 groupIssuesByBotLabel 分组')
})

test('组显示顺序为 bot-failed → bot-done → 其他（用户指定顺序）', () => {
  assert.deepEqual(ISSUE_GROUPS.map((g) => g.key),
                   ['failed', 'done', 'other'],
                   '组顺序应为 failed/done/other')
  assert.equal(BOT_STATUS_META.done.cls, 'issue-status-done')
  assert.equal(BOT_STATUS_META.failed.cls, 'issue-status-failed')
})

// ---- groupIssuesByBotLabel / botStatusKey 纯函数测试 ----

test('正常路径：三种状态 issue 各归其组', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'bot-failed' }] },
    { iid: 2, labels: [{ name: 'bot-done' }] },
    { iid: 3, labels: [{ name: 'feature' }] },
  ]
  const g = groupIssuesByBotLabel(issues)
  assert.deepEqual(g.failed.map((i) => i.iid), [1], 'bot-failed 归 failed 组')
  assert.deepEqual(g.done.map((i) => i.iid), [2], 'bot-done 归 done 组')
  assert.deepEqual(g.other.map((i) => i.iid), [3], '无终态标签归 other 组')
})

test('组内保持原始相对顺序（后端 updated_at 降序不被破坏）', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'bot-done' }] },
    { iid: 2, labels: [{ name: 'feature' }] },
    { iid: 3, labels: [{ name: 'bot-done' }] },
    { iid: 4, labels: [{ name: 'bot-failed' }] },
    { iid: 5, labels: [] },
    { iid: 6, labels: [{ name: 'bot-failed' }] },
  ]
  const g = groupIssuesByBotLabel(issues)
  assert.deepEqual(g.failed.map((i) => i.iid), [4, 6], 'failed 组保持原序')
  assert.deepEqual(g.done.map((i) => i.iid), [1, 3], 'done 组保持原序')
  assert.deepEqual(g.other.map((i) => i.iid), [2, 5], 'other 组保持原序')
})

test('bot-done 与 bot-failed 并存时归 done 组（成功为最终态）', () => {
  assert.equal(botStatusKey({ labels: [{ name: 'bot-failed' },
                                        { name: 'bot-done' }] }), 'done',
               '两标签并存应判定为 done')
  const g = groupIssuesByBotLabel([
    { iid: 1, labels: [{ name: 'bot-failed' }, { name: 'bot-done' }] },
  ])
  assert.equal(g.done.length, 1, '并存 issue 应归 done 组')
  assert.equal(g.failed.length, 0, 'failed 组应为空')
})

test('边界：labels 缺失 / null / 非数组归 other 组且不崩', () => {
  assert.equal(botStatusKey({ iid: 1 }), null, 'labels 缺失应为 null')
  assert.equal(botStatusKey({ iid: 2, labels: null }), null, 'labels 为 null 应为 null')
  assert.equal(botStatusKey({ iid: 3, labels: 'bot-done' }), null,
               'labels 为非数组应为 null')
  const g = groupIssuesByBotLabel([
    { iid: 1 },
    { iid: 2, labels: null },
    { iid: 3, labels: 'bot-done' },
  ])
  assert.equal(g.other.length, 3, '缺字段 issue 全部归 other 组')
})

test('边界：label 元素为 null / 缺 name 时忽略且不崩', () => {
  assert.equal(botStatusKey({ labels: [null, { name: 'bot-done' }] }), 'done',
               '含 null 元素的 labels 应跳过 null 继续判定')
  assert.equal(botStatusKey({ labels: [{}, { color: 'x' }] }), null,
               '缺 name 的 label 元素应忽略')
  assert.equal(botStatusKey({ labels: [null] }), null, '仅 null 元素应判定为 null')
})

test('边界：空数组返回三组全空', () => {
  const g = groupIssuesByBotLabel([])
  assert.deepEqual(g, { failed: [], done: [], other: [] })
})

test('边界：单元素仅归一组', () => {
  const g = groupIssuesByBotLabel([{ iid: 1, labels: [{ name: 'bot-done' }] }])
  assert.equal(g.done.length, 1)
  assert.equal(g.failed.length, 0)
  assert.equal(g.other.length, 0)
})

test('边界：100 条混合数据分组计数正确、总量不丢', () => {
  const issues = []
  for (let i = 1; i <= 100; i++) {
    const kind = i % 3
    const labels = kind === 0 ? [{ name: 'bot-done' }]
      : kind === 1 ? [{ name: 'bot-failed' }] : [{ name: 'feature' }]
    issues.push({ iid: i, labels })
  }
  const g = groupIssuesByBotLabel(issues)
  assert.equal(g.done.length, 33, '3n 位置 33 条归 done')
  assert.equal(g.failed.length, 34, '3n+1 位置 34 条归 failed')
  assert.equal(g.other.length, 33, '3n+2 位置 33 条归 other')
  assert.equal(g.done.length + g.failed.length + g.other.length, 100,
               '分组不得丢失任何 issue')
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

// 混合仓库：bot-failed 1 条、bot-done 2 条、其他 2 条
const MIXED_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', priority: 10,
    issues: [
      { iid: 101, title: '失败一',
        updated_at: '2026-08-15 10:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/101',
        labels: [{ name: 'bot-failed' }] },
      { iid: 102, title: '完成一',
        updated_at: '2026-08-15 09:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/102',
        labels: [{ name: 'bot-done' }, { name: 'feature' }] },
      { iid: 103, title: '普通一',
        updated_at: '2026-08-15 08:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/103',
        labels: [{ name: 'bug' }] },
      { iid: 104, title: '完成二',
        updated_at: '2026-08-15 07:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/104',
        labels: [{ name: 'bot-done' }] },
      { iid: 105, title: '普通二',
        updated_at: '2026-08-15 06:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/105' },
    ],
  }],
  errors: [], total: 5,
}

test('渲染：混合数据按 failed→done→other 顺序渲染组标题与计数', async () => {
  const { renderer, renderError } = await renderOverview(MIXED_PAYLOAD)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.equal(titles.length, 3, '三种状态均应渲染组标题')
    assert.deepEqual(titles.map((t) => t.props.children),
                     ['❌ bot-failed', '✅ bot-done', '📋 其他'],
                     '组标题顺序应为 failed → done → other')
    const counts = root.findAll((n) => n.props.className === 'issue-group-count')
    // JSX 中 `{items.length} 个` 产生 [数字, ' 个'] 两个子节点
    assert.deepEqual(counts.map((c) => c.props.children),
                     [[1, ' 个'], [2, ' 个'], [2, ' 个']], '组标题计数应正确')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：bot 状态徽章显示在 issue-link 按钮内，类名区分状态', async () => {
  const { renderer, renderError } = await renderOverview(MIXED_PAYLOAD)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const statuses = root.findAll(
      (n) => String(n.props.className || '').includes('issue-status'))
    const done = statuses.filter(
      (n) => n.props.className === 'issue-status issue-status-done')
    const failed = statuses.filter(
      (n) => n.props.className === 'issue-status issue-status-failed')
    assert.equal(done.length, 2, '两条 bot-done 应渲染完成徽章')
    assert.equal(failed.length, 1, '一条 bot-failed 应渲染失败徽章')
    assert.deepEqual(done.map((n) => n.props.children), ['✅ bot-done', '✅ bot-done'])
    assert.deepEqual(failed.map((n) => n.props.children), ['❌ bot-failed'])
    // 徽章位于 issue-link 按钮内（标题旁），不破坏点击打开右边栏
    const linkBtns = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    assert.equal(linkBtns.length, 5, '五条 issue 均应渲染为列表项按钮')
    // props.children 是 React 元素（非 test instance），按 className 匹配
    const badgesInLinks = linkBtns.filter((b) =>
      (b.props.children || []).some((c) => c && typeof c === 'object'
        && String(c.props?.className || '').includes('issue-status')))
    assert.equal(badgesInLinks.length, 3,
                 '带终态标签的三条 issue 的按钮内均应包含状态徽章')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：终态标签不再渲染为普通标签胶囊，其他标签保留', async () => {
  const { renderer, renderError } = await renderOverview(MIXED_PAYLOAD)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const pills = root.findAll((n) => n.props.className === 'label-pill')
    const names = pills.map((p) => p.props.children)
    assert.deepEqual(names.sort(), ['bug', 'feature'],
                     '仅普通标签渲染胶囊，bot-done/bot-failed 由徽章替代')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：全普通 issue 仓库仅渲染「其他」组，无状态徽章', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 1, title: '普通',
          updated_at: '2026-08-15 01:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/1',
          labels: [{ name: 'feature' }] },
      ],
    }],
    errors: [], total: 1,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => t.props.children), ['📋 其他'],
                     '无终态标签时仅渲染「其他」组')
    const statuses = root.findAll(
      (n) => String(n.props.className || '').includes('issue-status'))
    assert.equal(statuses.length, 0, '普通 issue 不应渲染状态徽章')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：空组不渲染组标题（无 bot-failed 时无对应标题）', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 1, title: '完成',
          updated_at: '2026-08-15 01:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/1',
          labels: [{ name: 'bot-done' }] },
        { iid: 2, title: '普通',
          updated_at: '2026-08-15 00:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/2',
          labels: [{ name: 'feature' }] },
      ],
    }],
    errors: [], total: 2,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => t.props.children),
                     ['✅ bot-done', '📋 其他'],
                     '无 bot-failed 的仓库不应渲染该空组标题')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：全部仓库无开放 issue 时显示全局空状态（不渲染组标题）', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues: [] }],
    errors: [], total: 0,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.equal(titles.length, 0, '全局空状态不应渲染任何组标题')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无开放 issue'), '应保持全局空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：部分仓库无开放 issue 时该仓库卡片保持仓库级空状态文案', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [
      { repo_id: 1, repo_name: 'botler', priority: 10,
        issues: [{ iid: 1, title: '有 issue',
                   updated_at: '2026-08-15 02:00:00',
                   web_url: 'https://gitlab.example.com/x/-/issues/1' }] },
      { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
    ],
    errors: [], total: 1,
  })
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('该仓库暂无开放 issue'),
              '空仓库卡片应显示仓库级空状态文案')
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.equal(titles.length, 1, '仅非空仓库渲染组标题')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：issue 缺失 labels 字段时照常归「其他」组渲染', async () => {
  const { renderer, renderError } = await renderOverview({
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 9, title: '无标签',
          updated_at: '2026-08-15 02:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/9' },
      ],
    }],
    errors: [], total: 1,
  })
  try {
    assert.equal(renderError, null, 'labels 缺失渲染不应崩溃')
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => t.props.children), ['📋 其他'])
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
