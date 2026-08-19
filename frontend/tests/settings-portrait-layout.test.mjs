// 设置页竖屏左右布局测试（issue #339）：竖屏（641~860px 且 orientation:
// portrait，平板/窄窗口）下设置页保持「设置左侧导航栏 + 设置面板」左右布局
// （类似手机/平板设置页面主从式），不再回落单栏置顶；手机竖屏（≤640px）
// 内容列装不下两栏（AI 供应商表格 min-content≈335px），保持 860px 断点
// 单栏；横屏窄视口（orientation: landscape）保持 860px 断点单栏；
// 桌面端（>860px）两栏不受影响。
//
// 断言（styles.css 源码级）：
// 1. 存在针对设置页的竖屏断点块（@media (min-width: 641px) and
//    (max-width: 860px) and (orientation: portrait) 内含 .settings-layout
//    规则）；
// 2. 竖屏断点内：.settings-layout 保持两栏网格
//    （grid-template-columns: auto minmax(0, 1fr)）——左右布局；
// 3. 竖屏断点内：.settings-sidebar 恢复 sticky 吸顶 + 宽度收窄为 200px
//    （紧凑，接近手机设置列表密度）；
// 4. 竖屏断点内：.settings-nav 恢复内部滚动（max-height + overflow-y: auto），
//    导航项多时左栏独立滚动、右侧面板滚动时左栏常驻可见；
// 5. 横屏窄视口保持单栏：普通 860px 断点内 .settings-layout 仍为
//    grid-template-columns: 1fr、.settings-sidebar 仍 position: static
//    （竖屏覆盖规则只存在于 portrait 断点，不吞掉横屏行为）；
// 6. 桌面端不受影响：基础 .settings-layout 规则仍为两栏
//    （grid-template-columns: auto minmax(0, 1fr)）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取首个包含 .settings-layout 规则的竖屏断点块（括号配平；
// 竖屏断点文件内可能有多个（issue 抽屉 #334 等），取设置页自己的块）
function settingsPortraitBlock(css) {
  const re = /@media \(min-width:\s*641px\)\s+and\s+\(max-width:\s*860px\)\s+and\s+\(orientation:\s*portrait\)\s*\{/g
  let m
  while ((m = re.exec(css))) {
    let depth = 0
    let end = -1
    for (let i = m.index; i < css.length; i++) {
      if (css[i] === '{') depth++
      else if (css[i] === '}') {
        depth--
        if (depth === 0) { end = i; break }
      }
    }
    assert.ok(end > m.index, '竖屏断点块应有闭合大括号')
    const block = css.slice(m.index, end + 1)
    if (block.includes('.settings-layout')) return block
  }
  assert.ok(false, 'styles.css 应存在针对设置页的竖屏断点块（portrait 断点内含 .settings-layout）')
}

// 提取全部普通 @media (max-width: 860px) 断点块（不含 orientation 限定）
function plain860Blocks(css) {
  const re = /@media \(max-width:\s*860px\)\s*\{/g
  const blocks = []
  let m
  while ((m = re.exec(css))) {
    let depth = 0
    let end = -1
    for (let i = m.index; i < css.length; i++) {
      if (css[i] === '{') depth++
      else if (css[i] === '}') {
        depth--
        if (depth === 0) { end = i; break }
      }
    }
    blocks.push(css.slice(m.index, end + 1))
    re.lastIndex = end + 1
  }
  return blocks
}

test('存在针对设置页的竖屏断点块（641~860px 竖屏）', () => {
  const block = settingsPortraitBlock(styles)
  assert.match(block,
    /@media \(min-width:\s*641px\)\s+and\s+\(max-width:\s*860px\)\s+and\s+\(orientation:\s*portrait\)/,
    '竖屏断点应限定 641~860px 且 orientation: portrait（≤640px 手机竖屏保持单栏）')
})

test('竖屏断点内：设置页保持左右布局（两栏网格）', () => {
  const block = settingsPortraitBlock(styles)
  const m = block.match(/\.settings-layout\s*\{([^}]*)\}/)
  assert.ok(m, '竖屏断点内应声明 .settings-layout 规则')
  assert.match(m[1], /grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)/,
    '竖屏下设置页应为「侧栏 auto + 面板 1fr」两栏网格（左右布局）')
})

test('竖屏断点内：左侧导航栏 sticky 吸顶 + 宽度收窄为 200px', () => {
  const block = settingsPortraitBlock(styles)
  const m = block.match(/\.settings-sidebar\s*\{([^}]*)\}/)
  assert.ok(m, '竖屏断点内应声明 .settings-sidebar 规则')
  assert.match(m[1], /position:\s*sticky/, '竖屏下左侧导航栏应恢复 sticky 吸顶（覆盖 860px 断点 static）')
  assert.match(m[1], /width:\s*200px/, '竖屏下左侧导航栏应收窄为 200px（手机设置列表密度）')
})

test('竖屏断点内：导航面板恢复内部滚动（内容超高不截断）', () => {
  const block = settingsPortraitBlock(styles)
  const m = block.match(/\.settings-nav\s*\{([^}]*)\}/)
  assert.ok(m, '竖屏断点内应声明 .settings-nav 规则')
  assert.match(m[1], /max-height:\s*calc\(100vh\s*-\s*92px\)/,
    '竖屏下导航面板应限高（calc(100vh - 92px)，吸顶导航内部滚动）')
  assert.match(m[1], /overflow-y:\s*auto/, '竖屏下导航面板应内部滚动（导航项多不截断）')
})

test('横屏窄视口保持单栏：普通 860px 断点仍保留单栏 + static 规则', () => {
  const blocks = plain860Blocks(styles)
  const single = blocks.find((b) =>
    /\.settings-layout\s*\{[^}]*grid-template-columns:\s*1fr/.test(b))
  assert.ok(single, '普通 860px 断点内应保留设置页单栏规则（横屏窄视口导航置顶）')
  const sidebarStatic = blocks.find((b) =>
    /\.settings-sidebar\s*\{[^}]*position:\s*static/.test(b))
  assert.ok(sidebarStatic, '普通 860px 断点内应保留设置侧栏 static（横屏窄视口）')
})

test('桌面端不受影响：基础 .settings-layout 仍为两栏网格', () => {
  // 基础规则（断点外第一个 .settings-layout 即桌面两栏布局）
  const base = styles.match(/\.settings-layout\s*\{([^}]*)\}/)
  assert.ok(base, '应存在 .settings-layout 基础规则')
  assert.match(base[1], /grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)/,
    '桌面设置页应保持两栏网格（防误伤桌面布局）')
  assert.match(base[1], /gap:\s*var\(--space-5\)/, '桌面设置页两栏间距应保持 24px（--space-5）')
})
