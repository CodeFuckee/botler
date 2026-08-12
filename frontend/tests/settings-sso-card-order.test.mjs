// 复现测试（issue #27 第三轮）：设置页「Synology SSO 登录」配置卡片不可见。
//
// 背景：用户反馈「在设置页找不到 Synology SSO 服务器配置」。实测（1440×1000 视口）：
// 设置页共 8 个卡片、总高 3575px，首屏 1000px 只能看到「任务调度/界面显示/网页通知」
// 三个卡片（「网页通知」卡片高达 577px）；「Synology SSO 登录」卡片在第 1267px 处，
// 位于第 4 位——用户打开设置页首屏看不到 SSO 配置入口，误以为没有该功能。
// 修复目标：SSO 配置卡片移到设置页**顶部第一位**（首屏直接可见），
// 其余卡片顺序保持不变。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

/** 按源码顺序提取所有设置卡片：{title, pos}（pos 为 <div className="card"> 的行号） */
function extractCards(src) {
  const cards = []
  const re = /<div className="card">\s*<h2>([^<]+)<\/h2>/g
  let m
  while ((m = re.exec(src)) !== null) {
    const pos = src.slice(0, m.index).split('\n').length
    cards.push({ title: m[1], pos })
  }
  return cards
}

test('「Synology SSO 登录」卡片是设置页第一个卡片（顶部可见，首屏不滚动即可见）', () => {
  const cards = extractCards(settings)
  assert.ok(cards.length >= 4, `设置页应至少包含 4 个配置卡片，实际 ${cards.length} 个`)

  const sso = cards.find((c) => c.title === 'Synology SSO 登录')
  assert.ok(sso, '设置页应存在「Synology SSO 登录」配置卡片')

  const first = cards[0]
  assert.equal(
    first.title, 'Synology SSO 登录',
    `SSO 配置卡片应是设置页第一个卡片（当前第一个是「${first.title}」，SSO 在第 ${cards.indexOf(sso) + 1} 位）`,
  )
})

test('「Synology SSO 登录」卡片应位于「任务调度」等业务配置卡片之前', () => {
  const cards = extractCards(settings)
  const sso = cards.find((c) => c.title === 'Synology SSO 登录')
  const task = cards.find((c) => c.title === '任务调度')
  const notify = cards.find((c) => c.title === '网页通知')
  assert.ok(sso, '缺少 SSO 卡片')
  if (task) assert.ok(sso.pos < task.pos, 'SSO 卡片应在「任务调度」之前')
  if (notify) assert.ok(sso.pos < notify.pos, 'SSO 卡片应在「网页通知」之前（当前被它压到首屏之外）')
})
