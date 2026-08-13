// 复现测试（issue #37）：任务列表"创建时间"、"用时"、"操作"三列在页面宽度足够时，
// 数据仍折成两行显示（用户截图：宽屏下页面左右两侧有空白，但时间戳的日期/时间分两行、
// 时长如"1 小时 23 分钟"分两行、操作列的"执行/重试"两个按钮上下堆叠）。
//
// 根因：任务表格 table-layout: fixed + 12 列显式宽度，三列固定宽度（135px / 60px / 65px）
// 不足以容纳其单行内容：
//   - 创建时间 `2026-08-13 12:06:19`（19 字符，14px 字号）约需 147px（含 td 左右 padding 20px）
//   - 用时最长格式 `12 天 23 小时`（数字+中文混排）约需 105px（含 padding）
//   - 操作列「执行」+「重试」两个按钮（最坏情况「重试中…」4 字）约需 136px（含 padding）
// 而三列内容均含空格（日期与时间之间、时长数字与单位之间、按钮之间），列宽不足时
// 浏览器在空格处折行。同时标题列 30%、失败原因列 22% 在宽视口下获得富余空间，
// 空间分配失衡——正是"两侧有空白、三列却换行"。
//
// 修复目标（对方案中立）：
// 1. 三列列宽 ≥ 其内容单行所需（本文件 test 1-3）；
// 2. 三列 td 设置 white-space: nowrap，即使列宽被压缩也不在空格处折行（test 4）；
// 3. 12 列全部使用 px 固定宽度且总和 ≤ 1600px（宽视口内容区上限），保证表格在宽视口下
//    不超出容器、不出现水平滚动条（test 5，issue #28 第二轮回归保护）；
// 4. 表格设 min-width（= 列宽总和），窄视口下保持既有横向滚动行为（test 6）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取 .table.tasks-table th:nth-child(n) 规则的 width 声明，返回 { value, unit }
function colWidth(n) {
  const m = styles.match(
    new RegExp(`\\.table\\.tasks-table\\s+th:nth-child\\(${n}\\)\\s*\\{([^}]*)\\}`)
  )
  assert.ok(m, `styles.css 缺少第 ${n} 列宽度规则`)
  const w = m[1].match(/width\s*:\s*([\d.]+)(px|%)/)
  assert.ok(w, `第 ${n} 列规则缺少 width 声明`)
  return { value: parseFloat(w[1]), unit: w[2] }
}

function colPx(n) {
  const w = colWidth(n)
  assert.equal(w.unit, 'px', `第 ${n} 列应使用 px 固定宽度（当前 ${w.value}${w.unit}）`)
  return w.value
}

test('创建时间列（第 10 列）宽度 ≥ 165px，容纳 `2026-08-13 12:06:19` 单行显示', () => {
  const w = colPx(10)
  assert.ok(
    w >= 165,
    `创建时间列宽 ${w}px < 165px：19 字符时间戳（约 127px）加 td 左右 padding 20px 约需 147px，` +
      '列宽不足时日期与时间在空格处折成两行'
  )
})

test('用时列（第 11 列）宽度 ≥ 120px，容纳 `12 天 23 小时` 单行显示', () => {
  const w = colPx(11)
  assert.ok(
    w >= 120,
    `用时列宽 ${w}px < 120px：最长时长文案（约 85px）加 td 左右 padding 20px 约需 105px，` +
      '列宽不足时数字与单位在空格处折成两行'
  )
})

test('操作列（第 12 列）宽度 ≥ 145px，容纳「执行」+「重试中…」两按钮单行显示', () => {
  const w = colPx(12)
  assert.ok(
    w >= 145,
    `操作列宽 ${w}px < 145px：两个 btn-mini 按钮最坏情况（重试中… 4 字 + 8px 间距 + 执行）约 116px，` +
      '加 td 左右 padding 20px 约需 136px，列宽不足时两个按钮上下堆叠成两行'
  )
})

test('三列 td 设置 white-space: nowrap，列宽被压缩时也不在空格处折行', () => {
  // nowrap 规则可能合并为一条多选择器规则（如 10/11/12 共用），从首个
  // td:nth-child 选择器起匹配整条规则（含换行），再做选择器与声明两个维度的断言
  const rule = styles.match(/\.table\.tasks-table\s+td:nth-child\([\s\S]*?\{[^}]*\}/)
  assert.ok(rule, 'styles.css 应存在三列（10/11/12）td 的 nowrap 规则')
  const columns = [...rule[0].matchAll(/td:nth-child\((\d+)\)/g)].map((m) => Number(m[1]))
  for (const n of [10, 11, 12]) {
    assert.ok(columns.includes(n), `第 ${n} 列 td 缺少 white-space: nowrap 规则`)
  }
  assert.match(rule[0], /white-space\s*:\s*nowrap/, 'nowrap 规则应声明 white-space: nowrap')
})

test('12 列全部 px 固定宽度且总和 ≤ 1600px，宽视口下表格不超出容器（issue #28 第二轮回归保护）', () => {
  let total = 0
  for (let n = 1; n <= 12; n++) total += colPx(n)
  assert.ok(
    total <= 1600,
    `12 列宽度总和 ${total}px > 1600px：宽视口内容区上限为 1600px，总和超出会导致` +
      '表格比容器宽，水平滚动条重新出现（回归 issue #28 第二轮）'
  )
})

test('任务表格设置 min-width = 列宽总和，窄视口下保持横向滚动不折行', () => {
  const tableRule = styles.match(/\.table\.tasks-table\s*\{([^}]*)\}/)
  assert.ok(tableRule, 'styles.css 缺少 .table.tasks-table 规则')
  const m = tableRule[1].match(/min-width\s*:\s*(\d+)px/)
  assert.ok(m, '.table.tasks-table 应声明 min-width（px）')
  let total = 0
  for (let n = 1; n <= 12; n++) total += colPx(n)
  assert.equal(
    Number(m[1]),
    total,
    `min-width ${m[1]}px 应等于 12 列宽度总和 ${total}px：容器窄于总和时表格横向滚动，` +
      '各列保持完整宽度不折行；容器宽于总和时表格撑满容器'
  )
})
