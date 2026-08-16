// 识图模型配置卡片测试（issue #135）：设置页增加「识图模型」配置，
// 内置 Gemini Nano Banana Pro 与 OpenAI GPT Image 2 两个预设，
// 用户可配置名称/类型/Base URL/API Key/默认模型/启用。
//
// 本测试断言：
// 1. 设置页挂载「识图模型」卡片（AI 供应商卡片之后）；
// 2. 卡片提供增删改表单（名称/类型/Base URL/API Key/默认模型/启用）；
// 3. 保存走 PUT /api/settings 的 image_models 段；API Key 留空 = 保持现有；
// 4. providers.js 内置 2 个识图模型预设 + custom，每个预设带默认
//    base_url/model（custom 除外），logo 复用 gemini / openai。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const card = readFileSync(path.join(ROOT, 'src/components/ImageModelsCard.jsx'), 'utf8')
const providers = readFileSync(path.join(ROOT, 'src/providers.jsx'), 'utf8')

test('设置页挂载「识图模型」卡片组件', () => {
  assert.match(settings, /import ImageModelsCard from '\.\.\/components\/ImageModelsCard\.jsx'/, '应导入卡片组件')
  assert.match(settings, /<ImageModelsCard \/>/, '设置页应挂载 ImageModelsCard')
})

test('「识图模型」卡片位于「AI API 供应商」卡片之后', () => {
  const aiPos = settings.search(/<AiProvidersCard \/>/)
  const imagePos = settings.search(/<ImageModelsCard \/>/)
  assert.ok(aiPos > 0, '应有 AiProvidersCard 挂载点')
  assert.ok(imagePos > aiPos, '识图模型卡片应在 AI 供应商卡片之后')
})

test('卡片提供识图模型列表表格与增删改操作', () => {
  assert.match(card, /<h2>识图模型<\/h2>/, '应有卡片标题')
  assert.match(card, /添加模型/, '应有「添加模型」按钮')
  assert.match(card, />编辑<\/button>/, '列表行应有「编辑」按钮')
  assert.match(card, /confirmDialog\(\{ message: `删除识图模型/, '删除应有自定义确认对话框提示')
  assert.match(card, /ProviderLogo provider=/, '列表应渲染模型 logo')
})

test('编辑表单字段齐全（名称/类型/Base URL/API Key/默认模型/启用）', () => {
  assert.match(card, /placeholder="如：Gemini 生产环境"/, '应有名称输入框')
  assert.match(card, /IMAGE_MODEL_PRESETS\.map/, '类型下拉应遍历识图模型预设清单')
  assert.match(card, /placeholder="https:\/\/api\.example\.com\/v1"/, '应有 Base URL 输入框')
  assert.match(card, /type="password"/, 'API Key 应为密码输入框')
  assert.match(card, /placeholder="如：gemini-3-pro-image"/, '应有默认模型输入框')
  assert.match(card, /启用该模型/, '应有启用开关')
})

test('保存走 PUT /api/settings 的 image_models 段，API Key 留空 = 保持现有', () => {
  assert.match(card, /api\.put\('\/api\/settings', \{ image_models: models \}\)/, '保存应提交 image_models 段')
  assert.match(card, /留空 = 保持现有/, '编辑时 API Key 输入框应提示留空保持现有')
  assert.match(card, /留空 = 暂不配置/, '新增时 API Key 输入框应提示留空暂不配置')
  assert.match(card, /api\.get\('\/api\/settings'\)/, '列表加载走 GET /api/settings')
})

test('providers.js 内置识图模型预设：gemini_nano_banana / openai_gpt_image / custom', () => {
  const keys = ['gemini_nano_banana', 'openai_gpt_image', 'custom']
  for (const key of keys) {
    assert.ok(
      new RegExp(`key: '${key}'`).test(providers),
      `识图模型预设清单应包含 ${key}`,
    )
  }
  for (const key of keys.filter((k) => k !== 'custom')) {
    const entry = new RegExp(`key: '${key}',[^\\n]+`).exec(providers)
    assert.ok(entry && entry[0].includes('baseUrl:') && entry[0].includes('model:'),
      `预设 ${key} 应带默认 baseUrl 与 model`)
  }
  // 两个预设的默认模型与官方接口一致
  assert.match(providers, /model: 'gemini-3-pro-image'/, 'Nano Banana Pro 默认模型应为 gemini-3-pro-image')
  assert.match(providers, /model: 'gpt-image-2'/, 'GPT Image 2 默认模型应为 gpt-image-2')
})

test('识图模型 logo 复用 gemini / openai 品牌图标', () => {
  // ImageModelsCard 通过 ProviderLogo 渲染，logo 定义在 providers.jsx 的 LOGOS
  assert.match(providers, /gemini: \{\s*\n\s*bg: '#4285F4'/, '应有 gemini logo')
  assert.match(providers, /openai: \{\s*\n\s*bg: '#10A37F'/, '应有 openai logo')
})
