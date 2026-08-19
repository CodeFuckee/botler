// 聚合告警（issue #229）前后端源码断言：
// - 设置页「聚合告警」卡片：总开关 + 四类告警开关与阈值输入，独立保存；
// - 前端通知映射：alert_* 事件类型经现有网页通知通道弹系统通知；
// - 后端：alerts.py 检测模块（四类检测）、settings API alerts 段与校验、
//   config alerts 段（阈值可配置）、notifier.record_alert 节流、
//   webhook_push.send_alert 告警推送、database 统计方法。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const card = readFileSync(path.join(ROOT, 'src/components/settings/AlertsCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsPage = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const notify = readFileSync(path.join(ROOT, 'src/notify.js'), 'utf8')
const apiSettings = readFileSync(path.join(ROOT, '../backend/botler/api/settings.py'), 'utf8')
const config = readFileSync(path.join(ROOT, '../backend/botler/config.py'), 'utf8')
const alerts = readFileSync(path.join(ROOT, '../backend/botler/alerts.py'), 'utf8')
const notifier = readFileSync(path.join(ROOT, '../backend/botler/notifier.py'), 'utf8')
const webhookPush = readFileSync(path.join(ROOT, '../backend/botler/webhook_push.py'), 'utf8')
const database = readFileSync(path.join(ROOT, '../backend/botler/database.py'), 'utf8')

test('设置页「聚合告警」卡片：四类告警开关与阈值输入', () => {
  assert.match(card, /聚合告警/, '卡片标题应为「聚合告警」')
  assert.match(card, /alerts\.enabled/, '应有聚合告警总开关')
  assert.match(card, /failure_rate_threshold/, '应有失败率阈值输入')
  assert.match(card, /queue_backlog_threshold/, '应有队列堆积阈值输入')
  assert.match(card, /queue_stall_minutes/, '应有无进度判定分钟输入')
  assert.match(card, /disk_min_free_mb/, '应有磁盘剩余阈值输入')
  assert.match(card, /saveAlerts/, '保存按钮应经 saveAlerts 处理')
})

test('设置数据 hook：setAlertField + saveAlerts 只提交 alerts 段', () => {
  assert.match(hook, /setAlertField/, 'hook 应提供 setAlertField')
  assert.match(hook, /saveAlerts/, 'hook 应提供 saveAlerts')
  assert.match(hook, /api\.put\('\/api\/settings', \{ alerts: \{ \.\.\.settings\.alerts \} \}\)/, '保存应只提交 alerts 段')
})

test('设置页挂载告警卡片区块', () => {
  assert.match(settingsPage, /AlertsCard/, 'Settings.jsx 应导入 AlertsCard')
  assert.match(settingsPage, /settings-alerts/, '应有 settings-alerts 区块')
})

test('前端通知映射：alert_* 事件类型可弹网页通知', () => {
  assert.match(notify, /alert_failure_rate: 'alert_failure_rate'/, '失败率告警事件映射')
  assert.match(notify, /alert_queue_backlog: 'alert_queue_backlog'/, '队列堆积告警事件映射')
  assert.match(notify, /alert_token_invalid: 'alert_token_invalid'/, 'token 失效告警事件映射')
  assert.match(notify, /alert_disk_low: 'alert_disk_low'/, '磁盘空间告警事件映射')
})

test('后端 settings API：GET alerts 段 + _validate_alerts 校验', () => {
  assert.match(apiSettings, /"alerts": \{/, 'GET 应含 alerts 段')
  assert.match(apiSettings, /_validate_alerts/, '应校验 alerts 段')
  assert.match(apiSettings, /failure_rate_threshold/, 'GET alerts 应含失败率阈值')
})

test('后端 config：alerts 字段 + KNOWN_FIELDS 白名单 + yaml 解析', () => {
  assert.match(config, /alerts_enabled: bool = True/, 'Settings 应含总开关字段')
  assert.match(config, /alert_failure_rate_threshold: float = 50\.0/, 'Settings 应含失败率阈值字段（默认 50%）')
  assert.match(config, /"alerts": \{/, 'KNOWN_FIELDS 应含 alerts 白名单')
  assert.match(config, /"alerts": SectionSchema\(fields=tuple\(KNOWN_FIELDS\["alerts"\]\)\)/, 'SECTION_SCHEMAS 应登记 alerts 段')
  assert.match(config, /alerts = data\.get\("alerts", \{\}\) or \{\}/, '_to_settings 应解析 alerts 段')
})

test('后端 alerts.py：AlertChecker 四类检测', () => {
  assert.match(alerts, /class AlertChecker/, '应有 AlertChecker')
  for (const m of ['_check_failure_rate', '_check_queue_backlog', '_check_token', '_check_disk']) {
    assert.match(alerts, new RegExp(`def ${m}`), `应有 ${m} 方法`)
  }
})

test('后端 notifier：record_alert + 告警事件类型常量', () => {
  assert.match(notifier, /def record_alert/, '应有 record_alert 节流记录')
  for (const t of ['ALERT_FAILURE_RATE', 'ALERT_QUEUE_BACKLOG', 'ALERT_TOKEN_INVALID', 'ALERT_DISK_LOW']) {
    assert.match(notifier, new RegExp(`${t} = "alert_`), `应有 ${t} 常量`)
  }
})

test('后端 webhook_push：send_alert 告警推送', () => {
  assert.match(webhookPush, /def send_alert/, '应有 send_alert')
  assert.match(webhookPush, /"type": "alert"/, 'payload 应含 type=alert')
})

test('后端数据库：失败率统计 / 无进度判定 / 告警节流查询', () => {
  assert.match(database, /def task_failure_stats/, '应有失败率统计方法')
  assert.match(database, /def count_terminal_since/, '应有窗口内终态任务统计')
  assert.match(database, /def last_alert_notification/, '应有全局告警节流查询')
})
