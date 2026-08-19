// 概览页「开放 Issue」板块正在运行的 issue 高亮测试（issue #99）：正在
// 执行的任务（running/retrying，即 LIVE_STATUSES）与开放 issue 列表按
// repo_id + issue_iid 匹配，命中的 issue 列表项高亮（浅蓝背景 + 左侧蓝色
// 竖条），标题旁显示「⚙️ 运行中」徽章；任务结束（从任务列表消失）后
// 高亮自动消失。数据复用概览页已有的任务轮询，不新增接口。
//
// 断言：
// 1. runningIssueKeys 纯函数：running/retrying 任务收集为 repo_id:iid 键
//    集合，非 LIVE_STATUSES 状态（queued/succeeded/failed）不收集；
// 2. 边界：tasks 缺失/null/非数组、任务缺 repo_id/issue_iid、null 值、
//    数字/字符串类型混合、重复任务（Set 去重）、100 条混合任务均不崩
//    且判定正确；
// 3. 渲染：命中的 issue 列表项带 issue-item-running 类与「⚙️ 运行中」
//    徽章，未命中的无高亮；跨仓库同 iid 不误高亮（repo_id 区分）；
//    retrying 同样高亮；任务列表为空时全部无高亮；高亮不破坏分组与
//    其他徽章（bot 终态徽章、标签胶囊并存）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, runningIssueKeys, LIVE_STATUSES } =
  await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// 渲染树节点 → 纯文本（递归；Lucide 图标组件无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}


// ---- 数据流源码断言 ----

test('Overview.jsx 导出 runningIssueKeys 函数，渲染使用该匹配逻辑', () => {
  assert.equal(typeof runningIssueKeys, 'function',
               '应导出 runningIssueKeys 匹配函数')
  assert.match(overview, /runningIssueKeys/,
               '渲染应调用 runningIssueKeys 判定高亮')
  assert.match(overview, /issue-item-running/,
               '渲染应包含 issue-item-running 高亮类')
  assert.match(overview, /issue-status-running/,
               '渲染应包含「运行中」徽章类')
})

// ---- runningIssueKeys 纯函数测试 ----

test('正常路径：running/retrying 任务收集为 repo_id:iid 键集合', () => {
  const tasks = [
    { id: 1, status: 'running', repo_id: 5, issue_iid: 101 },
    { id: 2, status: 'retrying', repo_id: 5, issue_iid: 102 },
    { id: 3, status: 'running', repo_id: 6, issue_iid: 101 },
  ]
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 3, '三个活跃任务应收集三个键')
  assert.ok(keys.has('5:101'), 'repo 5 + iid 101 应命中')
  assert.ok(keys.has('5:102'), 'retrying 任务同样收集')
  assert.ok(keys.has('6:101'), '跨仓库同 iid 为不同键')
})

test('非活跃状态（queued/succeeded/failed 等）不收集', () => {
  const tasks = [
    { id: 1, status: 'queued', repo_id: 1, issue_iid: 1 },
    { id: 2, status: 'succeeded', repo_id: 1, issue_iid: 2 },
    { id: 3, status: 'failed', repo_id: 1, issue_iid: 3 },
    { id: 4, status: 'interrupted', repo_id: 1, issue_iid: 4 },
    { id: 5, status: 'running', repo_id: 1, issue_iid: 5 },
  ]
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 1, '仅 running 任务被收集')
  assert.ok(keys.has('1:5'), 'running 任务应命中')
})

test('边界：tasks 缺失 / null / 非数组返回空集合且不崩', () => {
  assert.equal(runningIssueKeys(undefined).size, 0, 'undefined 应为空集合')
  assert.equal(runningIssueKeys(null).size, 0, 'null 应为空集合')
  assert.equal(runningIssueKeys('running').size, 0, '非数组应为空集合')
  assert.equal(runningIssueKeys({}).size, 0, '对象应为空集合')
  assert.equal(runningIssueKeys([]).size, 0, '空数组应为空集合')
})

