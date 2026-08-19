// 复现测试（issue #27 第三轮）：设置页「Synology SSO 登录」配置卡片不可见。
//
// 背景：用户反馈「在设置页找不到 Synology SSO 服务器配置」。实测（1440×1000 视口）：
// 设置页共 8 个卡片、总高 3575px，首屏 1000px 只能看到「任务调度/界面显示/网页通知」
// 三个卡片（「网页通知」卡片高达 577px）；「Synology SSO 登录」卡片在第 1267px 处，
// 位于第 4 位——用户打开设置页首屏看不到 SSO 配置入口，误以为没有该功能。
// 修复目标：SSO 配置卡片移到设置页**顶部第一位**（首屏直接可见），
// 其余卡片顺序保持不变。
//
// issue #201 拆分后：卡片 JSX 移到 components/settings/*，页面顺序由
// Settings.jsx 组合层（section 锚点顺序 + 卡片挂载顺序）决定——
// 本测试改为断言 SSO 区块是页面第一个设置区块、卡片组件内首个卡片即 SSO。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const page = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const ssoCard = readFileSync(path.join(ROOT, 'src/components/settings/SsoCard.jsx'), 'utf8')

/** 按源码顺序提取页面全部设置区块 {id, pos}（pos 为 <section> 的行号） */
function extractSections(src) {
  const sections = []
  const re = /<section id="(settings-[a-z-]+)" className="settings-section"[^>]*>/g
  let m
  while ((m = re.exec(src)) !== null) {
    const pos = src.slice(0, m.index).split('\n').length
    sections.push({ id: m[1], pos })
  }
  return sections
}

test('「Synology SSO 登录」卡片是设置页第一个卡片（顶部可见，首屏不滚动即可见）', () => {
  const sections = extractSections(page)
  assert.ok(sections.length >= 16, `设置页应至少包含 16 个设置区块，实际 ${sections.length} 个`)
  const first = sections[0]
  assert.equal(
    first.id, 'settings-sso',
    `SSO 配置区块应是设置页第一个区块（当前第一个是「${first.id}」，SSO 在第 ${sections.findIndex((s) => s.id === 'settings-sso') + 1} 位）`,
  )
  // 卡片组件内的卡片标题仍是「Synology SSO 登录」
  assert.match(ssoCard, /<h2>Synology SSO 登录<\/h2>/, 'SsoCard 组件应渲染「Synology SSO 登录」卡片标题')
})

test('「Synology SSO 登录」卡片应位于「任务调度」等业务配置卡片之前', () => {
  const sections = extractSections(page)
  const sso = sections.find((s) => s.id === 'settings-sso')
  const task = sections.find((s) => s.id === 'settings-tasks')
  const notify = sections.find((s) => s.id === 'settings-notifications')
  assert.ok(sso, '缺少 settings-sso 区块')
  assert.ok(sso.pos === Math.min(...sections.map((s) => s.pos)), 'SSO 区块应在页面最顶部')
  if (task) assert.ok(sso.pos < task.pos, 'SSO 区块应在「任务调度」之前')
  if (notify) assert.ok(sso.pos < notify.pos, 'SSO 区块应在「网页通知」之前（当前被它压到首屏之外）')
  // 页面组合层挂载顺序：SsoCard 第一个，TasksCard / NotificationsCard 在其后
  const ssoMount = page.indexOf('<SsoCard')
  const taskMount = page.indexOf('<TasksCard')
  const notifyMount = page.indexOf('<NotificationsCard')
  assert.ok(ssoMount > -1 && ssoMount < taskMount && ssoMount < notifyMount,
    'Settings.jsx 应先挂载 SsoCard，再挂载 TasksCard / NotificationsCard')
})
