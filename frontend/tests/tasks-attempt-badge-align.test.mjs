// 复现测试（issue #31）：任务页面"尝试"列，尝试次数数值与"恢复"文字不在同一水平线上。
//
// 背景：Tasks.jsx 任务列表"尝试"列单元格内混排两样东西——纯文本数值 {t.attempt_count}
// （继承 body 14px / line-height 1.6）与恢复任务的 <span className="badge resume">恢复</span>
// （inline-block，font-size 11px，padding 1px 8px，border 1px）。.badge 默认
// vertical-align: baseline：badge 的文字基线与数字基线对齐，但其上下 padding + border
// 使胶囊底边下沉到基线以下，且 11px 小字视觉中心低于 14px 数字中心，
// 两者不在同一水平线上。h1 .badge 已有 vertical-align: middle 处理同类问题，
// 唯独"尝试"列的 .badge.resume 遗漏。
// 修复目标：.badge.resume 增加 vertical-align: middle，使胶囊与数值垂直居中对齐。
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

test('.badge.resume 应设置 vertical-align: middle，与数值在同一水平线上', () => {
  const rule = styles.match(/\.badge\.resume\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .badge.resume 样式规则')
  assert.match(
    rule[0],
    /vertical-align\s*:\s*middle/,
    '.badge.resume 为 inline-block 且带 padding/border，与数字文本混排时需 vertical-align: middle 才能与数值在同一水平线上（同 h1 .badge 的处理）'
  )
})
