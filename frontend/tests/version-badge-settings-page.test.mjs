// 复现测试（issue #9 第二轮）：版本号与构建时间显示移到设置页面底部，
// 登录后用户名与退出按钮放到导航栏最右边。
//
// 背景：issue #9 第一轮把 VersionBadge 放在导航栏右侧（margin-left:auto 推到最右）。
// 用户 2026-08-13 09:40 评论提出两点调整：
// 1. 版本号 + 构建时间显示移到设置页面底部；
// 2. 登录后用户名称和退出登录按钮放在（导航栏）最右边。
// 期望行为（对实现方式中立）：
// - App.jsx 导航栏不再渲染 VersionBadge（版本信息只在设置页出现）；
// - Settings.jsx 页面底部渲染 VersionBadge；
// - 导航栏最后一块内容是登录用户区（issue #271 起为 UserMenu 组件），
//   且 .user-chip 用 margin-left:auto 占据最右侧（版本徽标移走后它应
//   承接"推到最右"的职责）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const userMenu = readFileSync(path.join(ROOT, 'src/components/UserMenu.jsx'), 'utf8')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

test('导航栏不应再渲染 VersionBadge（版本信息移到设置页）', () => {
  assert.doesNotMatch(
    app,
    /<VersionBadge\s*\/>/,
    'App.jsx 导航栏应移除 <VersionBadge />'
  )
})

test('设置页面应渲染 VersionBadge（页面底部）', () => {
  assert.match(
    settings,
    /<VersionBadge\s*\/>/,
    'Settings.jsx 应在页面底部渲染版本显示组件'
  )
})

test('导航栏用户区（UserMenu）应为最后一个元素（版本徽标之后不再有元素）', () => {
  // 登录用户区自 issue #271 起抽为 UserMenu 组件，App 导航栏以其收尾
  const userMenuTag = app.indexOf('<UserMenu')
  assert.ok(userMenuTag > -1, 'App.jsx 应渲染 <UserMenu />（登录用户区）')
  assert.ok(
    app.lastIndexOf('<UserMenu') < app.indexOf('</nav>'),
    'UserMenu 应在 nav 内'
  )
  // 导航栏最后一块内容应为 UserMenu：其右没有其他兄弟元素
  const navTail = app.slice(app.indexOf('<UserMenu'), app.indexOf('</nav>'))
  assert.doesNotMatch(navTail, /<VersionBadge/, 'UserMenu 之后不应再有 VersionBadge')
})

test('UserMenu 组件应渲染 user-chip（用户名+退出按钮）', () => {
  // className 自 issue #221 起为动态拼接（临期时附加 user-chip-expiring），
  // 断言放宽为样式类存在即可
  assert.match(
    userMenu,
    /user-chip/,
    'UserMenu.jsx 应渲染 .user-chip（登录用户区）'
  )
  assert.match(
    userMenu,
    /api\.post\('\/api\/auth\/logout'\)/,
    'UserMenu.jsx 退出按钮应调用 POST /api/auth/logout'
  )
})

test('.user-chip 应通过 margin-left:auto 占据导航栏最右侧', () => {
  const rule = styles.match(/\.user-chip\s*\{([^}]*)\}/)
  assert.ok(rule, 'styles.css 应有 .user-chip 样式规则')
  assert.match(
    rule[1],
    /margin-left:\s*auto/,
    '版本徽标移走后 .user-chip 需承接 margin-left:auto 推到最右'
  )
})
