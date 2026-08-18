// 复现测试（issue #292）：「网页通知」卡片只有开关与说明文字，没有独立的
// 保存按钮——用户修改开关后找不到保存入口，无法保存设置。
//
// 背景：用户反馈「设置里的网页通知增加一个保存按钮，现在无法保存设置」。
// 设置页全局「保存」按钮位于「任务调度」卡片内，「网页通知」卡片在其下方，
// 卡片内只有开关、浏览器授权、测试通知按钮——用户修改开关后找不到保存入口，
// 误以为无法保存（与 issue #27 SSO / #141 Webhook / #142 界面显示同款问题）。
// 修复目标：「网页通知」卡片内提供独立的「保存网页通知配置」按钮（只提交
// notifications 段，后端 PUT /api/settings 支持部分更新，与 SSO / Webhook /
// 界面显示卡片同模式），并更新卡片说明文字不再指向「上方保存」。
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

/** 提取具名箭头函数体源码（如 `const saveNotify = async () => {...}`），用于断言提交内容 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} = (?:async )?\\(\\) => \\{([\\s\\S]*?)\\n  \\}`)
  const m = src.match(re)
  return m ? m[1] : null
}

test('「网页通知」卡片内应有独立的保存按钮（用户反馈：现在无法保存设置）', () => {
  const card = cardSource(settings, '网页通知')
  assert.ok(card, '设置页应存在「网页通知」配置卡片')

  assert.match(
    card,
    /保存网页通知配置/,
    '「网页通知」卡片内应包含「保存网页通知配置」按钮文本（当前卡片内只有开关与说明，保存按钮在别处，用户找不到）',
  )
  assert.match(
    card,
    /onClick=\{saveNotify\}/,
    '「网页通知」卡片内的保存按钮应绑定独立的 saveNotify 处理函数',
  )
})

test('saveNotify 只提交 notifications 段（部分更新，不覆盖 worker/claude 等其他设置）', () => {
  const body = fnBody(settings, 'saveNotify')
  assert.ok(body, '应存在 saveNotify 函数')

  assert.match(body, /api\.put\('\/api\/settings', \{ notifications: \{ \.\.\.settings\.notifications \} \}\)/,
    'saveNotify 应只 PUT {notifications: ...}（后端 PUT /api/settings 支持部分更新）')
  assert.doesNotMatch(body, /\bworker\b/, 'saveNotify 不应携带 worker 字段')
  assert.doesNotMatch(body, /\bclaude\b/, 'saveNotify 不应携带 claude 字段')
  assert.doesNotMatch(body, /\bui\b/, 'saveNotify 不应携带 ui 字段')
  assert.doesNotMatch(body, /\bwebhook\b/, 'saveNotify 不应携带 webhook 字段')
})

test('「网页通知」卡片说明文字应指向卡片内保存按钮，不再指向「上方保存」', () => {
  const card = cardSource(settings, '网页通知')
  assert.ok(card, '设置页应存在「网页通知」配置卡片')
  assert.doesNotMatch(
    card,
    /点击上方「保存」/,
    '「网页通知」卡片说明不应再提示「点击上方保存」（保存按钮在其他卡片，会误导用户）',
  )
  assert.match(card, /点击下方「保存网页通知配置」/, '说明应提示点击卡片内的「保存网页通知配置」按钮')
})

test('全局「保存」按钮（任务调度卡片）仍然存在，其他设置不受影响', () => {
  const taskCard = cardSource(settings, '任务调度')
  assert.ok(taskCard, '设置页应存在「任务调度」卡片')
  assert.match(taskCard, /onClick=\{save\}/, '任务调度卡片应保留全局保存按钮')
})
