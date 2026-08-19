// 全局搜索默认文案测试（issue #345）：全局搜索入口默认只显示
// 「搜索」两个文字，不显示过长提示。
//
// 需求：全局搜索输入框默认文案「搜索任务、issue、灵感、仓库…」过长，
// 需缩短为只显示「搜索」二字（英文环境 "Search"）。
//
// 断言：
// 1. i18n 字典：zh-CN search.placeholder === "搜索"（恰好 2 个字符）、
//    en-US search.placeholder === "Search"（key 两语均存在）；
// 2. 渲染（默认中文）：侧边栏搜索按钮文字为「搜索」；打开浮层后输入框
//    placeholder / aria-label 均为「搜索」；
// 3. 渲染（英文 en-US）：侧边栏搜索按钮与浮层输入框 placeholder 均为
//    "Search"；
// 4. 边界：中文文案长度恰好为 2（验收标准「两个文字」）；search.* 其它
//    提示文案（emptyHint 等）保持完整，仅缩短默认显示文字。
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
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
const { default: SearchOverlay } = await vite.ssrLoadModule('/src/components/SearchOverlay.jsx')
const { I18nProvider, LANG_STORAGE_KEY } = await vite.ssrLoadModule('/src/i18n.jsx')

after(() => {
  globalThis.fetch = originalFetch
  vite.close()
})

// fetch 快速失败 mock（与 app-shortcuts 同法）：4xx 不重试，auth 快速回退
const originalFetch = globalThis.fetch
globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: 'not found' }) })

// document mock：捕获 App 全局 keydown 监听
const keyListeners = []
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}

// 内存版 localStorage
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  }
}

/** 以指定语言渲染 App（storage 预置语言偏好），等待 auth 流转 */
async function renderApp(lang) {
  const storage = memStorage(lang ? { [LANG_STORAGE_KEY]: lang } : {})
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(
        I18nProvider,
        { storage },
        React.createElement(MemoryRouter, { initialEntries: ['/'] }, React.createElement(App)),
      ),
    )
    await new Promise((resolve) => setTimeout(resolve, 40))
  })
  return { renderer, storage }
}

/** 取侧边栏搜索按钮内的文字节点 */
function sidebarSearchLabel(renderer) {
  const btn = renderer.root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('sidebar-search'))
  assert.equal(btn.length, 1, '应渲染唯一侧边栏搜索入口按钮')
  return btn[0].findAll((n) => String(n.props.className || '').includes('nav-label'))
}

/** 取搜索浮层输入框 */
function overlayInput(renderer) {
  const inputs = renderer.root.findAll(
    (n) => n.type === 'input' && String(n.props.className || '').includes('search-overlay-input'))
  assert.equal(inputs.length, 1, '浮层应渲染唯一搜索输入框')
  return inputs[0]
}

// ---- 1. i18n 字典 ----

test('i18n：中文 search.placeholder 恰为「搜索」两个字', () => {
  assert.equal(typeof zhCN['search.placeholder'], 'string', 'zh-CN 应定义 search.placeholder')
  assert.equal(zhCN['search.placeholder'], '搜索', '中文默认文案应为「搜索」')
  assert.equal(zhCN['search.placeholder'].length, 2, '中文默认文案应恰好 2 个字符（两个文字）')
})

test('i18n：英文 search.placeholder 为 "Search"', () => {
  assert.equal(typeof enUS['search.placeholder'], 'string', 'en-US 应定义 search.placeholder')
  assert.equal(enUS['search.placeholder'], 'Search', '英文默认文案应为 "Search"')
})

// ---- 2. 渲染（默认中文） ----

test('渲染：侧边栏搜索按钮默认显示「搜索」两字', async () => {
  const { renderer } = await renderApp(null)
  try {
    const labels = sidebarSearchLabel(renderer)
    assert.equal(labels.length, 1, '搜索按钮应包含 nav-label 文字节点')
    assert.deepEqual(
      labels[0].children.filter((c) => typeof c === 'string'),
      ['搜索'],
      '侧边栏搜索按钮文字应为「搜索」',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：打开浮层后输入框 placeholder / aria-label 为「搜索」', async () => {
  const { renderer } = await renderApp(null)
  try {
    const btn = renderer.root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('sidebar-search'))
    TestRenderer.act(() => btn[0].props.onClick())
    assert.equal(renderer.root.findAllByType(SearchOverlay).length, 1, '点击后应打开搜索浮层')
    const input = overlayInput(renderer)
    assert.equal(input.props.placeholder, '搜索', '浮层输入框 placeholder 应为「搜索」')
    assert.equal(input.props['aria-label'], '搜索', '浮层输入框 aria-label 应为「搜索」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 3. 渲染（英文 en-US） ----

test('渲染：英文环境下侧边栏与浮层显示 "Search"', async () => {
  const { renderer } = await renderApp('en-US')
  try {
    const labels = sidebarSearchLabel(renderer)
    assert.deepEqual(
      labels[0].children.filter((c) => typeof c === 'string'),
      ['Search'],
      '英文环境侧边栏搜索按钮文字应为 "Search"',
    )
    const btn = renderer.root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('sidebar-search'))
    TestRenderer.act(() => btn[0].props.onClick())
    assert.equal(renderer.root.findAllByType(SearchOverlay).length, 1, '应打开搜索浮层')
    const input = overlayInput(renderer)
    assert.equal(input.props.placeholder, 'Search', '英文环境浮层 placeholder 应为 "Search"')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 4. 边界：其它 search.* 提示文案保持完整 ----

test('边界：search.* 其它提示文案未被删减（仅缩短默认显示文字）', () => {
  // 空态提示 / 跳转提示等辅助文案必须保留完整，避免功能提示丢失
  assert.ok(zhCN['search.emptyHint'].length > 4, '中文空态提示应保持完整')
  assert.ok(enUS['search.emptyHint'].length > 10, '英文空态提示应保持完整')
  assert.ok(zhCN['search.jumpHint'].includes('Enter'), '中文跳转提示应保持完整')
  // 模块标题与其余搜索相关 key 均应存在（不因本次改动丢失）
  for (const key of ['search.title', 'search.tasks', 'search.issues', 'search.inspirations', 'search.repos', 'search.noResults', 'search.loading', 'search.error']) {
    assert.ok(key in zhCN, `zh-CN 应保留 ${key}`)
    assert.ok(key in enUS, `en-US 应保留 ${key}`)
  }
})
