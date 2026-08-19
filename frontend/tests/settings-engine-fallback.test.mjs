// 执行引擎健康探测与自动降级重试（issue #236）前端/后端源码断言：
// - 设置页「任务调度」卡片：备用引擎配置（fallback_engines /
//   fallback_after_failures）+ 引擎健康状态徽章（engine_health）；
// - 插件管理页：执行引擎卡片状态徽章（见 plugins-page.test.mjs）；
// - 任务详情：执行引擎 + 降级原因（engine_fallback）；
// - 后端：settings/plugins/tasks API 字段、config 字段、数据库列。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tasksCard = readFileSync(path.join(ROOT, 'src/components/settings/TasksCard.jsx'), 'utf8')
const settingsHook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const drawer = readFileSync(path.join(ROOT, 'src/components/TaskDetailDrawer.jsx'), 'utf8')
const taskDetail = readFileSync(path.join(ROOT, 'src/pages/TaskDetail.jsx'), 'utf8')
const apiSettings = readFileSync(path.join(ROOT, '../backend/botler/api/settings.py'), 'utf8')
const apiTasks = readFileSync(path.join(ROOT, '../backend/botler/api/tasks.py'), 'utf8')
const config = readFileSync(path.join(ROOT, '../backend/botler/config.py'), 'utf8')
const database = readFileSync(path.join(ROOT, '../backend/botler/database.py'), 'utf8')
const engineHealth = readFileSync(path.join(ROOT, '../backend/botler/engine_health.py'), 'utf8')

test('设置页「任务调度」卡片：备用引擎配置输入 + 引擎健康状态徽章', () => {
  assert.match(tasksCard, /worker\.fallback_engines/, '卡片应有备用引擎配置项')
  assert.match(tasksCard, /setFallbackEngines/, '备用引擎输入应经 setFallbackEngines 处理')
  assert.match(tasksCard, /engine_health/, '卡片应展示引擎健康状态（engine_health 数据源）')
  assert.match(tasksCard, /engine-health-badge/, '健康状态应使用徽章样式')
  assert.match(tasksCard, /正常/, '正常引擎徽章文案')
  assert.match(tasksCard, /异常/, '异常引擎徽章文案')
})

test('设置数据 hook：FIELD_LABELS 含降级阈值，保存提交 fallback_engines', () => {
  assert.match(settingsHook, /fallback_after_failures:\s*'降级触发失败次数'/, '数字字段应有中文标签')
  assert.match(settingsHook, /setFallbackEngines/, 'hook 应提供 setFallbackEngines')
  assert.match(settingsHook, /worker\.fallback_engines = settings\.worker\?\.fallback_engines \|\| \[\]/, '保存应提交 fallback_engines 数组')
})

test('styles.css 提供引擎健康徽章样式', () => {
  for (const cls of ['engine-health-badge', 'engine-health-ok', 'engine-health-fail', 'engine-health-unknown']) {
    assert.ok(new RegExp(`\\.${cls}\\s*\\{`).test(styles), `styles.css 应包含 .${cls} 样式规则`)
  }
})

test('任务详情展示执行引擎与降级原因（engine_fallback）', () => {
  assert.match(drawer, /task\.engine_fallback/, '抽屉详情应有降级原因行')
  assert.match(drawer, /降级原因/, '抽屉降级原因文案')
  assert.match(taskDetail, /task\.engine_fallback/, '完整任务页应有降级原因行')
  assert.match(taskDetail, /降级原因/, '完整任务页降级原因文案')
})

test('后端 settings API：GET 返回 fallback 配置与 engine_health，校验写回', () => {
  assert.match(apiSettings, /"fallback_engines": list\(s\.fallback_engines/, 'GET worker 应含 fallback_engines')
  assert.match(apiSettings, /"fallback_after_failures": s\.fallback_after_failures/, 'GET worker 应含 fallback_after_failures')
  assert.match(apiSettings, /"engine_health": engine_health_snapshot/, 'GET worker 应含 engine_health 快照')
  assert.match(apiSettings, /key == "fallback_engines"/, '_validate_worker 应校验 fallback_engines')
  assert.match(apiSettings, /key == "fallback_after_failures"/, '_validate_worker 应校验 fallback_after_failures')
})

test('后端 config：Settings 字段 + KNOWN_FIELDS 白名单 + yaml 解析', () => {
  assert.match(config, /fallback_engines: list\[str\] = field\(default_factory=list\)/, 'Settings 应含 fallback_engines 字段')
  assert.match(config, /fallback_after_failures: int = 2/, 'Settings 应含 fallback_after_failures 字段（默认 2）')
  assert.match(config, /"fallback_engines", "fallback_after_failures"/, 'KNOWN_FIELDS worker 白名单应含两项')
  assert.match(config, /fallback_engines=\[str\(e\)\.strip\(\).lower\(\)/, '_to_settings 应解析 fallback_engines')
  assert.match(config, /fallback_after_failures=max\(1, int\(worker\.get\("fallback_after_failures", 2\)\)\)/, '_to_settings 应解析 fallback_after_failures（下限 1）')
})

test('后端 tasks API：任务字典含 engine_fallback 降级原因', () => {
  assert.match(apiTasks, /"engine_fallback": row\["engine_fallback"\] or ""/, '任务字典应含 engine_fallback')
})

test('后端数据库：tasks 表 engine_fallback 列 + 字段白名单 + 迁移', () => {
  assert.match(database, /engine_fallback TEXT DEFAULT ''/, '_SCHEMA tasks 表应含 engine_fallback 列')
  assert.match(database, /"failure_category", "engine_fallback"/, '_TASK_FIELDS 白名单应含 engine_fallback')
  assert.match(database, /ALTER TABLE tasks ADD COLUMN engine_fallback TEXT DEFAULT ''/, '旧库迁移应补列')
})

test('后端引擎健康探测模块存在且覆盖三引擎', () => {
  assert.match(engineHealth, /def probe_claude/, '应有 claude 探测（claude --version）')
  assert.match(engineHealth, /def probe_hermes/, '应有 hermes 探测（runner 可加载）')
  assert.match(engineHealth, /def probe_dsh/, '应有 dsh 探测（SDK 导入）')
  assert.match(engineHealth, /def engine_health_snapshot/, '应有快照函数（徽章数据源）')
  assert.match(engineHealth, /find_spec\("run_agent"\)/, 'hermes 探测应检查 run_agent 模块')
  assert.match(engineHealth, /find_spec\("deepseek_harness"\)/, 'dsh 探测应检查 deepseek_harness 模块')
})
