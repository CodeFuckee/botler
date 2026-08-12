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
