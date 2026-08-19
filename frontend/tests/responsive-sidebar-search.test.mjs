// 竖屏下侧边栏全局搜索框被挤成竖线回归测试（issue #346）：
//
// 需求：竖屏（≤860px）侧边栏收成抽屉，抽屉内强制展开态视觉（品牌文字/
// 导航文字/底部工具区均显示，折叠偏好仅作用于桌面端——issue #324）。
// 但 `.sidebar.collapsed .sidebar-search` 折叠态规则（36px 居中图标竖条）
// 在 ≤860px 仍生效：桌面端折叠过侧边栏（localStorage 持久化
// botler.navCollapsed）的用户在竖屏打开抽屉时，搜索框被挤成一条竖线、
// 两侧留出约 100px 空白（issue #346）。
//
// 修复：折叠态 36px 规则限制在 @media (min-width: 861px) 内（仅桌面端），
// 竖屏搜索入口自然继承 `.sidebar-search` 全宽基础规则。
//
// 断言（styles.css 源码级）：
// 1. 折叠态 36px 规则位于 @media (min-width: 861px) 块内（竖屏不应用，
//    否则被挤成竖线）；
// 2. `.sidebar-search` 基础规则保持全宽（width: calc(100% - 24px)），
//    竖屏抽屉内搜索入口自然继承全宽并文字靠左；
// 3. 桌面折叠态不受影响：min-width 块内仍为 36px 图标窄条
//    （width:36px / justify-content:center），折叠偏好仅作用于桌面端。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取 @media (min-width: 861px) 断点块（括号配平）
function minWidth861Block(css) {
  const re = /@media \(min-width:\s*861px\)\s*\{/g
  let start = -1
  let m
  while ((m = re.exec(css))) start = m.index
  assert.ok(start >= 0, 'styles.css 应存在 @media (min-width: 861px) 断点（折叠态仅桌面端）')
  let depth = 0
  let end = -1
  for (let i = start; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > start, '861px 断点块应有闭合大括号')
  return css.slice(start, end + 1)
}

test('竖屏（≤860px）不应用折叠态 36px 竖条规则：该规则限定在 min-width:861px 内', () => {
  const block = minWidth861Block(styles)
  const body = block.replace(/^@media \(min-width:\s*861px\)\s*\{/, '').replace(/\}$/, '')
  const m = body.match(/\.sidebar\.collapsed\s+\.sidebar-search\s*\{([^}]*)\}/)
  assert.ok(m, 'min-width:861px 块内应声明 .sidebar.collapsed .sidebar-search 折叠态规则')
  assert.match(m[1], /width:\s*36px/, '桌面折叠态搜索入口为 36px 图标窄条')
  assert.match(m[1], /justify-content:\s*center/, '桌面折叠态图标居中')
})

test('竖屏抽屉内搜索入口继承全宽基础规则：.sidebar-search 保持 calc(100% - 24px)', () => {
  const m = styles.match(/\.sidebar-search\s*\{([^}]*)\}/)
  assert.ok(m, '应存在 .sidebar-search 基础规则')
  assert.match(m[1], /width:\s*calc\(100%\s*-\s*24px\)/, '基础规则保持全宽（竖屏抽屉内搜索框不再被挤成竖线）')
  assert.match(m[1], /display:\s*flex/, '基础规则为 flex 布局')
  assert.match(m[1], /align-items:\s*center/, '基础规则垂直居中')
})

test('基础规则在折叠态规则之前声明（折叠态仅覆盖桌面端，竖屏不受影响）', () => {
  // .sidebar-search 基础规则（全宽）须在 min-width:861px 折叠态规则之前——
  // 折叠态限定桌面端后，竖屏无任何规则覆盖全宽基础样式
  const baseIdx = styles.indexOf('.sidebar-search {')
  const desktopIdx = styles.indexOf('@media (min-width: 861px)')
  assert.ok(baseIdx >= 0 && desktopIdx > baseIdx, '基础规则应在桌面折叠态媒体查询之前声明')
})
