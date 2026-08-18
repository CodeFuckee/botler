// 概览页「开放 Issue」板块显示正在运行的任务测试（issue #114）：概览页
// 删除独立的「正在执行的任务」板块，正在运行任务的信息（状态徽章 / 执行
// 引擎 / 实时输出日志）改在「开放 Issue」板块 running 组的 issue 项内
// 展示。任务数据流不变：复用 /api/tasks 轮询与 SSE 事件流（liveLines），
// 按 repo_id+issue_iid 匹配任务与 issue（与 runningIssueKeys 同规则）。
//
// 断言：
// 1. 源码：任务板块（tasks-section/overview-grid）已从概览页删除，任务
//    信息渲染于 issue 项内（issue-task 容器）；
// 2. tasksForIssue 纯函数：正常匹配 / 任务缺失 null 非数组 / 字段缺失 /
//    非活跃状态跳过 / 数字字符串类型统一 / 0 值合法 / 多任务去重渲染；
// 3. 渲染：running 组的 issue 项内渲染任务状态徽章、执行引擎、实时输出
//    （SSE 推送实时更新）；无匹配任务不渲染任务块；跨仓库同 iid 不误显；
//    字段兜底（无引擎/无链接/无日志/未知状态均不崩）；
// 4. styles.css：任务板块样式移除、新增 issue-row/issue-task 样式，
//    剩余两板块（开放 Issue / CI/CD 流水线）列数仍统一。
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
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, tasksForIssue } =
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


// ---- 源码断言 ----

test('源码：概览页已删除独立任务板块（tasks-section 与任务卡片网格）', () => {
  assert.ok(!overview.includes('tasks-section'), '不应再有 tasks-section 板块容器')
  assert.ok(!overview.includes('className="overview-grid"'),
            '不应再有任务卡片网格 overview-grid')
  assert.ok(!overview.includes('<h2>正在执行的任务</h2>'), '不应再有任务板块 h2 标题')
  assert.ok(!overview.includes('当前没有正在执行的任务'), '不应再有任务板块空状态文案')
})

test('源码：任务信息渲染于开放 issue 列表项内（issue-task 容器）', () => {
  assert.equal(typeof tasksForIssue, 'function', '应导出 tasksForIssue 匹配函数')
  assert.match(overview, /tasksForIssue\(tasks,\s*r\.repo_id,\s*i\.iid\)/,
               '渲染应按 repo_id+iid 匹配 issue 的任务')
  assert.match(overview, /issue-task\b/, 'issue 项内应有任务信息容器 issue-task')
  assert.match(overview, /STATUS_META\[/, '任务状态徽章应复用 STATUS_META')
  assert.match(overview, /liveLines\[/, '实时输出应复用 SSE liveLines 缓存')
})

test('源码：实时输出自动滚动选择器随任务板块迁入 issue 列表', () => {
  assert.match(overview, /querySelectorAll\('\.issue-task-log'\)/,
               '自动滚动应选择 issue 项内的日志元素 issue-task-log')
  assert.ok(!overview.includes('.overview-log'), '不应再引用已删除的 overview-log')
})

// ---- tasksForIssue 纯函数测试 ----

test('正常路径：按 repo_id+issue_iid 匹配活跃任务（running/retrying）', () => {
  const tasks = [
    { id: 1, status: 'running', repo_id: 5, issue_iid: 101 },
    { id: 2, status: 'retrying', repo_id: 5, issue_iid: 101 },
    { id: 3, status: 'running', repo_id: 5, issue_iid: 102 },
    { id: 4, status: 'running', repo_id: 6, issue_iid: 101 },
  ]
  const hit = tasksForIssue(tasks, 5, 101)
  assert.deepEqual(hit.map((t) => t.id), [1, 2],
                   '同仓库同 issue 的 running+retrying 任务均应匹配')
  assert.equal(tasksForIssue(tasks, 5, 102).length, 1, '仅匹配目标 issue 的任务')
  assert.equal(tasksForIssue(tasks, 6, 101).length, 1, '跨仓库同 iid 各自独立匹配')
})

test('边界：非活跃状态（succeeded/failed/queued）任务跳过', () => {
  const tasks = [
    { id: 1, status: 'succeeded', repo_id: 1, issue_iid: 1 },
    { id: 2, status: 'failed', repo_id: 1, issue_iid: 1 },
    { id: 3, status: 'queued', repo_id: 1, issue_iid: 1 },
    { id: 4, status: 'interrupted', repo_id: 1, issue_iid: 1 },
  ]
  assert.equal(tasksForIssue(tasks, 1, 1).length, 0, '终态任务不得匹配')
})

test('边界：tasks 缺失 / null / 非数组返回空且不崩', () => {
  for (const bad of [undefined, null, 'x', {}, 42]) {
    assert.deepEqual(tasksForIssue(bad, 1, 1), [], `输入 ${String(bad)} 应返回空数组`)
  }
})

test('边界：任务元素 null / 缺字段跳过且不崩', () => {
  const tasks = [
    null,
    { id: 2, status: 'running', repo_id: 1 },          // 缺 issue_iid
    { id: 3, status: 'running', issue_iid: 1 },        // 缺 repo_id
    { id: 4, status: 'running', repo_id: null, issue_iid: 1 },
    { id: 5, status: 'running', repo_id: 1, issue_iid: null },
    { id: 6, repo_id: 1, issue_iid: 1 },               // 缺 status
    { id: 7, status: 'running', repo_id: 1, issue_iid: 1 },
  ]
  assert.deepEqual(tasksForIssue(tasks, 1, 1).map((t) => t.id), [7],
                   '异常任务元素应跳过，正常任务照常匹配')
})

test('边界：repoId/iid 数字与字符串类型统一匹配，0 为合法值', () => {
  const tasks = [
    { id: 1, status: 'running', repo_id: '5', issue_iid: '101' },
    { id: 2, status: 'running', repo_id: 0, issue_iid: 0 },
  ]
  assert.equal(tasksForIssue(tasks, 5, 101).length, 1, '字符串键应可命中')
  assert.equal(tasksForIssue(tasks, '5', '101').length, 1, '字符串参数应可命中')
  assert.equal(tasksForIssue(tasks, 0, 0).length, 1, '0 值应为合法匹配键')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// SSE 事件流 mock：记录实例，可手动 emit 事件驱动任务块实时输出
class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.closed = false
    FakeEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
  emit(event) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(event) })
  }
}

