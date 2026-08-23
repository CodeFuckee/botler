// 回到顶部按钮测试（issue #455）：所有需要竖向滚动的页面右下角提供
// 「回到顶部」浮动按钮。
//
// 测试层次：
// 1. 纯函数（无 DOM 依赖，node:test 直接断言）：
//    - shouldShowBackToTop：阈值边界（恰等于阈值不显示）、负数/NaN/Infinity/
//      null/undefined/非数值一律 false、自定义阈值；
//    - canScrollVertically：内容高于视口才可滚动、相等/不足/非法输入 false；
//    - scrollBehaviorFor：减弱动态效果 → auto，否则 smooth；
//    - prefersReducedMotion：matchMedia matches 真/假、无 window/matchMedia
//      兜底 false；
//    - currentScrollY / currentScrollHeight / currentViewportHeight：无
//      window/document 环境兜底 0，scrollingElement 缺失回退 documentElement；
// 2. 组件渲染（vite ssrLoadModule + react-test-renderer + window/document
//    mock）：
//    - 初始滚动 0 不渲染按钮（页面顶部不打扰）；
//    - 滚动超阈值且页面可滚动 → 渲染按钮（aria-label=「回到顶部」）；
//    - scroll 事件驱动显隐（0→600 出现，600→0 消失）；
//    - 页面不可滚动（内容不超视口）时即使滚动超阈值也不渲染；
//    - 点击按钮 → window.scrollTo({top:0,left:0,behavior:'smooth'})；
//      减弱动态效果时 behavior='auto'；
//    - 卸载移除 scroll/resize 监听器；
//    - raised=true 时按钮带 raised 类（版本更新横幅显示时上移让位）；
//    - 路由切换重评估：从长页切到不足一屏的短页后按钮消失。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import { Link, MemoryRouter, useLocation } from 'react-router-dom'
import TestRenderer from 'react-test-renderer'
import { readFileSync } from 'node:fs'

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与其他测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/components/BackToTop.jsx')
const BackToTop = mod.default
const {
  BACK_TO_TOP_THRESHOLD,
  shouldShowBackToTop,
  canScrollVertically,
  scrollBehaviorFor,
  prefersReducedMotion,
  currentScrollY,
  currentScrollHeight,
  currentViewportHeight,
} = mod

after(() => vite.close())

// ---- 纯函数测试 ----

test('BACK_TO_TOP_THRESHOLD 显示阈值约定（400px，约两屏）', () => {
  assert.equal(BACK_TO_TOP_THRESHOLD, 400)
})

test('shouldShowBackToTop：滚动超过阈值才显示，恰等于阈值不显示', () => {
  assert.equal(shouldShowBackToTop(0), false, '页面顶部不显示')
  assert.equal(shouldShowBackToTop(399), false, '未达阈值不显示')
  assert.equal(shouldShowBackToTop(400), false, '恰等于阈值不显示（避免临界闪烁）')
  assert.equal(shouldShowBackToTop(401), true, '超过阈值显示')
  assert.equal(shouldShowBackToTop(10000), true)
})

test('shouldShowBackToTop：非法输入一律 false（负数/NaN/Infinity/非数值）', () => {
  assert.equal(shouldShowBackToTop(-100), false, '负滚动位置不显示')
  assert.equal(shouldShowBackToTop(NaN), false)
  assert.equal(shouldShowBackToTop(Infinity), false)
  assert.equal(shouldShowBackToTop(-Infinity), false)
  assert.equal(shouldShowBackToTop(null), false)
  assert.equal(shouldShowBackToTop(undefined), false)
  assert.equal(shouldShowBackToTop('500'), false, '字符串数值不显示（类型必须为 number）')
  assert.equal(shouldShowBackToTop({}), false)
})

test('shouldShowBackToTop：支持自定义阈值', () => {
  assert.equal(shouldShowBackToTop(100, 100), false, '恰等于自定义阈值不显示')
  assert.equal(shouldShowBackToTop(101, 100), true)
  assert.equal(shouldShowBackToTop(0, 0), false, '阈值 0 时顶部的 0 不显示（0 > 0 为 false）')
  assert.equal(shouldShowBackToTop(1, 0), true)
})

