// 设置页「启用键盘快捷键」开关测试（issue #269）：界面显示卡片新增
// 一行开关（键 botler.shortcuts），与帮助面板开关同键位，任意一处
// 切换即时全局生效（分发处理器每次按键实时读取）。
//
// 断言：
// 1. 源码：设置页渲染「启用键盘快捷键」行，勾选状态来自
//    loadShortcutsEnabled，变更写回 saveShortcutsEnabled；
// 2. 渲染：开关初始值跟随 localStorage（未配置默认勾选），切换后
//    持久化到 botler.shortcuts。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：快捷键开关 JSX 移到 components/settings/UiCard.jsx，
// 启用状态初始化（loadShortcutsEnabled）收敛到 hooks/useSettingsData.js
const uiCard = readFileSync(path.join(ROOT, 'src/components/settings/UiCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const src = uiCard + '\n' + hook

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
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { SHORTCUTS_STORAGE_KEY } = await vite.ssrLoadModule('/src/keymap.js')
const { mock } = await import('node:test')

after(() => vite.close())

function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  }
}

// ---- 1. 源码断言 ----

test('源码：设置页「界面显示」卡片含快捷键开关行', () => {
  assert.match(src, /启用键盘快捷键 <code>botler\.shortcuts<\/code>/, '应渲染快捷键开关行')
  assert.match(src, /loadShortcutsEnabled\(/, '开关初始值应读启用状态')
  assert.match(src, /saveShortcutsEnabled\(/, '切换应持久化')
  assert.match(src, /shortcuts-toggle-input/, '开关应为复选框（独立类名便于测试定位）')
})

// ---- 2. 渲染与切换 ----

test('渲染：开关跟随 localStorage（默认勾选，取消写 0 / 勾选写 1）', async () => {
  const storage = memStorage()
  // 设置页直接读全局 localStorage（typeof 守卫），测试注入内存实现
  globalThis.localStorage = storage
  // fetch mock（与 settings-ui-theme.test.mjs 同款）：设置页全部接口
  // 返回完整结构，避免渲染期字段访问崩溃
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    const pathname = String(p)
    if (opts?.method === 'PUT') return { ok: true, status: 200, json: async () => ({}) }
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          worker: { issue_priority: ['bug'] }, sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '', theme: 'system' }, notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {}, ai_providers: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-18 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const shortcutToggle = renderer.root.findAll(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
      .find((t) => String(t.props.className || '').includes('shortcuts-toggle-input'))
    assert.ok(shortcutToggle, '应渲染快捷键复选框')
    assert.equal(shortcutToggle.props.checked, true, '未配置默认应勾选')
    TestRenderer.act(() => shortcutToggle.props.onChange({ target: { checked: false } }))
    assert.equal(storage.getItem(SHORTCUTS_STORAGE_KEY), '0', '取消勾选应写 0')
    // 重新勾选（重新查找节点引用，避免过期实例）
    const toggle2 = renderer.root.findAll(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
      .find((t) => String(t.props.className || '').includes('shortcuts-toggle-input'))
    TestRenderer.act(() => toggle2.props.onChange({ target: { checked: true } }))
    assert.equal(storage.getItem(SHORTCUTS_STORAGE_KEY), '1', '重新勾选应写 1')
  } finally {
    global.fetch = originalFetch
    delete globalThis.localStorage
    await TestRenderer.act(() => renderer.unmount())
  }
})
