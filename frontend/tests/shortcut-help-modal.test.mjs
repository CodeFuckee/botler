// 快捷键帮助面板测试（issue #269）：页面右上角「快捷键帮助」按钮打开
// 的弹窗——展示全部快捷键键位 + 「启用键盘快捷键」开关。
//
// 断言：
// 1. 源码：数据源为 keymap.js 的 SHORTCUT_DEFS（集中管理，不维护
//    第二份列表）；开关读写 localStorage（键 botler.shortcuts）；
//    × / 遮罩 / Esc 关闭；i18n 文案 key 齐全；
// 2. 渲染：六项键位（n / r / / / t / g o / g s）全部展示，开关默认
//    勾选（未配置 = 启用），提示文案渲染；
// 3. 开关：取消勾选写 '0'、重新勾选写 '1'，读写一致；
// 4. 关闭：× 按钮 / 点击遮罩 / Esc 均调用 onClose，面板内部点击
//    不关闭（stopPropagation）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))
const src = readFileSync(path.join(ROOT, 'src/components/ShortcutHelpModal.jsx'), 'utf8')

// 界面国际化（issue #268）：组件经 vite SSR 加载，useI18n 无 Provider
// 时回退默认中文，不影响渲染断言
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: ShortcutHelpModal } = await vite.ssrLoadModule('/src/components/ShortcutHelpModal.jsx')
const { SHORTCUT_DEFS, SHORTCUTS_STORAGE_KEY } = await vite.ssrLoadModule('/src/keymap.js')

after(() => vite.close())

// 内存版 localStorage 兼容对象
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  }
}

// 渲染树节点 → 纯文本
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

// ---- 1. 源码断言 ----

test('源码：键位数据源为 keymap.js SHORTCUT_DEFS 单一数据源', () => {
  assert.match(src, /import \{ SHORTCUT_DEFS[^}]*\} from '\.\.\/keymap\.js'/, '应导入 SHORTCUT_DEFS')
  assert.match(src, /SHORTCUT_DEFS\.map/, '应遍历 SHORTCUT_DEFS 渲染键位')
})

test('源码：开关读写 localStorage（键 botler.shortcuts）', () => {
  assert.match(src, /loadShortcutsEnabled\(storage\)/, '初始值应读启用开关')
  assert.match(src, /saveShortcutsEnabled\(storage, next\)/, '切换应持久化启用开关')
})

test('源码：× / 遮罩 / Esc 关闭，面板内部点击不关闭', () => {
  assert.match(src, /onClick=\{onClose\}/, '遮罩点击应关闭')
  assert.match(src, /onClick=\{\(e\) => e\.stopPropagation\(\)\}/, '面板内部点击应阻止冒泡')
  assert.match(src, /e\.key === 'Escape'/, '应监听 Esc 关闭')
  assert.match(src, /modal-close/, '应有 × 关闭按钮')
})

test('i18n：shortcuts.* 文案中英字典齐全', () => {
  for (const d of SHORTCUT_DEFS) {
    assert.equal(typeof zhCN[d.labelKey], 'string', `zh-CN 缺 ${d.labelKey}`)
    assert.equal(typeof enUS[d.labelKey], 'string', `en-US 缺 ${d.labelKey}`)
  }
  for (const k of ['shortcuts.helpTitle', 'shortcuts.enabled', 'shortcuts.enabledHint',
                   'shortcuts.helpBtn', 'shortcuts.helpBtnTitle']) {
    assert.equal(typeof zhCN[k], 'string', `zh-CN 缺 ${k}`)
    assert.equal(typeof enUS[k], 'string', `en-US 缺 ${k}`)
  }
})

// ---- 2. 渲染 ----

