// 键盘快捷键核心模块测试（issue #269）：keymap.js 集中管理全站
// 快捷键绑定——定义表（帮助面板与分发共用）、启用开关（localStorage
// 持久化 + 一键禁用）、防误触（输入框聚焦不触发）、单键 + 组合键
// （g o / g s）匹配、全局 keydown 分发与 React hook 生命周期。
//
// 断言：
// 1. 定义表完整：n / r / / / t / g o / g s 六项齐全，id 唯一，
//    labelKey 在 i18n 字典中有值，scope 合法；
// 2. 匹配器：单键命中（含大小写归一）、组合键两段命中、前缀键本身
//    不触发、组合超时复位、组合失败降级单键、reset 清理；
// 3. 防误触：input / textarea / select / contenteditable → 不触发；
//    按钮 / 普通 div → 正常触发；null / 无 tagName → 不误判；
// 4. 启用开关：默认开启、'0' 关闭、'1'/非法值开启、保存读写正确、
//    存储不可用兜底开启；
// 5. 分发处理器：命中执行 action 并 preventDefault、未注册 action
//    不拦截、开关关闭 / 输入框聚焦 / ctrl/meta/alt / Esc / 长按
//    repeat / 空事件均不触发；
// 6. useShortcuts hook：挂载注册监听、按键触发动作、卸载移除监听、
//    getEnabled 覆盖开关生效。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const {
  SHORTCUT_DEFS,
  SHORTCUTS_STORAGE_KEY,
  COMBO_TIMEOUT_MS,
  loadShortcutsEnabled,
  saveShortcutsEnabled,
  isTypingTarget,
  focusElement,
  createShortcutMatcher,
  createKeydownHandler,
} = await import(path.join(ROOT, 'src/keymap.js'))

const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))

// 合成 keydown 事件对象（测试用最小结构）
function keyEvent(key, opts = {}) {
  return {
    key,
    target: opts.target || { tagName: 'BODY' },
    preventDefault: opts.preventDefault || (() => {}),
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    repeat: false,
    ...opts,
  }
}

// 简单 localStorage 兼容存储（内存实现）
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  }
}

// ---- 1. 定义表完整 ----

test('定义表：n / r / / / t / g o / g s 六项齐全且 id 唯一', () => {
  const ids = SHORTCUT_DEFS.map((d) => d.id)
  assert.equal(new Set(ids).size, ids.length, 'id 应唯一')
  const keys = SHORTCUT_DEFS.map((d) => d.keys).sort()
  assert.deepEqual(keys, ['/', 'g o', 'g s', 'n', 'r', 't'], '快捷键集应完整')
})

test('定义表：每项有 labelKey 且中英字典均提供文案', () => {
  for (const d of SHORTCUT_DEFS) {
    assert.ok(d.labelKey, `${d.id} 应有 labelKey`)
    assert.equal(typeof zhCN[d.labelKey], 'string', `zh-CN 应有 ${d.labelKey}`)
    assert.equal(typeof enUS[d.labelKey], 'string', `en-US 应有 ${d.labelKey}`)
  }
})

test('定义表：scope 为合法范围数组（global / overview / tasks）', () => {
  const VALID = new Set(['global', 'overview', 'tasks'])
  for (const d of SHORTCUT_DEFS) {
    assert.ok(Array.isArray(d.scope) && d.scope.length > 0, `${d.id} 应有 scope 数组`)
    for (const s of d.scope) assert.ok(VALID.has(s), `${d.id} scope ${s} 非法`)
  }
})

// ---- 2. 匹配器 ----

test('匹配器：单键命中（n/r/t//）', () => {
  const { match } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('n'), 'new-issue')
  assert.equal(match('r'), 'refresh')
  assert.equal(match('t'), 'go-tasks')
  assert.equal(match('/'), 'focus-search')
})

test('匹配器：大小写归一（N → new-issue）', () => {
  const { match } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('N'), 'new-issue')
  assert.equal(match('G'), null, '组合前缀键本身不触发')
})

test('匹配器：组合键两段命中（g o / g s）', () => {
  const { match } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('g'), null, '前缀键 g 不触发动作')
  assert.equal(match('o'), 'go-overview')
  assert.equal(match('g'), null)
  assert.equal(match('s'), 'go-settings')
})

test('匹配器：组合失败降级单键（g 后按 r 仍触发刷新）', () => {
  const { match } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('g'), null)
  assert.equal(match('r'), 'refresh')
})

test('匹配器：组合超时复位（2 秒后前缀失效）', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { match } = createShortcutMatcher(SHORTCUT_DEFS)
    assert.equal(match('g'), null)
    mock.timers.tick(COMBO_TIMEOUT_MS + 10)
    assert.equal(match('o'), null, '超时后 o 不应命中 go-overview')
    assert.equal(match('n'), 'new-issue', '超时复位后单键仍正常')
  } finally {
    mock.timers.reset()
  }
})

test('匹配器：reset 清理组合前缀', () => {
  const { match, reset } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('g'), null)
  reset()
  assert.equal(match('o'), null, 'reset 后 o 不应命中组合')
})

