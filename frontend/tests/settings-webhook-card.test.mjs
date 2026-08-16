// 消息推送 Webhook 配置卡片测试（issue #136）：任务完成时调用 webhook
// 进行消息推送，可在设置页面配置：
//   1. webhook 地址
//   2. Content-Type
//   3. Authorization
//   4. POST 结构体（可使用全局模板中的占位符，请求时自动填充）
//
// 本测试断言：
// 1. 设置页挂载「消息推送 Webhook」卡片（网页通知卡片之后）；
// 2. 卡片提供启用开关 / 地址 / Content-Type / Authorization / POST 结构体；
// 3. 保存走 PUT /api/settings 的 webhook 段；Authorization 留空 = 保持现有；
// 4. POST 结构体说明展示全局模板占位符（templates.placeholders）；
// 5. 「发送测试推送」按钮调用 POST /api/settings/webhook-test。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

test('设置页挂载「消息推送 Webhook」卡片', () => {
  assert.match(settings, /<h2>消息推送 Webhook<\/h2>/, '应有卡片标题')
})

test('「消息推送 Webhook」卡片位于「网页通知」卡片之后', () => {
  const notifyPos = settings.search(/<h2>网页通知<\/h2>/)
  const webhookPos = settings.search(/<h2>消息推送 Webhook<\/h2>/)
  assert.ok(notifyPos > 0, '应有网页通知卡片')
  assert.ok(webhookPos > notifyPos, 'webhook 卡片应在网页通知卡片之后')
})

test('卡片字段齐全：启用 / 地址 / Content-Type / Authorization / POST 结构体', () => {
  assert.match(settings, /webhook\.enabled/, '应有启用开关（webhook.enabled）')
  assert.match(settings, /placeholder="https:\/\/example\.com\/webhook\/botler"/, '应有 webhook 地址输入框')
  assert.match(settings, /Content-Type <code>content_type<\/code>/, '应有 Content-Type 输入')
  assert.match(settings, /type="password"/, 'Authorization 应为密码输入框')
  assert.match(settings, /POST 结构体 <code>body_template<\/code>/, '应有 POST 结构体模板编辑区')
  assert.match(settings, /className="input textarea"/, 'POST 结构体应为 textarea')
})

test('保存走 PUT /api/settings 的 webhook 段，Authorization 留空 = 保持现有', () => {
  assert.match(settings, /webhook: buildWebhookPatch\(\)/, '全局保存应提交 webhook 段')
  assert.match(settings, /留空 = 保持现有凭据/, 'Authorization 输入框应提示留空保持现有')
  assert.match(settings, /if \(webhookAuthInput\.trim\(\)\) wh\.authorization/, '仅在输入非空时提交 authorization')
})

test('POST 结构体说明展示全局模板占位符（templates.placeholders）', () => {
  assert.match(settings, /settings\.templates\?\.placeholders/, '占位符说明应遍历 templates.placeholders（可选链兼容旧测试 mock 缺字段）')
  assert.match(settings, /请求时自动填充/, '应说明占位符请求时自动填充')
})

test('「发送测试推送」按钮调用 POST /api/settings/webhook-test', () => {
  assert.match(settings, /发送测试推送/, '应有测试推送按钮')
  assert.match(settings, /api\.post\('\/api\/settings\/webhook-test'\)/, '测试推送应调用后端测试端点')
  assert.match(settings, /webhookTestNote/, '应有测试结果提示')
})
