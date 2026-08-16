// 设置页「界面显示」卡片「显示未启用项目」开关测试（issue #142）：
// 灵感板块 / CI/CD 流水线板块是否显示未启用项目，通过设置
// ui.show_disabled_repos 配置（默认 true = 显示，保持现状）。
//
// 断言：
// 1. 「界面显示」卡片提供复选框开关，使用 ui.show_disabled_repos 配置键；
// 2. 开关说明覆盖灵感板块与 CI/CD 流水线板块；
// 3. 全局「保存」提交 show_disabled_repos（未配置时按 true 处理，兼容旧 mock）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

test('设置页「界面显示」卡片提供「显示未启用项目」复选框开关', () => {
  assert.match(settings, /显示未启用项目/, '应有开关文案')
  assert.match(settings, /ui\.show_disabled_repos/, '应使用 ui.show_disabled_repos 配置键')
  assert.match(settings, /type="checkbox"/, '应为复选框')
  assert.match(settings, /settings\.ui\?\.show_disabled_repos !== false/,
               '未配置时应按 true（显示）处理')
})

test('开关说明覆盖灵感板块与 CI/CD 流水线板块', () => {
  assert.match(settings, /灵感/, '说明应提及灵感板块')
  assert.match(settings, /CI\/CD/, '说明应提及 CI/CD 流水线板块')
})

test('buildUiPatch 提交 ui.show_disabled_repos 且全局保存复用 buildUiPatch', () => {
  // issue #142 反馈轮：新增卡片内独立保存后，ui 段统一由 buildUiPatch
  // 构建（全局「保存」与卡片内「保存界面显示配置」共用，行为一致）
  assert.match(settings, /show_disabled_repos: settings\.ui\?\.show_disabled_repos !== false/,
               'buildUiPatch 应提交 show_disabled_repos（关闭 = false，默认 true）')
  assert.match(settings, /ui: buildUiPatch\(\)/,
               '全局保存应复用 buildUiPatch 提交 ui 段')
})
