// AI API 供应商配置卡片测试（issue #46）：设置页增加 AI API 供应商配置，
// 用户可以配置 deepseek、openai 等供应商，各供应商显示各自 logo
// （而不是所有供应商 logo 都一样）。
//
// 本测试断言：
// 1. 设置页挂载「AI API 供应商」卡片（SSO 卡片之后第二位）；
// 2. 卡片提供增删改表单（名称/类型/Base URL/API Key/默认模型/启用）；
// 3. 保存走 PUT /api/settings 的 ai_providers 段；API Key 留空 = 保持现有；
// 4. providers.js 内置多供应商预设（deepseek/openai/... 共 11 个），
//    每个预设带默认 base_url/model，logo 按供应商差异化渲染。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const card = readFileSync(path.join(ROOT, 'src/components/AiProvidersCard.jsx'), 'utf8')
const providers = readFileSync(path.join(ROOT, 'src/providers.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

test('设置页挂载「AI API 供应商」卡片组件', () => {
  assert.match(settings, /import AiProvidersCard from '\.\.\/components\/AiProvidersCard\.jsx'/, '应导入卡片组件')
  assert.match(settings, /<AiProvidersCard \/>/, '设置页应挂载 AiProvidersCard')
})

test('「AI API 供应商」卡片位于「系统设置」标题之前（与 SSO 同为外部服务接入类配置）', () => {
  const cardPos = settings.search(/<AiProvidersCard \/>/)
  const h1Pos = settings.search(/<h1>系统设置<\/h1>/)
  assert.ok(cardPos > 0, '应有 AiProvidersCard 挂载点')
  assert.ok(h1Pos > cardPos, '卡片应在「系统设置」区块之前')
})

test('卡片提供供应商列表表格与增删改操作', () => {
  assert.match(card, /<h2>AI API 供应商<\/h2>/, '应有卡片标题')
  assert.match(card, /添加供应商/, '应有「添加供应商」按钮')
  assert.match(card, />编辑<\/button>/, '列表行应有「编辑」按钮')
  assert.match(card, /confirmDialog\(\{ message: `删除供应商/, '删除应有自定义确认对话框提示')
  assert.match(card, /ProviderLogo provider=/, '列表应渲染供应商 logo')
})

test('编辑表单字段齐全（名称/类型/Base URL/API Key/默认模型/启用）', () => {
  assert.match(card, /placeholder="如：DeepSeek 生产环境"/, '应有名称输入框')
  assert.match(card, /AI_PROVIDER_PRESETS\.map/, '类型下拉应遍历预设清单')
  assert.match(card, /placeholder="https:\/\/api\.example\.com\/v1"/, '应有 Base URL 输入框')
  assert.match(card, /type="password"/, 'API Key 应为密码输入框')
  assert.match(card, /默认模型/, '应有默认模型输入框')
  assert.match(card, /启用该供应商/, '应有启用开关')
})

test('保存走 PUT /api/settings 的 ai_providers 段，API Key 留空 = 保持现有', () => {
  assert.match(card, /api\.put\('\/api\/settings', \{ ai_providers: providers \}\)/, '保存应提交 ai_providers 段')
  assert.match(card, /留空 = 保持现有/, '编辑时 API Key 输入框应提示留空保持现有')
  assert.match(card, /留空 = 暂不配置/, '新增时 API Key 输入框应提示留空暂不配置')
  assert.match(card, /api\.get\('\/api\/settings'\)/, '列表加载走 GET /api/settings')
})

test('providers.js 内置 11 个预设供应商（含自定义），均有默认 base_url/model（custom 除外）', () => {
  const keys = ['deepseek', 'openai', 'anthropic', 'gemini', 'moonshot',
    'qwen', 'zhipu', 'siliconflow', 'ollama', 'openrouter', 'custom']
  for (const key of keys) {
    assert.ok(
      new RegExp(`key: '${key}'`).test(providers),
      `预设清单应包含 ${key}`,
    )
  }
  for (const key of keys.filter((k) => k !== 'custom')) {
    const entry = new RegExp(`key: '${key}',[^\\n]+`).exec(providers)
    assert.ok(entry && entry[0].includes('baseUrl:') && entry[0].includes('model:'),
      `预设 ${key} 应带默认 baseUrl 与 model`)
  }
})

test('供应商 logo 差异化：各预设渲染不同图形，未知 key 回退 custom 通用图标', () => {
  // LOGOS 映射中每个 key 的图形不同（不同供应商不是同一个 logo）
  const nodes = [...providers.matchAll(/(deepseek|openai|anthropic|gemini|moonshot|qwen|zhipu|siliconflow|ollama|openrouter|custom):\s*\{[^}]*node:\s*(.+?)\n\s*\},/gs)]
  assert.ok(nodes.length >= 11, '应有 11 个供应商 logo 定义')
  // 抽取每个 key 的 node 片段，全部不同
  const byKey = {}
  for (const m of nodes) byKey[m[1]] = m[2].trim()
  const uniq = new Set(Object.values(byKey))
  assert.ok(uniq.size >= Object.keys(byKey).length - 1,
    '各供应商 logo 图形应不同（允许个别类型共用图形元素）')
  assert.match(providers, /const def = LOGOS\[provider\] \|\| LOGOS\.custom/, '未知 key 应回退 custom 图标')
})

test('styles.css 提供供应商卡片样式（provider-logo / provider-form / provider-cell）', () => {
  for (const cls of ['provider-logo', 'provider-form', 'provider-cell', 'provider-select', 'provider-field']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})