// 渲染 Overview：mock 任务 / 流水线 / issue / 灵感四端点，任务与 issue 可注入
async function renderOverview({ tasks = [], repos = [] } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks, total: tasks.length, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos, errors: [], total: repos.length }
    // issue #131：灵感板块（本地数据库数据，测试注入空列表）
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
  return { renderer, renderError }
}

// 单仓库两条 issue：101 有运行中任务，102 无任务
function buildPayload(overrides = {}) {
  return {
    tasks: [{
      id: 11, status: 'running', repo_id: 1, repo_name: 'botler',
      project_id: 123, issue_iid: 101, issue_title: '运行中的 issue',
      issue_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/101',
      engine: 'claude', ...overrides,
    }],
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 101, title: '运行中的 issue',
          updated_at: '2026-08-15 10:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/101',
          labels: [{ name: 'feature' }] },
        { iid: 102, title: '普通的 issue',
          updated_at: '2026-08-15 09:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/102',
          labels: [{ name: 'bug' }] },
      ],
    }],
  }
}

test('渲染：running 组的 issue 项内展示任务状态徽章、引擎与实时输出', async () => {
  FakeEventSource.instances = []
  const saved = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  const p = buildPayload()
  const { renderer, renderError } = await renderOverview(p)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 任务信息容器只出现在运行中的 issue 项内
    const taskBlocks = root.findAll((n) => n.props.className === 'issue-task')
    assert.equal(taskBlocks.length, 1, '仅运行中的 issue 渲染任务信息块')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('执行中'), '任务块应显示任务状态徽章文案（执行中）')
    assert.ok(text.includes('Claude Code CLI'), '任务块应显示执行引擎')
    assert.ok(text.includes('（暂无输出）'), '无日志时应显示占位文案')
    // 任务块含 GitLab 跳转链接（原任务卡片功能迁入）
    assert.ok(
      root.findAllByType('a').some((a) => a.props.href?.includes('/-/issues/101')),
      '任务块应保留 GitLab 中打开 issue 的链接',
    )
    // SSE 推送实时输出到 issue 项内任务块
    assert.equal(FakeEventSource.instances.length, 1, '应为活跃任务创建事件流连接')
    await TestRenderer.act(async () => {
      FakeEventSource.instances[0].emit({ seq: 1, kind: 'text', text: '正在分析 bug…' })
      FakeEventSource.instances[0].emit({ seq: 2, kind: 'tool', tool: 'Bash',
                                          input: { command: 'git status' } })
    })
    const textAfter = JSON.stringify(renderer.toJSON())
    assert.ok(textAfter.includes('正在分析 bug…'), '任务块应实时展示 agent 输出')
    assert.ok(textAfter.includes('Bash'), '任务块应展示工具调用事件')
    // 普通 issue 不渲染任务块
    const plainItem = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item'))[1]
    assert.equal(plainItem.findAll((n) => n.props.className === 'issue-task').length, 0,
                 '无任务匹配的 issue 不得渲染任务块')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    globalThis.EventSource = saved
  }
})

