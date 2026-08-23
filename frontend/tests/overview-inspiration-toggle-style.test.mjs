// 美化测试（issue #460）：概览页灵感列表「展开灵感列表 / 收起灵感列表」
// 按钮（.inspiration-toggle-btn）升级为项目 apple-design 设计语言的
// 轻量 pill 按钮——从无样式裸文本（此前仅 margin-left: auto）变为
// 有明确按钮外观（内边距/圆角/柔和背景/边框）与完整交互反馈
// （hover 主色、active 微缩放、focus-visible 焦点环），并区分
// 展开/收起状态视觉（aria-expanded="true" 主色弱背景强调）。
//
// 断言：
// 1. 基础规则：独立 .inspiration-toggle-btn 规则存在，inline-flex +
//    align-items: center + gap（图标与文字垂直居中，Lucide 图标 1em
//    与行内文本基线对齐，issue #177）；
// 2. pill 外观：padding / border-radius / background / border 齐全；
// 3. 动效：transition 走 --dur-fast（150ms）+ --ease-out（apple-design
//    动效时长区间 150–300ms），hover/active/focus 反馈平滑出现；
// 4. hover：背景 --bg-hover、边框 --border-hover、文字主色 --primary；
// 5. active：translateY(1px) scale(0.98) 按下微缩放（与 .btn 一致，
//    apple-design press 反馈）；
// 6. focus-visible：--focus-ring 焦点环 + outline: none；
// 7. 展开态：[aria-expanded='true'] 时主色弱背景 --primary-weak + 主色
//    文字 --primary，折叠/展开状态一眼可辨；
// 8. 主题安全：所有颜色/背景均经 CSS 变量（var(--*)）引用，不硬编码
//    十六进制色值，深浅色主题（issue #217）自动适配。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取独立 .inspiration-toggle-btn 规则体（不与其它选择器逗号共享）
function toggleRule() {
  const m = styles.match(/^\.inspiration-toggle-btn\s*\{([^}]*)\}/m)
  return m ? m[1] : null
}
// 提取展开态规则体
function expandedRule() {
  const m = styles.match(/\.inspiration-toggle-btn\[aria-expanded=['"]true['"]\]\s*\{([^}]*)\}/m)
  return m ? m[1] : null
}

test('基础规则：按钮 inline-flex 图标文字垂直居中（issue #460）', () => {
  const rule = toggleRule()
  assert.ok(rule, 'styles.css 应有独立 .inspiration-toggle-btn 规则（区别于 .inspiration-load-more-btn 组合规则）')
  assert.match(rule, /display\s*:\s*inline-flex/, '应为 inline-flex 容纳图标与文字')
  assert.match(rule, /align-items\s*:\s*center/, '图标与文字应垂直居中')
  assert.match(rule, /gap\s*:\s*var\(--space-1\)/, '图标与文字应留 4px 间距')
})

test('pill 外观：内边距/圆角/柔和背景/边框齐全（issue #460）', () => {
  const rule = toggleRule()
  assert.ok(rule, '应有 .inspiration-toggle-btn 规则')
  assert.match(rule, /padding\s*:\s*var\(--space-1\)\s+var\(--space-2\)/, '内边距 4px 8px（HIG 4px 网格）')
  assert.match(rule, /border-radius\s*:\s*var\(--radius-lg\)/, 'pill 圆角复用 --radius-lg')
  assert.match(rule, /background\s*:\s*var\(--bg-soft\)/, '柔和背景 --bg-soft（次级操作层次）')
  assert.match(rule, /border\s*:\s*1px\s+solid\s+var\(--border\)/, '细边框 --border')
  assert.match(rule, /color\s*:\s*var\(--muted\)/, '次级文字色 --muted')
})

test('动效：过渡走 --dur-fast + --ease-out，覆盖颜色/边框/背景/位移/焦点（issue #460）', () => {
  const rule = toggleRule()
  assert.ok(rule, '应有 .inspiration-toggle-btn 规则')
  assert.match(rule, /transition\s*:/, '应声明过渡')
  assert.match(rule, /var\(--dur-fast\)/, '过渡时长用 --dur-fast（150ms，apple-design 150–300ms 区间）')
  assert.match(rule, /var\(--ease-out\)/, '缓动用 --ease-out')
  for (const prop of ['background', 'border-color', 'color', 'transform', 'box-shadow']) {
    assert.ok(rule.includes(prop), `过渡应覆盖 ${prop}`)
  }
})

test('hover：背景加深、边框加深、文字转主色（issue #460）', () => {
  const m = styles.match(/^\.inspiration-toggle-btn:hover\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .inspiration-toggle-btn:hover 规则')
  assert.match(m[1], /background\s*:\s*var\(--bg-hover\)/, 'hover 背景应加深为 --bg-hover')
  assert.match(m[1], /border-color\s*:\s*var\(--border-hover\)/, 'hover 边框应加深为 --border-hover')
  assert.match(m[1], /color\s*:\s*var\(--primary\)/, 'hover 文字应转主色 --primary')
})

test('active：按下微缩放 translateY(1px) scale(0.98)（issue #460）', () => {
  const m = styles.match(/^\.inspiration-toggle-btn(?::not\(:disabled\))?:active\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .inspiration-toggle-btn:active 规则')
  assert.match(m[1], /translateY\(1px\)\s*scale\(0\.98\)/, '按下应微缩放 + 下沉（与 .btn 一致，apple-design press 反馈）')
})

test('focus-visible：主色焦点环 + 去除默认 outline（issue #460）', () => {
  const m = styles.match(/^\.inspiration-toggle-btn:focus-visible\s*\{([^}]*)\}/m)
  assert.ok(m, '应有 .inspiration-toggle-btn:focus-visible 规则')
  assert.match(m[1], /box-shadow\s*:\s*var\(--focus-ring\)/, '键盘聚焦应有主色焦点环')
  assert.match(m[1], /outline\s*:\s*none/, '应去除浏览器默认 outline（焦点环替代）')
})

test('展开态：aria-expanded=true 主色弱背景强调，收起/展开一眼可辨（issue #460）', () => {
  const rule = expandedRule()
  assert.ok(rule, '应有 .inspiration-toggle-btn[aria-expanded="true"] 规则')
  assert.match(rule, /background\s*:\s*var\(--primary-weak\)/, '展开态背景应为主色弱背景')
  assert.match(rule, /color\s*:\s*var\(--primary\)/, '展开态文字应为主色')
})

test('主题安全：全部颜色经 CSS 变量引用，不硬编码色值（issue #460）', () => {
  // 先剔除注释——注释中的 issue 编号（如 issue #124）含 hex 字符，
  // 会误命中色值检测；色值只出现在声明语句里
  const rule = ((toggleRule() || '') + (expandedRule() || ''))
    .replace(/\/\*[\s\S]*?\*\//g, '')
  assert.ok(rule.length > 0, '应有按钮规则')
  assert.doesNotMatch(rule, /#[0-9a-fA-F]{3,8}\b/, '按钮颜色应全部经 var() 引用，深浅主题自动适配')
})
