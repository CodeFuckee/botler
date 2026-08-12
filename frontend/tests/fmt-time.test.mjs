// fmtTime 时区复现测试（issue #14）：页面显示的任务创建时间与本机时区不一致。
//
// 背景：后端 SQLite datetime('now') 存 UTC（如 '2026-08-12 01:25:54'），
// 前端 fmtTime 应转换为配置/本机时区显示；当前实现原样拼接 ' UTC' 导致差 8 小时。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fmtTime } from '../src/api.js'

test('fmtTime 将 UTC 时间按 Asia/Shanghai 转换（+8 小时）', () => {
  assert.equal(fmtTime('2026-08-12 01:25:54', 'Asia/Shanghai'), '2026-08-12 09:25:54')
})

test('fmtTime 空值返回占位符', () => {
  assert.equal(fmtTime(null), '—')
})
