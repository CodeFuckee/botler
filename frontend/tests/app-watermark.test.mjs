// 导航栏任务并发水位徽章测试（issue #257）：
//
// 需求：侧边栏任务导航项下方常驻展示「运行 N · 排队 M · 今日完成 K」：
//   - 数字来自 GET /api/tasks/watermark（与任务列表同表同口径，15s 轮询
//     + 通知事件即时刷新，不增加请求频率）；
//   - 运行 = running + retrying；排队 = queued；今日完成 = completed_today；
//   - 各段点击跳转任务列表对应过滤：运行 → /tasks?status=running,retrying、
//     排队 → /tasks?status=queued、今日完成 → /tasks?status=succeeded；
//   - 接口数据缺失（失败/旧后端）时徽章不渲染、页面不报错；
//   - 折叠态侧边栏（窄栏）隐藏徽章（CSS display:none）。
//
// 测试层次：
// 1. computeWatermarkDisplay 纯函数：空/非法输入 → null；running+retrying
//    求和、queued/completedToday/total 透传；
// 2. 渲染：mock /api/tasks/watermark 返回水位数据 → 徽章三段文案与链接
//    正确渲染（运行 2 · 排队 5 · 今日完成 12）；
// 3. 渲染：接口失败（404）→ 徽章不渲染不崩溃；
// 4. 源码断言：.watermark 样式存在、折叠态 display:none 隐藏。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/App.jsx')
const App = mod.default
const { computeWatermarkDisplay, WATERMARK_POLL_MS } = mod
// 预热懒加载路由页面模块（issue #202）：App 路由经 React.lazy 动态 import，
// 测试前先加载缓存，避免用例结束后懒加载仍在异步加载（vite 关闭后
// fetchModule 传输断开报 uncaughtException）
await vite.ssrLoadModule('/src/pages/Overview.jsx')

after(() => {
  globalThis.fetch = originalFetch
  vite.close()
})

// document mock：捕获 keydown 监听（App 全局快捷键，与 app-shortcuts 同法）
const keyListeners = []
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}

// fetch 路由 mock：auth 通过、watermark 返回水位数据，其余 404
// （settings/notifications/version 均失败，与 app-sidebar 同法静默降级）
const originalFetch = globalThis.fetch
let watermarkBody = {
  queued: 5, running: 1, retrying: 1, succeeded: 30, failed: 2,
  interrupted: 0, canceled_by_user: 0, total: 38, completed_today: 12,
  last_completed_at: '2026-08-23 11:00:00',
}
let watermarkOk = true
function okJson(body, status = 200) {
  return { ok: status < 400, status, json: async () => body }
}
globalThis.fetch = async (url) => {
  const u = String(url)
  if (u.includes('/api/auth/status')) return okJson({ enabled: false, user: null })
  if (u.includes('/api/tasks/watermark')) {
    return watermarkOk ? okJson(watermarkBody) : okJson({ error: 'not found' }, 404)
  }
  return okJson({ error: 'not found' }, 404)
}

/** 以指定初始路径渲染 App，等待 auth 与水位数据流转 */
async function renderApp(initialPath = '/') {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [initialPath] }, React.createElement(App))
    )
    await new Promise((resolve) => setTimeout(resolve, 80))
  })
  return renderer
}

/** 取渲染树中的水位徽章段（Link 渲染为 <a class="watermark-seg ...">） */
function findWatermarkSegs(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'a' && String(n.props.className || '').includes('watermark-seg'))
}

/** 递归收集节点下全部文本（文案在嵌套 span.nav-label 内，需深入子节点） */
function collectText(node) {
  let out = ''
  for (const c of node.children || []) {
    if (typeof c === 'string') out += c
    else out += collectText(c)
  }
  return out
}

// ---- 1. 纯函数 ----

test('computeWatermarkDisplay：null/非法输入返回 null', () => {
  assert.equal(computeWatermarkDisplay(null), null)
  assert.equal(computeWatermarkDisplay(undefined), null)
  assert.equal(computeWatermarkDisplay('x'), null)
  assert.equal(computeWatermarkDisplay(42), null)
})

test('computeWatermarkDisplay：运行=running+retrying，其余透传', () => {
  const d = computeWatermarkDisplay({
    queued: 5, running: 2, retrying: 3, succeeded: 30,
    completed_today: 12, total: 40,
  })
  assert.deepEqual(d, { running: 5, queued: 5, completedToday: 12, total: 40 })
})

test('computeWatermarkDisplay：缺字段按 0 兜底不 NaN', () => {
  const d = computeWatermarkDisplay({})
  assert.deepEqual(d, { running: 0, queued: 0, completedToday: 0, total: 0 })
})

test('WATERMARK_POLL_MS 与现有轮询共用 15s 周期（验收标准 3）', () => {
  assert.equal(WATERMARK_POLL_MS, 15000)
})

// ---- 2. 渲染：徽章三段文案与跳转链接 ----

test('渲染水位徽章：运行 2 · 排队 5 · 今日完成 12，链接对应过滤', async () => {
  const renderer = await renderApp('/')
  try {
    const segs = findWatermarkSegs(renderer)
    assert.equal(segs.length, 3, '应渲染三段水位徽章')

    const text = segs.map(collectText).join(' | ')
    assert.ok(text.includes('运行 2'), `应显示「运行 2」：${text}`)
    assert.ok(text.includes('排队 5'), `应显示「排队 5」：${text}`)
    assert.ok(text.includes('今日完成 12'), `应显示「今日完成 12」：${text}`)

    const hrefs = segs.map((s) => s.props.href)
    assert.ok(hrefs.includes('/tasks?status=running,retrying'), `运行段链接：${hrefs}`)
    assert.ok(hrefs.includes('/tasks?status=queued'), `排队段链接：${hrefs}`)
    assert.ok(hrefs.includes('/tasks?status=succeeded'), `今日完成段链接：${hrefs}`)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('水位接口失败：徽章不渲染、页面不崩溃', async () => {
  watermarkOk = false
  try {
    const renderer = await renderApp('/')
    try {
      assert.equal(findWatermarkSegs(renderer).length, 0, '接口失败时应无徽章段')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    watermarkOk = true
  }
})

// ---- 3. 源码断言：样式与折叠态 ----

test('CSS：.watermark 样式存在，折叠态侧边栏隐藏徽章', () => {
  assert.ok(styles.includes('.watermark {'), '应定义 .watermark 样式')
  assert.ok(styles.includes('.watermark-seg'), '应定义 .watermark-seg 样式')
  assert.ok(styles.includes('.sidebar.collapsed .watermark { display: none; }'),
    '折叠态侧边栏应隐藏水位徽章')
})
