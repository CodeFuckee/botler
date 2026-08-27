// 设置页左侧边栏「整体折叠/展开」测试（issue #168）：
//
// 需求：设置页左侧边栏（SettingsNav）目前固定 240px 宽，只能折叠内部
// 分组，不能整体收起。本 issue 优化为**可以整体折叠查看**的左侧边栏：
//   - 展开态（默认）：保持现状——搜索框 + 分组导航，内容区 240px 列；
//   - 折叠态：侧边栏收成一条 44px 窄栏（仅保留「展开侧边栏」入口），
//     设置内容区占满全宽，最大化阅读/编辑空间；
//   - 折叠偏好持久化到 localStorage（botler.settings.sidebarCollapsed），
//     刷新/重进设置页保持用户偏好；
//   - 无障碍：折叠/展开按钮带 aria-label / aria-expanded / aria-controls；
//   - 无 localStorage 环境（SSR/隐私模式）默认展开且不崩溃。
//
// 测试层次：
// 1. 纯函数 loadSidebarCollapsed / saveSidebarCollapsed 边界用例（无存储/
//    异常存储/乱值/写回）；
// 2. 源码断言：折叠/展开按钮、aria 属性、持久化键与 collapsed 类；
// 3. 渲染：默认展开（搜索框 + 17 子项 + 收起按钮）；
// 4. 渲染：点击「收起侧边栏」→ 导航面板隐藏、窄栏展开按钮可见、
//    aria-expanded 翻转；
// 5. 渲染：折叠态点击「展开侧边栏」→ 恢复搜索框与全部分组子项；
// 6. 持久化：折叠后写入 localStorage；预置折叠值时初始即折叠；
// 7. CSS：折叠窄栏 / 展开按钮样式与两栏布局回落存在。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/components/SettingsNav.jsx')
const SettingsNav = mod.default
const { loadSidebarCollapsed, saveSidebarCollapsed, SIDEBAR_STORAGE_KEY } = mod

after(() => vite.close())

const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

/** 与设置页一致的假 DOM 结构（直接子节点按渲染顺序排列） */
function fakeSettingsContent() {
  const group = (title) => ({
    tagName: 'H2', className: 'settings-group-title', textContent: title,
  })
  const section = (id, h2) => ({
    tagName: 'SECTION', className: 'settings-section', id,
    querySelector: () => (h2 ? { textContent: h2 } : null),
    getAttribute: () => null,
  })
  return {
    querySelectorAll: () => [
      group('外部服务接入'),
      section('settings-sso', 'Synology SSO 登录'),
      section('settings-ai-providers', 'AI API 供应商'),
      section('settings-image-models', '生图模型'),
      section('settings-vision-models', '识图模型'),
      section('settings-minio', 'MinIO 对象存储'),
      group('系统设置'),
      section('settings-tasks', '任务调度'),
      section('settings-ui', '界面显示'),
      section('settings-notifications', '网页通知'),
      section('settings-alerts', '聚合告警'),
      section('settings-webhook', '消息推送 Webhook'),
      group('执行引擎'),
      section('settings-claude', 'Claude Code'),
      section('settings-dsh', 'dsh 引擎'),
      group('运维与数据'),
      section('settings-environment', '本地环境检测'),
      section('settings-backup', '数据备份'),
      group('账号与安全'),
      section('settings-owner-token', 'Owner GitLab Token（issue 编辑专用）'),
      section('settings-gitlab-cred', 'GitLab 凭据（只读）'),
      group('关于'),
      section('settings-version', '版本信息'),
    ],
  }
}

/** 简单内存 storage 替身（localStorage 子集） */
function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  }
}

/** 渲染 SettingsNav：mock document.querySelector 提供 .settings-content；
 *  storage 传入时挂到 global.localStorage（否则删除，模拟无存储环境） */
function renderNav({ storage } = {}) {
  const realDocument = global.document
  const realStorage = global.localStorage
  global.document = {
    querySelector: (sel) => (sel === '.settings-content' ? fakeSettingsContent() : null),
  }
  if (storage !== undefined) global.localStorage = storage
  else delete global.localStorage
  let renderer = null
  try {
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(SettingsNav))
    })
    return {
      renderer,
      root: renderer.root,
      restore: () => {
        global.document = realDocument
        if (realStorage === undefined) delete global.localStorage
        else global.localStorage = realStorage
      },
    }
  } catch (e) {
    global.document = realDocument
    if (realStorage === undefined) delete global.localStorage
    else global.localStorage = realStorage
    throw e
  }
}

const linkCount = (root) =>
  root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#')).length

const findAriaButton = (root, label) =>
  root.findAll((n) => n.type === 'button' && n.props['aria-label'] === label)

