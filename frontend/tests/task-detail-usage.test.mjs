// 任务详情页 token 用量卡片测试（issue #235）。
//
// 需求：三种引擎执行的任务都能在任务详情页看到 token 用量（引擎 / 模型 /
// prompt / completion / total / 估算费用）；无用量数据时显示「无数据」而
// 不是报错；estimated_cost 为 null（未配置单价）时只展示 token 数。
//
// 断言：
// 1. TaskDetail 渲染 <UsageCard>（完整任务页与抽屉组件共用）；
// 2. 有 usage 数据 → 展示 引擎/模型/输入/输出/总 tokens/估算费用；
// 3. 无 usage 数据（null）→ 显示「无数据」不崩溃；
// 4. 无单价（estimated_cost=null）→ 显示「未估算（未配置单价）」；
// 5. UsageCard 的 fmtTokens / fmtCost 纯函数格式化正确；
// 6. styles.css 提供 usage-card 样式。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

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
const { default: UsageCard, fmtTokens, fmtCost } =
  await vite.ssrLoadModule('/src/components/UsageCard.jsx')
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
}

const BASE_TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: 'token 用量采集', issue_url: null,
  status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
  exit_code: 0, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null,
  log_file_tail: null, logs: [], environment: null,
  created_at: '2026-08-18 09:00:00', started_at: '2026-08-18 09:01:00',
  finished_at: '2026-08-18 09:30:00', prompt: null,
}

function mockTaskApi(taskOverrides = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...BASE_TASK, ...taskOverrides }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return { status: 'succeeded', session_id: null, log_offset: 0,
               log_delta: [], transcript: [], transcript_truncated: false }
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

// ---- UsageCard 纯函数 ----

test('fmtTokens / fmtCost 格式化', () => {
  assert.equal(fmtTokens(1234567), '1,234,567')
  assert.equal(fmtTokens(null), '—')
  assert.equal(fmtCost(0.0123, 'USD'), 'USD 0.0123')
  assert.equal(fmtCost(null, 'USD'), null)
})

// ---- UsageCard 组件 ----

test('UsageCard：无用量数据时显示「无数据」而不是报错', () => {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(UsageCard, { usage: null }))
  })
  const text = allText(renderer)
  assert.match(text, /Token 用量/)
  assert.match(text, /无数据/)
})

test('UsageCard：有数据展示引擎/模型/token 明细与估算费用', () => {
  const usage = {
    engine: 'claude', model: 'deepseek-v4-flash[1m]',
    prompt_tokens: 170, completion_tokens: 30, total_tokens: 200,
    estimated_cost: 0.5, currency: 'USD', raw_usage: {},
  }
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(UsageCard, { usage }))
  })
  const text = allText(renderer)
  assert.match(text, /Token 用量/)
  assert.match(text, /claude/)
  assert.match(text, /deepseek-v4-flash\[1m\]/)
  assert.match(text, /170/)   // 输入 tokens
  assert.match(text, /30/)    // 输出 tokens
  assert.match(text, /200/)   // 总 tokens
  assert.match(text, /USD 0\.5000/)
})

test('UsageCard：无单价（estimated_cost=null）只展示 token 数', () => {
  const usage = {
    engine: 'dsh', model: 'deepseek-v4-flash',
    prompt_tokens: 100, completion_tokens: 50, total_tokens: 150,
    estimated_cost: null, currency: 'USD', raw_usage: {},
  }
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(UsageCard, { usage }))
  })
  const text = allText(renderer)
  assert.match(text, /未估算/)
  assert.match(text, /150/)
})

// ---- TaskDetail 页面集成 ----

test('TaskDetail：有 usage 数据时渲染用量卡片', withEventSource(async () => {
  mockTaskApi({
    usage: { engine: 'claude', model: 'claude-3-5', prompt_tokens: 170,
             completion_tokens: 30, total_tokens: 200,
             estimated_cost: 0.5, currency: 'USD', raw_usage: {} },
  })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = allText(renderer)
    assert.match(text, /Token 用量/)
    assert.match(text, /claude-3-5/)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
}))

test('TaskDetail：无 usage 数据时显示「无数据」不报错', withEventSource(async () => {
  mockTaskApi({ usage: null })
  const { renderer, renderError } = await renderDetail()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = allText(renderer)
    assert.match(text, /Token 用量/)
    assert.match(text, /无数据/)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
}))

test('styles.css 提供用量卡片样式', () => {
  assert.match(styles, /\.usage-card\s*\{/, '应有用量卡片容器样式')
  assert.match(styles, /\.usage-summary\s*\{/, '应有列表用量摘要样式')
})