test('canScrollVertically：内容高度大于视口高度才可竖向滚动', () => {
  assert.equal(canScrollVertically(1000, 800), true, '内容超出视口可滚动')
  assert.equal(canScrollVertically(800, 800), false, '恰好一屏不可滚动')
  assert.equal(canScrollVertically(600, 800), false, '内容不足一屏不可滚动')
})

test('canScrollVertically：非法输入一律 false', () => {
  assert.equal(canScrollVertically(null, 800), false)
  assert.equal(canScrollVertically(1000, null), false)
  assert.equal(canScrollVertically(undefined, undefined), false)
  assert.equal(canScrollVertically(NaN, 800), false)
  assert.equal(canScrollVertically(1000, NaN), false)
  assert.equal(canScrollVertically(Infinity, 800), false)
  assert.equal(canScrollVertically('1000', '800'), false, '字符串数值不可滚动')
})

test('scrollBehaviorFor：减弱动态效果用 auto，否则平滑滚动', () => {
  assert.equal(scrollBehaviorFor(false), 'smooth')
  assert.equal(scrollBehaviorFor(true), 'auto')
})

// ---- window/document 环境 mock ----
// 组件渲染依赖 window（scroll/resize 监听、scrollTo、matchMedia、innerHeight）
// 与 document（scrollingElement.scrollHeight）；node --test 环境均不存在，
// 由本 helper 安装可变的假环境并记录监听器/滚动调用。
const scrollCalls = []
const removedListeners = []

function installEnv(env = {}) {
  const state = {
    scrollY: env.scrollY ?? 0,
    innerHeight: env.innerHeight ?? 800,
    scrollHeight: env.scrollHeight ?? 2000,
    reduced: env.reduced ?? false,
  }
  const listeners = { scroll: [], resize: [] }
  scrollCalls.length = 0
  removedListeners.length = 0
  const win = {
    get scrollY() { return state.scrollY },
    set scrollY(v) { state.scrollY = v },
    get innerHeight() { return state.innerHeight },
    scrollTo: (opts) => { scrollCalls.push(opts) },
    matchMedia: () => ({ matches: state.reduced, addEventListener() {}, removeEventListener() {} }),
    addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn) },
    removeEventListener: (ev, fn) => {
      removedListeners.push([ev, fn])
      const arr = listeners[ev]
      if (arr) {
        const i = arr.indexOf(fn)
        if (i >= 0) arr.splice(i, 1)
      }
    },
  }
  const doc = {
    scrollingElement: {
      get scrollHeight() { return state.scrollHeight },
      set scrollHeight(v) { state.scrollHeight = v },
    },
  }
  const hadWin = 'window' in globalThis
  const hadDoc = 'document' in globalThis
  const origWin = globalThis.window
  const origDoc = globalThis.document
  globalThis.window = win
  globalThis.document = doc
  return {
    state,
    fireScroll: () => { for (const fn of [...listeners.scroll]) fn() },
    fireResize: () => { for (const fn of [...listeners.resize]) fn() },
    restore: () => {
      if (hadWin) globalThis.window = origWin
      else delete globalThis.window
      if (hadDoc) globalThis.document = origDoc
      else delete globalThis.document
    },
  }
}

// ---- 纯函数（依赖 window/document 的部分）----

test('prefersReducedMotion：matchMedia matches 为真 → true，否则 false', () => {
  const env = installEnv({ reduced: true })
  try {
    assert.equal(prefersReducedMotion(), true)
  } finally { env.restore() }
  const env2 = installEnv({ reduced: false })
  try {
    assert.equal(prefersReducedMotion(), false)
  } finally { env2.restore() }
})

test('prefersReducedMotion：无 window / 无 matchMedia 环境兜底 false（SSR）', () => {
  const hadWin = 'window' in globalThis
  const origWin = globalThis.window
  delete globalThis.window
  try {
    assert.equal(prefersReducedMotion(), false)
  } finally {
    if (hadWin) globalThis.window = origWin
  }
  const env = installEnv()
  try {
    globalThis.window.matchMedia = undefined
    assert.equal(prefersReducedMotion(), false)
  } finally { env.restore() }
})

