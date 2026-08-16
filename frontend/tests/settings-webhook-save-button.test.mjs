// 复现测试（issue #141）：消息推送 Webhook 配置卡片没有保存按钮。
//
// 背景：用户反馈「消息推送 Webhook设置没有保存按钮，无法保存设置」。
// 设置页的全局「保存」按钮位于上方「任务调度」卡片内，而「消息推送
// Webhook」卡片位于页面下方，卡片内只有表单字段与「发送测试推送」——
// 用户滚动到该卡片修改配置后找不到保存入口，误以为无法保存。
// 修复目标：Webhook 卡片内提供独立的「保存 Webhook 配置」按钮（只提交
// webhook 段，后端 PUT /api/settings 支持部分更新，与 SSO 卡片同模式），
// 并更新卡片说明文字不再指向别处的「上方保存」。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

/** 提取指定标题卡片的源码片段（从该 card div 起到下一个 card div 前） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名箭头函数体源码（如 `const saveWebhook = async () => {...}`、`const buildWebhookPatch = () => {...}`），用于断言提交内容 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} = (?:async )?\\(\\) => \\{([\\s\\S]*?)\\n  \\}`)
  const m = src.match(re)
  return m ? m[1] : null
}

test('「消息推送 Webhook」卡片内应有独立的保存按钮（用户反馈：Webhook 设置没有保存按钮）', () => {
  const whCard = cardSource(settings, '消息推送 Webhook')
  assert.ok(whCard, '设置页应存在「消息推送 Webhook」配置卡片')

  assert.match(
    whCard,
    /保存 Webhook 配置/,
    'Webhook 卡片内应包含「保存 Webhook 配置」按钮文本（当前卡片内只有表单字段与测试推送按钮，保存按钮在别处，用户找不到）',
  )
  assert.match(
    whCard,
    /onClick=\{saveWebhook\}/,
    'Webhook 卡片内的保存按钮应绑定独立的 saveWebhook 处理函数',
  )
})

test('saveWebhook 只提交 webhook 段（部分更新，不覆盖 worker/claude 等其他设置）', () => {
  const body = fnBody(settings, 'saveWebhook')
  assert.ok(body, '应存在 saveWebhook 函数')

  assert.match(body, /api\.put\('\/api\/settings', \{ webhook: buildWebhookPatch\(\) \}\)/,
    'saveWebhook 应只 PUT {webhook: ...}（后端 PUT /api/settings 支持部分更新）')
  assert.doesNotMatch(body, /\bworker\b/, 'saveWebhook 不应携带 worker 字段')
  assert.doesNotMatch(body, /\bclaude\b/, 'saveWebhook 不应携带 claude 字段')
  assert.doesNotMatch(body, /\bnotifications\b/, 'saveWebhook 不应携带 notifications 字段')
})

test('authorization 留空时 saveWebhook 不覆盖已配置凭据（保持现有凭据）', () => {
  const body = fnBody(settings, 'buildWebhookPatch')
  assert.ok(body, '应存在 buildWebhookPatch（webhook 段构建函数，saveWebhook 与全局 save 共用）')

  assert.match(body, /if \(webhookAuthInput\.trim\(\)\) wh\.authorization = webhookAuthInput\.trim\(\)/,
    'authorization 留空 = 保持现有凭据（后端掩码不覆盖），非空才带上')
})

test('Webhook 卡片说明文字应指向卡片内保存按钮，不再指向「上方保存」', () => {
  const whCard = cardSource(settings, '消息推送 Webhook')
  assert.ok(whCard, '设置页应存在「消息推送 Webhook」配置卡片')
  assert.doesNotMatch(
    whCard,
    /点击上方「保存」/,
    'Webhook 卡片说明不应再提示「点击上方保存」（保存按钮在其他卡片，会误导用户）',
  )
  assert.match(whCard, /点击下方「保存 Webhook 配置」/, '说明应提示点击卡片内的「保存 Webhook 配置」按钮')
})

test('全局「保存」按钮（任务调度卡片）仍然存在，其他设置不受影响', () => {
  const taskCard = cardSource(settings, '任务调度')
  assert.ok(taskCard, '设置页应存在「任务调度」卡片')
  assert.match(taskCard, /onClick=\{save\}/, '任务调度卡片应保留全局保存按钮')
})