test('边界：任务元素为 null / 缺 repo_id / 缺 issue_iid 跳过且不崩', () => {
  const tasks = [
    null,
    { id: 1, status: 'running', repo_id: 1 },                    // 缺 issue_iid
    { id: 2, status: 'running', issue_iid: 2 },                  // 缺 repo_id
    { id: 3, status: 'running', repo_id: null, issue_iid: 3 },   // repo_id 为 null
    { id: 4, status: 'running', repo_id: 4, issue_iid: null },   // issue_iid 为 null
    { id: 5, status: 'running', repo_id: 0, issue_iid: 0 },      // 0 为合法值
    { id: 6, status: 'running', repo_id: 6, issue_iid: 6 },      // 正常任务
  ]
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 2, '仅字段齐全的任务被收集（含 0 值）')
  assert.ok(keys.has('0:0'), 'repo_id/issue_iid 为 0 应视为合法值')
  assert.ok(keys.has('6:6'), '正常任务应命中')
})

test('边界：任务元素缺 status 字段跳过（防御异常数据）', () => {
  const keys = runningIssueKeys([
    { id: 1, repo_id: 1, issue_iid: 1 },
    { id: 2, status: null, repo_id: 2, issue_iid: 2 },
    { id: 3, status: 'running', repo_id: 3, issue_iid: 3 },
  ])
  assert.equal(keys.size, 1, '缺 status 的任务应跳过')
  assert.ok(keys.has('3:3'), '正常任务应命中')
})

test('边界：数字/字符串类型混合仍正确匹配（键统一字符串化）', () => {
  const tasks = [
    { id: 1, status: 'running', repo_id: '5', issue_iid: '101' },
    { id: 2, status: 'running', repo_id: 5, issue_iid: 101 },
  ]
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 1, '字符串与数字应归一为同一键（Set 去重）')
  assert.ok(keys.has('5:101'), '字符串化的键应可命中')
})

test('边界：重复任务（重试记录）Set 去重', () => {
  const tasks = [
    { id: 1, status: 'running', repo_id: 1, issue_iid: 1 },
    { id: 2, status: 'retrying', repo_id: 1, issue_iid: 1 },
    { id: 3, status: 'running', repo_id: 1, issue_iid: 1 },
  ]
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 1, '同一 issue 的多个任务记录应去重为一个键')
})

test('边界：100 条混合任务计数正确', () => {
  const tasks = []
  for (let i = 1; i <= 100; i++) {
    tasks.push({ id: i, status: i % 2 === 0 ? 'running' : 'succeeded',
                 repo_id: 1, issue_iid: i })
  }
  const keys = runningIssueKeys(tasks)
  assert.equal(keys.size, 50, '100 条中 50 条 running 应收集 50 个键')
})

