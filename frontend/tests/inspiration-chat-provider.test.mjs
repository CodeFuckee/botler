// 灵感 AI 对话供应商选择（issue #249）：顶部下拉、持久化切换与无配置引导。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
const component = readFileSync(path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')
const zh = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))

test('前端请求灵感供应商并持久化切换，发送时携带 provider', () => {
  assert.match(hook, /chatProviders/)
  assert.match(hook, /\/chat-providers/)
  assert.match(hook, /\/chat-provider/)
  assert.match(hook, /provider: chatProvider/)
  assert.match(hook, /setChatProvider/)
})

test('对话面板渲染供应商下拉并按 provider/model 标注', () => {
  assert.match(component, /chat-provider-select/)
  assert.match(component, /chatProviders\.map/)
  assert.match(component, /provider\.provider.*provider\.model|provider\.model.*provider\.provider/s)
  assert.match(component, /settings-ai-providers/)
})

test('未配置供应商显示设置页引导文案', () => {
  assert.equal(zh['overview.chatNoProviders'], '未配置 AI 供应商，请先前往设置页配置')
  assert.equal(zh['overview.chatGoSettings'], '前往设置')
})