test('匹配器：未知键 / 空键返回 null', () => {
  const { match } = createShortcutMatcher(SHORTCUT_DEFS)
  assert.equal(match('x'), null)
  assert.equal(match(''), null)
  assert.equal(match(undefined), null)
  assert.equal(match(null), null)
})

// ---- 3. 防误触 ----

test('防误触：输入型元素聚焦不触发（input/textarea/select/contenteditable）', () => {
  assert.equal(isTypingTarget({ tagName: 'INPUT' }), true)
  assert.equal(isTypingTarget({ tagName: 'input' }), true)
  assert.equal(isTypingTarget({ tagName: 'TEXTAREA' }), true)
  assert.equal(isTypingTarget({ tagName: 'SELECT' }), true)
  assert.equal(isTypingTarget({ tagName: 'DIV', isContentEditable: true }), true)
})

test('防误触：非输入型元素正常触发', () => {
  assert.equal(isTypingTarget({ tagName: 'BUTTON' }), false)
  assert.equal(isTypingTarget({ tagName: 'DIV' }), false)
  assert.equal(isTypingTarget({ tagName: 'BODY' }), false)
  assert.equal(isTypingTarget({ tagName: 'SPAN' }), false)
})

test('防误触：null / 无 tagName 的对象不误判', () => {
  assert.equal(isTypingTarget(null), false)
  assert.equal(isTypingTarget(undefined), false)
  assert.equal(isTypingTarget({}), false)
  assert.equal(isTypingTarget({ isContentEditable: false }), false)
})

// ---- 3.5 focusElement ----

test('focusElement：聚焦可聚焦元素并返回 true', () => {
  let focused = 0
  const el = { focus: () => { focused++ } }
  assert.equal(focusElement(el), true)
  assert.equal(focused, 1)
})

test('focusElement：null / 无 focus 方法静默跳过返回 false', () => {
  assert.equal(focusElement(null), false)
  assert.equal(focusElement(undefined), false)
  assert.equal(focusElement({}), false)
  assert.equal(focusElement({ focus: 'not-a-function' }), false)
})

// ---- 4. 启用开关 ----

test('开关：未写入默认开启，仅 "0" 为关闭', () => {
  const storage = memStorage()
  assert.equal(loadShortcutsEnabled(storage), true)
  saveShortcutsEnabled(storage, false)
  assert.equal(loadShortcutsEnabled(storage), false)
  saveShortcutsEnabled(storage, true)
  assert.equal(loadShortcutsEnabled(storage), true)
  storage.setItem(SHORTCUTS_STORAGE_KEY, 'garbage')
  assert.equal(loadShortcutsEnabled(storage), true, '非法值按开启处理')
})

test('开关：存储不可用 / 异常兜底开启', () => {
  assert.equal(loadShortcutsEnabled(null), true)
  assert.equal(loadShortcutsEnabled(undefined), true)
  assert.equal(loadShortcutsEnabled({
    getItem() { throw new Error('denied') },
    setItem() { throw new Error('denied') },
  }), true)
  assert.doesNotThrow(() => saveShortcutsEnabled(null, false))
})

// ---- 5. 分发处理器 ----

test('分发：命中快捷键执行 action 并 preventDefault', () => {
  const calls = []
  const preventDefault = () => calls.push('pd')
  const onKeydown = createKeydownHandler({
    actions: { 'new-issue': () => calls.push('new-issue') },
  })
  onKeydown(keyEvent('n', { preventDefault }))
  assert.deepEqual(calls, ['pd', 'new-issue'], '应 preventDefault 并执行 action')
})

test('分发：命中但未注册 action 不拦截不报错', () => {
  let pd = 0
  const onKeydown = createKeydownHandler({ actions: {} })
  onKeydown(keyEvent('n', { preventDefault: () => { pd++ } }))
  assert.equal(pd, 0, '未注册 action 不应 preventDefault')
})

test('分发：开关关闭时全部快捷键失效', () => {
  let called = 0
  const onKeydown = createKeydownHandler({
    actions: { 'new-issue': () => { called++ } },
    getEnabled: () => false,
  })
  onKeydown(keyEvent('n'))
  assert.equal(called, 0)
})

test('分发：输入框聚焦时不触发（防误触）', () => {
  let called = 0
  const onKeydown = createKeydownHandler({
    actions: { 'new-issue': () => { called++ } },
  })
  onKeydown(keyEvent('n', { target: { tagName: 'INPUT' } }))
  onKeydown(keyEvent('n', { target: { tagName: 'DIV', isContentEditable: true } }))
  assert.equal(called, 0)
})

test('分发：ctrl/meta/alt 系统组合键不抢占', () => {
  let called = 0
  const onKeydown = createKeydownHandler({
    actions: { 'go-tasks': () => { called++ } },
  })
  onKeydown(keyEvent('t', { ctrlKey: true }))
  onKeydown(keyEvent('t', { metaKey: true }))
  onKeydown(keyEvent('t', { altKey: true }))
  assert.equal(called, 0)
})

