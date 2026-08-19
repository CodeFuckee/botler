// 左侧边栏「折叠/展开」测试（issue #324）：
//
// 需求：顶部导航选项卡过多（11 个）改为左侧边栏，可整体折叠/展开：
//   - 展开态（默认）：240px 侧边栏，品牌 + 11 个导航项（图标+文字）
//     + 底部工具区（语言切换 / 快捷键帮助 / 登录用户区）+ 收起按钮；
//   - 折叠态：侧边栏收成 56px 图标窄栏（文字与底部工具区隐藏，
//     仅保留图标导航，悬停 title 提示），点击展开按钮恢复；
//   - 折叠偏好持久化到 localStorage（botler.navCollapsed），刷新保持；
//   - 无障碍：折叠/展开按钮带 aria-label / aria-expanded / aria-controls；
//   - 无 localStorage 环境（SSR/隐私模式）默认展开且不崩溃；
//   - 窄视口（≤860px）侧边栏转为抽屉（顶栏汉堡打开，遮罩/点击导航项关闭，
//     移出文档流不挤压内容区）。
//
// 测试层次：
// 1. 纯函数 loadSidebarCollapsed / saveSidebarCollapsed 边界用例；
// 2. 源码断言：sidebar 结构（品牌/11 导航项/折叠按钮/底部工具区/aria）；
// 3. 渲染：默认展开（11 个 navlink + 收起按钮 + 无 collapsed 类）；
// 4. 渲染：点击「收起侧边栏」→ collapsed 类出现、aria-expanded 翻转；
// 5. 渲染：折叠态点击「展开侧边栏」→ 恢复展开；
// 6. 持久化：折叠后写入 localStorage；预置折叠值时初始即折叠；
// 7. CSS：.sidebar 布局（sticky 全高 / 折叠宽度 / 窄视口回落）存在。
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
const appSrc = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/App.jsx')
const App = mod.default
const { loadSidebarCollapsed, saveSidebarCollapsed, SIDEBAR_STORAGE_KEY } = mod

after(() => {
  globalThis.fetch = originalFetch
  vite.close()
})

// fetch 快速失败 mock（issue #226）：4xx 不重试，auth 状态快速回退渲染主界面
const originalFetch = globalThis.fetch
globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: 'not found' }) })

// document mock：捕获 keydown 监听（App 全局快捷键，与 app-shortcuts 同法）
globalThis.document = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keyListeners.push(fn) },
  removeEventListener: (ev, fn) => {
    const i = keyListeners.indexOf(fn)
    if (i >= 0) keyListeners.splice(i, 1)
  },
  querySelectorAll: () => [],
}
const keyListeners = []

/** 内存版 localStorage（node 无 localStorage，App 侧 typeof 守卫读取） */
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  }
}

/** 以指定初始路径渲染 App，等待 auth 状态流转（与 app-shortcuts 同法） */
async function renderApp(initialPath = '/') {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [initialPath] }, React.createElement(App))
    )
    await new Promise((resolve) => setTimeout(resolve, 40))
  })
  return renderer
}

/** 从渲染树取侧边栏折叠/展开按钮 */
function findToggle(root) {
  const btns = root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('sidebar-toggle'))
  assert.equal(btns.length, 1, '应渲染唯一折叠/展开按钮')
  return btns[0]
}

/** 取渲染树中的 navlink 列表（NavLink 渲染为 <a class="navlink[ active]">） */
function findNavlinks(root) {
  return root.findAll(
    (n) => n.type === 'a' && String(n.props.className || '').includes('navlink'))
}

// ---- 1. 纯函数边界 ----

