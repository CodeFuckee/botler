// summarizeToolInput 工具调用摘要测试（issue #20）：实时执行面板中
// 工具调用（tool_use）输入的一行式展示文本。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { summarizeToolInput } from '../src/api.js'

test('Bash 命令提取 command 字段', () => {
  assert.equal(
    summarizeToolInput({ command: 'git status' }, 'Bash'),
    '$ git status',
  )
})

test('非命令对象序列化为单行 JSON', () => {
  assert.equal(
    summarizeToolInput({ path: 'a.py', line: 3 }, 'Read'),
    '{"path":"a.py","line":3}',
  )
})

test('字符串输入原样截断', () => {
  assert.equal(summarizeToolInput('hello', 'Read'), 'hello')
})

test('长命令截断到 120 字符', () => {
  const long = 'x'.repeat(200)
  const s = summarizeToolInput({ command: long }, 'Bash')
  assert.equal(s.length, 121) // '$ ' 前缀 + 118 字符 + '…'
  assert.ok(s.endsWith('…'))
})

test('空值返回占位符', () => {
  assert.equal(summarizeToolInput(null, 'Bash'), '—')
  assert.equal(summarizeToolInput(undefined, 'Bash'), '—')
  assert.equal(summarizeToolInput('', 'Bash'), '—')
})

test('非对象非字符串输入转字符串', () => {
  assert.equal(summarizeToolInput(123, 'Bash'), '123')
  assert.equal(summarizeToolInput(['a', 'b'], 'Bash'), '["a","b"]')
})
