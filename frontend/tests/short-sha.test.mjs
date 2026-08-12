// shortSha 提交号短显示测试（issue #19）：任务页面 commit 链接的展示文本。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { shortSha } from '../src/api.js'

test('shortSha 完整 sha 截断为前 8 位', () => {
  assert.equal(shortSha('deadbeef000111222333444555666777888999aa'), 'deadbeef')
})

test('shortSha 8 位以内原样返回', () => {
  assert.equal(shortSha('abc12345'), 'abc12345')
  assert.equal(shortSha('abc'), 'abc')
})

test('shortSha 空值返回占位符', () => {
  assert.equal(shortSha(null), '—')
  assert.equal(shortSha(''), '—')
  assert.equal(shortSha(undefined), '—')
})

test('shortSha 非字符串输入返回占位符', () => {
  assert.equal(shortSha(12345), '—')
})
