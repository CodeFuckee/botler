// 任务列表/详情展示「实际生效的超时/重试/引擎与来源」（issue #237）。
//
// 需求：仓库级任务参数覆盖后，任务列表抽屉与详情页展示该任务实际生效的
// 参数值与其来源（仓库覆盖 / 继承全局），用户一眼看出当前任务按什么参数执行。
//
// 断言：
// 1. 抽屉（窄视口 ⋯ 展开）：仓库覆盖任务展示「600 秒（仓库覆盖）」「3 次
//    （仓库覆盖）」「dsh（仓库覆盖）」；
// 2. 抽屉：继承全局任务展示「1800 秒（继承全局）」「2 次（继承全局）」
//    「claude（继承全局）」；
// 3. 详情页（TaskDetail）：同样展示生效参数与来源。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

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
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { default: TaskDetail } = await vite.ssrLoadModule('/src/pages/TaskDetail.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

function mkTask(overrides = {}) {
  return {
    id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
    issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
    status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
    exit_code: 0, error_message: null, error_detail: null,
    resumed: false, commit_sha: 'abc1234', commit_url: null,
    log_path: null, started_at: null, finished_at: null,
    created_at: '2026-08-13 09:50:00',
    // issue #237：任务生效参数与来源（repo=仓库覆盖 / global=继承全局）
    timeout_seconds: 1800, timeout_source: 'global',
    max_retries: 2, max_retries_source: 'global',
    effective_engine: 'claude', engine_source: 'global',
    ...overrides,
  }
}

function withViewport(width) {
  globalThis.window = {
    innerWidth: width,
    addEventListener: () => {},
    removeEventListener: () => {},
  }
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

async function renderTasks(tasks) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks, total: tasks.length, stats: {} }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Tasks)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

async function openDrawer(renderer) {
  // 窄视口下操作列出现 ⋯ 按钮，点击弹出抽屉
  const dots = renderer.root
    .findAllByType('button')
    .filter((b) => textOf(b.props.children).includes('⋯'))
  await TestRenderer.act(async () => {
    await dots[0].props.onClick()
  })
  return renderer.root.findAll((n) => n.props.className === 'drawer')
}

test('任务抽屉展示生效参数与来源（仓库覆盖）', async () => {
  withViewport(1000)
  const repoOverride = mkTask({
    timeout_seconds: 600, timeout_source: 'repo',
    max_retries: 3, max_retries_source: 'repo',
    effective_engine: 'dsh', engine_source: 'repo',
  })
  const { renderer, renderError } = await renderTasks([repoOverride])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const drawers = await openDrawer(renderer)
    assert.equal(drawers.length, 1, '应弹出抽屉')
    const text = textOf(drawers[0])
    assert.match(text, /生效超时/, '抽屉应有生效超时行')
    assert.match(text, /600 秒（仓库覆盖）/, '应展示仓库覆盖超时 600 秒')
    assert.match(text, /3 次（仓库覆盖）/, '应展示仓库覆盖重试 3 次')
    assert.match(text, /dsh（仓库覆盖）/, '应展示仓库覆盖引擎 dsh')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('任务抽屉展示生效参数与来源（继承全局）', async () => {
  withViewport(1000)
  const { renderer, renderError } = await renderTasks([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const drawers = await openDrawer(renderer)
    assert.equal(drawers.length, 1, '应弹出抽屉')
    const text = textOf(drawers[0])
    assert.match(text, /1800 秒（继承全局）/, '应展示继承全局超时 1800 秒')
    assert.match(text, /2 次（继承全局）/, '应展示继承全局重试 2 次')
    assert.match(text, /claude（继承全局）/, '应展示继承全局引擎 claude')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// TestInstance.children 遍历（详情页根节点 props.children 为空，用 children 递归）
function allText(renderer) {
  const parts = []
  const walk = (n) => {
    if (n == null) return
    if (typeof n === 'string') { parts.push(n); return }
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.children) walk(n.children)
  }
  walk(renderer.root)
  return parts.join('')
}

test('任务详情页展示生效参数与来源（仓库覆盖）', async () => {
  const task = mkTask({
    timeout_seconds: 900, timeout_source: 'repo',
    max_retries: 5, max_retries_source: 'repo',
    effective_engine: 'hermes', engine_source: 'repo',
  })
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') {
      return { ...task, logs: [] }
    }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return { status: 'succeeded', session_id: null, log_offset: 0,
               log_delta: [], transcript: [], transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      // mock-router 的 useParams 固定返回 id=3，直接挂 TaskDetail 即可
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(TaskDetail)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = allText(renderer)
    assert.match(text, /生效超时/, '详情页应有生效超时行')
    assert.match(text, /900 秒（仓库覆盖）/, '应展示仓库覆盖超时 900 秒')
    assert.match(text, /5 次（仓库覆盖）/, '应展示仓库覆盖重试 5 次')
    assert.match(text, /hermes（仓库覆盖）/, '应展示仓库覆盖引擎 hermes')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
