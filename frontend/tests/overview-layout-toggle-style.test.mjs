// 美化测试（issue #479）：概览页「开放 Issue」布局切换按钮
// （.issue-filter-layouts 托盘 + .issue-layout-option 选项）升级为项目
// apple-design 设计语言的分段控件（segmented control）——从无样式裸
// 按钮（issue #471 引入时未配 CSS，浏览器默认渲染，与过滤条内排序/状态
// 胶囊视觉割裂）变为有明确控件外观（托盘内嵌选项）与完整交互反馈
// （hover 背景加深、选中主色弱底高亮、active 微缩放、focus-visible
// 焦点环），并为「仓库卡片 / 单列分组」两个选项配置语义图标（网格 /
// 列表），图标与文字垂直居中。
//
// 断言：
// 1. 托盘规则：独立 .issue-filter-layouts 规则存在，inline-flex +
//    gap + padding + 背景 + 边框 + 圆角（分段控件托盘容器）；
// 2. 基础规则：独立 .issue-layout-option 规则存在，inline-flex +
//    align-items: center + gap（图标与文字垂直居中，Lucide 图标 1em
//    与行内文本基线对齐，issue #177）；
// 3. pill 外观：padding / border-radius / background / border 齐全；
// 4. 动效：transition 走 --dur-fast（150ms）+ --ease-out（apple-design
//    动效时长区间 150–300ms），hover/active/focus 反馈平滑出现；
// 5. hover：背景 --bg-hover、文字 --text；
// 6. active：按下微缩放 translateY(1px) scale(0.98)（与 .btn 一致，
//    apple-design press 反馈）；
// 7. focus-visible：--focus-ring 焦点环 + outline: none；
// 8. 选中态：[.active]（aria-pressed="true"）时主色弱背景 --primary-weak
//    + 主色文字 --primary，当前布局一眼可辨；
// 9. 图标：Icon.jsx 提供 layoutGrid / layoutList 语义图标，布局按钮
//    渲染对应图标（卡片→网格、单列→列表）；
// 10. 主题安全：所有颜色/背景均经 CSS 变量（var(--*)）引用，不硬编码
//     十六进制色值，深浅色主题（issue #217）自动适配。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const sectionSrc = readFileSync(
  path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')
const iconSrc = readFileSync(path.join(ROOT, 'src/components/Icon.jsx'), 'utf8')

// 提取独立 .issue-filter-layouts 规则体（不与其它选择器逗号共享）
function trayRule() {
  const m = styles.match(/^\.issue-filter-layouts\s*\{([^}]*)\}/m)
  return m ? m[1] : null
}
// 提取独立 .issue-layout-option 规则体
function optionRule() {
  const m = styles.match(/^\.issue-layout-option\s*\{([^}]*)\}/m)
  return m ? m[1] : null
}
// 提取选中态规则体（.active）
function activeRule() {
  const m = styles.match(/\.issue-layout-option\.active\s*\{([^}]*)\}/m)
  return m ? m[1] : null
}

test('托盘规则：.issue-filter-layouts 分段控件容器（issue #479）', () => {
  const rule = trayRule()
  assert.ok(rule, 'styles.css 应有独立 .issue-filter-layouts 规则（布局切换分段控件托盘）')
  assert.match(rule, /display\s*:\s*inline-flex/, '托盘应为 inline-flex 容纳选项')
  assert.match(rule, /gap\s*:\s*var\(--space-1\)/, '选项间应留 4px 间隙')
  assert.match(rule, /padding\s*:\s*var\(--space-1\)/, '托盘内边距 4px（HIG 4px 网格）')
  assert.match(rule, /background\s*:\s*var\(--bg-card\)/, '托盘背景 --bg-card（控件表面）')
  assert.match(rule, /border\s*:\s*1px\s+solid\s+var\(--border\)/, '托盘细边框 --border')
  assert.match(rule, /border-radius\s*:\s*var\(--radius\)/, '托盘圆角复用 --radius')
})

test('基础规则：按钮 inline-flex 图标文字垂直居中（issue #479）', () => {
  const rule = optionRule()
  assert.ok(rule, 'styles.css 应有独立 .issue-layout-option 规则（区别于托盘规则）')
  assert.match(rule, /display\s*:\s*inline-flex/, '应为 inline-flex 容纳图标与文字')
  assert.match(rule, /align-items\s*:\s*center/, '图标与文字应垂直居中')
  assert.match(rule, /justify-content\s*:\s*center/, '图标与文字应水平居中')
  assert.match(rule, /gap\s*:\s*var\(--space-1\)/, '图标与文字应留 4px 间距')
})

