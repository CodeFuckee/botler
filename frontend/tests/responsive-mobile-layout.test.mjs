// 移动端响应式布局测试（issue #270）：≤860px 窄视口整体移动化适配
// （复用设置页 #139 断点约定；验收标准要求 ≤768px 三核心页可完成
// 主要操作，860px 更早回落保证全覆盖）。
//
// 断言（styles.css 源码级）：
// 1. 存在 @media (max-width: 860px) 断点块；
// 2. 断点内：概览页 .issues-list / .pipelines-list 卡片网格降为单列
//    （grid-template-columns: 1fr），手机竖屏一卡一行；
// 3. 断点内：.drawer 全宽（width:100% / max-width:100%），不再 92vw 留缝；
// 4. 断点内：.drawer-bottom-actions 底部操作栏显示（flex + sticky bottom），
//    桌面默认隐藏（display:none）；
// 5. 断点内：.drawer.issue-drawer 头部操作按钮隐藏（.issue-drawer-actions
//    display:none），任务执行详情第二层（.task-detail-drawer）不受影响；
// 6. 桌面端不受影响：单列规则只出现在 860px 断点内，.issues-list 桌面
//    仍为 auto-fit 自适应网格（防误伤桌面布局）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取最后一个 @media (max-width: 860px) 断点块（括号配平；文件内可能有
// 多个 860px 断点——设置页 #139 与移动端 #270，取最后一个即 issue #270 块）
function lastMedia860(css) {
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

function mobileMediaBlock(css) {
  const block = lastMedia860(css)
  return block.replace(/^@media \(max-width:\s*860px\)\s*\{/, '').replace(/\}$/, '')
}

// 提取某选择器的规则体：遍历全部同名规则，返回首个含指定特征属性的规则体
function ruleBody(css, selector, feature) {
  const re = new RegExp(`\\.${selector.replace(/\./g, '\\\\.')}\\s*\\{([^}]*)\\}`, 'g')
  let m
  while ((m = re.exec(css))) {
    if (!feature || m[1].includes(feature)) return m[1]
  }
  assert.ok(false, `styles.css 应存在 .${selector} 规则（特征：${feature || '任意'}）`)
}


test('断点内：概览/流水线卡片网格降为单列（手机竖屏一卡一行）', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.issues-list\s*,\s*\.pipelines-list\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应同时声明 .issues-list 与 .pipelines-list')
  assert.match(m[1], /grid-template-columns:\s*1fr/, '窄视口卡片网格应为单列（1fr）')
})

test('断点内：抽屉全宽（width 100%），不再 92vw 留缝', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应声明 .drawer 全宽规则')
  assert.match(m[1], /width:\s*100%/, '窄视口抽屉应 width:100%')
  assert.match(m[1], /max-width:\s*100%/, '窄视口抽屉应 max-width:100%（覆盖桌面 92vw/640px）')
})

test('断点内：底部操作栏显示（flex + sticky bottom）；桌面默认隐藏', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer-bottom-actions\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应声明 .drawer-bottom-actions 显示规则')
  assert.match(m[1], /display:\s*flex/, '底部操作栏应为 flex 布局')
  assert.match(m[1], /position:\s*sticky/, '底部操作栏应 sticky 常驻')
  assert.match(m[1], /bottom:\s*0/, '底部操作栏应吸附底部')
  // 桌面默认隐藏（断点外的基础规则）
  const base = ruleBody(styles, 'drawer-bottom-actions', 'display')
  assert.match(base, /display:\s*none/, '桌面端底部操作栏默认隐藏（操作按钮在头部）')
})

test('断点内：抽屉全宽需禁止 flex 收缩（width:100% 百分比基准实测退化为内容宽度）', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应声明 .drawer 全宽规则')
  assert.match(m[1], /flex-shrink:\s*0/, '窄视口抽屉应 flex-shrink: 0（实测 375px 视口否则被压到 243px）')
})

