// 复现测试（issue #142 反馈轮）：「界面显示」卡片「显示未启用项目」开关
// 取消勾选后没有保存按钮，无法保存设置。
//
// 背景：用户反馈「取消勾选后没有保存按钮，无法保存设置」。设置页全局
// 「保存」按钮位于上方「任务调度」卡片内，「界面显示」卡片在其下方，
// 卡片内只有开关与说明文字——用户修改开关后找不到保存入口，误以为无法保存。
// 修复目标：「界面显示」卡片内提供独立的「保存界面显示配置」按钮（只提交
// ui 段，后端 PUT /api/settings 支持部分更新，与 SSO 卡片 issue #27 /
// Webhook 卡片 issue #141 同模式），并更新卡片说明文字不再指向「上方保存」。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：界面显示卡片 JSX 移到 components/settings/UiCard.jsx，
// 保存处理函数收敛到 hooks/useSettingsData.js——静态断言跟随新文件
const uiCard = readFileSync(path.join(ROOT, 'src/components/settings/UiCard.jsx'), 'utf8')
const tasksCard = readFileSync(path.join(ROOT, 'src/components/settings/TasksCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settings = uiCard + '\n' + tasksCard + '\n' + hook

/** 提取指定标题卡片的源码片段（从该 card div 起到下一个 card div 前） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名箭头函数体源码（如 `const saveUi = async () => {...}`、`const buildUiPatch = () => {...}`），用于断言提交内容 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} = (?:async )?\\(\\) => \\{([\\s\\S]*?)\\n  \\}`)
  const m = src.match(re)
  return m ? m[1] : null
}

test('「界面显示」卡片内应有独立的保存按钮（用户反馈：取消勾选后没有保存按钮）', () => {
  const uiCard = cardSource(settings, '界面显示')
  assert.ok(uiCard, '设置页应存在「界面显示」配置卡片')

  assert.match(
    uiCard,
    /保存界面显示配置/,
    '「界面显示」卡片内应包含「保存界面显示配置」按钮文本（当前卡片内只有开关与说明，保存按钮在别处，用户找不到）',
  )
  assert.match(
    uiCard,
    /onClick=\{saveUi\}/,
    '「界面显示」卡片内的保存按钮应绑定独立的 saveUi 处理函数',
  )
})

test('saveUi 只提交 ui 段（部分更新，不覆盖 worker/claude 等其他设置）', () => {
  const body = fnBody(settings, 'saveUi')
  assert.ok(body, '应存在 saveUi 函数')

  assert.match(body, /api\.put\('\/api\/settings', \{ ui: buildUiPatch\(\) \}\)/,
    'saveUi 应只 PUT {ui: ...}（后端 PUT /api/settings 支持部分更新）')
  assert.doesNotMatch(body, /\bworker\b/, 'saveUi 不应携带 worker 字段')
  assert.doesNotMatch(body, /\bclaude\b/, 'saveUi 不应携带 claude 字段')
  assert.doesNotMatch(body, /\bnotifications\b/, 'saveUi 不应携带 notifications 字段')
  assert.doesNotMatch(body, /\bwebhook\b/, 'saveUi 不应携带 webhook 字段')
})

test('buildUiPatch 同时携带 timezone 与 show_disabled_repos（开关默认 true）', () => {
  const body = fnBody(settings, 'buildUiPatch')
  assert.ok(body, '应存在 buildUiPatch（ui 段构建函数，saveUi 与全局 save 共用）')

  assert.match(body, /show_disabled_repos: settings\.ui\?\.show_disabled_repos !== false/,
    'buildUiPatch 应提交 show_disabled_repos（未配置时按 true 处理，兼容旧配置）')
  assert.match(body, /timezone: settings\.ui\?\.timezone \|\| ''/,
    'buildUiPatch 应同时携带 timezone（与全局保存行为一致）')
})

test('「界面显示」卡片说明文字应指向卡片内保存按钮，不再指向「上方保存」', () => {
  const uiCard = cardSource(settings, '界面显示')
  assert.ok(uiCard, '设置页应存在「界面显示」配置卡片')
  assert.doesNotMatch(
    uiCard,
    /点击上方「保存」/,
    '「界面显示」卡片说明不应再提示「点击上方保存」（保存按钮在其他卡片，会误导用户）',
  )
  assert.match(uiCard, /点击下方「保存界面显示配置」/, '说明应提示点击卡片内的「保存界面显示配置」按钮')
})

test('全局「保存」按钮（任务调度卡片）仍然存在，其他设置不受影响', () => {
  const taskCard = cardSource(settings, '任务调度')
  assert.ok(taskCard, '设置页应存在「任务调度」卡片')
  assert.match(taskCard, /onClick=\{save\}/, '任务调度卡片应保留全局保存按钮')
})
