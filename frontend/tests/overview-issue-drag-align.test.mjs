// 复现测试（issue #343）：概览页「其他」分组 issue 列表的拖动手柄图标
// （gripVertical）与 issue 标题不在同一高度、图标偏上。
//
// 背景：.issue-row 为 flex 布局（align-items: flex-start），拖动手柄
// （.issue-drag-handle，inline-flex，仅图标 1em=14px 高）顶部对齐；而
// issue 标题行（.issue-link）继承 body 的 line-height 1.6（14px 字号 →
// 行高 22.4px），文本垂直中心在行盒中心（0.8em）。手柄图标中心仅位于
// 顶部 + 0.5em 处，比标题行中心高半个行高差（0.3em = 3.2px）——
// Chromium 实测图标中心 y=410.8、标题行中心 y=414.0，偏差 3.2px。
//
// 修复目标（本文件断言）：
// 1. 手柄图标下移半个行高差，使图标中心与标题行中心重合
//    （margin-top: calc((1.6em - 1em) / 2)，按 em 计算随字号自适应）；
// 2. 手柄仍顶部对齐（align-self: flex-start）——图标始终对齐第一行
//    标题，issue 存在标签/里程碑副行时也不会漂移到整行居中位置；
// 3. 图标在手柄内垂直/水平居中（inline-flex + align-items/justify-content）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

test('拖动手柄应下移半个行高差，使图标中心与标题行中心重合（issue #343）', () => {
  const rule = styles.match(/\.issue-drag-handle\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .issue-drag-handle 样式规则')
  // 标题行 line-height 1.6em、图标 1em：图标需下移 (1.6em - 1em) / 2 = 0.3em，
  // 按 em 计算而非写死像素，字号/行高变化时依然对齐
  assert.match(
    rule[0],
    /margin-top\s*:\s*calc\(\s*\(\s*1\.6em\s*-\s*1em\s*\)\s*\/\s*2\s*\)/,
    '.issue-drag-handle 应 margin-top: calc((1.6em - 1em) / 2) 使图标中心'
    + '与标题行中心重合（修复前 margin-top: 1px，图标偏上 3.2px）'
  )
})

test('拖动手柄保持顶部对齐——有标签/里程碑副行时图标仍对齐标题行', () => {
  const rule = styles.match(/\.issue-drag-handle\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .issue-drag-handle 样式规则')
  // align-self: flex-start：图标锚定标题首行；若改为 center 会在 issue
  // 带标签/里程碑副行时漂移到整行居中，偏离标题
  assert.match(
    rule[0],
    /align-self\s*:\s*flex-start/,
    '.issue-drag-handle 应保持 align-self: flex-start（对齐标题首行而非整行居中）'
  )
})

test('图标在手柄内水平垂直居中（inline-flex 居中布局）', () => {
  const rule = styles.match(/\.issue-drag-handle\s*\{[^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .issue-drag-handle 样式规则')
  assert.match(rule[0], /display\s*:\s*inline-flex/, '手柄应为 inline-flex')
  assert.match(rule[0], /align-items\s*:\s*center/, '图标应在手柄内垂直居中')
  assert.match(rule[0], /justify-content\s*:\s*center/, '图标应在手柄内水平居中')
})
