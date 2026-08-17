// 任务详情页执行环境快照测试（issue #276）。
//
// 需求：任务开始时采集执行环境（引擎版本/模型/起始提交/平台版本/config
// 关键项 hash）落库 tasks.environment，任务详情页「元信息」区以折叠面板
// 展示；无快照显示「暂无环境快照」，采集失败显示「环境快照获取失败」，
// 任务照常执行不阻塞。
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

const BASE_TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '任务执行环境详情记录', issue_url: null,
  status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
  exit_code: 0, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null,
  log_file_tail: null, logs: [],
  created_at: '2026-08-18 09:00:00', started_at: '2026-08-18 09:01:00',
  finished_at: '2026-08-18 09:30:00', prompt: null,
}

const ENV_SNAPSHOT = {
  engine: { name: 'claude', version: '2.1.226' },
  model: { name: 'deepseek-v4-pro[1m]', provider: 'deepseek' },
  git: { branch: 'main', commit_sha: 'a'.repeat(40) },
  platform: { version: '1.0.289' },
  config_hash: 'abc123def456',
  captured_at: '2026-08-18T09:00:05+08:00',
}

function mockTaskApi(taskOverrides = {}, execution = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...BASE_TASK, ...taskOverrides }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: 'succeeded', session_id: null,
                            log_offset: 0, log_delta: [], transcript: [],
                            transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
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

// 收集 kv 表格所有 <th> 单元格文本（键）
function thTexts(renderer) {
  return renderer.root.findAllByType('th').map((n) => {
    const parts = []
    const walk = (c) => {
      if (c == null) return
      if (typeof c === 'string') { parts.push(c); return }
      if (Array.isArray(c)) { c.forEach(walk); return }
      if (typeof c === 'object' && c.props) walk(c.props.children)
    }
    walk(n.props.children)
    return parts.join('')
  })
}

// 递归提取 <td> 单元格内文本（含 <code> 内联元素）
function tdTexts(renderer) {
  return renderer.root.findAllByType('td').map((n) => {
    const parts = []
    const walk = (c) => {
      if (c == null) return
      if (typeof c === 'string') { parts.push(c); return }
      if (Array.isArray(c)) { c.forEach(walk); return }
      if (typeof c === 'object' && c.props) walk(c.props.children)
    }
    walk(n.props.children)
    return parts.join('')
  })
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
  return renderer.root.findAllByType('button')
    .filter((b) => buttonText(b).includes(text))
}

// 提取整棵树可见文本（用于无表格结构的提示文案断言）。
// renderer.toJSON() 返回 DOM 节点树，递归收集所有字符串叶子节点
function allText(renderer) {
  const parts = []
  const walk = (n) => {
    if (n == null) return
    if (typeof n === 'string') { parts.push(n); return }
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.children) walk(n.children)
  }
  walk(renderer.toJSON())
  return parts.join(' ')
}

// ---- 环境快照面板测试 ----

test('有环境快照时元信息区展示引擎/模型/起始提交/平台版本/配置哈希',
  withEventSource(async () => {
    mockTaskApi({ environment: ENV_SNAPSHOT })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      // 折叠按钮存在且默认展开
      const toggles = findButtons(renderer, '执行环境快照')
      assert.equal(toggles.length, 1, '应有且仅有一个「执行环境快照」折叠按钮')
      assert.equal(toggles[0].props['aria-expanded'], true, '环境快照面板默认应展开')

      const ths = thTexts(renderer)
      const tds = tdTexts(renderer)
      assert.ok(ths.includes('引擎'), '应展示「引擎」行')
      assert.ok(ths.includes('模型'), '应展示「模型」行')
      assert.ok(ths.includes('起始提交'), '应展示「起始提交」行')
      assert.ok(ths.includes('平台版本'), '应展示「平台版本」行')
      assert.ok(ths.includes('配置哈希'), '应展示「配置哈希」行')
      assert.ok(ths.includes('采集时间'), '应展示「采集时间」行')
      assert.ok(tds.some((t) => t.includes('claude') && t.includes('2.1.226')), '应展示引擎名与版本')
      assert.ok(tds.some((t) => t.includes('deepseek-v4-pro[1m]')), '应展示模型名')
      assert.ok(tds.some((t) => t.includes('main')), '应展示分支名')
      assert.ok(tds.some((t) => t.includes('abc123def456')), '应展示配置哈希')
      assert.ok(tds.some((t) => t.includes('1.0.289')), '应展示平台版本')
    } finally {
      renderer.unmount()
    }
  }))

test('环境快照面板可折叠收起再展开（内容不丢失）',
  withEventSource(async () => {
    mockTaskApi({ environment: ENV_SNAPSHOT })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null)
      const toggles = findButtons(renderer, '执行环境快照')
      assert.equal(toggles.length, 1)
      // 收起
      await TestRenderer.act(async () => { toggles[0].props.onClick() })
      assert.equal(findButtons(renderer, '执行环境快照')[0].props['aria-expanded'], false)
      assert.ok(!thTexts(renderer).includes('引擎'), '收起后应隐藏快照内容')
      // 展开恢复
      await TestRenderer.act(async () => { findButtons(renderer, '执行环境快照')[0].props.onClick() })
      assert.ok(thTexts(renderer).includes('引擎'), '再次展开应恢复内容')
    } finally {
      renderer.unmount()
    }
  }))

test('采集失败时显示「环境快照获取失败」且不阻塞页面',
  withEventSource(async () => {
    mockTaskApi({ environment: { error: '环境快照获取失败', captured_at: '2026-08-18T09:00:05+08:00' } })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      assert.ok(allText(renderer).includes('环境快照获取失败'), '应显示「环境快照获取失败」')
      // 页面主体仍正常渲染（任务标题/状态）
      assert.ok(allText(renderer).includes('任务 #'), '任务详情主体应正常渲染')
      assert.ok(allText(renderer).includes('成功'), '任务状态徽标应正常渲染')
    } finally {
      renderer.unmount()
    }
  }))

test('无环境快照时显示「暂无环境快照」（旧任务兼容）',
  withEventSource(async () => {
    mockTaskApi({ environment: null })
    const { renderer, renderError } = await renderDetail()
    try {
      assert.equal(renderError, null)
      assert.ok(allText(renderer).includes('暂无环境快照'), '应显示「暂无环境快照」')
    } finally {
      renderer.unmount()
    }
  }))
