// fmtTime 时区复现测试（issue #14）：页面显示的任务创建时间与本机时区不一致。
//
// 背景：后端 SQLite datetime('now') 存 UTC（如 '2026-08-12 01:25:54'），
// 前端 fmtTime 应转换为配置/本机时区显示；当前实现原样拼接 ' UTC' 导致差 8 小时。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fmtTime, fmtDuration } from '../src/api.js'

test('fmtTime 将 UTC 时间按 Asia/Shanghai 转换（+8 小时）', () => {
  assert.equal(fmtTime('2026-08-12 01:25:54', 'Asia/Shanghai'), '2026-08-12 09:25:54')
})

test('fmtTime 空值返回占位符', () => {
  assert.equal(fmtTime(null), '—')
})

// ---- unix 秒时间戳（issue #271：会话过期时间 exp 展示）----

test('fmtTime 数字秒级时间戳 → 正常格式化（会话过期 exp）', () => {
  // 1785542400 = 2026-08-01 00:00:00 UTC
  assert.equal(fmtTime(1785542400, 'Asia/Shanghai'), '2026-08-01 08:00:00')
  assert.equal(fmtTime(1785542400, 'UTC'), '2026-08-01 00:00:00')
})

test('fmtTime 数字毫秒级时间戳原样使用（不重复乘 1000）', () => {
  assert.equal(fmtTime(1785542400000, 'UTC'), '2026-08-01 00:00:00')
})

test('fmtTime 非法数字兜底：NaN（falsy）→ 占位符，Infinity → 原样', () => {
  assert.equal(fmtTime(NaN), '—')
  assert.equal(fmtTime(Infinity), 'Infinity')
})

// ---- 任务执行时长 fmtDuration（issue #23：任务页面显示完成 issue 所用时长）----

const S = '2026-08-12 01:00:00' // UTC 基准时刻

test('fmtDuration：30 秒 → x 秒', () => {
  assert.equal(fmtDuration(S, '2026-08-12 01:00:30'), '30 秒')
})

test('fmtDuration：0 秒（起止相同）→ 0 秒', () => {
  assert.equal(fmtDuration(S, S), '0 秒')
})

test('fmtDuration：60 秒 → 1 分钟（向下取整）', () => {
  assert.equal(fmtDuration(S, '2026-08-12 01:01:00'), '1 分钟')
  assert.equal(fmtDuration(S, '2026-08-12 01:01:59'), '1 分钟')
})

test('fmtDuration：59 分钟 → x 分钟', () => {
  assert.equal(fmtDuration(S, '2026-08-12 01:59:59'), '59 分钟')
})

test('fmtDuration：1 小时整 → x 小时', () => {
  assert.equal(fmtDuration(S, '2026-08-12 02:00:00'), '1 小时')
})

test('fmtDuration：超过 1 小时带分钟 → x 小时 y 分钟', () => {
  assert.equal(fmtDuration(S, '2026-08-12 02:01:00'), '1 小时 1 分钟')
})

test('fmtDuration：25 小时 → 1 天 1 小时', () => {
  assert.equal(fmtDuration(S, '2026-08-13 02:00:00'), '1 天 1 小时')
})

test('fmtDuration：48 小时整 → 2 天', () => {
  assert.equal(fmtDuration(S, '2026-08-14 01:00:00'), '2 天')
})

test('fmtDuration：缺字段 → null', () => {
  assert.equal(fmtDuration(null, S), null)
  assert.equal(fmtDuration(S, null), null)
  assert.equal(fmtDuration(null, null), null)
})

test('fmtDuration：非法日期 → null', () => {
  assert.equal(fmtDuration('not-a-date', S), null)
  assert.equal(fmtDuration(S, 'not-a-date'), null)
})

test('fmtDuration：结束早于开始（时钟异常）→ null', () => {
  assert.equal(fmtDuration('2026-08-12 02:00:00', '2026-08-12 01:00:00'), null)
})
