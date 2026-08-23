// 移动端竖屏 issue 详情右边栏底部操作栏按钮单行回归测试（issue #463）：
// 小尺寸手机（375px 竖屏）下，底部操作栏（.drawer-bottom-actions）的
// 五个按钮——「关闭 issue / 执行 / 重试 / 查看执行的详情 / 在 GitLab
// 中打开」——原 flex-wrap: wrap 因宽度不足被拆成两行、右对齐的第二行
// 零散不整齐，用户体验差。修复后：
//   1) 按钮永不换行（flex-wrap: nowrap）——单行常驻；
//   2) 内容超宽时容器横向滚动（overflow-x: auto）且滚动条隐藏
//      （scrollbar-width: none + ::-webkit-scrollbar display:none），
//      触摸/触控板仍可滑动看到全部按钮；
//   3) 右对齐语义（issue #340）改由首按钮 margin-left: auto 实现——
//      宽度足够时右对齐；超宽时 auto margin 失效、按钮从左排布可滚动，
//      避免 justify-content: flex-end 下溢出在左侧、首按钮不可达。
// 布局真实几何由 e2e issue-drawer-bottom-actions-wrap.spec.js 验证。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取最后一个 @media (max-width: 860px) 断点块（与 overview 系测试同逻辑）
function mobileMediaBlock(css) {
  const re = /@media \(max-width:\s*860px\)\s*\{/g
  let start = -1
  let m
  while ((m = re.exec(css))) start = m.index
  assert.ok(start >= 0, 'styles.css 应存在 @media (max-width: 860px) 断点')
  let depth = 0
  let end = -1
  for (let i = start; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > start, '860px 断点块应有闭合大括号')
  return css.slice(start, end + 1)
}

// 提取最后一个 @media (max-width: 860px) and (orientation: portrait) 断点块
function portraitMediaBlock(css) {
  const re = /@media \(max-width:\s*860px\)\s+and\s+\(orientation:\s*portrait\)\s*\{/g
  let start = -1
  let m
  while ((m = re.exec(css))) start = m.index
  assert.ok(start >= 0, 'styles.css 应存在 portrait 断点')
  let depth = 0
  let end = -1
  for (let i = start; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > start, 'portrait 断点块应有闭合大括号')
  return css.slice(start, end + 1)
}

test('styles.css：底部操作栏按钮单行（flex-wrap: nowrap），不再拆成两行（issue #463）', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer-bottom-actions\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应声明 .drawer-bottom-actions 规则')
  assert.match(m[1], /flex-wrap:\s*nowrap/,
               '底部操作栏应禁止换行（nowrap）——修复前 wrap 在 375px 下拆成两行')
  // 超宽时容器横向滚动（单行按钮可滑动看到全部）
  assert.match(m[1], /overflow-x:\s*auto/,
               '底部操作栏应允许横向滚动（超宽时按钮不裁切不可达）')
  // 滚动条隐藏：Firefox scrollbar-width: none + WebKit ::-webkit-scrollbar
  assert.match(m[1], /scrollbar-width:\s*none/, '底部操作栏应隐藏 Firefox 滚动条')
  assert.match(block, /\.drawer-bottom-actions::-webkit-scrollbar\s*\{[^}]*display:\s*none/,
               '底部操作栏应隐藏 WebKit 滚动条')
  // 布局属性保持（sticky 常驻底部不回归）
  assert.match(m[1], /position:\s*sticky/, '底部操作栏仍应 sticky 常驻底部')
  assert.match(m[1], /bottom:\s*0/, '底部操作栏仍应吸附底部')
})

test('styles.css：竖屏右对齐改由首按钮 auto margin 实现，超宽时可滚动（issue #463）', () => {
  const block = portraitMediaBlock(styles)
  const firstChild = block.match(
    /\.drawer\.issue-drawer\s+\.drawer-bottom-actions\s+>\s+:first-child\s*\{([^}]*)\}/)
  assert.ok(firstChild, 'portrait 断点内应声明底部操作栏首按钮 auto margin 规则')
  assert.match(firstChild[1], /margin-left:\s*auto/,
               '宽度足够时按钮组应右对齐（首按钮 margin-left: auto）')
  // 不得回退 justify-content: flex-end——超宽时溢出在左侧且容器不可滚动
  assert.ok(!/\.drawer\.issue-drawer\s+\.drawer-bottom-actions\s*\{[^}]*justify-content:\s*flex-end/.test(block),
            '底部操作栏不得使用 justify-content: flex-end（超宽时首按钮不可达）')
})
