// 抽屉/右边栏回到顶部按钮测试（issue #457）：所有需要竖向滚动的右边栏
// （右侧抽屉）右下角提供「回到顶部」浮动按钮。
//
// 背景：issue #455 已实现全局回到顶部按钮（监听 window 滚动，面向整页
// 滚动）；本 issue 面向**自身竖向滚动**的右边栏抽屉（.drawer 系列）——
// issue 详情 / 流水线详情 / 任务执行详情 / 灵感 AI 对话 / 任务详情快览，
// 这些抽屉 overflow-y: auto 内部滚动，全局按钮（监听 window）不生效，
// 需要按钮滚动**抽屉容器自身**。
//
// 测试层次：
// 1. 组件行为（vite ssrLoadModule + react-test-renderer + mock 滚动容器）：
//    - 初始 scrollTop 0 不渲染按钮（顶部不打扰）；
//    - 滚动超阈值且容器可竖向滚动 → 渲染按钮（aria-label=「回到顶部」，
//      带 back-to-top in-drawer 类）；
//    - 容器不可滚动（内容不超容器可视高）时即使 scrollTop 超阈值也不渲染；
//    - scroll 事件驱动显隐（0→600 出现，600→0 消失）；
//    - 点击按钮 → 容器 scrollTo({top:0,left:0,behavior:'smooth'})；
//      减弱动态效果时 behavior='auto'；
//    - 卸载移除容器 scroll / window resize 监听（无泄漏）；
//    - containerRef 为空（无容器）时安全渲染不崩溃（SSR 兜底）；
// 2. 接入完整性（源码级）：5 个右边栏抽屉组件均引入并渲染
//    ScrollContainerBackToTop——「所有需要竖向滚动的右边栏」覆盖完整；
// 3. 样式（styles.css 源码级）：抽屉内按钮 absolute 定位容器右下角、
//   .drawer 相对定位承托、移动端 issue 抽屉按钮上移避开底部操作栏。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/components/BackToTop.jsx')
const { ScrollContainerBackToTop, BACK_TO_TOP_THRESHOLD } = mod

after(() => vite.close())

// ---- mock 滚动容器（模拟 .drawer：scrollTop / scrollHeight / clientHeight
//      + scrollTo / scroll 监听；window 提供 resize 监听与 matchMedia）----
const scrollCalls = []
const removedListeners = []

function createContainer(env) {
  const listeners = { scroll: [] }
  return {
    get scrollTop() { return env.scrollTop },
    set scrollTop(v) { env.scrollTop = v },
    get scrollHeight() { return env.scrollHeight },
    get clientHeight() { return env.clientHeight },
    scrollTo: (opts) => { scrollCalls.push(opts) },
    addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn) },
    removeEventListener: (ev, fn) => {
      removedListeners.push([ev, fn])
      const arr = listeners[ev]
      if (arr) {
        const i = arr.indexOf(fn)
        if (i >= 0) arr.splice(i, 1)
      }
    },
    // 测试钩子：触发容器滚动事件
    _listeners: listeners,
  }
}

function installEnv(env = {}) {
  const state = {
    scrollTop: env.scrollTop ?? 0,
    scrollHeight: env.scrollHeight ?? 2400,
    clientHeight: env.clientHeight ?? 800,
    reduced: env.reduced ?? false,
  }
  const winListeners = { resize: [] }
  const container = createContainer(state)
  scrollCalls.length = 0
  removedListeners.length = 0
  const win = {
    matchMedia: () => ({ matches: state.reduced, addEventListener() {}, removeEventListener() {} }),
    addEventListener: (ev, fn) => { (winListeners[ev] ||= []).push(fn) },
    removeEventListener: (ev, fn) => {
      removedListeners.push([ev, fn])
      const arr = winListeners[ev]
      if (arr) {
        const i = arr.indexOf(fn)
        if (i >= 0) arr.splice(i, 1)
      }
    },
  }
  const hadWin = 'window' in globalThis
  const origWin = globalThis.window
  globalThis.window = win
  return {
    state,
    container,
    fireScroll: () => { for (const fn of [...container._listeners.scroll]) fn() },
    fireResize: () => { for (const fn of [...winListeners.resize]) fn() },
    restore: () => {
      if (hadWin) globalThis.window = origWin
      else delete globalThis.window
    },
  }
}

