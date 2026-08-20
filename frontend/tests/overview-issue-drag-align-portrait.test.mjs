// 复现测试（issue #350）：竖屏页面（触屏设备 pointer: coarse）下概览页
// 「其他」分组 issue 列表拖动手柄图标与 issue 标题不在同一高度、图标偏上。
//
// 背景：issue #343 用 margin-top: calc((1.6em - 1em) / 2)（0.3em = 4.2px）
// 让手柄图标中心与标题行中心重合——但仅对桌面端（fine pointer）成立：
// 竖屏/触屏设备命中触控目标规则（.issue-link min-height: 44px），按钮盒
// 被撑高、按钮内文本垂直居中，标题文本中心从 11.2px 降至 22px，而手柄
// 图标中心仍停留在 11.2px——Chromium 实测（390×844 + hasTouch）图标中心
// y=627.7、标题行中心 y=638.5，偏差 10.8px，图标偏上。
//
// 修复目标（本文件断言）：
// 1. 桌面端保持 issue #343 的行高差对齐（margin-top: calc((1.6em - 1em)/2)）；
// 2. pointer: coarse 下手柄同样成为 44px 触控目标（min-height: 44px）且
//    margin-top 归零——图标（inline-flex 垂直居中）中心 22px 与标题文本
//    中心重合；桌面（fine pointer）不受影响。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

test('桌面端手柄保持行高差对齐，不被触屏规则覆盖（issue #343 不回归）', () => {
  // 基础规则（非 coarse 覆盖）：含 calc 的 margin-top 只存在于基础规则
  const rule = styles.match(/\.issue-drag-handle\s*\{[^}]*margin-top\s*:\s*calc\([^}]*\}/)
  assert.ok(rule, 'styles.css 缺少 .issue-drag-handle 基础规则（margin-top: calc）')
  assert.match(
    rule[0],
    /margin-top\s*:\s*calc\(\s*\(\s*1\.6em\s*-\s*1em\s*\)\s*\/\s*2\s*\)/,
    '.issue-drag-handle 基础规则应保持 margin-top: calc((1.6em - 1em) / 2)'
  )
})

test('触屏（pointer: coarse）下手柄归零 margin-top 并提升为 44px 触控目标（issue #350）', () => {
  // 仅断言 coarse 块内存在 .issue-drag-handle 覆盖规则（margin-top: 0 +
  // min-height: 44px），与标题按钮同高、图标垂直居中后与标题文本中心重合
  assert.match(
    styles,
    /@media \(pointer: coarse\)[\s\S]*?\.issue-item \.issue-drag-handle\s*\{[^}]*margin-top\s*:\s*0[^}]*min-height\s*:\s*44px[^}]*\}/,
    'pointer: coarse 块内应存在 .issue-item .issue-drag-handle { margin-top: 0;'
    + ' min-height: 44px; }（双类选择器优先级高于基础规则，避免被后声明的'
    + ' calc 覆盖）'
  )
})

test('触屏规则不改变手柄 inline-flex 居中布局与顶部对齐', () => {
  const baseRule = styles.match(/\.issue-drag-handle\s*\{[^}]*align-self[^}]*\}/)[0]
  assert.match(baseRule, /align-self\s*:\s*flex-start/, '手柄保持顶部对齐（对齐标题首行）')
  assert.match(baseRule, /display\s*:\s*inline-flex/, '手柄保持 inline-flex')
  assert.match(baseRule, /align-items\s*:\s*center/, '图标在手柄内垂直居中')
  assert.match(baseRule, /justify-content\s*:\s*center/, '图标在手柄内水平居中')
})