test('loadSidebarCollapsed：无存储 / 空值 / 乱值 / 异常存储均兜底展开', () => {
  assert.equal(loadSidebarCollapsed(null), false, '无存储环境应默认展开')
  assert.equal(loadSidebarCollapsed(undefined), false, 'undefined 存储应默认展开')
  assert.equal(loadSidebarCollapsed({ getItem: () => null }), false, '无记录应默认展开')
  assert.equal(loadSidebarCollapsed({ getItem: () => '0' }), false, "'0' 应视为展开")
  assert.equal(loadSidebarCollapsed({ getItem: () => '1' }), true, "'1' 应视为折叠")
  assert.equal(loadSidebarCollapsed({ getItem: () => 'true' }), true, "'true' 应视为折叠")
  assert.equal(loadSidebarCollapsed({ getItem: () => 'yes' }), false, '乱值应视为展开')
  assert.equal(
    loadSidebarCollapsed({ getItem: () => { throw new Error('denied') } }),
    false,
    'getItem 抛异常（隐私模式）应兜底展开不崩溃',
  )
})

test("saveSidebarCollapsed：写回 '1'/'0'；无存储/异常存储静默忽略", () => {
  const storage = memStorage()
  saveSidebarCollapsed(storage, true)
  assert.equal(storage.getItem(SIDEBAR_STORAGE_KEY), '1', "折叠应写 '1'")
  saveSidebarCollapsed(storage, false)
  assert.equal(storage.getItem(SIDEBAR_STORAGE_KEY), '0', "展开应写 '0'")
  assert.doesNotThrow(() => saveSidebarCollapsed(null, true), '无存储环境不应抛错')
  assert.doesNotThrow(
    () => saveSidebarCollapsed({ setItem: () => { throw new Error('denied') } }, true),
    'setItem 抛异常应静默忽略',
  )
})

// ---- 2. 源码断言 ----

test('源码：App.jsx 渲染左侧边栏（品牌 + 11 个导航项 + 折叠按钮 + 底部工具区）', () => {
  // 侧边栏容器：className 随折叠状态追加 collapsed
  assert.ok(appSrc.includes("className={'sidebar' + (sidebarCollapsed ? ' collapsed' : '') + (mobileNavOpen ? ' open' : '')}"),
    '侧边栏 className 应随折叠/抽屉状态切换')
  assert.match(appSrc, /aria-label=\{t\('nav\.ariaMain'\)\}/, '侧边栏应带主导航 aria-label')
  // 11 个导航项全部在侧边栏内（to 与现有路由一一对应）
  const navBlock = appSrc.match(/<nav[\s\S]*?<\/nav>/)[0]
  const routes = ['overview', 'repos', 'tasks', 'stats', 'templates', 'labels',
    'plugins', 'skills', 'tools', 'settings', 'terminal']
  for (const r of routes) {
    assert.match(navBlock, new RegExp(`to="/${r}"`), `侧边栏应有 /${r} 导航项`)
  }
  // 每个导航项图标 + 文字（折叠态由 CSS 隐藏 label）
  assert.match(navBlock, /<Icon name="compass" aria-hidden="true" \/>/, '概览应有图标')
  assert.match(navBlock, /<span className="nav-label">\{t\('nav\.terminal'\)\}<\/span>/, '导航项文字应包 nav-label')
  // 折叠按钮：aria-expanded 同步折叠状态、aria-controls 指向导航列表
  assert.match(appSrc, /className="sidebar-toggle"/, '应渲染折叠/展开按钮')
  assert.match(appSrc, /aria-expanded=\{!sidebarCollapsed\}/, '折叠按钮 aria-expanded 应同步状态')
  assert.match(appSrc, /aria-controls="sidebar-nav"/, '折叠按钮 aria-controls 应指向导航列表')
  assert.match(appSrc, /title=\{sidebarCollapsed \? t\('nav\.expandSidebar'\) : t\('nav\.collapseSidebar'\)\}/,
    '折叠按钮 title 应随状态切换')
  // 底部工具区：语言切换 / 快捷键帮助 / 登录用户区（UserMenu 仍在 nav 内）
  assert.match(appSrc, /<div className="sidebar-foot">/, '应有底部工具区容器')
  assert.match(appSrc, /className="lang-switch"/, '底部工具区应含语言切换')
  assert.match(appSrc, /className="btn btn-sm shortcuts-help-btn"/, '底部工具区应含快捷键帮助按钮')
  assert.ok(appSrc.lastIndexOf('<UserMenu') < appSrc.indexOf('</nav>'),
    'UserMenu 应在 nav 内（版本徽标 issue #9 约定）')
})