const asideClass = (root) =>
  String(root.find((n) => n.type === 'aside').props.className || '')

// ---------- 纯函数边界 ----------

test('loadSidebarCollapsed：无存储/异常存储一律返回展开（不崩溃）', () => {
  assert.equal(loadSidebarCollapsed(undefined), false, '无 storage 应默认展开')
  assert.equal(loadSidebarCollapsed(null), false, 'null storage 应默认展开')
  const throwing = { getItem: () => { throw new Error('denied') } }
  assert.equal(loadSidebarCollapsed(throwing), false, 'getItem 抛异常（隐私模式）应默认展开且不抛错')
})

test('saveSidebarCollapsed：无存储/异常存储静默忽略（不崩溃）', () => {
  assert.doesNotThrow(() => saveSidebarCollapsed(undefined, true))
  assert.doesNotThrow(() => saveSidebarCollapsed(null, false))
  const throwing = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveSidebarCollapsed(throwing, true))
})

test('loadSidebarCollapsed：\u20181\u2019/\u2018true\u2019 视为折叠，其余一律展开', () => {
  assert.equal(loadSidebarCollapsed(makeStorage({ [SIDEBAR_STORAGE_KEY]: '1' })), true, "'1' 应为折叠")
  assert.equal(loadSidebarCollapsed(makeStorage({ [SIDEBAR_STORAGE_KEY]: 'true' })), true, "'true' 应为折叠")
  assert.equal(loadSidebarCollapsed(makeStorage({ [SIDEBAR_STORAGE_KEY]: '0' })), false, "'0' 应为展开")
  assert.equal(loadSidebarCollapsed(makeStorage({ [SIDEBAR_STORAGE_KEY]: 'false' })), false, "'false' 应为展开")
  assert.equal(loadSidebarCollapsed(makeStorage({ [SIDEBAR_STORAGE_KEY]: 'garbage' })), false, '乱值应为展开')
  assert.equal(loadSidebarCollapsed(makeStorage({})), false, '未存过应为展开')
})

test('saveSidebarCollapsed：true 写回 \u20181\u2019、false 写回 \u20180\u2019', () => {
  const s1 = makeStorage()
  saveSidebarCollapsed(s1, true)
  assert.equal(s1.getItem(SIDEBAR_STORAGE_KEY), '1')
  const s2 = makeStorage()
  saveSidebarCollapsed(s2, false)
  assert.equal(s2.getItem(SIDEBAR_STORAGE_KEY), '0')
})

// ---------- 源码断言 ----------

