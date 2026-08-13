// 复现测试（issue #53）：任务页面左右两边还有空白时，任务列表不应出现水平滚动条。
//
// 根因分析：全局 `* { box-sizing: border-box }` 下，任务表格的可用宽度 =
//   .content 的 max-width（--content-width）− 40px（.content 左右 padding 各 20px）
//   − 40px（.card 左右 padding 各 20px）
// 而 .table.tasks-table 的 min-width 为 12 列宽度总和 1360px（issue #37，保证窄视口
// 各列保持完整宽度不折行），因此"无滚动"需要 --content-width ≥ 1360 + 80 = 1440px。
// 现状（修复前）：≥1600px 视口 --content-width 仅 1360px → 表格可用 1280px < 1360px，
// 表格在 .table-wrap 内出现水平滚动条，页面两侧却有 120~280px 空白 —— 与用户描述一致。
// 修复目标：视口 ≥ 表格所需总宽度（1440px）时，表格可用宽度 ≥ min-width，不出现滚动；
// 视口更窄时（装不下表格）保持既有 .table-wrap 横向滚动行为不变。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// .content 左右 padding 与 .card 左右 padding（styles.css 中的布局常量，
// 与 styles.css 的 .content / .card 规则保持一致；改动样式时需同步此处）
const CONTENT_PAD_X = 20 // .content { padding: 24px 20px 60px; }
const CARD_PAD_X = 20 // .card { padding: 20px; }

// 从 styles.css 提取 :root 默认 --content-width
function defaultContentWidth(css) {
  const rootBlock = css.match(/:root\s*\{[^}]*\}/)
  assert.ok(rootBlock, 'styles.css 缺少 :root 变量块')
  const m = rootBlock[0].match(/--content-width:\s*(\d+)px/)
  assert.ok(m, ':root 缺少 --content-width 变量')
  return Number(m[1])
}

// 从 styles.css 提取所有 (min-width, --content-width) 断点，按视口阈值升序
function contentBreakpoints(css) {
  const breaks = []
  const re = /@media \(min-width:\s*(\d+)px\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}/g
  let m
  while ((m = re.exec(css))) {
    const cw = m[2].match(/--content-width:\s*(\d+)px/)
    if (cw) breaks.push({ min: Number(m[1]), width: Number(cw[1]) })
  }
  return breaks.sort((a, b) => a.min - b.min)
}

// 模拟指定视口宽度下生效的 --content-width（断点按 min-width 升序，后者覆盖前者）
function contentWidthAt(viewport, breaks, defaultWidth) {
  let width = defaultWidth
  for (const b of breaks) if (viewport >= b.min) width = b.width
  return width
}

// 提取 .table.tasks-table 的 min-width 数值
function tasksTableMinWidth(css) {
  const rule = css.match(/\.table\.tasks-table\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .table.tasks-table 规则')
  const m = rule[0].match(/min-width:\s*(\d+)px/)
  assert.ok(m, '.table.tasks-table 缺少 min-width')
  return Number(m[1])
}

const MIN_WIDTH = tasksTableMinWidth(styles)
const DEFAULT_WIDTH = defaultContentWidth(styles)
const BREAKS = contentBreakpoints(styles)
// 表格无滚动所需的最小 --content-width：min-width + 两侧容器 padding
const REQUIRED_CONTENT_WIDTH = MIN_WIDTH + 2 * (CONTENT_PAD_X + CARD_PAD_X)
// 表格无滚动所需的最小视口宽度：与上相同（视口足够时 .content 应放宽到该宽度）
const REQUIRED_VIEWPORT = REQUIRED_CONTENT_WIDTH

test('styles.css 存在按视口放宽 --content-width 的媒体查询断点', () => {
  assert.ok(BREAKS.length >= 1, '缺少 min-width 媒体查询断点')
})

test('视口宽度足够装下表格时，任务表格可用宽度 ≥ min-width，不出现水平滚动条', () => {
  // 覆盖各断点区间：1440（恰好装下）、1500、1600、1750、1919、1920、2560（2K）
  const viewports = [REQUIRED_VIEWPORT, 1500, 1600, 1750, 1919, 1920, 2560]
  for (const vp of viewports) {
    const contentWidth = contentWidthAt(vp, BREAKS, DEFAULT_WIDTH)
    // border-box 下表格可用宽度 = content-width − .content 左右 padding − .card 左右 padding
    const tableArea = contentWidth - 2 * (CONTENT_PAD_X + CARD_PAD_X)
    assert.ok(
      tableArea >= MIN_WIDTH,
      `视口 ${vp}px（≥ 表格所需 ${REQUIRED_VIEWPORT}px）应无水平滚动：` +
        `--content-width=${contentWidth}px，表格可用 ${tableArea}px < 表格 min-width ${MIN_WIDTH}px`,
    )
  }
})

test('窄视口装不下表格时保留 .table-wrap 横向滚动（issue #28 既有行为不变）', () => {
  const wrap = styles.match(/\.table-wrap\s*\{[^}]*\}/)
  assert.ok(wrap, 'styles.css 缺少 .table-wrap 规则')
  assert.match(wrap[0], /overflow-x\s*:\s*auto/, '.table-wrap 应保持 overflow-x: auto')
})
