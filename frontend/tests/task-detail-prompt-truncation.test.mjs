// 任务详情页提示词完整展示与截断标记测试（issue #90）。
//
// 需求：任务详情页聊天记录中用户发送的提示词被 5000 字符硬截断、与
// 全局模版不一致，且「查看提示词」按钮永远只显示占位文案。修复后：
// - 首条 user 消息（提示词）后端不截断，聊天记录完整展示
// - 其余被截断的长消息渲染「内容过长，已截断」标记
// - 聊天记录消息数量超上限时显示截断提示（transcript_truncated）
// - 「查看提示词」按钮展示 execution 响应懒加载的 prompt 全文
// - prompt 缺失（会话文件丢失）时回退占位文案
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom mock（与 task-detail-collapsible-sections.test.mjs 一致）
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

function mockTaskApi(execution) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...TASK }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: 'running', session_id: null, prompt: null,
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

function buttonText(button) {
  const parts = []
  const walk = (c) => {
    if (c == null) return
    if (typeof c === 'string') { parts.push(c); return }
    if (Array.isArray(c)) { c.forEach(walk); return }
    if (typeof c === 'object' && c.props) walk(c.props.children)
  }
  walk(button.props.children)
  return parts.join('')
}

function findButtons(renderer, text) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => buttonText(b).includes(text))
}

function click(renderer, button) {
  return TestRenderer.act(async () => {
    button.props.onClick()
  })
}

// ---- 测试 ----

test('被截断的长消息渲染「内容过长，已截断」标记，未截断消息不渲染',
  withEventSource(async () => {
    mockTaskApi({
      status: 'running', session_id: 'sess-123', prompt: null,
      log_offset: 0, log_delta: [], transcript_truncated: false,
      transcript: [
        { role: 'user', text: '完整提示词', ts: '2026-08-13 10:00:00', truncated: false },
        { role: 'assistant', text: 'x'.repeat(6000), ts: '2026-08-13 10:00:01', truncated: true },
        { role: 'tool_result', tool_use_id: 'toolu_1', text: 'y'.repeat(6000),
          tool_error: false, ts: '2026-08-13 10:00:02', truncated: true },
      ],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /完整提示词/, '首条 user 消息应展示')
      const mark = tree.split('内容过长，已截断').length - 1
      assert.equal(mark, 2, `两条截断消息应各带一个截断标记，实际 ${mark} 个`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('聊天记录消息数量超上限（transcript_truncated）显示截断提示',
  withEventSource(async () => {
    mockTaskApi({
      status: 'succeeded', session_id: 'sess-123', prompt: null,
      log_offset: 0, log_delta: [], transcript_truncated: true,
      transcript: [
        { role: 'user', text: '完整提示词', ts: '2026-08-13 10:00:00', truncated: false },
        { role: 'assistant', text: '消息', ts: '2026-08-13 10:00:01', truncated: false },
      ],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /聊天记录过多/, '消息数量截断应显示提示')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('「查看提示词」按钮展示 execution 懒加载的 prompt 全文',
  withEventSource(async () => {
    const prompt = '渲染后的完整提示词\n- 推送后必须用 glab ci status --branch 监控\n' + 'p'.repeat(6000)
    mockTaskApi({
      status: 'succeeded', session_id: 'sess-123', prompt,
      log_offset: 0, log_delta: [], transcript_truncated: false,
      transcript: [],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const btn = findButtons(renderer, '提示词')[0]
      assert.match(buttonText(btn), /查看提示词/, '按钮初始文案应为「查看提示词」')
      await click(renderer, btn)
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /渲染后的完整提示词/, '点开后应展示 prompt 全文')
      assert.match(tree, /glab ci status/, '截断点之后的内容也应完整展示')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('prompt 缺失（会话文件丢失）时「查看提示词」回退占位文案',
  withEventSource(async () => {
    mockTaskApi({
      status: 'succeeded', session_id: null, prompt: null,
      log_offset: 0, log_delta: [], transcript_truncated: false,
      transcript: [],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      await click(renderer, findButtons(renderer, '提示词')[0])
      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /提示词未持久化/, '无 prompt 时应展示占位文案')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))