function renderComponent(container, props = {}) {
  const ref = { current: container }
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(
      React.createElement(ScrollContainerBackToTop, { containerRef: ref, ...props }),
    )
  })
  return renderer
}

// ---- 组件行为测试 ----

test('抽屉按钮：初始滚动 0（容器顶部）不渲染按钮', () => {
  const env = installEnv({ scrollTop: 0 })
  try {
    const renderer = renderComponent(env.container)
    assert.equal(renderer.toJSON(), null, '容器顶部不应渲染按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：滚动超阈值且容器可竖向滚动 → 渲染按钮（aria-label=回到顶部）', () => {
  const env = installEnv({ scrollTop: 600 })
  try {
    const renderer = renderComponent(env.container)
    const btn = renderer.root.findByType('button')
    assert.equal(btn.props['aria-label'], '回到顶部', 'aria-label 应本地化为「回到顶部」')
    assert.equal(btn.props.title, '回到顶部')
    assert.equal(btn.props.type, 'button')
    assert.match(btn.props.className, /back-to-top/, '应带 back-to-top 类')
    assert.match(btn.props.className, /in-drawer/, '应带 in-drawer 类（抽屉内定位）')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：容器不可滚动（内容不超可视高）时即使滚动超阈值也不渲染', () => {
  const env = installEnv({ scrollTop: 600, scrollHeight: 800, clientHeight: 800 })
  try {
    const renderer = renderComponent(env.container)
    assert.equal(renderer.toJSON(), null, '内容不足一屏不应渲染按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：scroll 事件驱动显隐（0→600 出现，600→0 消失）', () => {
  const env = installEnv({ scrollTop: 0 })
  try {
    const renderer = renderComponent(env.container)
    assert.equal(renderer.toJSON(), null, '初始隐藏')
    TestRenderer.act(() => {
      env.state.scrollTop = 600
      env.fireScroll()
    })
    assert.ok(renderer.root.findAllByType('button').length === 1, '滚动超阈值后应出现按钮')
    TestRenderer.act(() => {
      env.state.scrollTop = 100
      env.fireScroll()
    })
    assert.equal(renderer.toJSON(), null, '回到顶部附近应隐藏按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：resize 后重算（容器可视高变大、内容不再溢出 → 隐藏）', () => {
  const env = installEnv({ scrollTop: 600, scrollHeight: 2400, clientHeight: 800 })
  try {
    const renderer = renderComponent(env.container)
    assert.ok(renderer.root.findAllByType('button').length === 1, '初始可见')
    TestRenderer.act(() => {
      env.state.clientHeight = 3000
      env.fireResize()
    })
    assert.equal(renderer.toJSON(), null, '容器不可滚动后应隐藏按钮')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：点击 → 容器 scrollTo 平滑回顶（top:0, behavior:smooth）', () => {
  const env = installEnv({ scrollTop: 800 })
  try {
    const renderer = renderComponent(env.container)
    TestRenderer.act(() => renderer.root.findByType('button').props.onClick())
    assert.deepEqual(scrollCalls, [{ top: 0, left: 0, behavior: 'smooth' }],
      '点击应滚动抽屉容器自身到顶部')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：开启减弱动态效果时点击 → behavior=auto（尊重无障碍偏好）', () => {
  const env = installEnv({ scrollTop: 800, reduced: true })
  try {
    const renderer = renderComponent(env.container)
    TestRenderer.act(() => renderer.root.findByType('button').props.onClick())
    assert.deepEqual(scrollCalls, [{ top: 0, left: 0, behavior: 'auto' }],
      '减弱动态效果时不应平滑滚动')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：卸载时移除容器 scroll 与 window resize 监听（无泄漏）', () => {
  const env = installEnv({ scrollTop: 600 })
  try {
    const renderer = renderComponent(env.container)
    renderer.unmount()
    const events = removedListeners.map(([ev]) => ev)
    assert.ok(events.includes('scroll'), '应移除容器 scroll 监听')
    assert.ok(events.includes('resize'), '应移除 window resize 监听')
  } finally { env.restore() }
})

test('抽屉按钮：containerRef 为空（容器未挂载/SSR）时安全渲染不崩溃', () => {
  const env = installEnv()
  try {
    let renderer = null
    TestRenderer.act(() => {
      renderer = TestRenderer.create(
        React.createElement(ScrollContainerBackToTop, { containerRef: { current: null } }),
      )
    })
    assert.equal(renderer.toJSON(), null, '无容器不应渲染按钮且不抛错')
    renderer.unmount()
  } finally { env.restore() }
})

test('抽屉按钮：显示阈值与全局版一致（400px，约两屏）', () => {
  assert.equal(BACK_TO_TOP_THRESHOLD, 400)
})

// ---- 接入完整性（源码级）：所有需要竖向滚动的右边栏均接入 ----
// 右侧抽屉体系（.drawer overflow-y: auto）的 5 个入口：
// issue 详情 / 流水线详情 / 任务执行详情 / 灵感 AI 对话 / 任务详情快览。
const drawerSources = {
  'IssueDrawer.jsx（issue 详情右边栏）': 'src/components/IssueDrawer.jsx',
  'PipelineDrawer.jsx（流水线详情右边栏）': 'src/components/PipelineDrawer.jsx',
  'TaskDetailDrawer.jsx（任务执行详情右边栏）': 'src/components/TaskDetailDrawer.jsx',
  'InspirationSection.jsx（灵感 AI 对话右边栏）': 'src/components/overview/InspirationSection.jsx',
  'Tasks.jsx（任务详情快览右边栏）': 'src/pages/Tasks.jsx',
}
for (const [name, file] of Object.entries(drawerSources)) {
  test(`接入完整性：${name} 引入并渲染 ScrollContainerBackToTop`, () => {
    const src = readFileSync(new URL(`../${file}`, import.meta.url), 'utf8')
    assert.match(src, /import\s*\{[^}]*ScrollContainerBackToTop[^}]*\}\s*from\s*['"][^'"]*BackToTop[^'"]*['"]/,
      '应 import ScrollContainerBackToTop（自 BackToTop.jsx）')
    assert.match(src, /<ScrollContainerBackToTop\b/, '应渲染 <ScrollContainerBackToTop>')
    assert.match(src, /ref\s*=\s*\{[^}]+\}/, '抽屉滚动容器应挂 ref（按钮据此定位/监听）')
  })
}

// ---- 样式断言 ----
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')

function ruleBody(css, selector, feature) {
  const re = new RegExp(`${selector}\\s*\\{([^}]*)\\}`, 'g')
  let m
  while ((m = re.exec(css))) {
    if (!feature || m[1].includes(feature)) return m[1]
  }
  assert.ok(false, `styles.css 应存在 ${selector} 规则（特征：${feature || '任意'}）`)
}

test('CSS：.drawer 为相对定位（承托抽屉内 absolute 按钮）', () => {
  const body = ruleBody(styles, '\\.drawer', 'position')
  assert.match(body, /position:\s*relative/, '.drawer 应 position: relative')
})

test('CSS：.back-to-top.in-drawer 相对抽屉右下角定位（absolute）', () => {
  const body = ruleBody(styles, '\\.back-to-top\\.in-drawer', 'position')
  assert.match(body, /position:\s*absolute/, '抽屉内按钮应 absolute 定位（相对 .drawer）')
  assert.match(body, /right:\s*var\(--space-4/, '右下角定位应使用 --space-4 令牌')
  assert.match(body, /bottom:\s*var\(--space-4/, '右下角定位应使用 --space-4 令牌')
})

test('CSS：移动端（≤860px）issue 抽屉按钮上移，避开底部 sticky 操作栏', () => {
  const re = /@media\s*\(max-width:\s*860px\)\s*\{([\s\S]*)\}/s
  const m = re.exec(styles)
  assert.ok(m, '应存在 ≤860px 媒体查询')
  assert.match(m[1], /\.drawer\.issue-drawer\s+\.back-to-top\.in-drawer\s*\{[^}]*\}/,
               '媒体查询内应有 issue 抽屉按钮上移规则')
  assert.match(m[1], /bottom:\s*calc\(var\(--space-4[^)]*\)\s*\+\s*64px\)/,
               '按钮应上移避开底部操作栏')
})
