// gen-version.mjs 版本自增规则测试（issue #179 + issue #283）：
// 需求「修改一下版本自增到规则，逢100进一，意思就是1.0.99的下个版本号是
// 1.1.0，1.99.99的下一个版本号是2.0.0」；issue #283 补充修复「平台版本
// 已到 300+ 但高位版本号未加一」——逢百进位应对任意 ≥100 的值持续生效，
// 保证版本号无限自增、高位版本号同步加一。
//
// 进位规则（base-100 归一化）：
//   - patch 位逢百进位：patch ≥ 100 时按 100 整除进位到 minor、余数保留
//     （1.0.99 → 1.1.0；已超 99 的历史值同样进位，1.0.299 → 1.3.0、
//      1.0.310 → 1.3.11）；
//   - minor 位逢百进位：minor ≥ 100 时按 100 整除进位到 major、余数保留
//     （1.99.99 → 2.0.0；1.150.5 → 2.50.6）；
//   - major 位不设进位上限（99.99.99 → 100.0.0）；
//   - 非法/缺失输入沿用既有行为：从 1.0.0 重新开始自增（→ 1.0.1）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { nextVersion } from '../scripts/gen-version.mjs'

// ---- 普通自增（不触发进位）----

test('nextVersion：普通自增 1.0.0 → 1.0.1', () => {
  assert.equal(nextVersion('1.0.0'), '1.0.1')
})

test('nextVersion：普通自增 1.2.3 → 1.2.4', () => {
  assert.equal(nextVersion('1.2.3'), '1.2.4')
})

test('nextVersion：patch 98 → 99 不进位', () => {
  assert.equal(nextVersion('1.2.98'), '1.2.99')
})

// ---- 逢100进一：patch → minor ----

test('nextVersion：issue 示例 1.0.99 → 1.1.0（patch 进位到 minor）', () => {
  assert.equal(nextVersion('1.0.99'), '1.1.0')
})

test('nextVersion：minor 进位但未到 100：1.5.99 → 1.6.0', () => {
  assert.equal(nextVersion('1.5.99'), '1.6.0')
})

test('nextVersion：0 起始 0.0.99 → 0.1.0', () => {
  assert.equal(nextVersion('0.0.99'), '0.1.0')
})

// ---- 逐级进位：minor → major ----

test('nextVersion：issue 示例 1.99.99 → 2.0.0（minor 逐级进位到 major）', () => {
  assert.equal(nextVersion('1.99.99'), '2.0.0')
})

test('nextVersion：0.99.99 → 1.0.0', () => {
  assert.equal(nextVersion('0.99.99'), '1.0.0')
})

// ---- 边界：超过 99 的历史值 / 大数 ----

test('nextVersion：patch 已超 99 逢百进位（1.0.299 → 1.3.0，issue #283）', () => {
  assert.equal(nextVersion('1.0.299'), '1.3.0')
})

test('nextVersion：patch 到 200 逢百进位（1.0.199 → 1.2.0）', () => {
  assert.equal(nextVersion('1.0.199'), '1.2.0')
})

test('nextVersion：major 无进位上限（99.99.99 → 100.0.0）', () => {
  assert.equal(nextVersion('99.99.99'), '100.0.0')
})

// ---- issue #283：平台版本已到 300+，高位版本号必须同步加一 ----

test('nextVersion：300+ 平台版本高位加一（1.0.310 → 1.3.11）', () => {
  assert.equal(nextVersion('1.0.310'), '1.3.11')
})

test('nextVersion：patch 进位后 minor 随之到 100 逐级进位（1.99.150 → 2.0.51）', () => {
  assert.equal(nextVersion('1.99.150'), '2.0.51')
})

test('nextVersion：minor 已超 99 的历史值逢百进位（1.150.5 → 2.50.6）', () => {
  assert.equal(nextVersion('1.150.5'), '2.50.6')
})

test('nextVersion：patch 非整百的超 99 值保留余数进位（1.0.305 → 1.3.6）', () => {
  assert.equal(nextVersion('1.0.305'), '1.3.6')
})

// ---- 非法 / 缺失输入回退 ----

test('nextVersion：空字符串回退 1.0.1', () => {
  assert.equal(nextVersion(''), '1.0.1')
})

test('nextVersion：非数字/非三段式版本回退 1.0.1', () => {
  assert.equal(nextVersion('abc'), '1.0.1')
  assert.equal(nextVersion('1.2'), '1.0.1')
  assert.equal(nextVersion('1.2.x'), '1.0.1')
  assert.equal(nextVersion('1.0.99.9'), '1.0.1')
})

test('nextVersion：null / undefined 回退 1.0.1', () => {
  assert.equal(nextVersion(null), '1.0.1')
  assert.equal(nextVersion(undefined), '1.0.1')
})
