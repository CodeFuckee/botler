// 概览页 issue 右边栏「查看执行的详情」测试（issue #167）：
// 概览页点击 issue 弹出的右边栏中新增「查看执行的详情」按钮，点击后
// 再弹出一个右边栏（TaskDetailDrawer）显示该 issue 的任务执行详情。
//
// 数据流：
// - 打开时拉取 GET /api/issues/{project_id}/{iid}/tasks（该 issue 全部
//   任务记录，id 倒序最新在前），默认选中最新一条；
// - 选中任务后拉取 GET /api/tasks/{task_id}（详情 + 日志）与
//   GET /api/tasks/{task_id}/execution（聊天记录/实时输出增量轮询）、
//   SSE 事件流（逐事件展示执行过程）；
// - 无任务记录 → 空态「该 issue 暂无任务执行记录」；
// - 关闭：× / 点击遮罩 / Esc（只关第二层，不误关下层 issue 抽屉）。
//
// 断言：
// 1. 源码：IssueDrawer 含「查看执行的详情」按钮与 TaskDetailDrawer 渲染、
//    第二层打开时本层不响应 Esc；TaskDetailDrawer 拉取任务列表/详情/
//    执行/事件流接口；
// 2. 纯函数：taskStatusMeta 兜底映射；renderEvent / renderChatMessage
//    各事件类型与异常输入不崩溃；
// 3. 渲染：点击按钮打开第二层抽屉，展示任务列表（最新在前）与选中任务
//    详情（元信息/事件流/聊天/日志）；切换任务重新拉详情；
// 4. 边界：无任务记录空态；任务列表加载失败错误 + 重试；详情加载失败
//    错误 + 重试；× 关闭第二层后 issue 抽屉仍打开。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom 的 CJS 构建无法被 vite SSR 转译（module is not
// defined），TaskDetailDrawer 内的 Link 用测试 mock（渲染 <a>）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: IssueDrawer } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { default: TaskDetailDrawer, taskStatusMeta, renderEvent, renderChatMessage } =
  await vite.ssrLoadModule('/src/components/TaskDetailDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const detailSrc = readFileSync(path.join(ROOT, 'src/components/TaskDetailDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 源码：含「查看执行的详情」按钮并渲染 TaskDetailDrawer', () => {
  assert.match(drawerSrc, /查看执行的详情/, '抽屉应含「查看执行的详情」按钮')
  assert.match(drawerSrc, /setDetailOpen\(true\)/, '点击应打开第二层抽屉')
  assert.match(drawerSrc, /TaskDetailDrawer/, '应渲染 TaskDetailDrawer 组件')
  assert.match(drawerSrc, /projectId=\{i\.project_id\}/, '应传 project_id 给第二层')
})

test('IssueDrawer Esc 关闭：第二层抽屉打开时本层不响应 Esc', () => {
  assert.match(drawerSrc, /if \(detailOpen\) return/, '第二层打开时本层应跳过 Esc 处理')
  assert.match(drawerSrc, /\[onClose, detailOpen\]/, 'Esc 监听依赖应含 detailOpen')
})

test('TaskDetailDrawer 源码：拉取任务列表/详情/执行数据/事件流', () => {
  assert.match(detailSrc, /\/api\/issues\/\$\{projectId\}\/\$\{issueIid\}\/tasks/,
    '应拉取该 issue 的任务执行记录列表')
  assert.match(detailSrc, /\/api\/tasks\/\$\{taskId\}/, '应拉取选中任务详情')
  assert.match(detailSrc, /execution\?after_byte=/, '应拉取实时执行数据（增量续读）')
  assert.match(detailSrc, /openTaskEventStream/, '应订阅任务事件流 SSE')
  assert.match(detailSrc, /暂无任务执行记录/, '无任务记录应显示空态')
})

// ---- 纯函数 ----

test('taskStatusMeta 兜底映射', () => {
  assert.equal(taskStatusMeta('running').label, '执行中')
  assert.equal(taskStatusMeta('succeeded').label, '成功')
  assert.equal(taskStatusMeta('unknown').label, 'unknown', '未知状态原样兜底')
  assert.equal(taskStatusMeta(null).label, '—', '空值显示占位符')
  assert.equal(taskStatusMeta(undefined).label, '—', 'undefined 显示占位符')
})

