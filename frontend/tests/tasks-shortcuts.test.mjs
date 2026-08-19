// 任务页键盘快捷键集成测试（issue #269 / #216）：r = 手动刷新任务列表。
// issue #216 起 / 升级为全站全局搜索快捷键（App 层注册打开搜索浮层），
// 任务页不再注册 focus-search——本页搜索框保留点击使用，按 / 不再
// 产生任何动作（preventDefault 计数为 0 证明未命中任务页快捷键）。
//
// 断言：
// 1. 源码：Tasks 经 useShortcuts 只注册 refresh 动作，未注册
//    focus-search（/ 已移交 App 层全局搜索，issue #216）；
// 2. 行为：按 r 重新拉取 /api/tasks（复用已有 refreshList）；
// 3. 行为：按 / 不再命中任务页快捷键（不 preventDefault）；输入框
//    聚焦时按 / 同样不触发（防误触由 keymap.js 统一处理）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  // react-router-dom CJS 构建不能被 vite SSR 转译，alias 到测试 mock
  // （与 tasks-refresh-button.test.mjs 一致）
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// document mock：捕获 keydown 监听（Tasks 页面级 useShortcuts）
const keyListeners = []
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}

// 向全部 keydown 监听派发按键；返回 preventDefault 被调用的次数
function press(key, opts = {}) {
  let pd = 0
  const ev = {
    key,
    target: opts.target || { tagName: 'BODY' },
    preventDefault: () => { pd++ },
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    repeat: false,
    ...opts,
  }
  for (const fn of [...keyListeners]) {
    TestRenderer.act(() => fn(ev))
  }
  return pd
}

async function renderTasks() {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [], total: 0, stats: { queued: 0, running: 0, retrying: 0 } }
    }
    if (pathname === '/api/repos') return { repos: [] }
    return {}
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Tasks))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError, getCalls }
}

// ---- 1. 源码断言 ----

test('源码：Tasks 只注册 refresh，未注册 focus-search（/ 移交全局搜索）', () => {
  assert.match(src, /useShortcuts\(/, '应使用 useShortcuts 注册')
  assert.match(src, /'refresh': \(\) => refreshList\(\)/, 'r 应复用 refreshList')
  assert.doesNotMatch(src, /focus-search/, '任务页不应再注册 focus-search（issue #216 全局搜索）')
})

// ---- 2. 行为 ----

test('行为：按 r 重新拉取任务列表', async () => {
  const { renderer, renderError, getCalls } = await renderTasks()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const before = getCalls.filter((p) => p.startsWith('/api/tasks?')).length
    assert.ok(before >= 1, '初始应已拉取任务列表')
    press('r')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)) })
    const after = getCalls.filter((p) => p.startsWith('/api/tasks?')).length
    assert.ok(after > before, '按 r 应重新拉取任务列表（复用 refreshList）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：按 / 不再命中任务页快捷键（全局搜索已接管）', async () => {
  const { renderer, renderError } = await renderTasks()
  try {
    assert.equal(renderError, null)
    const pd = press('/')
    assert.equal(pd, 0, '任务页未注册 focus-search，按 / 不应 preventDefault')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：搜索框聚焦时按 / 不触发（防误触）', async () => {
  const { renderer, renderError } = await renderTasks()
  try {
    assert.equal(renderError, null)
    const pd = press('/', { target: { tagName: 'INPUT' } })
    assert.equal(pd, 0, '输入框聚焦时按 / 不应命中快捷键')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
