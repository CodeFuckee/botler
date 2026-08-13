// 复现测试（issue #31）：任务页面"尝试"列，尝试次数数值与"恢复"文字不在同一水平线上。
//
// 背景：Tasks.jsx 任务列表"尝试"列单元格内混排两样东西——纯文本数值 {t.attempt_count}
// （继承 body 14px / line-height 1.6）与恢复任务的 <span className="badge resume">恢复</span>
// （inline-block，font-size 11px，padding 1px 8px，border 1px，margin-left 6px）。
//
// 第一轮修复（vertical-align: middle）未生效的根因：.tasks-table 为 table-layout: fixed，
// "尝试"列（第 6 列）固定 width: 68px，td padding 10px → 内容区仅 48px；而数值（2 位约
// 16px）+ 空格（约 4px）+ margin-left 6px + badge（11px×2 字 + 8px×2 padding + 1px×2
// border = 40px）同行所需约 66px > 48px，inline-block badge 被整体折到第二行——
// 数字在上、恢复在下，vertical-align 只作用于同一行框，折行后完全无效。
// （真实 Chromium 1280 视口实测：数字中心 y=78，badge 中心 y=102，垂直中心差 24px。）
//
// 修复目标：
// 1. "尝试"列宽足够容纳「数值 + badge」同行显示（本文件 test 3）；
// 2. 同一行内 badge 与数值垂直居中对齐（vertical-align: middle，本文件 test 2）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const tasks = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

test('任务列表"尝试"列单元格内混排数值与"恢复"badge（bug 场景存在）', () => {
  // 尝试列 td：{t.attempt_count} 数字后紧跟 {t.resumed && (<span className="badge resume">恢复</span>)}
  const cell = tasks.match(/\{t\.attempt_count\}\s*\n?\s*\{t\.resumed\s*&&\s*\([\s\S]*?badge resume[\s\S]*?\)\}/)
  assert.ok(cell, 'Tasks.jsx"尝试"列应混排尝试次数数值与 badge.resume 恢复标记')
})

test('.badge.resume 应设置 vertical-align: middle，同行时与数值垂直居中对齐', () => {
  const rule = styles.match(/\.badge\.resume\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .badge.resume 样式规则')
  assert.match(
    rule[0],
    /vertical-align\s*:\s*middle/,
    '.badge.resume 为 inline-block 且带 padding/border，与数字文本同行混排时需 vertical-align: middle 才能垂直居中对齐（同 h1 .badge 的处理）'
  )
})

test('"尝试"列宽应足够容纳数值与"恢复"badge 同行显示（不折行）', () => {
  // 解析"尝试"列（第 6 列）宽度：.table.tasks-table th:nth-child(6) { width: Npx }
  const col = styles.match(/\.table\.tasks-table\s+th:nth-child\(6\)\s*\{[^}]*\}/)
  assert.ok(col, 'styles.css 缺少 .tasks-table 第 6 列（尝试列）宽度定义')
  const colWidth = Number(col[0].match(/width\s*:\s*([\d.]+)px/)[1])

  // 解析 td 水平 padding：.table th, .table td { padding: 9px 10px } → 左右各 10px
  const cellRule = styles.match(/\.table\s+th,\s*\.table\s+td\s*\{[^}]*\}/)
  assert.ok(cellRule, 'styles.css 缺少 .table th, .table td 样式规则')
  const padX = Number(cellRule[0].match(/padding\s*:\s*[\d.]+px\s+([\d.]+)px/)[1])

  // badge 同行所需宽度：
  //   数值 2 位（14px 数字实测约 15.6px，取 16px）+ 空格约 4px
  //   + margin-left 6px + badge 宽（11px 中文字等宽 ×2 + padding 8×2 + border 1×2 = 40px）
  //   ≈ 66px；内容区 = 列宽 - 2×padding，需 ≥ 64px（留少量余量）
  const needed = 64
  const contentWidth = colWidth - padX * 2
  assert.ok(
    contentWidth >= needed,
    `"尝试"列内容区 ${contentWidth}px < 同行所需 ${needed}px，` +
      '数值与"恢复"badge 会折行显示（数字在上、恢复在下），应加宽该列使两者同处一行'
  )
})