test('renderEvent 事件类型与异常输入', () => {
  assert.equal(renderEvent(null), null, '空值不渲染')
  assert.equal(renderEvent({}), null, '无 kind 事件不渲染')
  assert.ok(renderEvent({ kind: 'thinking', text: '思考' }), 'thinking 渲染为折叠块')
  assert.ok(renderEvent({ kind: 'tool', tool: 'Bash', input: { command: 'ls' } }), '工具调用渲染')
  assert.ok(renderEvent({ kind: 'tool_result', text: '输出' }), '工具结果渲染')
  assert.ok(renderEvent({ kind: 'tool_result', text: '', is_error: true }), '失败工具结果渲染')
  assert.ok(renderEvent({ kind: 'status', model: 'claude' }), '状态事件渲染')
  assert.ok(renderEvent({ kind: 'result', result: '完成' }), '结果摘要渲染')
  assert.ok(renderEvent({ kind: 'text', text: '正文' }), '普通文本渲染')
})

test('renderChatMessage 消息类型与异常输入', () => {
  assert.equal(renderChatMessage(null), null, '空值不渲染')
  assert.equal(renderChatMessage({}), null, '无 role 消息不渲染')
  assert.ok(renderChatMessage({ role: 'user', text: '你好' }), '用户消息渲染')
  assert.ok(renderChatMessage({ role: 'assistant', text: '你好' }), '助手消息渲染')
  assert.ok(renderChatMessage({ role: 'tool', tool: 'Bash', input: { command: 'pwd' } }), '工具调用渲染')
  assert.ok(renderChatMessage({ role: 'tool_result', text: '输出', truncated: true }), '工具结果含截断标记')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

const FULL_ISSUE = {
  iid: 64, title: '概览页面增加读取已启用的仓库issue',
  state: 'opened',
  updated_at: '2026-08-14 10:20:00',
  created_at: '2026-08-10 09:00:00',
  project_id: 1,
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
  description: '**需求**\n\n- 要点一',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
  milestone: 'v1.0',
  assignees: [{ name: 'Agent', username: 'agent' }],
  user_notes_count: 3,
}

const TASK_101 = {
  id: 101, repo_id: 1, repo_name: 'botler', project_id: 1, issue_iid: 64,
  issue_title: '概览页面增加读取已启用的仓库issue',
  status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
  exit_code: 0, error_message: '', engine: 'claude',
  commit_sha: '0123456789abcdef0123456789abcdef01234567',
  commit_url: 'https://gitlab.example.com/botler/-/commit/0123456789abcdef0123456789abcdef01234567',
  created_at: '2026-08-14 08:00:00', started_at: '2026-08-14 08:00:10',
  finished_at: '2026-08-14 08:05:00',
  logs: [{ id: 1, ts: '2026-08-14 08:00:11', level: 'info', message: '任务开始' }],
  log_file_tail: 'claude 输出尾部',
}

const TASK_102 = {
  ...TASK_101, id: 102, status: 'failed', attempt_count: 2,
  engine: 'dsh', error_message: '执行超时', commit_sha: null, commit_url: null,
}

// 打开概览页 → 点击 issue 项 → 点击「查看执行的详情」
async function openDetailDrawer(taskListBody, listError, detailBodies = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10,
                         issues: [FULL_ISSUE] }], errors: [] }
    }
    if (pathname === '/api/issues/1/64/detail') return { notes: [], engine: 'claude' }
    if (pathname === '/api/issues/1/64/tasks') {
      if (listError) throw new Error(listError)
      return taskListBody
    }
    // 任务详情 / 执行数据（按 id 分发）
    const m = pathname.match(/^\/api\/tasks\/(\d+)$/)
    if (m) {
      const id = Number(m[1])
      if (detailBodies[id] === undefined) throw new Error('任务不存在 ' + id)
      return detailBodies[id]
    }
    const em = pathname.match(/^\/api\/tasks\/(\d+)\/execution/)
    if (em) {
      const id = Number(em[1])
      if (detailBodies[id] === undefined) throw new Error('任务不存在 ' + id)
      return { status: detailBodies[id].status, session_id: 'sess-1',
               log_offset: 0, log_delta: [],
               transcript: [{ role: 'assistant', text: '分析需求中…' }],
               transcript_truncated: false, prompt: null }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Overview)))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  const root = renderer.root
  // 点击 issue 列表项打开第一层抽屉
  const issueBtn = root.findByProps({ className: 'issue-link' })
  await TestRenderer.act(async () => {
    issueBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  // 点击「查看执行的详情」打开第二层抽屉
  const viewBtn = root.findAll((n) => n.type === 'button'
    && String(n.props.children).includes('查看执行的详情'))
  assert.ok(viewBtn.length > 0, '应有「查看执行的详情」按钮')
  await TestRenderer.act(async () => {
    viewBtn[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return { renderer, root }
}

// 渲染树 → 纯文本（toJSON 树与 TestInstance 通用，与既有抽屉测试同约定）：
// JSX 文本插值（如 `#{i.iid}`）会被拆成 "#" 与数字两个节点，JSON 序列化
// 后不连续，断言需先拼成纯文本
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') return toText(node.children ?? node.props?.children)
  return ''
}

function treeText(renderer) {
  return toText(renderer.toJSON())
}

// 渲染树中是否存在该文本（整树拼接后子串匹配）
function hasText(renderer, text) {
  return treeText(renderer).includes(text)
}

// 第二层任务执行详情抽屉容器（className 含 task-detail-drawer）
function findDetailDrawer(root) {
  return root.findAll(
    (n) => String(n.props.className || '').includes('task-detail-drawer'))
}

// 第一层 issue 详情抽屉容器（复用既有测试的判定：含 issue-drawer 且
// 内部容器带 stopPropagation 点击）
function findIssueDrawer(root) {
  return root.findAll(
    (n) => String(n.props.className || '').includes('issue-drawer')
      && n.props.onClick)
}

test('渲染：点击按钮打开第二层抽屉，展示任务列表与选中任务详情', async () => {
  const { renderer, root } = await openDetailDrawer(
    { tasks: [TASK_101, TASK_102], total: 2 },
    null,
    { 101: TASK_101, 102: TASK_102 },
  )
  try {
    // 第二层抽屉容器
    assert.ok(findDetailDrawer(root).length > 0, '应弹出第二层抽屉')
    // 任务列表：两条任务记录（最新在前：#101 在前）
    const items = root.findAll((n) => n.type === 'button'
      && String(n.props.className || '').includes('task-detail-item'))
    assert.equal(items.length, 2, '应渲染两条任务记录')
    assert.match(toText(items[0]), /#101/)
    assert.match(toText(items[1]), /#102/)
    // 默认选中最新一条 #101：展示其详情（引擎/提交/日志）
    assert.ok(hasText(renderer, '执行引擎'))
    assert.ok(hasText(renderer, 'claude'))
    assert.ok(hasText(renderer, '任务开始'), '应展示执行日志')
    assert.ok(hasText(renderer, '分析需求中…'), '应展示聊天记录')
    assert.ok(hasText(renderer, '暂无事件'), '无事件流时显示占位')
  } finally {
    renderer.unmount()
  }
})

test('渲染：切换任务重新拉详情并展示对应任务', async () => {
  const { renderer, root } = await openDetailDrawer(
    { tasks: [TASK_101, TASK_102], total: 2 },
    null,
    { 101: TASK_101, 102: TASK_102 },
  )
  try {
    const items = root.findAll((n) => n.type === 'button'
      && String(n.props.className || '').includes('task-detail-item'))
    // 点击 #102（失败任务）
    await TestRenderer.act(async () => {
      items[1].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.ok(hasText(renderer, '执行超时'), '应展示 #102 的错误信息')
    assert.ok(hasText(renderer, 'dsh'), '应展示 #102 的执行引擎')
    assert.ok(hasText(renderer, '暂无事件'), '切换后事件流为空占位')
  } finally {
    renderer.unmount()
  }
})

test('边界：无任务记录时显示空态', async () => {
  const { renderer, root } = await openDetailDrawer({ tasks: [], total: 0 }, null)
  try {
    assert.ok(hasText(renderer, '该 issue 暂无任务执行记录'), '应显示空态文案')
    assert.ok(!hasText(renderer, '查看完整任务页'), '无任务不应渲染任务详情区')
  } finally {
    renderer.unmount()
  }
})

test('边界：任务列表加载失败显示错误并可重试', async () => {
  const { renderer, root } = await openDetailDrawer(null, '加载失败')
  try {
    assert.ok(hasText(renderer, '加载失败'), '应显示错误信息')
    const retry = root.findAll((n) => n.type === 'button'
      && String(n.props.children).includes('重试'))
    assert.ok(retry.length > 0, '应有重试按钮')
  } finally {
    renderer.unmount()
  }
})

test('边界：详情加载失败显示错误并可重试（任务列表正常）', async () => {
  const { renderer, root } = await openDetailDrawer(
    { tasks: [TASK_101], total: 1 }, null, {})
  try {
    assert.ok(hasText(renderer, '任务不存在 101'), '应显示详情加载错误')
    const retry = root.findAll((n) => n.type === 'button'
      && String(n.props.children).includes('重试'))
    assert.ok(retry.length > 0, '应有重试按钮')
  } finally {
    renderer.unmount()
  }
})

test('关闭：× 只关第二层抽屉，issue 抽屉保持打开', async () => {
  const { renderer, root } = await openDetailDrawer(
    { tasks: [TASK_101], total: 1 }, null, { 101: TASK_101 })
  try {
    assert.ok(findDetailDrawer(root).length > 0, '第二层应已打开')
    const closeBtn = root.findAll((n) => n.type === 'button'
      && n.props['aria-label'] === '关闭任务执行详情右边栏')
    assert.ok(closeBtn.length > 0, '第二层应有关闭按钮')
    await TestRenderer.act(async () => {
      closeBtn[0].props.onClick()
    })
    assert.equal(findDetailDrawer(root).length, 0, '× 应关闭第二层')
    assert.ok(findIssueDrawer(root).length > 0, 'issue 抽屉应保持打开')
  } finally {
    renderer.unmount()
  }
})

test('关闭：点击遮罩只关第二层抽屉，issue 抽屉保持打开', async () => {
  const { renderer, root } = await openDetailDrawer(
    { tasks: [TASK_101], total: 1 }, null, { 101: TASK_101 })
  try {
    // 第二层遮罩 = 渲染树中第二个 drawer-overlay（第一层 + 第二层叠加）
    const overlays = root.findAll((n) => String(n.props.className || '').includes('drawer-overlay'))
    assert.ok(overlays.length >= 2, '应有两层遮罩')
    await TestRenderer.act(async () => {
      overlays[overlays.length - 1].props.onClick()
    })
    assert.equal(findDetailDrawer(root).length, 0, '点击遮罩应关闭第二层')
    assert.ok(findIssueDrawer(root).length > 0, 'issue 抽屉应保持打开')
  } finally {
    renderer.unmount()
  }
})

test('关闭：Esc 只关第二层抽屉（第一层在第二层打开时不响应 Esc）', () => {
  // 第二层自己监听 keydown 关闭自身（SSR 测试环境无 document，Esc 行为
  // 以源码断言为准，与既有抽屉 Esc 测试约定一致）
  assert.match(detailSrc, /addEventListener\('keydown'/, '第二层应监听 keydown')
  assert.match(detailSrc, /key\s*===\s*'Escape'/, '第二层 Esc 判定应关闭自身')
  assert.match(drawerSrc, /if \(detailOpen\) return/, '第一层在第二层打开时不响应 Esc')
})

// 直接渲染 TaskDetailDrawer（独立组件层验证：任务列表/详情/空态）
async function renderDetailDrawerDirect(body, listError, detailBodies = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/issues/1/64/tasks') {
      if (listError) throw new Error(listError)
      return body
    }
    const m = pathname.match(/^\/api\/tasks\/(\d+)$/)
    if (m) {
      const id = Number(m[1])
      if (detailBodies[id] === undefined) throw new Error('任务不存在 ' + id)
      return detailBodies[id]
    }
    const em = pathname.match(/^\/api\/tasks\/(\d+)\/execution/)
    if (em) {
      const id = Number(em[1])
      if (detailBodies[id] === undefined) throw new Error('任务不存在 ' + id)
      return { status: detailBodies[id].status, session_id: null, log_offset: 0,
               log_delta: [], transcript: [], transcript_truncated: false, prompt: null }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, null,
        React.createElement(TaskDetailDrawer, { projectId: 1, issueIid: 64,
                                                issueTitle: '测试 issue', onClose: () => {} })))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return renderer
}

test('TaskDetailDrawer 独立渲染：无任务时展示空态', async () => {
  const renderer = await renderDetailDrawerDirect({ tasks: [], total: 0 })
  try {
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /该 issue 暂无任务执行记录/, '应显示空态文案')
  } finally {
    renderer.unmount()
  }
})

test('TaskDetailDrawer 独立渲染：任务列表加载失败可重试', async () => {
  const renderer = await renderDetailDrawerDirect(null, '列表加载失败')
  try {
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /列表加载失败/, '应显示错误信息')
    assert.match(text, /重试/, '应有重试按钮')
  } finally {
    renderer.unmount()
  }
})

test('TaskDetailDrawer 独立渲染：选中任务展示完整详情区', async () => {
  const renderer = await renderDetailDrawerDirect(
    { tasks: [TASK_101], total: 1 }, null, { 101: TASK_101 })
  try {
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /#101/, '应渲染任务编号')
    assert.match(text, /成功/, '应渲染状态徽章文案')
    assert.match(text, /执行引擎/, '应有执行引擎行')
    assert.match(text, /执行日志/, '应有执行日志区块')
    assert.match(text, /查看完整任务页/, '应有完整任务页链接')
  } finally {
    renderer.unmount()
  }
})
