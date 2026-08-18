// 任务页键盘快捷键集成测试（issue #269）：r = 手动刷新任务列表，
// / = 聚焦搜索框（经 keymap.js focusElement 防御性聚焦，真实 DOM
// 聚焦搜索框、测试渲染器 ref 为 null 时静默跳过）。
//
// 断言：
// 1. 源码：Tasks 经 useShortcuts 注册 refresh / focus-search 动作，
//    / 动作走 focusElement(searchRef.current) 防御性聚焦；
// 2. 行为：按 r 重新拉取 /api/tasks（复用已有 refreshList）；
// 3. 行为：按 / 命中 focus-search 快捷键（preventDefault 证明命中
//    并进入动作分发）；focusElement 聚焦行为由 keymap 单测覆盖；
// 4. 防误触：搜索框聚焦时按 / 不触发。
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

test('源码：Tasks 注册 refresh / focus-search 快捷键动作', () => {
  assert.match(src, /useShortcuts\(/, '应使用 useShortcuts 注册')
  assert.match(src, /'refresh': \(\) => refreshList\(\)/, 'r 应复用 refreshList')
  assert.match(src, /'focus-search': \(\) => focusElement\(searchRef\.current\)/, '/ 应聚焦搜索框 ref')
  assert.match(src, /ref=\{searchRef\}/, '搜索框应挂 ref')
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

test('行为：按 / 命中 focus-search 快捷键（preventDefault）', async () => {
  const { renderer, renderError } = await renderTasks()
  try {
    assert.equal(renderError, null)
    const pd = press('/')
    assert.equal(pd, 1, '按 / 应命中 focus-search（先 preventDefault 再执行动作）')
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
