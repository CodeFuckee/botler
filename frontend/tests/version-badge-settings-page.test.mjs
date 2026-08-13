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
// - 导航栏最后一个是用户信息（user-chip），且 .user-chip 用 margin-left:auto
//   占据最右侧（版本徽标移走后它应承接"推到最右"的职责）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
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

test('导航栏 user-chip 应为最后一个元素（版本徽标之后不再有元素）', () => {
  // 版本徽标移走后，用户信息（登录后显示）应位于导航栏最右
  const userChip = app.indexOf('user-chip')
  assert.ok(userChip > -1, 'App.jsx 应保留 user-chip（用户名+退出按钮）')
  assert.ok(
    app.lastIndexOf('user-chip') < app.indexOf('</nav>'),
    'user-chip 应在 nav 内'
  )
  // 导航栏最后一块内容应为 user-chip 区块：其右没有其他兄弟元素
  const navTail = app.slice(app.indexOf('user-chip'), app.indexOf('</nav>'))
  assert.doesNotMatch(navTail, /<VersionBadge/, 'user-chip 之后不应再有 VersionBadge')
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
