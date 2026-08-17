// 任务事件流（SSE 实时输出）测试：api 封装 + 任务详情页事件流面板。
//
// 需求：任务执行期间逐事件实时看到 Claude Code / hermes 输出（文本、
// thinking、工具调用与结果），任务结束后回放完整事件流（后端支持）。
// - api.openTaskEventStream(taskId, handlers)：EventSource 订阅
//   /api/tasks/{id}/events，data 为归一化事件 JSON；done 事件触发 onDone
//   并关闭连接；非法 data 容错不抛异常
// - TaskDetail 事件流面板：运行中自动订阅；事件按序渲染（thinking 默认
//   隐藏、勾选「显示思考过程」后展开显示，issue #176；工具调用显示名称
//   与输入，文本直接显示）；终态任务进入页面时回放已有事件后流结束
//   （后端以 done 收尾）
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom mock（与 tasks-duration-calculation.test.mjs 一致）
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
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: TaskDetail } = await vite.ssrLoadModule('/src/pages/TaskDetail.jsx')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// ---- EventSource mock ----

class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.onerror = null
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

function withEventSource(fn) {
  return async () => {
    FakeEventSource.instances = []
    const saved = globalThis.EventSource
    globalThis.EventSource = FakeEventSource
    try {
      await fn()
    } finally {
      globalThis.EventSource = saved
      FakeEventSource.instances = []
    }
  }
}

// ---- api.openTaskEventStream 封装 ----

test('openTaskEventStream 订阅正确端点并转发事件', withEventSource(async () => {
  const events = []
  const es = api.openTaskEventStream(7, { onEvent: (ev) => events.push(ev) })
  assert.equal(FakeEventSource.instances.length, 1)
  assert.equal(FakeEventSource.instances[0].url, '/api/tasks/7/events')
  FakeEventSource.instances[0].emit({ seq: 1, kind: 'text', text: '开始处理' })
  assert.deepEqual(events, [{ seq: 1, kind: 'text', text: '开始处理' }])
  assert.equal(es, FakeEventSource.instances[0])
}))

test('openTaskEventStream 收到 done 事件回调 onDone 并关闭连接', withEventSource(async () => {
  let doneCount = 0
  const es = api.openTaskEventStream(7, { onDone: () => { doneCount += 1 } })
  FakeEventSource.instances[0].emit({ kind: 'done' })
  assert.equal(doneCount, 1)
  assert.equal(es.closed, true)
}))

test('openTaskEventStream 非法 data 容错不抛异常', withEventSource(async () => {
  const events = []
  api.openTaskEventStream(7, { onEvent: (ev) => events.push(ev) })
  const es = FakeEventSource.instances[0]
  es.onmessage({ data: 'not-json{' })
  es.onmessage({ data: '' })
  es.onmessage({ data: '"str"' })
  es.emit('"a string"') // data 为 JSON 字符串而非对象
  assert.equal(events.length, 0)
}))

// ---- TaskDetail 事件流面板 ----

const TASK_RUNNING = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
  status: 'running', attempt_count: 1, triggered_by: 'webhook',
  exit_code: null, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null, log_file_tail: null,
  created_at: '2026-08-13 09:50:00', started_at: '2026-08-13 10:00:00',
  finished_at: null, prompt: null,
}

function mockTaskApi(task, execution = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...task, logs: [] }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: task.status, session_id: null,
                            log_offset: 0, log_delta: [], transcript: [],
                            transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
}

async function renderDetail() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(TaskDetail)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

test('TaskDetail 运行中任务订阅事件流并把事件渲染进面板',
  withEventSource(async () => {
    mockTaskApi(TASK_RUNNING)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)

      // 运行中任务应创建一个 EventSource 连接
      assert.equal(FakeEventSource.instances.length, 1)
      const es = FakeEventSource.instances[0]
      assert.equal(es.url, '/api/tasks/3/events')

      // 推送事件后渲染：文本 / thinking（默认隐藏，issue #176）/ 工具调用
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'thinking', text: '先定位报错位置' })
        es.emit({ seq: 2, kind: 'text', text: '我来修复登录问题。' })
        es.emit({ seq: 3, kind: 'tool', tool: 'Bash', input: { command: 'git status' } })
        es.emit({ seq: 4, kind: 'tool_result', text: 'On branch main', is_error: false })
      })
      const tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /先定位报错位置/, 'thinking 默认隐藏，内容不应渲染')
      assert.match(tree, /我来修复登录问题。/, '文本事件应渲染')
      assert.match(tree, /Bash/, '工具名应渲染')
      assert.match(tree, /On branch main/, '工具结果应渲染')
      assert.match(tree, /git status/, '工具输入应渲染')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('TaskDetail 终态任务回放事件流后流结束',
  withEventSource(async () => {
    mockTaskApi({ ...TASK_RUNNING, status: 'succeeded' })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)

      // 终态任务同样连接事件流（回放已有事件），后端推完即发 done
      assert.equal(FakeEventSource.instances.length, 1)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'text', text: '历史事件回放' })
        es.emit({ kind: 'done' })
      })
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /历史事件回放/, '回放事件应渲染')
      assert.equal(es.closed, true, 'done 后连接关闭')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('TaskDetail 事件按 seq 去重（断线重连回放重叠不重复渲染）',
  withEventSource(async () => {
    mockTaskApi(TASK_RUNNING)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'text', text: '只出现一次' })
        es.emit({ seq: 1, kind: 'text', text: '只出现一次' }) // 重复 seq
        es.emit({ seq: 2, kind: 'text', text: '第二条' })
      })
      const tree = JSON.stringify(renderer.toJSON())
      const count = tree.split('只出现一次').length - 1
      assert.equal(count, 1, `重复 seq 事件应去重，实际出现 ${count} 次`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))