test('currentScrollY / currentViewportHeight：读取 window 值，无 window 兜底 0', () => {
  const env = installEnv({ scrollY: 600, innerHeight: 900 })
  try {
    assert.equal(currentScrollY(), 600)
    assert.equal(currentViewportHeight(), 900)
  } finally { env.restore() }
  const hadWin = 'window' in globalThis
  const origWin = globalThis.window
  delete globalThis.window
  try {
    assert.equal(currentScrollY(), 0)
    assert.equal(currentViewportHeight(), 0)
  } finally {
    if (hadWin) globalThis.window = origWin
  }
})

test('currentScrollHeight：读取 scrollingElement 高度；缺失回退 documentElement；无 document 兜底 0', () => {
  const env = installEnv({ scrollHeight: 2500 })
  try {
    assert.equal(currentScrollHeight(), 2500)
  } finally { env.restore() }
  // scrollingElement 缺失 → 回退 documentElement
  const env2 = installEnv({ scrollHeight: 1800 })
  try {
    globalThis.document.scrollingElement = undefined
    globalThis.document.documentElement = { scrollHeight: 1800 }
    assert.equal(currentScrollHeight(), 1800, '应回退 documentElement')
  } finally { env2.restore() }
  const hadDoc = 'document' in globalThis
  const origDoc = globalThis.document
  delete globalThis.document
  try {
    assert.equal(currentScrollHeight(), 0)
  } finally {
    if (hadDoc) globalThis.document = origDoc
  }
})

// ---- 组件渲染测试 ----

function renderComponent(props = {}, { router = false, entries = ['/a'] } = {}) {
  let renderer = null
  TestRenderer.act(() => {
    const el = router
      ? React.createElement(
          MemoryRouter,
          { initialEntries: entries },
          React.createElement(Harness, props),
        )
      : React.createElement(BackToTop, props)
    renderer = TestRenderer.create(el)
  })
  return renderer
}

// 路由环境 harness：展示当前路径 + 提供跳转 Link（供路由切换重评估测试）。
// 注意：测试文件由 node --test 直接执行（不支持 JSX），必须用 createElement。
function Harness({ raised = false }) {
  const location = useLocation()
  return React.createElement(
    'div',
    null,
    React.createElement('span', { className: 'path' }, location.pathname),
    React.createElement(Link, { to: '/b' }, 'go-b'),
    React.createElement(BackToTop, { raised }),
  )
}