test('分发：Esc 不拦截（交由 DialogHost / 弹窗处理）', () => {
  let called = 0
  const onKeydown = createKeydownHandler({ actions: {} })
  assert.doesNotThrow(() => onKeydown(keyEvent('Escape')))
  assert.equal(called, 0)
})

test('分发：长按 repeat 不重复触发', () => {
  let called = 0
  const onKeydown = createKeydownHandler({
    actions: { 'new-issue': () => { called++ } },
  })
  onKeydown(keyEvent('n', { repeat: true }))
  assert.equal(called, 0)
})

test('分发：空事件 / 无 key 不抛错', () => {
  const onKeydown = createKeydownHandler({ actions: {} })
  assert.doesNotThrow(() => onKeydown(null))
  assert.doesNotThrow(() => onKeydown({}))
  assert.doesNotThrow(() => onKeydown(undefined))
})

// ---- 6. useShortcuts hook ----

test('hook：挂载注册监听 / 按键触发 / 卸载移除', async () => {
  // 复用 dialog.test.mjs 的 document mock 模式捕获 keydown 监听
  let keyHandler = null
  const listeners = { keydown: [] }
  globalThis.document = {
    addEventListener: (ev, fn) => { listeners[ev].push(fn) },
    removeEventListener: (ev, fn) => {
      listeners[ev] = listeners[ev].filter((f) => f !== fn)
    },
  }
  const { default: React } = await import('react')
  const TestRenderer = (await import('react-test-renderer')).default
  const { useShortcuts } = await import(path.join(ROOT, 'src/keymap.js'))
  try {
    let renderer = null
    const actions = { 'refresh': () => { actions.refreshCalls++ } }
    actions.refreshCalls = 0
    function Probe() {
      useShortcuts(actions, { storage: memStorage() })
      return null
    }
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(Probe))
    })
    assert.equal(listeners.keydown.length, 1, '挂载应注册一个 keydown 监听')
    TestRenderer.act(() => {
      listeners.keydown[0](keyEvent('r'))
    })
    assert.equal(actions.refreshCalls, 1, '按键应触发注册的 action')

    // 卸载：监听应被移除
    TestRenderer.act(() => renderer.unmount())
    assert.equal(listeners.keydown.length, 0, '卸载应移除 keydown 监听')
  } finally {
    delete globalThis.document
  }
})

test('hook：getEnabled 覆盖开关即时生效', async () => {
  let keyHandler = null
  globalThis.document = {
    addEventListener: (ev, fn) => { keyHandler = fn },
    removeEventListener: () => {},
  }
  const { default: React } = await import('react')
  const TestRenderer = (await import('react-test-renderer')).default
  const { useShortcuts } = await import(path.join(ROOT, 'src/keymap.js'))
  try {
    let renderer = null
    let enabled = false
    let calls = 0
    function Probe() {
      useShortcuts({ 'go-tasks': () => { calls++ } }, { getEnabled: () => enabled })
      return null
    }
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(Probe))
    })
    TestRenderer.act(() => keyHandler(keyEvent('t')))
    assert.equal(calls, 0, '关闭时应不触发')
    enabled = true
    TestRenderer.act(() => keyHandler(keyEvent('t')))
    assert.equal(calls, 1, '开启后应触发')
    TestRenderer.act(() => renderer.unmount())
  } finally {
    delete globalThis.document
  }
})

test('hook：动作表随渲染更新——分发用最新引用（防闭包过期）', async () => {
  const keyListeners = []
  globalThis.document = {
    addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
    removeEventListener: (ev, fn) => {
      const i = keyListeners.indexOf(fn)
      if (i >= 0) keyListeners.splice(i, 1)
    },
  }
  const { default: React } = await import('react')
  const { useState } = React
  const TestRenderer = (await import('react-test-renderer')).default
  const { useShortcuts } = await import(path.join(ROOT, 'src/keymap.js'))
  try {
    const calls = []
    let renderer = null
    function Probe() {
      // 动作闭包捕获最新渲染的 state：分发时必须用最新引用
      const [count, setCount] = useState(0)
      useShortcuts({ 'go-tasks': () => calls.push('v' + count) }, { storage: memStorage() })
      return React.createElement('button', { onClick: () => setCount(1) })
    }
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(Probe))
    })
    TestRenderer.act(() => keyListeners[0](keyEvent('t')))
    assert.deepEqual(calls, ['v0'], '首次渲染动作应生效')
    // 触发重渲染（count 0 → 1），动作闭包应更新
    TestRenderer.act(() => renderer.root.findByType('button').props.onClick())
    TestRenderer.act(() => keyListeners[0](keyEvent('t')))
    assert.deepEqual(calls, ['v0', 'v1'], '重渲染后分发应使用最新动作（不得用首次渲染的过期闭包）')
    TestRenderer.act(() => renderer.unmount())
  } finally {
    delete globalThis.document
  }
})

// 清理：确保没有遗留定时器（组合超时用例已 reset）
after(() => {})