test('断点内：kv 详情表格 fixed 布局收窄 + 单元格断词（长值不再撑破视口）', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.table\.kv\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应声明 .table.kv 收窄规则')
  assert.match(m[1], /table-layout:\s*fixed/, 'kv 表格应 fixed 布局（实测 auto 布局被长值撑到 512px）')
  const cells = block.match(/\.table\.kv\s+th,\s*\.table\.kv\s+td\s*\{([^}]*)\}/)
  assert.ok(cells, '断点内应声明 kv 单元格断词规则')
  assert.match(cells[1], /overflow-wrap:\s*anywhere/, '单元格应允许任意处断行')
  assert.match(cells[1], /word-break:\s*break-word/, '单元格应允许单词内断行')
})

test('断点内：表单行换行 + kv 单元格控件限宽（select 固有宽度不再撑破行/表）', () => {
  const block = mobileMediaBlock(styles)
  const row = block.match(/\.form-row\s*\{([^}]*)\}/)
  assert.ok(row, '860px 断点内应声明 .form-row 换行规则')
  assert.match(row[1], /flex-wrap:\s*wrap/, '表单行窄视口应换行')
  const rowInput = block.match(/\.form-row\s+\.input\s*\{([^}]*)\}/)
  assert.ok(rowInput, '断点内应声明 .form-row .input 限宽')
  assert.match(rowInput[1], /max-width:\s*100%/, '表单行输入控件应限宽 100%')
  const kvInput = block.match(/\.table\.kv\s+\.input\s*,[^}]*\{([^}]*)\}/)
  assert.ok(kvInput, '断点内应声明 .table.kv .input 限宽（选择器组含 select/textarea）')
  assert.match(kvInput[1], /max-width:\s*100%/, 'kv 表格内控件应限宽 100%（实测 273px select 撑破 131px 单元格）')
})

test('断点内：issue 抽屉头部操作按钮隐藏（仅 .drawer.issue-drawer），第二层不受影响', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer\.issue-drawer\s+\.issue-drawer-actions\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应隐藏 issue 抽屉头部操作按钮（仅 issue 抽屉）')
  assert.match(m[1], /display:\s*none/, '头部操作按钮窄视口应隐藏（下沉底部操作栏）')
  // 第二层抽屉（.task-detail-drawer）头部 × 关闭按钮不受该规则影响：
  // 规则选择器必须限定 .drawer.issue-drawer
  const taskDetail = block.match(/\.drawer\.task-detail-drawer\s+\.issue-drawer-actions/)
  assert.ok(!taskDetail, '第二层抽屉头部操作按钮不应被隐藏规则命中')
})

test('桌面端不受影响：单列规则仅存在于 860px 断点内，桌面仍为 auto-fit 网格', () => {
  // .issues-list 基础规则（断点外）必须是 auto-fit 自适应网格（防误伤桌面）
  const base = ruleBody(styles, 'issues-list', 'display')
  assert.match(base, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(280px,\s*1fr\)\)/,
               '桌面 .issues-list 应保持 auto-fit 自适应列网格')
  // 单列规则只能出现在 860px 断点内（从断点块提取后，其余文件区域不得再有
  // 针对 .issues-list 的 1fr 覆盖）
  const outside = styles.replace(mobileMediaBlock(styles), '')
  const singleCol = outside.match(/\.issues-list[^{]*\{[^}]*grid-template-columns:\s*1fr/)
  assert.ok(!singleCol, '文件其余区域不得再有 .issues-list 单列覆盖（桌面不受影响）')
})

test('桌面端不受影响：抽屉基础规则保持 520px/92vw 上限（断点外）', () => {
  const base = ruleBody(styles, 'drawer', 'width')
  assert.match(base, /width:\s*520px/, '桌面抽屉宽度仍为 520px')
  assert.match(base, /max-width:\s*92vw/, '桌面抽屉仍保留 92vw 上限')
  assert.match(base, /overflow-y:\s*auto/, '抽屉滚动行为保持不变')
})
