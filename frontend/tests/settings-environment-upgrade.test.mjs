// 设置页「本地环境检测」工具升级按钮（issue #465）前后端源码断言：
// - 前端：EnvironmentCard 对可升级版本（已安装 + 最新版本已知 + 非最新）
//   的工具行显示「升级」按钮（点击经 onUpgrade 处理，升级中禁用）；
//   升级成功/失败分别展示提示；升级调用 POST /api/environment/upgrade。
// - 后端：POST /api/environment/upgrade 按发布源分派升级
//   （npm install -g / pip install -U / gh release 二进制），成功调度重启，
//   失败转 HTTP 400。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const card = readFileSync(path.join(ROOT, 'src/components/settings/EnvironmentCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const apiEnv = readFileSync(path.join(ROOT, '../backend/botler/api/environment.py'), 'utf8')
const envModule = readFileSync(path.join(ROOT, '../backend/botler/environment.py'), 'utf8')

test('EnvironmentCard：仅可升级版本（已装+非最新）显示「升级」按钮', () => {
  assert.match(card, /upgradeEnvTool/, '升级点击应经 upgradeEnvTool 处理函数')
  assert.match(card, /upgradingKey/, '应有升级中状态（禁用/忙碌）')
  // 逻辑：已是最新（t.up_to_date）提前返回；否则落入可升级分支显示按钮
  assert.match(card, /if \(t\.up_to_date\) return <span className="ok-text">/,
    '已是最新时提前返回，不显示升级按钮')
  assert.match(card, /<button/, '可升级分支应渲染升级按钮')
  assert.match(card, /可升级/, '保留「可升级」提示')
  assert.match(card, /升级/, '按钮文案含「升级」')
  assert.match(card, /upgradeError/, '应有升级失败提示展示')
  assert.match(card, /upgradeNote/, '应有升级成功提示展示')
})

test('useSettingsData：upgradeEnvTool 调用 POST /api/environment/upgrade', () => {
  assert.match(hook, /upgradeEnvTool/, 'hook 应提供 upgradeEnvTool')
  assert.match(hook, /api\.post\('\/api\/environment\/upgrade', \{ key \}\)/,
    '升级应提交 {key} 到 POST /api/environment/upgrade')
  assert.match(hook, /upgradingKey/, 'hook 应有升级中状态')
  assert.match(hook, /服务正在自动重启/, '升级成功后应提示服务自动重启')
})

test('后端 API：POST /api/environment/upgrade 路由 + 重启调度', () => {
  assert.match(apiEnv, /@router\.post\("\/upgrade"\)/, '应有 POST /upgrade 路由')
  assert.match(apiEnv, /upgrade_tool\(key\)/, '应调用 environment.upgrade_tool')
  assert.match(apiEnv, /schedule_restart/, '成功后应调度服务重启')
  assert.match(apiEnv, /UpgradeError/, '升级失败应捕获 UpgradeError')
  assert.match(apiEnv, /HTTPException\(400/, '失败应转 HTTP 400')
})

test('后端 environment：upgrade_tool 按发布源分派升级', () => {
  assert.match(envModule, /class UpgradeError/, '应有 UpgradeError 异常')
  assert.match(envModule, /def upgrade_tool/, '应有 upgrade_tool 入口')
  assert.match(envModule, /npm.*install.*-g/, 'npm 工具走 npm install -g')
  assert.match(envModule, /pip.*install.*-U/, 'pypi 工具走 pip install -U')
  assert.match(envModule, /def schedule_restart/, '应有延迟重启调度函数')
  assert.match(envModule, /os\.execv/, '重启应为 os.execv 原地替换（与备份恢复同模式）')
})
