// 页面截图配置单元测试（issue #445）
// 验证 e2e/screenshots/screenshot-config.mjs 的页面路由清单、视口（屏幕
// 尺寸/比例）清单与路径/索引页辅助函数，保证「CI 截图范围」与「应用实际
// 路由、需求要求的多尺寸比例」一致——配置变更漏改时测试立即失败。
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PAGE_ROUTES,
  VIEWPORTS,
  SCREENSHOT_DIR,
  routeDirName,
  screenshotFileName,
  relativeScreenshotPath,
  buildScreenshotIndexHtml,
} from '../e2e/screenshots/screenshot-config.mjs'

// 应用全部路由（与 frontend/src/App.jsx 的 <Route> 一一对应；/tasks/1 用
// e2e 种子任务 id=1）。截图范围必须覆盖应用每个页面。
const APP_ROUTES = [
  '/overview',
  '/repos',
  '/tasks',
  '/tasks/1',
  '/stats',
  '/templates',
  '/labels',
  '/plugins',
  '/skills',
  '/tools',
  '/settings',
  '/terminal',
]

test('页面路由清单覆盖应用全部页面（App.jsx 路由）', () => {
  const paths = PAGE_ROUTES.map((r) => r.path)
  for (const route of APP_ROUTES) {
    assert.ok(paths.includes(route), `缺少页面路由: ${route}`)
  }
  // 不重复
  assert.equal(new Set(paths).size, paths.length)
  // 每个路由都有页面名与等待选择器
  for (const r of PAGE_ROUTES) {
    assert.ok(r.name && r.name.length > 0, `${r.path} 缺少页面名`)
    assert.ok(r.ready && r.ready.length > 0, `${r.path} 缺少 ready 选择器`)
  }
})

test('视口清单覆盖需求要求的屏幕尺寸与比例', () => {
  // 尺寸：宽屏桌面 / 常规桌面 / 小桌面 / 平板横竖屏 / 手机竖屏 / 小屏手机
  const sizes = VIEWPORTS.map((v) => `${v.width}x${v.height}`)
  for (const size of ['1920x1080', '1366x768', '1440x900', '1024x768', '768x1024', '375x667', '320x568']) {
    assert.ok(sizes.includes(size), `缺少视口尺寸: ${size}`)
  }
  // 比例：16:9、16:10、4:3、3:4、9:16
  const ratios = VIEWPORTS.map((v) => v.width / v.height)
  for (const r of [16 / 9, 16 / 10, 4 / 3, 3 / 4, 9 / 16]) {
    assert.ok(ratios.some((x) => Math.abs(x - r) < 0.02), `缺少宽高比: ${r}`)
  }
  // 视口名唯一、尺寸为正整数
  const names = VIEWPORTS.map((v) => v.name)
  assert.equal(new Set(names).size, names.length)
  for (const v of VIEWPORTS) {
    assert.ok(Number.isInteger(v.width) && v.width > 0)
    assert.ok(Number.isInteger(v.height) && v.height > 0)
  }
})

test('routeDirName 将路由转为安全目录名', () => {
  assert.equal(routeDirName('/overview'), 'overview')
  assert.equal(routeDirName('/tasks/1'), 'tasks-1')
  assert.equal(routeDirName('/'), 'index')
  // 危险/URL 特殊字符全部清洗为连字符，避免路径穿越与非法文件名
  assert.equal(routeDirName('/a/b?x=1&y=2#z'), 'a-b-x-1-y-2-z')
})

test('截图文件名与相对路径', () => {
  const vp = { name: 'mobile', width: 375, height: 667 }
  assert.equal(screenshotFileName(vp), 'mobile-375x667.png')
  assert.equal(
    relativeScreenshotPath({ path: '/tasks/1' }, vp),
    'tasks-1/mobile-375x667.png',
  )
  assert.equal(
    relativeScreenshotPath({ path: '/overview' }, { name: 'desktop-wide', width: 1920, height: 1080 }),
    'overview/desktop-wide-1920x1080.png',
  )
  assert.equal(SCREENSHOT_DIR, 'screenshots')
})

test('buildScreenshotIndexHtml 生成完整索引页（含全部截图与转义）', () => {
  const entries = [
    { route: '/overview', name: '概览页', viewport: 'desktop', file: 'overview/desktop-1920x1080.png' },
    { route: '/tasks/1', name: '任务详情', viewport: 'mobile', file: 'tasks-1/mobile-375x667.png' },
  ]
  const html = buildScreenshotIndexHtml(entries)
  assert.ok(html.includes('<html'))
  assert.ok(html.includes('<img'))
  assert.ok(html.includes('overview/desktop-1920x1080.png'))
  assert.ok(html.includes('tasks-1/mobile-375x667.png'))
  assert.ok(html.includes('1920x1080'))
  // 页面名含特殊字符（< &）时须转义，不得破坏 HTML 结构
  const dirty = buildScreenshotIndexHtml([
    { route: '/x', name: 'A <B> & C', viewport: 'desktop', file: 'x/desktop.png' },
  ])
  assert.ok(!dirty.includes('<B> & C'))
  assert.ok(dirty.includes('A &lt;B&gt; &amp; C'))
  // 空条目也返回合法 HTML
  const empty = buildScreenshotIndexHtml([])
  assert.ok(empty.includes('<html'))
  assert.ok(empty.includes('暂无截图'))
})