test('渲染：任务字段兜底——无引擎 / 无链接 / 未知状态均不崩', async () => {
  const saved = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  FakeEventSource.instances = []
  const p = buildPayload({
    engine: '', issue_url: null, status: 'running', issue_title: null,
  })
  const { renderer, renderError } = await renderOverview(p)
  try {
    assert.equal(renderError, null, '字段缺失不得导致渲染崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(!text.includes('Claude Code CLI'), '无引擎时不得显示引擎文案')
    assert.ok(!text.includes('在 GitLab 中打开'), '无链接时不得渲染跳转链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    globalThis.EventSource = saved
  }
})

test('渲染：同一 issue 多条活跃任务时逐条渲染任务块', async () => {
  const saved = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  FakeEventSource.instances = []
  const p = buildPayload()
  p.tasks.push({
    id: 12, status: 'retrying', repo_id: 1, repo_name: 'botler',
    project_id: 123, issue_iid: 101, issue_title: '运行中的 issue',
    issue_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/101',
    engine: 'dsh',
  })
  const { renderer, renderError } = await renderOverview(p)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const taskBlocks = root.findAll((n) => n.props.className === 'issue-task')
    assert.equal(taskBlocks.length, 2, '两条任务记录应渲染两个任务块')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('重试中'), 'retrying 任务块应显示「重试中」徽章')
    assert.ok(text.includes('deepseek-harness SDK'), 'dsh 引擎应显示对应文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    globalThis.EventSource = saved
  }
})

test('渲染：跨仓库同 iid 任务不误渲染到其他仓库的 issue 项', async () => {
  const saved = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  FakeEventSource.instances = []
  const { renderer, renderError } = await renderOverview({
    tasks: [{
      id: 11, status: 'running', repo_id: 1, repo_name: 'botler',
      project_id: 123, issue_iid: 101, issue_title: 't',
      issue_url: 'https://gitlab.example.com/x/-/issues/101', engine: '',
    }],
    repos: [
      { repo_id: 2, repo_name: 'shipyard', priority: 20,
        issues: [{ iid: 101, title: '跨仓库同 iid',
                   updated_at: '2026-08-15 08:00:00',
                   web_url: 'https://gitlab.example.com/x/-/issues/101',
                   labels: [] }] },
    ],
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const taskBlocks = root.findAll((n) => n.props.className === 'issue-task')
    assert.equal(taskBlocks.length, 0, 'repo_id 不匹配不得渲染任务块')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    globalThis.EventSource = saved
  }
})

test('渲染：无活跃任务时概览页仅剩三板块且不渲染任何任务块', async () => {
  const { renderer, renderError } = await renderOverview({
    tasks: [],
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [{ iid: 101, title: '普通的 issue',
                 updated_at: '2026-08-15 10:00:00',
                 web_url: 'https://gitlab.example.com/x/-/issues/101',
                 labels: [] }],
    }],
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const h2s = root.findAll((n) => n.type === 'h2')
      .map((n) => textOf(n.props.children).trim())
    // issue #131：新增灵感板块；issue #180：新增「Issue 完成耗时」板块
    // issue #293：灵感组件保持原始位置（放在开放 Issue 板块下方），
    // issue #235：新增「Token 用量统计」板块（置于完成耗时之后），
    // 板块顺序为 开放 Issue → 灵感 → CI/CD 流水线 → Issue 完成耗时 → Token 用量统计
    assert.deepEqual(h2s, ['开放 Issue', '灵感', 'CI/CD 流水线', 'Issue 完成耗时', 'Token 用量统计'],
                     '概览页板块顺序应为：开放 Issue → 灵感 → CI/CD 流水线 → Issue 完成耗时 → Token 用量统计')
    assert.equal(root.findAll((n) => n.props.className === 'issue-task').length, 0,
                 '无任务时不得渲染任务块')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- styles.css 断言 ----

test('styles.css：任务板块样式移除，新增 issue 项内任务块样式', () => {
  for (const sel of ['.tasks-section', '.overview-grid', '.overview-card',
                     '.overview-card-head', '.overview-repo', '.overview-issue',
                     '.overview-log']) {
    assert.ok(!styles.includes(`${sel} {`), `不应再有已删除板块样式 ${sel}`)
  }
  assert.match(styles, /\.issue-row\s*\{/, '应有 .issue-row 行布局样式')
  assert.match(styles, /\.issue-task\s*\{/, '应有 .issue-task 任务块样式')
  assert.match(styles, /\.issue-task-log\s*\{/, '应有 .issue-task-log 日志样式')
})
