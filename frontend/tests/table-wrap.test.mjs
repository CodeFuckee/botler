// 复现测试（issue #28）：任务列表页面宽度缩小时，数据行内容超出白色底（.card 容器）。
//
// 背景：任务列表为 12 列表格，各列内容（仓库名/标题/失败原因/时间戳/提交等）的最小宽度
// 之和约 1306px；窄视口下 .table 的 min-content 宽度超过卡片可用宽度，表格撑破 .card，
// 数据行内容直接画出白色底（浏览器实测：1400px 视口溢出 266px，760px 视口超出视口 586px）。
// 修复目标：表格外层增加 .table-wrap 横向滚动容器（overflow-x: auto），
// 窄视口下表格在容器内横向滚动而非溢出卡片。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const tasks = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

test('styles.css 提供 .table-wrap 横向滚动容器，防止表格溢出卡片', () => {
  const rule = styles.match(/\.table-wrap\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .table-wrap 样式规则')
  assert.match(rule[0], /overflow-x\s*:\s*auto/, '.table-wrap 应设置 overflow-x: auto，使表格在窄视口下横向滚动')
})

test('Tasks.jsx 的任务表格被 .table-wrap 容器包裹', () => {
  assert.match(tasks, /<div className="table-wrap">/, '任务列表 <table> 应包裹在 <div className="table-wrap"> 内')
})

// 第二轮（用户反馈 2026-08-12 18:47）：页面宽度足够时任务页不应出现水平滚动条。
// 根因：.table 为 table-layout: auto，表格实际宽度被内容撑到 min-content（实测 1628px，
// ≥1600px 视口下 ellipsis 列各 520px），而 .content max-width 封顶 1600px、容器最多 1520px，
// 任何桌面视口下容器都装不下表格 → .table-wrap 必然出现水平滚动条（实测 1920px 视口溢出 108px、
// 1750px 溢出 348px，页面两侧却有空白）。
// 修复：任务表格启用 table-layout: fixed + 12 列显式宽度，表格宽度恒等于容器宽度，
// 宽视口下表格撑满容器不再滚动；列内长文本由 ellipsis 列截断。
test('任务表格启用 table-layout: fixed，表格宽度恒等于容器宽度，宽视口不再出现水平滚动条', () => {
  const rule = styles.match(/\.table\.tasks-table\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .table.tasks-table 规则')
  assert.match(rule[0], /table-layout\s*:\s*fixed/, '任务表格应使用 table-layout: fixed，使表格宽度恒等于容器宽度')
  assert.match(tasks, /className="table tasks-table"/, '任务列表 <table> 应带 tasks-table 类，以单独应用 fixed 布局，避免影响其他页面的 .table 表格')
})

test('任务表格 12 列均显式分配宽度，列宽受控不再被内容撑破', () => {
  const colRules = styles.match(/\.table\.tasks-table\s+th:nth-child\(\d+\)\s*\{[^}]*width\s*:[^}]*\}/g) || []
  assert.equal(colRules.length, 12, '应存在 12 条 .table.tasks-table th:nth-child(n) 列宽规则（#、仓库、Issue、标题、状态、尝试、来源、失败原因、提交、创建时间、用时、操作）')
})
