// 侧边栏搜索框宽度与下方导航项对齐回归测试（issue #354）：
//
// 需求：左侧边栏的全局搜索入口（.sidebar-search）宽度应与下方导航项
// （.sidebar-nav .navlink）左右边缘对齐。
//
// 问题根因：.sidebar-search 基础规则为 width: calc(100% - 24px) 且
// margin: 12px 12px 4px——左右各内缩 12px；而导航项 .navlink 为
// width: 100%（box-sizing: border-box，左右边缘与侧边栏内容区齐平），
// 导致搜索框比导航项左右各窄 12px、未对齐。
//
// 修复：.sidebar-search 基础规则改为 width: 100%; margin: 12px 0 4px;
// （左右边缘与导航项完全对齐，仅保留上下间距）；折叠态 36px 图标窄条
// 规则（min-width:861px 内，issue #346）与竖屏抽屉继承全宽行为不受影响。
//
// 断言（styles.css 源码级）：
// 1. .sidebar-search 基础规则 width 为 100%（不再 calc(100% - 24px)）；
// 2. .sidebar-search 基础规则左右 margin 为 0（12px 0 4px，不再 12px 12px）；
// 3. 对齐基准：.sidebar-nav .navlink width: 100% 且无左右 margin
//    （box-sizing: border-box，左右边缘 = 侧边栏内容区边缘）；
// 4. 桌面折叠态（min-width:861px 内）仍为 36px 图标窄条，不受影响；
// 5. 竖屏（≤860px）无规则覆盖 .sidebar-search 宽度，继承全宽。
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
  assert.ok(start >= 0, 'styles.css 应存在 @media (min-width: 861px) 断点')
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

test('搜索框基础规则 width 为 100%（与导航项对齐，不再 calc(100% - 24px)）', () => {
  const m = styles.match(/\.sidebar-search\s*\{([^}]*)\}/)
  assert.ok(m, '应存在 .sidebar-search 基础规则')
  assert.match(m[1], /width:\s*100%/, '搜索框宽度应为 100%（左右边缘与导航项对齐）')
  assert.doesNotMatch(m[1], /calc\(100%\s*-\s*24px\)/, '不再使用 calc(100% - 24px)（旧实现左右各内缩 12px）')
  assert.match(m[1], /display:\s*flex/, '基础规则为 flex 布局')
  assert.match(m[1], /align-items:\s*center/, '基础规则垂直居中')
})

test('搜索框基础规则左右 margin 为 0（不再左右缩进 12px）', () => {
  const m = styles.match(/\.sidebar-search\s*\{([^}]*)\}/)
  assert.ok(m, '应存在 .sidebar-search 基础规则')
  assert.match(m[1], /margin:\s*12px\s+0\s+4px/, 'margin 应为 12px 0 4px（左右为 0，上下间距保留）')
  assert.doesNotMatch(m[1], /margin:\s*12px\s+12px\s+4px/, '不再使用 12px 12px 4px（旧实现左右各缩进 12px）')
})

test('对齐基准：.sidebar-nav .navlink width 100% 且无左右 margin（左右边缘与内容区齐平）', () => {
  const m = styles.match(/\.sidebar-nav\s+\.navlink\s*\{([^}]*)\}/)
  assert.ok(m, '应存在 .sidebar-nav .navlink 规则')
  assert.match(m[1], /width:\s*100%/, '导航项宽度 100%（box-sizing: border-box，左右边缘 = 内容区边缘）')
  assert.doesNotMatch(m[1], /margin\s*:/, '导航项无 margin（搜索框 0 左右 margin 后左右边缘与之齐平）')
})

test('桌面折叠态（min-width:861px 内）搜索框仍为 36px 图标窄条，不受影响', () => {
  const block = minWidth861Block(styles)
  const body = block.replace(/^@media \(min-width:\s*861px\)\s*\{/, '').replace(/\}$/, '')
  const m = body.match(/\.sidebar\.collapsed\s+\.sidebar-search\s*\{([^}]*)\}/)
  assert.ok(m, 'min-width:861px 块内应声明折叠态规则')
  assert.match(m[1], /width:\s*36px/, '折叠态搜索入口为 36px 图标窄条')
  assert.match(m[1], /justify-content:\s*center/, '折叠态图标居中')
})

test('竖屏（≤860px）无规则覆盖 .sidebar-search 宽度：继承全宽 100%', () => {
  // 折叠态 36px 规则限定在 min-width:861px 内；≤860px 区块不出现
  // .sidebar-search 宽度覆盖（竖屏抽屉内搜索框保持全宽与导航项对齐）
  const media = styles.match(/@media \(max-width:\s*860px\)\s*\{([^}]*)\}/s)
  assert.ok(media, '应存在 max-width:860px 断点')
  assert.doesNotMatch(media[1], /\.sidebar-search\s*\{[^}]*width/, '竖屏断点内不应覆盖 .sidebar-search 宽度')
})