test('pill 外观：内边距/圆角/背景/边框齐全（issue #479）', () => {
  const rule = optionRule()
  assert.ok(rule, '应有 .issue-layout-option 规则')
  assert.match(rule, /padding\s*:\s*var\(--space-1\)\s+var\(--space-2\)/, '内边距 4px 8px（HIG 4px 网格）')
  assert.match(rule, /border-radius\s*:\s*var\(--radius\)/, '圆角复用 --radius')
  assert.match(rule, /background\s*:\s*transparent/, '默认透明背景（托盘内选项）')
  assert.match(rule, /color\s*:\s*var\(--muted\)/, '次级文字色 --muted')
  assert.match(rule, /cursor\s*:\s*pointer/, '应声明 pointer 光标')
})

test('动效：过渡走 --dur-fast + --ease-out，覆盖颜色/背景/位移/焦点（issue #479）', () => {
  const rule = optionRule()
  assert.ok(rule, '应有 .issue-layout-option 规则')
  assert.match(rule, /transition\s*:/, '应声明过渡')
  assert.match(rule, /var\(--dur-fast\)/, '过渡时长用 --dur-fast（150ms，apple-design 150–300ms 区间）')
  assert.match(rule, /var\(--ease-out\)/, '缓动用 --ease-out')
  for (const prop of ['background', 'color', 'transform', 'box-shadow']) {
    assert.ok(rule.includes(prop), `过渡应覆盖 ${prop}`)
  }
})

test('hover：背景加深、文字转主文本色（issue #479）', () => {
  const m = styles.match(/^\.issue-layout-option:hover\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .issue-layout-option:hover 规则')
  assert.match(m[1], /background\s*:\s*var\(--bg-hover\)/, 'hover 背景应加深为 --bg-hover')
  assert.match(m[1], /color\s*:\s*var\(--text\)/, 'hover 文字应转 --text')
})

test('active：按下微缩放 translateY(1px) scale(0.98)（issue #479）', () => {
  const m = styles.match(/^\.issue-layout-option:active\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .issue-layout-option:active 规则')
  assert.match(m[1], /transform\s*:\s*translateY\(1px\)\s+scale\(0\.98\)/,
               '按下微缩放与 .btn 一致（apple-design press 反馈）')
})

test('focus-visible：--focus-ring 焦点环 + outline none（issue #479）', () => {
  const m = styles.match(/^\.issue-layout-option:focus-visible\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .issue-layout-option:focus-visible 规则')
  assert.match(m[1], /box-shadow\s*:\s*var\(--focus-ring\)/, '焦点环用 --focus-ring')
  assert.match(m[1], /outline\s*:\s*none/, '应关闭默认 outline（焦点环替代）')
})

test('选中态：主色弱底 + 主色文字，当前布局一眼可辨（issue #479）', () => {
  const rule = activeRule()
  assert.ok(rule, '应有 .issue-layout-option.active 规则')
  assert.match(rule, /background\s*:\s*var\(--primary-weak\)/, '选中背景主色弱底 --primary-weak')
  assert.match(rule, /color\s*:\s*var\(--primary\)/, '选中文字主色 --primary')
})

test('图标：布局按钮渲染语义图标（卡片→网格、单列→列表）（issue #479）', () => {
  assert.match(iconSrc, /LayoutGrid/, 'Icon.jsx 应引入 LayoutGrid（lucide）')
  assert.match(iconSrc, /LayoutList/, 'Icon.jsx 应引入 LayoutList（lucide）')
  assert.match(iconSrc, /layoutGrid\s*:\s*LayoutGrid/, 'ICONS 应映射 layoutGrid 语义名')
  assert.match(iconSrc, /layoutList\s*:\s*LayoutList/, 'ICONS 应映射 layoutList 语义名')
  assert.match(sectionSrc,
               /name=\{l\.key === 'cards' \? 'layoutGrid' : 'layoutList'\}/,
               '布局按钮应按布局键渲染网格/列表图标（卡片→网格、单列→列表）')
})

test('主题安全：颜色/背景均经 CSS 变量引用，无硬编码色值（issue #479）', () => {
  const allRules = [
    trayRule(), optionRule(),
    (styles.match(/^\.issue-layout-option:hover\s*\{([^}]*)\}/m) || [])[1],
    (styles.match(/^\.issue-layout-option:active\s*\{([^}]*)\}/m) || [])[1],
    (styles.match(/^\.issue-layout-option:focus-visible\s*\{([^}]*)\}/m) || [])[1],
    activeRule(),
  ]
  for (const rule of allRules) {
    assert.ok(rule, '所有布局切换相关规则应存在')
    const hexMatches = rule.match(/#[0-9a-fA-F]{3,8}\b/g) || []
    assert.deepEqual(hexMatches, [], `不应硬编码十六进制色值（${rule.trim().slice(0, 60)}…）`)
    const rgbMatches = rule.match(/rgba?\(/g) || []
    assert.deepEqual(rgbMatches, [], '不应硬编码 rgb/rgba 色值')
  }
})