test('组件：初始滚动 0（页面顶部）不渲染按钮', () => {
  const env = installEnv({ scrollY: 0 })
  try {
    const renderer = renderComponent()
    assert.equal(renderer.toJSON(), null, '页面顶部不应渲染按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：初始即滚动超阈值且页面可滚动 → 渲染按钮（aria-label=回到顶部）', () => {
  const env = installEnv({ scrollY: 600 })
  try {
    const renderer = renderComponent()
    const btn = renderer.root.findByType('button')
    assert.equal(btn.props['aria-label'], '回到顶部', 'aria-label 应本地化为「回到顶部」')
    assert.equal(btn.props.title, '回到顶部')
    assert.equal(btn.props.type, 'button')
    assert.match(btn.props.className, /back-to-top/, '应带 back-to-top 类')
    assert.ok(!btn.props.className.includes('raised'), '默认不带上移类')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：scroll 事件驱动显隐（0→600 出现，600→0 消失）', () => {
  const env = installEnv({ scrollY: 0 })
  try {
    const renderer = renderComponent()
    assert.equal(renderer.toJSON(), null, '初始隐藏')
    // 向下滚动超过阈值
    TestRenderer.act(() => {
      env.state.scrollY = 600
      env.fireScroll()
    })
    assert.ok(renderer.root.findAllByType('button').length === 1, '滚动超阈值后应出现按钮')
    // 回到顶部附近
    TestRenderer.act(() => {
      env.state.scrollY = 100
      env.fireScroll()
    })
    assert.equal(renderer.toJSON(), null, '回到顶部附近应隐藏按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：resize 事件后重算可见性（视口放大后内容不再溢出 → 隐藏）', () => {
  const env = installEnv({ scrollY: 600, innerHeight: 800, scrollHeight: 2000 })
  try {
    const renderer = renderComponent()
    assert.ok(renderer.root.findAllByType('button').length === 1, '初始可见')
    TestRenderer.act(() => {
      env.state.innerHeight = 2500 // 视口放大超过内容高度 → 页面不可滚动
      env.fireResize()
    })
    assert.equal(renderer.toJSON(), null, '页面不可滚动后应隐藏按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：页面不可滚动（内容不超视口）时即使滚动超阈值也不渲染', () => {
  const env = installEnv({ scrollY: 600, innerHeight: 800, scrollHeight: 800 })
  try {
    const renderer = renderComponent()
    assert.equal(renderer.toJSON(), null, '内容不足一屏不应渲染按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：点击按钮 → window.scrollTo 平滑回顶（top:0, behavior:smooth）', () => {
  const env = installEnv({ scrollY: 800 })
  try {
    const renderer = renderComponent()
    const btn = renderer.root.findByType('button')
    TestRenderer.act(() => btn.props.onClick())
    assert.deepEqual(scrollCalls, [{ top: 0, left: 0, behavior: 'smooth' }],
      '点击应调用 window.scrollTo 平滑回顶')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：开启减弱动态效果时点击 → behavior=auto（尊重无障碍偏好）', () => {
  const env = installEnv({ scrollY: 800, reduced: true })
  try {
    const renderer = renderComponent()
    TestRenderer.act(() => renderer.root.findByType('button').props.onClick())
    assert.deepEqual(scrollCalls, [{ top: 0, left: 0, behavior: 'auto' }],
      '减弱动态效果时不应平滑滚动')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：卸载时移除 scroll/resize 监听器（无泄漏）', () => {
  const env = installEnv({ scrollY: 600 })
  try {
    const renderer = renderComponent()
    renderer.unmount()
    const events = removedListeners.map(([ev]) => ev)
    assert.ok(events.includes('scroll'), '应移除 scroll 监听')
    assert.ok(events.includes('resize'), '应移除 resize 监听')
  } finally { env.restore() }
})

test('组件：raised=true 时按钮带 raised 类（版本横幅显示时上移让位）', () => {
  const env = installEnv({ scrollY: 600 })
  try {
    const renderer = renderComponent({ raised: true })
    const btn = renderer.root.findByType('button')
    assert.ok(btn.props.className.includes('raised'), 'raised 时应带 raised 类')
    renderer.unmount()
  } finally { env.restore() }
})

test('组件：路由切换后重新评估——长页切到不足一屏的短页后按钮消失', () => {
  const env = installEnv({ scrollY: 500, innerHeight: 800, scrollHeight: 2000 })
  try {
    const renderer = renderComponent({}, { router: true, entries: ['/a'] })
    assert.ok(renderer.root.findAllByType('button').length === 1, '长页初始可见')
    // 模拟新路由页面内容不足一屏（高度变为 800 == 视口）
    TestRenderer.act(() => {
      env.state.scrollHeight = 800
      env.state.scrollY = 500 // 旧滚动位置残留（浏览器钳制场景）
    })
    // 点击 Link 切换路由 → location 变化 → 重评估
    // （findByType('a') 取 Link 渲染出的宿主锚点，其 props 含 onClick）
    const link = renderer.root.findByType('a')
    TestRenderer.act(() => link.props.onClick({
      preventDefault() {}, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
    }))
    assert.equal(renderer.root.findAllByType('button').length, 0,
      '切到不可滚动短页后按钮应消失')
    renderer.unmount()
  } finally { env.restore() }
})

test('CSS：.back-to-top 为右下角固定浮动按钮，raised 上移避开版本横幅', () => {
  const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(styles, /\.back-to-top\s*\{/, '应定义 .back-to-top 样式')
  assert.match(styles, /position:\s*fixed/, '按钮应固定定位')
  assert.match(styles, /right:\s*var\(--gutter/, '右下角定位应使用 --gutter 令牌')
  assert.match(styles, /bottom:\s*var\(--gutter/, '底部定位应使用 --gutter 令牌')
  assert.match(styles, /z-index:\s*90/, '层级应在对话框/toast（100）之下')
  assert.match(styles, /\.back-to-top\.raised\s*\{[^}]*bottom:\s*calc\(var\(--gutter/, 'raised 应上移（calc 基于 --gutter）')
  assert.match(styles, /prefers-reduced-motion/, '全局应保留减弱动态效果规则')
})
