// 任务详情页区块折叠与完整展示测试（issue #52）。
//
// 需求：任务详情页只保留最外层的垂直滚动条；事件流、聊天记录、执行日志、
// claude 输出尾部四个区块取消内部垂直滚动条、内容直线完整显示，并改为
// 可折叠（标题点击切换展开/收起）。
// - 事件流/聊天记录/执行日志：标题为可点击折叠按钮，默认展开，点击后
//   内容隐藏、再次点击恢复（内容不丢失）
// - claude 输出尾部：沿用「展开/收起」按钮切换
// - 事件流全量渲染：批量事件逐条完整展示，无虚拟化截断
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

// 递归提取按钮内的纯文本（children 可能含 React 元素，直接 stringify
// 会撞上循环引用）
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

// ---- 折叠行为测试 ----

test('事件流/聊天记录/执行日志 标题均为折叠按钮且默认展开',
  withEventSource(async () => {
    mockTaskApi({
      logs: [{ id: 1, ts: '2026-08-13 10:00:00', level: 'info', message: '日志一行' }],
    }, {
      status: 'running', session_id: 'sess-123', log_offset: 0, log_delta: [],
      transcript: [{ role: 'user', text: '你好，帮我看看' }],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances.find((i) => i.url === '/api/tasks/3/events')
      ?? FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'text', text: '事件内容X' })
      })

      for (const title of ['事件流', '聊天记录', '执行日志']) {
        const btns = findButtons(renderer, title)
        assert.equal(btns.length, 1, `应有且仅有一个「${title}」折叠按钮，实际 ${btns.length} 个`)
        assert.equal(btns[0].props['aria-expanded'], true, `「${title}」默认应展开`)
      }

      const tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /事件内容X/, '事件流内容默认可见')
      assert.match(tree, /你好，帮我看看/, '聊天记录内容默认可见')
      assert.match(tree, /日志一行/, '执行日志内容默认可见')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('点击「事件流」折叠后事件内容隐藏，再次点击恢复',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances.find((i) => i.url === '/api/tasks/3/events')
      ?? FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        es.emit({ seq: 1, kind: 'text', text: '事件内容X' })
        es.emit({ seq: 2, kind: 'text', text: '事件内容Y' })
      })
      assert.match(JSON.stringify(renderer.toJSON()), /事件内容X/)

      // 折叠：内容隐藏，事件保留在状态中
      await click(renderer, findButtons(renderer, '事件流')[0])
      let tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /事件内容X/, '折叠后事件内容应隐藏')
      assert.equal(findButtons(renderer, '事件流')[0].props['aria-expanded'], false)

      // 再次点击恢复显示
      await click(renderer, findButtons(renderer, '事件流')[0])
      tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /事件内容X/, '重新展开后事件内容应恢复')
      assert.match(tree, /事件内容Y/, '重新展开后全部事件均应恢复')
      assert.equal(findButtons(renderer, '事件流')[0].props['aria-expanded'], true)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('点击「聊天记录」折叠/展开切换',
  withEventSource(async () => {
    mockTaskApi(TASK, {
      status: 'running', session_id: 'sess-123', log_offset: 0, log_delta: [],
      transcript: [{ role: 'user', text: '你好，帮我看看' }],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      assert.match(JSON.stringify(renderer.toJSON()), /你好，帮我看看/)

      await click(renderer, findButtons(renderer, '聊天记录')[0])
      let tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /你好，帮我看看/, '折叠后聊天记录应隐藏')
      assert.equal(findButtons(renderer, '聊天记录')[0].props['aria-expanded'], false)

      await click(renderer, findButtons(renderer, '聊天记录')[0])
      tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /你好，帮我看看/, '重新展开后聊天记录应恢复')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('点击「执行日志」折叠/展开切换',
  withEventSource(async () => {
    mockTaskApi({
      logs: [
        { id: 1, ts: '2026-08-13 10:00:00', level: 'info', message: '日志第一行' },
        { id: 2, ts: '2026-08-13 10:00:01', level: 'error', message: '日志第二行' },
      ],
    })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      assert.match(JSON.stringify(renderer.toJSON()), /日志第一行/)

      await click(renderer, findButtons(renderer, '执行日志')[0])
      let tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /日志第一行/, '折叠后执行日志应隐藏')
      assert.doesNotMatch(tree, /日志第二行/, '折叠后全部日志行应隐藏')

      await click(renderer, findButtons(renderer, '执行日志')[0])
      tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /日志第一行/, '重新展开后日志应恢复')
      assert.match(tree, /日志第二行/, '重新展开后全部日志行应恢复')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('claude 输出尾部「展开/收起」按钮切换',
  withEventSource(async () => {
    mockTaskApi({ log_file_tail: 'claude 输出尾部内容' })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      assert.match(JSON.stringify(renderer.toJSON()), /claude 输出尾部内容/, '默认展开显示尾部')

      const btn = findButtons(renderer, 'claude 输出尾部')[0]
      assert.match(buttonText(btn), /收起/, '展开态按钮文案应为「收起」')
      await click(renderer, btn)
      assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /claude 输出尾部内容/, '收起后尾部应隐藏')

      await click(renderer, findButtons(renderer, 'claude 输出尾部')[0])
      assert.match(JSON.stringify(renderer.toJSON()), /claude 输出尾部内容/, '再次展开后尾部应恢复')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))

test('事件流全量渲染：批量事件逐条完整展示（无虚拟化截断）',
  withEventSource(async () => {
    mockTaskApi(TASK)
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const es = FakeEventSource.instances.find((i) => i.url === '/api/tasks/3/events')
      ?? FakeEventSource.instances[0]
      await TestRenderer.act(async () => {
        for (let i = 1; i <= 120; i += 1) {
          es.emit({ seq: i, kind: 'text', text: `批量事件${i}` })
        }
      })
      const tree = JSON.stringify(renderer.toJSON())
      const count = tree.split('批量事件').length - 1
      assert.equal(count, 120, `120 条事件应全部渲染，实际 ${count} 条`)
      assert.match(tree, /批量事件1/, '第一条事件应渲染')
      assert.match(tree, /批量事件120/, '最后一条事件应渲染')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }))
