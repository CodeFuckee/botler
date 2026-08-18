// 全站快捷键与帮助按钮测试（issue #269）：App 导航栏右上角「快捷键
// 帮助」按钮打开帮助面板；全局快捷键 t / g o / g s 跳转对应页面；
// 快捷键开关关闭时全部失效。
//
// 断言：
// 1. 渲染：导航栏渲染「快捷键帮助」按钮，点击打开帮助面板（键位表
//    完整：n / r / / / t / g o / g s）；
// 2. 行为：按 t 跳转任务列表页、g o 跳转概览页、g s 跳转设置页
//    （与点击导航等效）；
// 3. 开关：localStorage botler.shortcuts = '0' 时按 t 不跳转（一键
//    禁用生效，验收标准 3）。
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
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')
const { default: ShortcutHelpModal } = await vite.ssrLoadModule('/src/components/ShortcutHelpModal.jsx')

after(() => vite.close())

// document mock：捕获 keydown 监听（App 全局 + 各页面级快捷键）
const keyListeners = []
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}

// 内存版 localStorage（node 无 localStorage，App 侧 typeof 守卫读取）
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  }
}

// 以指定初始路径渲染 App，等待 auth 状态流转（与 app-default-page 同法）
async function renderAt(initialPath) {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [initialPath] }, React.createElement(App))
    )
    await new Promise((resolve) => setTimeout(resolve, 40))
  })
  return renderer
}

// 向全部 keydown 监听派发按键（BODY 目标 = 非输入框）
function press(key) {
  const ev = {
    key,
    target: { tagName: 'BODY' },
    preventDefault: () => {},
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    repeat: false,
  }
  for (const fn of [...keyListeners]) TestRenderer.act(() => fn(ev))
}

// ---- 1. 帮助按钮与面板 ----

test('渲染：导航栏「快捷键帮助」按钮打开帮助面板（键位完整）', async () => {
  const renderer = await renderAt('/')
  try {
    const helpBtn = renderer.root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('shortcuts-help-btn'))
    assert.equal(helpBtn.length, 1, '导航栏应渲染快捷键帮助按钮')
    TestRenderer.act(() => helpBtn[0].props.onClick())
    const modal = renderer.root.findAllByType(ShortcutHelpModal)
    assert.equal(modal.length, 1, '点击应打开帮助面板')
    const kbdTexts = renderer.root.findAll((n) => n.type === 'kbd').map((n) => n.props.children)
    assert.deepEqual(kbdTexts.sort(), ['/', 'g o', 'g s', 'n', 'r', 't'].sort(), '帮助面板应展示全部键位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 2. 全局跳转 ----

test('行为：按 t 跳转任务列表页', async () => {
  const renderer = await renderAt('/')
  try {
    assert.equal(renderer.root.findAllByType(Overview).length, 1, '初始应在概览页')
    press('t')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)) })
    assert.equal(renderer.root.findAllByType(Tasks).length, 1, '按 t 应跳转任务列表页')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('行为：g o / g s 组合键跳转概览 / 设置页', async () => {
  const renderer = await renderAt('/tasks')
  try {
    assert.equal(renderer.root.findAllByType(Tasks).length, 1, '初始应在任务页')
    press('g')
    press('o')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)) })
    assert.equal(renderer.root.findAllByType(Overview).length, 1, 'g o 应跳转概览页')
    press('g')
    press('s')
    await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)) })
    assert.equal(renderer.root.findAllByType(Settings).length, 1, 'g s 应跳转设置页')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 3. 一键禁用 ----

test('开关：localStorage 关闭时按 t 不跳转（一键禁用生效）', async () => {
  // 预置禁用开关（'0'），App 侧 storage 读取到关闭状态
  globalThis.localStorage = memStorage({ 'botler.shortcuts': '0' })
  try {
    const renderer = await renderAt('/')
    try {
      assert.equal(renderer.root.findAllByType(Overview).length, 1, '初始应在概览页')
      press('t')
      await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)) })
      assert.equal(renderer.root.findAllByType(Tasks).length, 0, '开关关闭时按 t 不应跳转')
      assert.equal(renderer.root.findAllByType(Overview).length, 1, '应停留在概览页')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    delete globalThis.localStorage
  }
})