test('源码：移动端抽屉（顶栏汉堡 + open 类 + 遮罩）结构存在', () => {
  assert.match(appSrc, /className="topbar"/, '应渲染移动端顶栏容器')
  assert.match(appSrc, /className="topbar-menu"/, '顶栏应含汉堡按钮')
  assert.match(appSrc, /aria-controls="sidebar-nav"/, '汉堡按钮 aria-controls 应指向导航列表')
  assert.match(appSrc, /\+ \(mobileNavOpen \? ' open' : ''\)/, '侧边栏 className 应随抽屉状态追加 open')
  assert.match(appSrc, /className="sidebar-backdrop"/, '应渲染抽屉遮罩（打开时）')
  assert.match(appSrc, /setMobileNavOpen\(false\)/, '点击导航项 / 遮罩应关闭抽屉')
})

test('源码：折叠偏好存储键与持久化 effect 存在', () => {
  assert.equal(SIDEBAR_STORAGE_KEY, 'botler.navCollapsed', '折叠偏好存储键应唯一命名')
  assert.match(appSrc, /loadSidebarCollapsed\(themeStorage\)/, '初始状态应读取折叠偏好')
  assert.match(appSrc, /saveSidebarCollapsed\(themeStorage, sidebarCollapsed\)/, '状态变化应持久化偏好')
})

// ---- 3. 渲染：默认展开 ----

