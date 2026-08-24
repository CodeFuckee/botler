// 设置页「本地环境检测」工具安装按钮（issue #468）前后端源码断言：
// - 前端：EnvironmentCard 对「未安装且可自动安装」（installable）的工具行
//   显示「安装」按钮（点击经 installEnvTool 处理，安装中禁用）；
//   安装成功/失败分别展示提示；安装调用 POST /api/environment/install。
// - 后端：POST /api/environment/install 按发布源分派安装
//   （npm install -g / pip install / gh release 二进制），成功调度重启，
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

test('EnvironmentCard：未安装且可自动安装的工具显示「安装」按钮', () => {
  assert.match(card, /installEnvTool/, '安装点击应经 installEnvTool 处理函数')
  assert.match(card, /installingKey/, '应有安装中状态（禁用/忙碌）')
  // 逻辑：未安装时按 t.installable 判定是否渲染安装按钮
  assert.match(card, /t\.installable/, '应以 installable 判定是否可安装')
  assert.match(card, /安装中…/, '安装中应显示忙碌文案')
  assert.match(card, /安装/, '按钮文案含「安装」')
  assert.match(card, /installError/, '应有安装失败提示展示')
  assert.match(card, /installNote/, '应有安装成功提示展示')
})

test('useSettingsData：installEnvTool 调用 POST /api/environment/install', () => {
  assert.match(hook, /installEnvTool/, 'hook 应提供 installEnvTool')
  assert.match(hook, /api\.post\('\/api\/environment\/install', \{ key \}\)/,
    '安装应提交 {key} 到 POST /api/environment/install')
  assert.match(hook, /installingKey/, 'hook 应有安装中状态')
  assert.match(hook, /服务正在自动重启/, '安装成功后应提示服务自动重启')
})

test('后端 API：POST /api/environment/install 路由 + 重启调度', () => {
  assert.match(apiEnv, /@router\.post\("\/install"\)/, '应有 POST /install 路由')
  assert.match(apiEnv, /install_tool\(key\)/, '应调用 environment.install_tool')
  assert.match(apiEnv, /schedule_restart/, '成功后应调度服务重启')
  assert.match(apiEnv, /InstallError/, '安装失败应捕获 InstallError')
  assert.match(apiEnv, /HTTPException\(400/, '失败应转 HTTP 400')
})

test('后端 environment：install_tool 按发布源分派安装', () => {
  assert.match(envModule, /class InstallError/, '应有 InstallError 异常')
  assert.match(envModule, /def install_tool/, '应有 install_tool 入口')
  assert.match(envModule, /def _install_command/, '应有安装命令构造函数')
  assert.match(envModule, /def _install_gh/, '应有 gh 二进制安装函数')
  assert.match(envModule, /GH_INSTALL_DIR/, 'gh 应有安装目标目录')
  assert.match(envModule, /installable/, '检测结果应含 installable 标记')
})
