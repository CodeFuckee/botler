// 任务失败自动创建 GitLab issue 上报（issue #347）前后端源码断言：
// - 设置页「任务失败自动上报」卡片：启用开关 + 负责人输入 + 独立保存；
// - 后端：auto_issue notifier 插件（task_failed 事件分发创建 issue，
//   标签 bug+bot-failed 防对账重新领取）、settings API auto_issue 段与
//   校验、config auto_issue 段（enabled/assignee）、config.example.yaml
//   示例段。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const card = readFileSync(path.join(ROOT, 'src/components/settings/AutoIssueCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsPage = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const plugin = readFileSync(path.join(ROOT, '../backend/botler/plugins/auto_issue.py'), 'utf8')
const pluginInit = readFileSync(path.join(ROOT, '../backend/botler/plugins/__init__.py'), 'utf8')
const apiSettings = readFileSync(path.join(ROOT, '../backend/botler/api/settings.py'), 'utf8')
const config = readFileSync(path.join(ROOT, '../backend/botler/config.py'), 'utf8')
const configExample = readFileSync(path.join(ROOT, '../backend/config.example.yaml'), 'utf8')

test('设置页「任务失败自动上报」卡片：启用开关 + 负责人 + 独立保存', () => {
  assert.match(card, /任务失败自动上报/, '卡片标题应为「任务失败自动上报」')
  assert.match(card, /auto_issue\.enabled/, '应有启用开关')
  assert.match(card, /assignee/, '应有负责人输入')
  assert.match(card, /saveAutoIssue/, '保存按钮应经 saveAutoIssue 处理')
})

test('设置数据 hook：setAutoIssueField + saveAutoIssue 只提交 auto_issue 段', () => {
  assert.match(hook, /setAutoIssueField/, 'hook 应提供 setAutoIssueField')
  assert.match(hook, /saveAutoIssue/, 'hook 应提供 saveAutoIssue')
  assert.match(hook, /api\.put\('\/api\/settings', \{ auto_issue: \{ \.\.\.settings\.auto_issue \} \}\)/, '保存应只提交 auto_issue 段')
})

test('设置页挂载自动上报卡片区块', () => {
  assert.match(settingsPage, /AutoIssueCard/, 'Settings.jsx 应导入 AutoIssueCard')
  assert.match(settingsPage, /settings-auto-issue/, '应有 settings-auto-issue 区块')
})

test('后端 auto_issue 插件：task_failed 分发创建 issue（bug+bot-failed）', () => {
  assert.match(plugin, /class AutoIssueNotifierPlugin/, '应有 AutoIssueNotifierPlugin')
  assert.match(plugin, /send_task_failed/, '应实现 send_task_failed')
  assert.match(plugin, /create_issue/, '应调用 create_issue 创建上报 issue')
  assert.match(plugin, /AUTO_ISSUE_LABELS = \("bug", "bot-failed"\)/, '标签应为 bug + bot-failed（防对账重新领取）')
  assert.match(plugin, /register_plugin\(AutoIssueNotifierPlugin\(\)\)/, '模块导入即注册')
  assert.match(pluginInit, /import auto_issue as _auto_issue/, 'plugins 包应导入 auto_issue 触发注册')
})

test('后端 settings API：GET auto_issue 段 + _validate_auto_issue 校验', () => {
  assert.match(apiSettings, /"auto_issue": \{/, 'GET 应含 auto_issue 段')
  assert.match(apiSettings, /_validate_auto_issue/, '应校验 auto_issue 段')
  assert.match(apiSettings, /c\.config\.update_section\("auto_issue", auto_issue\)/, 'PUT 应写回 auto_issue 段')
})

test('后端 config：auto_issue 字段 + KNOWN_FIELDS 白名单 + yaml 解析', () => {
  assert.match(config, /auto_issue_enabled: bool = True/, 'Settings 应含 enabled 字段（默认开启）')
  assert.match(config, /auto_issue_assignee: str = "agent"/, 'Settings 应含 assignee 字段（默认 agent）')
  assert.match(config, /"auto_issue": \{"enabled", "assignee"\}/, 'KNOWN_FIELDS 应含 auto_issue 白名单')
  assert.match(config, /"auto_issue": SectionSchema\(fields=tuple\(KNOWN_FIELDS\["auto_issue"\]\)\)/, 'SECTION_SCHEMAS 应登记 auto_issue 段')
  assert.match(config, /auto_issue = data\.get\("auto_issue", \{\}\) or \{\}/, '_to_settings 应解析 auto_issue 段')
})

test('config.example.yaml：auto_issue 示例段', () => {
  assert.match(configExample, /auto_issue:/, '示例配置应含 auto_issue 段')
  assert.match(configExample, /enabled: true/, '示例段应含启用开关')
  assert.match(configExample, /assignee: agent/, '示例段应含负责人默认值')
})
