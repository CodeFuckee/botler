// 复现测试（issue #33）：任务页面"操作"列，表头文字"操作"与列内容"执行"按钮的左边缘
// 没有竖直方向对齐。
//
// 背景：Tasks.jsx 任务列表操作列（第 12 列）单元格内容为
//   <Link className="btn btn-mini">执行</Link>，是单元格第一个（也是唯一）内容；
// 而 .btn-mini 带 margin-left: 8px（styles.css，为"详情"按钮与前置失败原因文字留 8px
// 间距而设计）。.table th, .table td 左右 padding 均为 10px、text-align: left，因此
//   「操作」文字左边缘 x = 列左边缘 + 10px
//   「执行」按钮左边缘 x = 列左边缘 + 10px + margin-left(8px)
// 两者相差 8px —— 表头与列内容左边缘不在同一竖线（用户截图：'操作'与'执行'的最左边
// 没有竖直方向对齐）。
//
// 修复目标（对方案中立）：
// 1. 操作列单元格第一个内容（执行按钮）的左外边距合计应为 0（本文件 test 2）；
// 2. "详情"按钮仍在失败原因文字之后，需保留与前置文字的水平间距（本文件 test 3，
//    防止修复操作列时顺带把详情按钮的间距也删掉）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const tasks = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

// 提取指定 className 中每个 class 在 styles.css 里定义的 margin-left 总和（px）。
// 值支持 px 字面量与 var(--space-N) token 引用（issue #111 间距 token 化，
// 引用值从 :root token 定义解析，语义与字面量等价）
function resolvePx(value) {
  const ref = value.trim().match(/^var\((--space-\d+)\)$/)
  if (ref) {
    const root = styles.match(/:root\s*\{([^}]*)\}/s)
    assert.ok(root, 'styles.css 缺少 :root token 定义')
    const v = root[1].match(new RegExp(`${ref[1]}:\\s*(\\d+)px`))
    assert.ok(v, `styles.css 缺少 ${ref[1]} token 定义`)
    return Number(v[1])
  }
  return parseFloat(value) || 0
}

function marginLeftSum(className) {
  let sum = 0
  for (const cls of className.split(/\s+/)) {
    const rule = styles.match(new RegExp(`\\.${cls}\\s*\\{([^}]*)\\}`))
    if (!rule) continue
    const body = rule[1]
    // margin-left 直取；margin 简写按标准语义取左外边距：
    // 1 值→四边相同；2 值→上下/左右；3 值→上/左右/下；4 值→上/右/下/左
    for (const m of body.matchAll(/(margin-left|margin)\s*:\s*([^;]+);/g)) {
      const parts = m[2].trim().split(/\s+/).map(resolvePx)
      const left = m[1] === 'margin-left'
        ? parts[0]
        : parts.length === 4 ? parts[3] : parts.length >= 2 ? parts[1] : parts[0]
      sum += left || 0
    }
  }
  return sum
}

test('任务列表操作列单元格内容为"执行"链接（bug 场景存在）', () => {
  // 操作列表头"操作"与单元格内"执行"链接：表头文字与按钮应左边缘对齐
  assert.match(tasks, /\{tr\('tasks\.actions'\)\}/, '操作列表头应经 t() 国际化')
  assert.equal(zhCN['tasks.actions'], '操作', '中文「操作」文案应保留')
  assert.match(
    tasks,
    /<td>\s*\n?\s*<Link to=\{`\/tasks\/\$\{t\.id\}\?live=1`\}[\s\S]*?>\{tr\('tasks\.run'\)\}<\/Link>/,
    '操作列单元格内容应为"执行"链接（经 t() 国际化）'
  )
  assert.equal(zhCN['tasks.run'], '执行', '中文「执行」文案应保留')
})

test('操作列"执行"按钮左外边距合计应为 0（与表头"操作"文字左边缘对齐）', () => {
  // 提取操作列执行链接的 className（如 btn btn-mini）
  const m = tasks.match(/<Link to=\{`\/tasks\/\$\{t\.id\}\?live=1`\}\s+className="([^"]+)"/)
  assert.ok(m, '操作列"执行"链接应带 className')
  const sum = marginLeftSum(m[1])
  assert.equal(
    sum,
    0,
    `操作列"执行"按钮的 class（${m[1]}）左外边距合计 ${sum}px ≠ 0：` +
      '按钮是单元格第一个内容，任何 margin-left 都会使按钮左边缘比表头文字右移，' +
      '导致"操作"与"执行"左边缘不在同一竖线'
  )
})

test('失败原因列"详情"按钮与前置文字之间应保留 ≥6px 水平间距（不受操作列修复影响）', () => {
  // "详情"按钮紧跟失败原因文字之后，.btn-mini 原设计 margin-left: 8px 即为该间距
  const m = tasks.match(/<button className="([^"]+)" onClick=\{\(\) => setDetailTask\(t\)\}>\{tr\('tasks\.detail'\)\}<\/button>/)
  assert.ok(m, '失败原因列应有"详情"按钮')
  const sum = marginLeftSum(m[1])
  assert.ok(
    sum >= 6,
    `"详情"按钮的 class（${m[1]}）左外边距合计 ${sum}px < 6px：` +
      '详情按钮在失败原因文字之后，间距过小会与文字贴在一起'
  )
})
