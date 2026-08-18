// 任务详情页单任务停止/重试按钮测试（issue #214）。
//
// 需求：任务详情页（TaskDetail.jsx）新增「停止」按钮（仅 running 任务，
// 确认后调 POST /api/tasks/{id}/stop，停止不可逆）与「重试」按钮
// （failed/interrupted 任务，复用任务列表页手动重试 issue #36 的
// POST /api/tasks/{id}/retry 逻辑）；操作带确认与状态反馈，失败提示
// 行内横幅展示不整页替换。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'
import { readFileSync } from 'node:fs'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const detailSrc = readFileSync(path.join(ROOT, 'src/pages/TaskDetail.jsx'), 'utf8')

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
const dialog = await vite.ssrLoadModule('/src/dialog.js')

after(() => vite.close())

class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.onerror = null
    this.closed = false
    FakeEventSource.instances.push(this)
  }
  close() { this.closed = true }
  emit(event) { if (this.onmessage) this.onmessage({ data: JSON.stringify(event) }) }
}

function withEventSource(fn) {
  return async () => {
    FakeEventSource.instances = []
    const saved = globalThis.EventSource
    globalThis.EventSource = FakeEventSource
    try { await fn() } finally {
      globalThis.EventSource = saved
      FakeEventSource.instances = []
    }
  }
}

const BASE_TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '修复登录问题', issue_url: null,
  status: 'running', attempt_count: 1, triggered_by: 'webhook',
  exit_code: null, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null,
  log_file_tail: null, logs: [],
  created_at: '2026-08-18 09:00:00', started_at: '2026-08-18 09:01:00',
  finished_at: null, prompt: null,
}

function mockTaskApi(taskOverrides = {}, execution = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...BASE_TASK, ...taskOverrides }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: taskOverrides.status || 'running', session_id: null,
                            log_offset: 0, log_delta: [], transcript: [],
                            transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
}

// 渲染树节点 → 纯文本
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
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
    } catch (e) { renderError = e }
  })
  return { renderer, renderError }
}

function findButtons(renderer) {
  return renderer.root.findAllByType('button').filter((b) => {
    const t = textOf(b.props.children)
    return t === '停止' || t === '停止中…' || t === '重试' || t === '重试中…'
  })
}

function withConfirm(value) {
  dialog.installAutoAnswer(() => value)
}

async function flushMicrotasks() {
  await new Promise((resolve) => setTimeout(resolve, 10))
}

// ---- 源码断言 ----

test('任务详情页源码含停止/重试按钮与确认交互', () => {
  assert.match(detailSrc, /confirmDialog/, '点击应先弹自定义确认对话框')
  assert.match(
    detailSrc,
    /api\.post\(`\/api\/tasks\/\$\{[^}]+\}\/stop`\)/,
    '停止确认后应调 POST /api/tasks/{id}/stop',
  )
  assert.match(
    detailSrc,
    /api\.post\(`\/api\/tasks\/\$\{[^}]+\}\/retry`\)/,
    '重试确认后应调 POST /api/tasks/{id}/retry',
  )
  assert.match(detailSrc, /alert-ok/, '成功后应显示绿色成功提示')
  assert.match(detailSrc, /actionErr/, '失败应有行内错误提示（不整页替换）')
})

test('停止按钮仅 running 渲染，重试按钮仅 failed/interrupted 渲染', () => {
  assert.match(
    detailSrc,
    /task\.status === 'running'/,
    '停止按钮应按 running 状态条件渲染',
  )
  assert.match(
    detailSrc,
    /task\.status === 'failed' \|\| task\.status === 'interrupted'/,
    '重试按钮应按失败/中断状态条件渲染',
  )
})

// ---- 组件渲染 ----

test('running 任务显示停止按钮，不显示重试', withEventSource(async () => {
  mockTaskApi({ status: 'running' })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = findButtons(renderer)
    assert.equal(btns.length, 1, 'running 任务应只有停止按钮')
    assert.ok(textOf(btns[0].props.children).includes('停止'))
    assert.notEqual(btns[0].props.disabled, true, '停止按钮应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('failed 任务显示重试按钮，不显示停止', withEventSource(async () => {
  mockTaskApi({ status: 'failed', error_message: '重试耗尽后仍失败' })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    const btns = findButtons(renderer)
    assert.equal(btns.length, 1, 'failed 任务应只有重试按钮')
    assert.ok(textOf(btns[0].props.children).includes('重试'))
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('interrupted 任务显示重试按钮', withEventSource(async () => {
  mockTaskApi({ status: 'interrupted', error_message: '用户手动停止' })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    const btns = findButtons(renderer)
    assert.equal(btns.length, 1, 'interrupted 任务应显示重试按钮')
    assert.ok(textOf(btns[0].props.children).includes('重试'))
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('succeeded 任务不显示停止/重试按钮', withEventSource(async () => {
  mockTaskApi({ status: 'succeeded', exit_code: 0 })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    assert.equal(findButtons(renderer).length, 0, '终态成功任务无操作按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('停止确认框取消时不调用接口', withEventSource(async () => {
  withConfirm(false)
  mockTaskApi({ status: 'running' })
  const postCalls = []
  mock.method(api, 'post', async (p) => { postCalls.push(p); return { task_id: 3 } })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(postCalls.length, 0, '取消确认后不应调用停止接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('确认停止后调用接口并显示成功提示', withEventSource(async () => {
  withConfirm(true)
  mockTaskApi({ status: 'running' })
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { task_id: 3, status: 'interrupted' }
  })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.deepEqual(postCalls, ['/api/tasks/3/stop'], '确认后应调停止接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已停止'), '应显示停止成功提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('停止失败显示行内错误提示不整页替换', withEventSource(async () => {
  withConfirm(true)
  mockTaskApi({ status: 'running' })
  mock.method(api, 'post', async () => {
    throw new Error('停止失败：内部错误')
  })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(async () => {
      findButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('停止失败：内部错误'), '应显示行内错误提示')
    assert.ok(text.includes('执行日志'), '页面应保留任务详情（未整页替换）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('确认重试后调用接口并显示成功提示', withEventSource(async () => {
  withConfirm(true)
  mockTaskApi({ status: 'failed', error_message: '原因' })
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { task_id: 3, status: 'queued' }
  })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.deepEqual(postCalls, ['/api/tasks/3/retry'], '确认后应调重试接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已重新入队'), '应显示重试成功提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))

test('停止请求进行中按钮禁用', withEventSource(async () => {
  withConfirm(true)
  mockTaskApi({ status: 'running' })
  let resolvePost = null
  mock.method(api, 'post', async () => {
    await new Promise((resolve) => { resolvePost = resolve })
    return { task_id: 3, status: 'interrupted' }
  })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null)
    let clickPromise = null
    await TestRenderer.act(async () => {
      clickPromise = findButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(findButtons(renderer)[0].props.disabled, true, '请求中停止按钮应禁用')
    resolvePost()
    await TestRenderer.act(() => clickPromise)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
}))