test('LIVE_STATUSES 与任务板块定义一致（running + retrying）', () => {
  assert.deepEqual([...LIVE_STATUSES].sort(), ['retrying', 'running'],
                   '活跃状态应为 running 与 retrying 两个')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询 tasks / pipelines / issues 三个端点，
// mock 按路径分流；tasks 与 issues 数据均可注入。
async function renderOverview(tasksPayload, issuesPayload) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return tasksPayload
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

// 三个仓库：repo 1 有一条 running 任务对应 issue 101；issue 102 无任务
// 对应；repo 2 同 iid 101 但无任务（验证 repo_id 区分）
function buildIssuesPayload() {
  return {
    repos: [
      { repo_id: 1, repo_name: 'botler', priority: 10,
        issues: [
          { iid: 101, title: '运行中的 issue',
            updated_at: '2026-08-15 10:00:00',
            web_url: 'https://gitlab.example.com/x/-/issues/101',
            labels: [{ name: 'feature' }] },
          { iid: 102, title: '普通的 issue',
            updated_at: '2026-08-15 09:00:00',
            web_url: 'https://gitlab.example.com/x/-/issues/102',
            labels: [{ name: 'bug' }] },
        ] },
      { repo_id: 2, repo_name: 'shipyard', priority: 20,
        issues: [
          { iid: 101, title: '跨仓库同 iid',
            updated_at: '2026-08-15 08:00:00',
            web_url: 'https://gitlab.example.com/x/-/issues/101',
            labels: [] },
        ] },
    ],
    errors: [], total: 3,
  }
}

function buildTasksPayload(overrides = {}) {
  return {
    tasks: [
      { id: 1, status: 'running', repo_id: 1, repo_name: 'botler',
        issue_iid: 101, issue_title: '运行中的 issue',
        issue_url: 'https://gitlab.example.com/x/-/issues/101', ...overrides },
    ],
    total: 1, stats: {},
  }
}

test('渲染：正在运行的任务对应 issue 高亮并显示「运行中」徽章', async () => {
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload(), buildIssuesPayload())
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 命中的列表项带高亮类，未命中的不带
    const items = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item'))
    assert.equal(items.length, 3, '三条 issue 均应渲染为列表项')
    const runningItems = items.filter((n) =>
      String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 1, '仅运行中的 issue 列表项高亮')
    // 「运行中」徽章只出现在命中项内
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 1, '仅一条「运行中」徽章')
    assert.deepEqual(badges.map((b) => textOf(b.props.children).trim()), ['运行中'])
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：跨仓库同 iid 不误高亮（repo_id 参与匹配）', async () => {
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload(), buildIssuesPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const items = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item'))
    // repo 2 的 issue 101 不应高亮（无对应任务）
    const runningItems = items.filter((n) =>
      String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 1, '跨仓库同 iid 不得误高亮')
    // 通过标题定位：repo 1 的 101 高亮，repo 2 的 101 不高亮
    // （标题是 issue-link 按钮 children 中的纯字符串节点）
    const runningTitles = runningItems.map((n) => {
      const btn = n.findAll((x) => x.type === 'button'
        && String(x.props.className || '').includes('issue-link'))[0]
      return (btn?.props.children || [])
        .filter((c) => typeof c === 'string').join('')
    })
    assert.deepEqual(runningTitles, ['运行中的 issue'],
                     '高亮项应为 repo 1 的运行中 issue')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：retrying 状态任务同样高亮对应 issue', async () => {
  const tasks = buildTasksPayload({ status: 'retrying' })
  const { renderer, renderError } =
    await renderOverview(tasks, buildIssuesPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 1, 'retrying 任务对应 issue 应显示运行徽章')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：任务列表为空时全部 issue 无高亮', async () => {
  const { renderer, renderError } =
    await renderOverview({ tasks: [], total: 0, stats: {} }, buildIssuesPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const items = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item'))
    assert.equal(items.length, 3, 'issue 列表应正常渲染')
    const runningItems = items.filter((n) =>
      String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 0, '无活跃任务时不得高亮任何 issue')
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 0, '无活跃任务时不得显示运行徽章')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：任务与 issue 全不匹配时不误高亮', async () => {
  const tasks = {
    tasks: [
      { id: 1, status: 'running', repo_id: 9, repo_name: 'other',
        issue_iid: 999, issue_title: '别的仓库的任务',
        issue_url: 'https://gitlab.example.com/o/-/issues/999' },
    ],
    total: 1, stats: {},
  }
  const { renderer, renderError } =
    await renderOverview(tasks, buildIssuesPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const runningItems = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 0, '不匹配的任务不得触发任何高亮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：高亮与置顶分组并存，不破坏标签胶囊与终态分组', async () => {
  // 运行中的 issue 带 in-progress 标签（executor 处理中标签），应照常
  // 渲染为标签胶囊；issue #101 起运行中的 issue 置顶为「⚙️ 运行中」组，
  // bot-done 的 issue 仍留在「✅ bot-done」组
  const issues = {
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 101, title: '运行中',
          updated_at: '2026-08-15 10:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/101',
          labels: [{ name: 'feature' }, { name: 'in-progress' }] },
        { iid: 102, title: '已完成',
          updated_at: '2026-08-15 09:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/102',
          labels: [{ name: 'bot-done' }] },
      ],
    }],
    errors: [], total: 2,
  }
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload(), issues)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 分组：运行中组置顶 + bot-done 组（issue #101 置顶分组）
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['运行中', 'bot-done'],
                     '运行中的 issue 置顶为运行中组，bot-done 组照常')
    // 运行徽章与 in-progress 标签胶囊并存
    const pills = root.findAll((n) => n.props.className === 'label-pill')
    assert.deepEqual(pills.map((p) => p.props.children),
                     ['feature', 'in-progress'], '标签胶囊应照常渲染')
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 1, '运行徽章应显示')
    // 点击运行中的 issue 仍打开右边栏（issue-link 按钮不失效）
    const linkBtns = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    assert.equal(linkBtns.length, 2, '两条 issue 的详情按钮均应存在')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
