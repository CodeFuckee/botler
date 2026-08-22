// 全局搜索浮层透明回归测试（issue #444）：
//
// 现象：全局搜索框（/ 打开的命令面板浮层 .search-overlay）现在是全透明的，
// 完全看不到搜索框的内容——浮层面板透出被遮罩压暗的页面，占位符/提示/
// 图标/分组标题等次级文字全部不可见。
//
// 根因：issue #216 引入的全局搜索样式引用了设计令牌 --surface /
// --text-muted / --border-strong / --accent，但 styles.css 的浅色
// :root、深色 @media(prefers-color-scheme: dark)、手动深色
// :root[data-theme='dark'] 三套令牌集中从未定义过这些变量（--shadow-sm
// 同理缺失）：
//   - .search-overlay { background: var(--surface) }  → 变量未定义，
//     background 计算值为 transparent，浮层面板全透明；
//   - .search-overlay-input::placeholder / .search-overlay-icon /
//     .search-overlay-hint / .search-overlay-empty / .search-group-title
//     / .sidebar-search / .topbar-search { color: var(--text-muted) }
//     → 变量未定义，占位符/图标/提示文字不可见；
//   - .sidebar-search:hover { border-color: var(--border-strong) } → 未定义；
//   - .introspect-result a / .discover-result a / .discover-repo a
//     { color: var(--accent) } → 未定义（且无 fallback）；
//   - .repo-logo-btn:hover { box-shadow: var(--shadow-sm) } → 未定义。
//
// 断言（styles.css 源码级）：
// 1. styles.css 中每个「无 fallback」的 var(--X) 引用都必须在文件中被
//    定义（--X: ...），不允许「引用未定义变量」；
// 2. 三套主题令牌集（浅色 :root / 深色 @media / 手动深色 [data-theme]
//    ='dark'）都必须定义 --surface / --text-muted / --border-strong /
//    --accent / --shadow-sm 五个令牌，保证浅色与深色主题下搜索框均不
//    透明、次级文字可见。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取 styles.css 中定义的所有令牌名（--x: 或 --x :）
function definedTokens(css) {
  const names = new Set()
  const re = /--([a-z0-9-]+)\s*:/gi
  let m
  while ((m = re.exec(css))) names.add(`--${m[1].toLowerCase()}`)
  return names
}

// 提取样式表中「无 fallback」的 var(--x) 引用（var(--x, ...) 有兜底不算）。
// 正则先取完整变量名再判分隔符：--shadow-lg 之类长名不得被截断成 --shadow-l。
function bareVarRefs(css) {
  const refs = []
  const re = /var\(\s*(--[a-z0-9-]+)\s*(,|\))/gi
  let m
  while ((m = re.exec(css))) {
    if (m[2] === ')') refs.push(m[1].toLowerCase()) // 无 fallback 才算引用
  }
  return [...new Set(refs)]
}

test('源码：styles.css 中无 fallback 引用的 CSS 令牌全部已定义（无引用未定义变量）', () => {
  const defined = definedTokens(styles)
  const missing = bareVarRefs(styles).filter((v) => !defined.has(v))
  assert.deepEqual(
    missing,
    [],
    `styles.css 引用了未定义的 CSS 令牌：${missing.join(', ')}`
      + ' —— 会导致 .search-overlay 背景透明、占位符/提示文字不可见（issue #444）',
  )
})

test('源码：浅色与深色三套主题令牌集均定义 --surface / --text-muted / --border-strong / --accent / --shadow-sm', () => {
  const required = ['--surface', '--text-muted', '--border-strong', '--accent', '--shadow-sm']
  // 三套令牌块：浅色 :root（文件开头第一个 :root { ... }）、
  // 深色 @media (prefers-color-scheme: dark)、手动深色 :root[data-theme='dark']
  const blocks = {
    '浅色 :root': styles.match(/:root\s*\{([^}]*)\}/)?.[1] || '',
    '深色 @media(prefers-color-scheme: dark)':
      styles.match(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{[^}]*:root:not\(\[data-theme='light'\]\)\s*\{([^}]*)\}/)?.[1] || '',
    '手动深色 :root[data-theme="dark"]':
      styles.match(/:root\[data-theme='dark'\]\s*\{([^}]*)\}/)?.[1] || '',
  }
  for (const [name, body] of Object.entries(blocks)) {
    assert.ok(body.length > 0, `${name} 令牌块应存在`)
    const defined = definedTokens(`:root { ${body} }`)
    for (const v of required) {
      assert.ok(defined.has(v), `${name} 令牌集缺少 ${v}（搜索浮层依赖，缺失则全透明/文字不可见，issue #444）`)
    }
  }
})
