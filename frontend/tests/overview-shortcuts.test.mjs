// 概览页键盘快捷键集成测试（issue #269）：n = 打开首个仓库的「添加
// Issue」弹窗（与点击仓库卡片右上角按钮等效），r = 手动刷新当前页
// 数据（开放 issue / 活跃任务 / 流水线 / 灵感）。
//
// 断言：
// 1. 源码：Overview 经 useShortcuts 注册 new-issue / refresh 动作，
//    动作走已有 setAddIssueRepo / loadIssues 等函数（低成本复用）；
// 2. 行为：按 n 打开添加 Issue 弹窗（目标 = 第一个仓库），且弹窗
//    已打开时再按 n 不重复打开；
// 3. 行为：按 r 重新拉取开放 issue（复用已有加载函数）；
// 4. 防误触：输入框聚焦时按 n 不打开弹窗。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// document mock：捕获全部 keydown 监听（Overview + 弹窗 Esc 监听），
// 提供 querySelectorAll 供任务日志自动滚动 effect 使用
const keyListeners = []
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}

const ISSUES_PAYLOAD = {
  repos: [
    { repo_id: 1, repo_name: 'botler', priority: 10, issues: [
      { iid: 11, title: '已有 issue',
        updated_at: '2026-08-15 01:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/11' },
    ] },
    { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
  ],
  errors: [],
}

const FORM_META = {
  members: [{ id: 20, username: 'agent', name: 'Agent' }],
  labels: [{ name: 'feature', color: 'FF0000', text_color: 'FFFFFF' }],
}

async function renderOverview() {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return ISSUES_PAYLOAD
    if (pathname.startsWith('/api/issues/form-meta/')) return FORM_META
    if (pathname === '/api/settings') return { gitlab: { owner_token_masked: true } }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    if (pathname === '/api/settings/deepseek-balance') return { configured: false }
    if (pathname === '/api/issues/completion-stats') return { completed_count: 0, avg_seconds: null, trend: [] }
    if (pathname.startsWith('/api/usage/stats')) return { summary: [], by_repo: [], by_engine: [], by_date: [] }
    return {}
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
  return { renderer, renderError, getCalls }
}

// 向全部 keydown 监听派发按键（合成事件；target 默认 BODY 非输入框）
function press(renderer, key, opts = {}) {
  const ev = {
    key,
    target: opts.target || { tagName: 'BODY' },
    preventDefault: () => {},
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    repeat: false,
    ...opts,
  }
  for (const fn of [...keyListeners]) {
    TestRenderer.act(() => fn(ev))
  }
}

function addIssueModalCount(renderer) {
  return renderer.root.findAll(
    (n) => String(n.props.className || '').includes('modal add-issue')).length
}

// ---- 1. 源码断言 ----

test('源码：Overview 注册 new-issue / refresh 快捷键动作', () => {
  assert.match(src, /useShortcuts\(/, '应使用 useShortcuts 注册')
  assert.match(src, /'new-issue': \(\) => \{/, '应注册 new-issue')
  assert.match(src, /'refresh': \(\) => \{/, '应注册 refresh')
  assert.match(src, /setAddIssueRepo\(repoIssues\[0\]\)/, 'n 应打开首个仓库弹窗')
  assert.match(src, /loadIssues\(\)/, 'r 应刷新开放 issue')
})

// ---- 2. 行为 ----

test('行为：按 n 打开首个仓库的添加 Issue 弹窗', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(addIssueModalCount(renderer), 0, '初始不应有弹窗')
    press(renderer, 'n')
    // 等弹窗元数据加载
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    assert.equal(addIssueModalCount(renderer), 1, '按 n 应打开添加 Issue 弹窗')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：弹窗已打开时按 n 不重复打开', async () => {
  const { renderer } = await renderOverview()
  try {
    press(renderer, 'n')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    press(renderer, 'n')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    assert.equal(addIssueModalCount(renderer), 1, '已打开时重复按 n 不应叠加弹窗')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：按 r 重新拉取开放 issue 数据', async () => {
  const { renderer, getCalls } = await renderOverview()
  try {
    const before = getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(before >= 1, '初始应已拉取开放 issue')
    press(renderer, 'r')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    const after = getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(after > before, '按 r 应重新拉取开放 issue（复用已有加载函数）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：输入框聚焦时按 n 不打开弹窗（防误触）', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    press(renderer, 'n', { target: { tagName: 'INPUT' } })
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    assert.equal(addIssueModalCount(renderer), 0, '输入框聚焦时按 n 不应打开弹窗')
    // 对照：非输入框聚焦时按 n 正常打开
    press(renderer, 'n')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    assert.equal(addIssueModalCount(renderer), 1, '非输入框聚焦时按 n 应正常打开')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