test('渲染：默认展开（11 个导航项 + 收起按钮 + 无 collapsed 类）', async () => {
  const renderer = await renderApp('/')
  try {
    const sidebar = renderer.root.findAll((n) => String(n.props.className || '').includes('sidebar'))
    const navEl = sidebar.find((n) => String(n.props.className).includes('sidebar') && !String(n.props.className).includes('navlink'))
    assert.ok(navEl, '应渲染侧边栏容器')
    assert.ok(!String(navEl.props.className).includes('collapsed'), '默认应展开（无 collapsed 类）')
    assert.equal(findNavlinks(renderer.root).length, 11, '应渲染全部 11 个导航项')
    const toggle = findToggle(renderer.root)
    assert.equal(toggle.props['aria-expanded'], true, '展开态 aria-expanded 应为 true')
    assert.equal(toggle.props['aria-label'], '收起侧边栏', '展开态按钮文案应为「收起侧边栏」')
    // 底部工具区三件套渲染（语言切换 / 快捷键帮助 / 登录用户区）
    assert.equal(renderer.root.findAll((n) => n.type === 'select' && String(n.props.className || '').includes('lang-switch')).length, 1, '应渲染语言切换')
    assert.equal(renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('shortcuts-help-btn')).length, 1, '应渲染快捷键帮助按钮')
    assert.equal(renderer.root.findAll((n) => String(n.props.className || '').includes('user-chip')).length, 1, '应渲染登录用户区')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 4. 渲染：点击收起 / 展开 ----

test('交互：点击「收起侧边栏」→ collapsed 类出现、aria-expanded 翻转、偏好写入存储', async () => {
  globalThis.localStorage = memStorage()
  try {
    const renderer = await renderApp('/')
    try {
      const sidebar = () => renderer.root.findAll((n) => n.type === 'nav' && String(n.props.className || '').includes('sidebar'))[0]
      assert.ok(sidebar(), '应渲染侧边栏')
      TestRenderer.act(() => findToggle(renderer.root).props.onClick())
      assert.ok(String(sidebar().props.className).includes('collapsed'), '点击收起后应有 collapsed 类')
      assert.equal(findToggle(renderer.root).props['aria-expanded'], false, '折叠态 aria-expanded 应为 false')
      assert.equal(findToggle(renderer.root).props['aria-label'], '展开侧边栏', '折叠态按钮文案应为「展开侧边栏」')
      assert.equal(globalThis.localStorage.getItem(SIDEBAR_STORAGE_KEY), '1', '折叠偏好应写入 localStorage')
      // 再点一次恢复展开
      TestRenderer.act(() => findToggle(renderer.root).props.onClick())
      assert.ok(!String(sidebar().props.className).includes('collapsed'), '再次点击应恢复展开')
      assert.equal(findToggle(renderer.root).props['aria-expanded'], true, '恢复后 aria-expanded 应为 true')
      assert.equal(globalThis.localStorage.getItem(SIDEBAR_STORAGE_KEY), '0', '展开偏好应写回 0')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    delete globalThis.localStorage
  }
})

// ---- 5. 渲染：预置折叠偏好 ----

test('持久化：预置折叠值（botler.navCollapsed=1）时初始即折叠', async () => {
  globalThis.localStorage = memStorage({ [SIDEBAR_STORAGE_KEY]: '1' })
  try {
    const renderer = await renderApp('/')
    try {
      const sidebar = renderer.root.findAll((n) => n.type === 'nav' && String(n.props.className || '').includes('sidebar'))[0]
      assert.ok(String(sidebar.props.className).includes('collapsed'), '预置折叠值应初始即折叠')
      assert.equal(findToggle(renderer.root).props['aria-expanded'], false, '折叠态 aria-expanded 应为 false')
      assert.equal(findToggle(renderer.root).props['aria-label'], '展开侧边栏', '折叠态按钮文案应为「展开侧边栏」')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    delete globalThis.localStorage
  }
})

// ---- 5b. 渲染：移动端抽屉开/关 ----

test('交互：汉堡按钮打开抽屉（open 类 + 遮罩渲染），遮罩/导航点击关闭', async () => {
  const renderer = await renderApp('/')
  try {
    const sidebar = () => renderer.root.findAll((n) => n.type === 'nav' && String(n.props.className || '').includes('sidebar'))[0]
    const backdrop = () => renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('sidebar-backdrop'))
    assert.ok(sidebar(), '应渲染侧边栏')
    assert.ok(!String(sidebar().props.className).includes('open'), '默认抽屉应关闭（无 open 类）')
    assert.equal(backdrop().length, 0, '默认不应渲染遮罩')
    // 点击汉堡按钮 → 抽屉打开
    const menuBtn = renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('topbar-menu'))
    assert.equal(menuBtn.length, 1, '应渲染顶栏汉堡按钮')
    TestRenderer.act(() => menuBtn[0].props.onClick())
    assert.ok(String(sidebar().props.className).includes('open'), '点击汉堡后应有 open 类')
    assert.equal(menuBtn[0].props['aria-expanded'], true, '打开后汉堡 aria-expanded 应为 true')
    assert.equal(backdrop().length, 1, '打开后应渲染遮罩')
    // 点击遮罩 → 关闭
    TestRenderer.act(() => backdrop()[0].props.onClick())
    assert.ok(!String(sidebar().props.className).includes('open'), '点击遮罩应关闭抽屉')
    assert.equal(backdrop().length, 0, '关闭后遮罩应卸载')
    // 再次打开，点击导航列表（事件冒泡）→ 关闭
    TestRenderer.act(() => menuBtn[0].props.onClick())
    assert.ok(String(sidebar().props.className).includes('open'), '再次点击汉堡应打开')
    const navList = renderer.root.findAll((n) => String(n.props.className || '').includes('sidebar-nav'))[0]
    TestRenderer.act(() => navList.props.onClick())
    assert.ok(!String(sidebar().props.className).includes('open'), '点击导航项应关闭抽屉')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 6. CSS：布局 / 折叠宽度 / 窄视口回落 ----

/** 提取指定选择器首个规则体 */
function ruleBody(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = styles.match(new RegExp(esc + '\\s*\\{([^}]*)\\}', 's'))
  return m ? m[1] : null
}

test('styles.css：.sidebar 为 sticky 全高左侧栏（导航层浮于内容之上）', () => {
  const body = ruleBody('.sidebar')
  assert.ok(body, '应存在 .sidebar 规则')
  assert.match(body, /position:\s*sticky/, '.sidebar 应为 sticky（导航层浮于内容之上）')
  assert.match(body, /top:\s*0/, '.sidebar 应吸附视口顶部')
  assert.match(body, /height:\s*100vh/, '.sidebar 应占满视口全高')
  assert.match(body, /width:\s*var\(--sidebar-width,\s*240px\)/, '展开态侧边栏宽度应为 240px')
  assert.match(body, /border-right:\s*1px\s+solid\s+var\(--hairline\)/, '.sidebar 应有右分隔线')
})

test('styles.css：折叠态收成 56px 图标窄栏，文字与底部工具区隐藏', () => {
  const collapsed = ruleBody('.sidebar.collapsed')
  assert.ok(collapsed, '应存在 .sidebar.collapsed 规则')
  assert.match(collapsed, /width:\s*56px/, '折叠态宽度应为 56px')
  assert.match(styles, /\.sidebar\.collapsed \.nav-label\s*\{\s*display:\s*none;/, '折叠态应隐藏导航文字')
  assert.match(styles, /\.sidebar\.collapsed \.sidebar-foot\s*\{\s*display:\s*none;/, '折叠态应隐藏底部工具区')
  assert.match(styles, /\.sidebar-nav \.navlink\s*\{[\s\S]*?display:\s*flex;/, '导航项应为 flex 排布（图标+文字）')
})

test('styles.css：窄视口（≤860px）侧边栏转抽屉（脱离文档流，内容区不挤压）', () => {
  // 取文件首个 860 断点（本特性断点位于文件前部、早于移动端 #270 断点，
  // 保持 responsive-mobile-layout「取最后一个 860 断点」约定不变）
  const m = styles.match(/@media \(max-width:\s*860px\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(m, '应存在窄视口媒体查询')
  // 侧边栏移出文档流：fixed + translateX(-100%) 收起，.open 滑入
  assert.match(m[1], /\.sidebar,\s*\.sidebar\.collapsed\s*\{[\s\S]*?position:\s*fixed/, '窄视口侧边栏应脱离文档流（fixed）')
  assert.match(m[1], /\.sidebar,\s*\.sidebar\.collapsed\s*\{[\s\S]*?transform:\s*translateX\(-100%\)/, '窄视口侧边栏应默认滑出屏幕')
  assert.match(m[1], /\.sidebar\.open\s*\{\s*transform:\s*none;/, '抽屉打开应滑入（.open → transform none）')
  assert.match(m[1], /\.topbar\s*\{[\s\S]*?display:\s*flex/, '窄视口应显示顶栏（汉堡入口）')
  assert.match(m[1], /\.sidebar-toggle\s*\{\s*display:\s*none;/, '窄视口应隐藏折叠按钮（抽屉恒展开态）')
  // 内容区恢复全宽（.app 改 block，侧边栏不再占位）
  assert.match(m[1], /\.app\s*\{\s*display:\s*block;/, '窄视口 .app 应改 block（内容区全宽）')
  assert.match(styles, /\.content\s*\{[\s\S]*?flex:\s*1;/, '.content 应占满剩余空间')
  assert.match(styles, /\.content\s*\{[\s\S]*?min-width:\s*0;/, '.content 应允许收缩不溢出')
})
