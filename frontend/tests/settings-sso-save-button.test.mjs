// 复现测试（issue #27 第四轮）：Synology SSO 配置卡片没有保存按钮。
//
// 背景：用户反馈「synology sso服务器配置没有保存按钮」。设置页的全局「保存」
// 按钮位于第二个「任务调度」卡片内，而「Synology SSO 登录」卡片（设置页第一个
// 卡片）内只有表单字段——用户首屏只看到 SSO 卡片，找不到保存按钮，误以为
// 无法保存 SSO 配置。
// 修复目标：SSO 卡片内提供独立的「保存 SSO 配置」按钮（只提交 sso 段，
// 后端 PUT /api/settings 支持部分更新），并更新卡片说明文字不再指向
// 别处的「上方保存」。
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

/** 提取具名箭头函数体源码（如 `const saveSso = async () => {...}`、`const buildSsoPatch = () => {...}`），用于断言提交内容 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} = (?:async )?\\(\\) => \\{([\\s\\S]*?)\\n  \\}`)
  const m = src.match(re)
  return m ? m[1] : null
}

test('「Synology SSO 登录」卡片内应有独立的保存按钮（用户反馈：SSO 配置没有保存按钮）', () => {
  const ssoCard = cardSource(settings, 'Synology SSO 登录')
  assert.ok(ssoCard, '设置页应存在「Synology SSO 登录」配置卡片')

  assert.match(
    ssoCard,
    /保存 SSO 配置/,
    'SSO 卡片内应包含「保存 SSO 配置」按钮文本（当前卡片内只有表单字段，保存按钮在下方「任务调度」卡片中，用户首屏找不到）',
  )
  assert.match(
    ssoCard,
    /onClick=\{saveSso\}/,
    'SSO 卡片内的保存按钮应绑定独立的 saveSso 处理函数',
  )
})

test('saveSso 只提交 sso 段（部分更新，不覆盖 worker/claude 等其他设置）', () => {
  const body = fnBody(settings, 'saveSso')
  assert.ok(body, '应存在 saveSso 函数')

  assert.match(body, /api\.put\('\/api\/settings', \{ sso: buildSsoPatch\(\) \}\)/,
    'saveSso 应只 PUT {sso: ...}（后端 PUT /api/settings 支持部分更新）')
  assert.doesNotMatch(body, /\bworker\b/, 'saveSso 不应携带 worker 字段')
  assert.doesNotMatch(body, /\bclaude\b/, 'saveSso 不应携带 claude 字段')
  assert.doesNotMatch(body, /\bnotifications\b/, 'saveSso 不应携带 notifications 字段')
})

test('client_secret 留空时 saveSso 不覆盖已配置凭据（保持现有凭据）', () => {
  const body = fnBody(settings, 'buildSsoPatch')
  assert.ok(body, '应存在 buildSsoPatch（sso 段构建函数，saveSso 与全局 save 共用）')

  assert.match(body, /if \(ssoSecretInput\.trim\(\)\) sso\.client_secret = ssoSecretInput\.trim\(\)/,
    'client_secret 留空 = 保持现有凭据（后端掩码不覆盖），非空才带上')
})

test('SSO 卡片说明文字应指向卡片内保存按钮，不再指向「上方保存」', () => {
  const ssoCard = cardSource(settings, 'Synology SSO 登录')
  assert.ok(ssoCard, '设置页应存在「Synology SSO 登录」配置卡片')
  assert.doesNotMatch(
    ssoCard,
    /点击上方「保存」/,
    'SSO 卡片说明不应再提示「点击上方保存」（上方没有保存按钮，会误导用户）',
  )
  assert.match(ssoCard, /点击下方「保存 SSO 配置」/, '说明应提示点击卡片内的「保存 SSO 配置」按钮')
})

test('全局「保存」按钮（任务调度卡片）仍然存在，其他设置不受影响', () => {
  const taskCard = cardSource(settings, '任务调度')
  assert.ok(taskCard, '设置页应存在「任务调度」卡片')
  assert.match(taskCard, /onClick=\{save\}/, '任务调度卡片应保留全局保存按钮')
})
