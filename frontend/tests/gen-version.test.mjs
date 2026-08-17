// gen-version.mjs 版本自增规则测试（issue #179）：
// 需求「修改一下版本自增到规则，逢100进一，意思就是1.0.99的下个版本号是
// 1.1.0，1.99.99的下一个版本号是2.0.0」。
//
// 进位规则：
//   - patch 位自增到 100 时向 minor 进位（patch 归零、minor +1）；
//   - minor 位随之到 100 时再向 major 进位（minor 归零、major +1）；
//   - major 位不设进位上限（99.99.99 → 100.0.0）；
//   - 已超过 99 的历史版本号不做回写修正，仅对后续自增生效
//     （1.0.299 → 1.0.300，patch 300 不是 100，不进位）；
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

test('nextVersion：已超 99 的历史值仅 +1，不再进位（1.0.299 → 1.0.300）', () => {
  assert.equal(nextVersion('1.0.299'), '1.0.300')
})

test('nextVersion：非 100 整数倍的大 patch 不进位（1.0.199 → 1.0.200）', () => {
  assert.equal(nextVersion('1.0.199'), '1.0.200')
})

test('nextVersion：major 无进位上限（99.99.99 → 100.0.0）', () => {
  assert.equal(nextVersion('99.99.99'), '100.0.0')
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