test('渲染：六项键位全部展示，开关默认勾选', () => {
  const storage = memStorage()
  let onCloseCalls = 0
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(
      ShortcutHelpModal,
      { onClose: () => { onCloseCalls++ }, storage },
    ))
  })
  try {
    const kbdTexts = renderer.root.findAll((n) => n.type === 'kbd').map((n) => textOf(n.props.children))
    assert.deepEqual(kbdTexts.sort(), SHORTCUT_DEFS.map((d) => d.keys).sort(), '键位应完整展示')
    const toggle = renderer.root.find(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
    assert.equal(toggle.props.checked, true, '未配置（默认）应勾选启用')
    const bodyText = textOf(renderer.root.find((n) => String(n.props.className || '').includes('shortcuts-help-body')).props.children)
    assert.ok(bodyText.includes(zhCN['shortcuts.enabledHint']), '应展示启用开关提示')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：键位标签来自 i18n 文案', () => {
  const renderer = TestRenderer.create(React.createElement(ShortcutHelpModal, {
    onClose: () => {},
    storage: memStorage(),
  }))
  try {
    const bodyText = textOf(renderer.root.find((n) => String(n.props.className || '').includes('shortcuts-help-body')).props.children)
    for (const d of SHORTCUT_DEFS) {
      assert.ok(bodyText.includes(zhCN[d.labelKey]), `应展示 ${d.labelKey} 文案`)
    }
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 3. 开关 ----

test('开关：取消勾选写 0 / 重新勾选写 1', () => {
  const storage = memStorage()
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(
      ShortcutHelpModal,
      { onClose: () => {}, storage },
    ))
  })
  try {
    const toggle = renderer.root.find(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
    TestRenderer.act(() => toggle.props.onChange({ target: { checked: false } }))
    assert.equal(storage.getItem(SHORTCUTS_STORAGE_KEY), '0', '取消勾选应写入 0')
    const toggle2 = renderer.root.find(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
    assert.equal(toggle2.props.checked, false, '勾选状态应同步为关闭')
    TestRenderer.act(() => toggle2.props.onChange({ target: { checked: true } }))
    assert.equal(storage.getItem(SHORTCUTS_STORAGE_KEY), '1', '重新勾选应写入 1')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 4. 关闭 ----

test('关闭：× 按钮与遮罩调用 onClose，面板内部点击不关闭', () => {
  let onCloseCalls = 0
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(ShortcutHelpModal, {
      onClose: () => { onCloseCalls++ },
      storage: memStorage(),
    }))
  })
  try {
    // 面板内部点击：不应关闭
    const modal = renderer.root.find((n) => String(n.props.className || '').includes('shortcuts-help'))
    TestRenderer.act(() => modal.props.onClick({ stopPropagation: () => {} }))
    assert.equal(onCloseCalls, 0, '面板内部点击不应关闭')
    // 遮罩点击：应关闭
    const overlay = renderer.root.find((n) => String(n.props.className || '').includes('modal-overlay'))
    TestRenderer.act(() => overlay.props.onClick())
    assert.equal(onCloseCalls, 1, '遮罩点击应关闭')
    // × 按钮：应关闭
    const close = renderer.root.find((n) => String(n.props.className || '').includes('modal-close'))
    TestRenderer.act(() => close.props.onClick())
    assert.equal(onCloseCalls, 2, '× 按钮应关闭')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('关闭：Esc 键调用 onClose（document mock 捕获监听）', () => {
  let keyHandler = null
  globalThis.document = {
    addEventListener: (ev, fn) => { if (ev === 'keydown') keyHandler = fn },
    removeEventListener: () => {},
  }
  let onCloseCalls = 0
  let renderer = null
  try {
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(ShortcutHelpModal, {
        onClose: () => { onCloseCalls++ },
        storage: memStorage(),
      }))
    })
    assert.ok(keyHandler, '挂载应注册 keydown 监听')
    TestRenderer.act(() => keyHandler({ key: 'Escape' }))
    assert.equal(onCloseCalls, 1, 'Esc 应关闭')
    TestRenderer.act(() => keyHandler({ key: 'Enter' }))
    assert.equal(onCloseCalls, 1, '其他键不应关闭')
  } finally {
    delete globalThis.document
    if (renderer) TestRenderer.act(() => renderer.unmount())
  }
})
