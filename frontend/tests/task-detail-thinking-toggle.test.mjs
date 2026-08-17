// 任务详情页「思考过程显示开关」测试（issue #176）。
//
// 需求：任务详情页面事件流默认隐藏思考过程，在事件流右边增加一个
// checkbox，勾选后打开思考过程显示。
// - 默认（未勾选）：thinking 事件整条不渲染（非 thinking 事件不受影响）
// - 勾选「显示思考过程」：thinking 事件以展开态渲染（内容直接可见）
// - 取消勾选：thinking 事件再次隐藏
// - 开关状态驱动：勾选后新推送的 thinking 事件立即展开显示
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom mock（与 task-events-stream.test.mjs 一致）
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

// ---- 任务数据与 API mock ----

const TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
  status: 'running', attempt_count: 1, triggered_by: 'webhook',
  exit_code: null, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null,
  log_file_tail: null, logs: [],
  created_at: '2026-08-13 09:50:00', started_at: '2026-08-13 10:00:00',
  finished_at: null, prompt: null,
}

function mockTaskApi(task, execution = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...TASK, ...task }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: task.status ?? TASK.status, session_id: null,
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

// 查找「显示思考过程」checkbox（任务详情页应仅有一个 checkbox）
function findThinkingCheckbox(renderer) {
  const inputs = renderer.root.findAllByProps({ type: 'checkbox' })
  assert.equal(inputs.length, 1,
    `应有且仅有一个显示思考过程 checkbox，实际 ${inputs.length} 个`)
  return inputs[0]
}

// 触发 checkbox 切换（模拟用户点击勾选/取消）
function toggleThinking(renderer, checked) {
  return TestRenderer.act(async () => {
    findThinkingCheckbox(renderer).props.onChange({ target: { checked } })
  })
}

// ---- 思考过程显示开关测试 ----

test('默认隐藏思考过程：thinking 事件不渲染，其他事件正常渲染',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'thinking', text: '先定位报错位置' })
        es.emit({ seq: 2, kind: 'text', text: '我来修复登录问题。' })
        es.emit({ seq: 3, kind: 'tool', tool: 'Bash', input: { command: 'git status' } })
      })
      const tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /先定位报错位置/, '默认应隐藏思考过程内容')
      assert.doesNotMatch(tree, /💭 思考过程/, '默认不应渲染思考过程摘要')
      assert.match(tree, /我来修复登录问题。/, '文本事件不受影响')
      assert.match(tree, /Bash/, '工具事件不受影响')
      // 开关默认处于未勾选状态
      assert.equal(findThinkingCheckbox(renderer).props.checked, false,
        '显示思考过程 checkbox 默认应为未勾选')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('勾选「显示思考过程」后 thinking 事件展开显示，取消勾选再次隐藏',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'thinking', text: '先定位报错位置' })
        es.emit({ seq: 2, kind: 'text', text: '我来修复登录问题。' })
      })
      assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /先定位报错位置/, '勾选前应隐藏')

      // 勾选 → 思考过程展开显示
      await toggleThinking(renderer, true)
      let tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /先定位报错位置/, '勾选后思考过程内容应显示')
      assert.match(tree, /💭 思考过程/, '勾选后思考过程摘要应显示')
      assert.equal(findThinkingCheckbox(renderer).props.checked, true, '勾选后 checkbox 应为选中态')

      // 取消勾选 → 再次隐藏
      await toggleThinking(renderer, false)
      tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /先定位报错位置/, '取消勾选后思考过程应再次隐藏')
      assert.match(tree, /我来修复登录问题。/, '文本事件始终不受影响')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('勾选状态下新推送的 thinking 事件立即展开显示（状态驱动增量）',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'thinking', text: '第一条思考' })
      })
      assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /第一条思考/, '默认隐藏')

      // 先勾选，再推送新的 thinking 事件
      await toggleThinking(renderer, true)
      await TestRenderer.act(async () => {
        es.emit({ seq: 2, kind: 'thinking', text: '第二条思考' })
        es.emit({ seq: 3, kind: 'text', text: '文本事件' })
      })
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /第一条思考/, '勾选前已推送的 thinking 应显示')
      assert.match(tree, /第二条思考/, '勾选后新推送的 thinking 应立即显示')
      assert.match(tree, /文本事件/, '文本事件始终不受影响')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('无 thinking 事件时开关正常显示且不影响事件流渲染',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'text', text: '只有文本事件' })
      })
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /只有文本事件/, '无思考事件时文本事件正常渲染')
      assert.match(tree, /显示思考过程/, '开关标签应显示')
      assert.equal(findThinkingCheckbox(renderer).props.checked, false)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))