test('源码：SettingsNav 提供整体折叠/展开按钮与无障碍属性', () => {
  assert.match(navSrc, /SIDEBAR_STORAGE_KEY/, '应导出侧边栏折叠偏好存储键')
  assert.match(navSrc, /loadSidebarCollapsed/, '应导出 loadSidebarCollapsed 纯函数')
  assert.match(navSrc, /saveSidebarCollapsed/, '应导出 saveSidebarCollapsed 纯函数')
  assert.match(navSrc, /aria-label="收起侧边栏"/, '展开态应有「收起侧边栏」按钮')
  assert.match(navSrc, /aria-label="展开侧边栏"/, '折叠态应有「展开侧边栏」按钮')
  assert.match(navSrc, /aria-expanded=\{/, '折叠按钮应暴露展开状态')
  assert.match(navSrc, /aria-controls="settings-nav-panel"/, '折叠按钮应关联导航面板')
  assert.match(navSrc, /settings-sidebar'\s*\+/, 'aside 应动态拼接 collapsed 类')
  assert.match(navSrc, /settings-nav-rail/, '折叠态应渲染窄栏 settings-nav-rail')
})

// ---------- 渲染断言：默认展开 ----------

test('渲染：默认展开——搜索框、17 个子项与「收起侧边栏」按钮可见', () => {
  const { renderer, root, restore } = renderNav()
  try {
    assert.equal(linkCount(root), 17, '默认应渲染 17 个子项链接')
    assert.ok(root.findAll((n) => n.type === 'input' && n.props.type === 'search').length === 1,
      '默认应显示搜索框')
    const collapse = findAriaButton(root, '收起侧边栏')
    assert.equal(collapse.length, 1, '默认应显示「收起侧边栏」按钮')
    assert.equal(collapse[0].props['aria-expanded'], true, '展开态 aria-expanded 应为 true')
    assert.equal(collapse[0].props['aria-controls'], 'settings-nav-panel', '应指向导航面板 id')
    assert.ok(!asideClass(root).includes('collapsed'), '默认 aside 不应带 collapsed 类')
    assert.equal(findAriaButton(root, '展开侧边栏').length, 0, '默认不应显示「展开侧边栏」按钮')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

// ---------- 渲染断言：折叠交互 ----------

test('渲染：点击「收起侧边栏」→ 导航隐藏、窄栏展开按钮可见、aria-expanded 翻转', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const collapse = findAriaButton(root, '收起侧边栏')[0]
    TestRenderer.act(() => { collapse.props.onClick() })
    assert.ok(asideClass(root).includes('collapsed'), '折叠后 aside 应带 collapsed 类')
    assert.equal(linkCount(root), 0, '折叠后不应渲染子项链接')
    assert.equal(root.findAll((n) => n.type === 'input' && n.props.type === 'search').length, 0,
      '折叠后应隐藏搜索框')
    assert.equal(findAriaButton(root, '收起侧边栏').length, 0, '折叠后不应显示收起按钮')
    const expand = findAriaButton(root, '展开侧边栏')
    assert.equal(expand.length, 1, '折叠后应显示「展开侧边栏」按钮')
    assert.equal(expand[0].props['aria-expanded'], false, '折叠态 aria-expanded 应为 false')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：折叠态点击「展开侧边栏」→ 恢复搜索框与 17 个子项', () => {
  const { renderer, root, restore } = renderNav()
  try {
    TestRenderer.act(() => { findAriaButton(root, '收起侧边栏')[0].props.onClick() })
    const expand = findAriaButton(root, '展开侧边栏')[0]
    TestRenderer.act(() => { expand.props.onClick() })
    assert.ok(!asideClass(root).includes('collapsed'), '展开后 aside 不应带 collapsed 类')
    assert.equal(linkCount(root), 17, '展开后应恢复 17 个子项链接')
    assert.equal(root.findAll((n) => n.type === 'input' && n.props.type === 'search').length, 1,
      '展开后应恢复搜索框')
    assert.equal(findAriaButton(root, '收起侧边栏').length, 1, '展开后应恢复收起按钮')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

// ---------- 持久化 ----------

test('持久化：折叠后写入 localStorage，展开后写回 \u20180\u2019', () => {
  const storage = makeStorage()
  const { renderer, root, restore } = renderNav({ storage })
  try {
    TestRenderer.act(() => { findAriaButton(root, '收起侧边栏')[0].props.onClick() })
    assert.equal(storage.getItem(SIDEBAR_STORAGE_KEY), '1', '折叠后应写入 \u20181\u2019')
    TestRenderer.act(() => { findAriaButton(root, '展开侧边栏')[0].props.onClick() })
    assert.equal(storage.getItem(SIDEBAR_STORAGE_KEY), '0', '展开后应写回 \u20180\u2019')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('持久化：预置折叠值时初始即折叠，预置展开值初始展开', () => {
  const collapsed = renderNav({ storage: makeStorage({ [SIDEBAR_STORAGE_KEY]: '1' }) })
  try {
    assert.ok(asideClass(collapsed.root).includes('collapsed'), '预置 \u20181\u2019 应初始折叠')
    assert.equal(linkCount(collapsed.root), 0, '初始折叠不应渲染子项')
  } finally {
    TestRenderer.act(() => collapsed.renderer.unmount())
    collapsed.restore()
  }
  const expanded = renderNav({ storage: makeStorage({ [SIDEBAR_STORAGE_KEY]: '0' }) })
  try {
    assert.ok(!asideClass(expanded.root).includes('collapsed'), '预置 \u20180\u2019 应初始展开')
    assert.equal(linkCount(expanded.root), 17, '初始展开应渲染 17 个子项')
  } finally {
    TestRenderer.act(() => expanded.renderer.unmount())
    expanded.restore()
  }
})

// ---------- CSS 断言 ----------

test('styles.css 提供整体折叠窄栏与展开按钮样式', () => {
  assert.match(styles, /\.settings-layout\s*\{\s*display: grid/, '设置页仍为 grid 两栏布局')
  assert.match(styles, /\.settings-sidebar\s*\{[^}]*width: 240px/, '展开态侧边栏宽度 240px')
  assert.match(styles, /\.settings-sidebar\.collapsed\s*\{[^}]*width: 0/, '折叠态侧边栏收为零宽（不留独立竖条，展开入口改为幽灵图标）')
  assert.match(styles, /\.settings-layout:has\(> \.settings-sidebar\.collapsed\)/, '折叠态栅格收为零宽首列，内容区取回全部宽度')
  assert.match(styles, /\.settings-sidebar\.collapsed\s*\.settings-nav\s*\{\s*display: none/, '折叠时应隐藏导航面板')
  assert.match(styles, /\.settings-nav-rail\s*\{/, '应有窄栏样式')
  assert.match(styles, /\.settings-nav-collapse\s*\{/, '应有收起按钮样式')
})
