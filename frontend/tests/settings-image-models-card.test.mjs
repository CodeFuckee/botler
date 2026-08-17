// 生图模型配置卡片测试（issue #135 / #137）：设置页「生图模型」配置，
// 内置 Gemini Nano Banana Pro 与 OpenAI GPT Image 2 两个预设，
// 用户可配置名称/生图模式/Base URL/API Key/默认模型/启用；
// issue #137：配置好 url / apikey / 生图模式后可点「测试」真实调用
// 一次生图接口验证配置是否可用（编辑表单内测试当前填写值、列表行测试
// 已保存配置，均走 POST /api/settings/image-model-test）。
//
// 本测试断言：
// 1. 设置页挂载「生图模型」卡片（AI 供应商卡片之后）；
// 2. 卡片提供增删改表单（名称/生图模式/Base URL/API Key/默认模型/启用）；
// 3. 保存走 PUT /api/settings 的 image_models 段；API Key 留空 = 保持现有；
// 4. providers.js 内置 2 个生图模型预设 + custom，每个预设带默认
//    base_url/model（custom 除外），logo 复用 gemini / openai；
// 5. 测试按钮调用 POST /api/settings/image-model-test，成功/失败提示。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const card = readFileSync(path.join(ROOT, 'src/components/ImageModelsCard.jsx'), 'utf8')
const providers = readFileSync(path.join(ROOT, 'src/providers.jsx'), 'utf8')

test('设置页挂载「生图模型」卡片组件', () => {
  assert.match(settings, /import ImageModelsCard from '\.\.\/components\/ImageModelsCard\.jsx'/, '应导入卡片组件')
  assert.match(settings, /<ImageModelsCard \/>/, '设置页应挂载 ImageModelsCard')
})

test('「生图模型」卡片位于「AI API 供应商」卡片之后', () => {
  const aiPos = settings.search(/<AiProvidersCard \/>/)
  const imagePos = settings.search(/<ImageModelsCard \/>/)
  assert.ok(aiPos > 0, '应有 AiProvidersCard 挂载点')
  assert.ok(imagePos > aiPos, '生图模型卡片应在 AI 供应商卡片之后')
})

test('卡片提供生图模型列表表格与增删改操作', () => {
  assert.match(card, /<h2>生图模型<\/h2>/, '应有卡片标题')
  assert.match(card, /添加模型/, '应有「添加模型」按钮')
  assert.match(card, />编辑<\/button>/, '列表行应有「编辑」按钮')
  assert.match(card, /confirmDialog\(\{ message: `删除生图模型/, '删除应有自定义确认对话框提示')
  assert.match(card, /ProviderLogo provider=/, '列表应渲染模型 logo')
})

test('编辑表单字段齐全（名称/生图模式/Base URL/API Key/默认模型/启用）', () => {
  assert.match(card, /placeholder="如：Gemini 生产环境"/, '应有名称输入框')
  assert.match(card, /生图模式/, '应有「生图模式」标签（模型类型下拉）')
  assert.match(card, /IMAGE_MODEL_PRESETS\.map/, '生图模式下拉应遍历生图模型预设清单')
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

test('测试按钮调用 POST /api/settings/image-model-test（列表行 + 编辑表单）', () => {
  // 列表行「测试」按钮：只提交 name + provider，后端按已保存配置测试
  assert.match(card, /api\.post\('\/api\/settings\/image-model-test', payload\)/, '应有测试调用封装')
  assert.match(card, /onClick=\{\(\) => testModel\(\{ name: m\.name, provider: m\.provider \}\)\}/,
    '列表行测试应提交 name + provider')
  // 编辑表单「测试配置」按钮：提交当前表单值（url / api key / 生图模式）
  assert.match(card, /onClick=\{testCurrentForm\}/, '应有表单内测试按钮')
  assert.match(card, /'测试配置'/, '表单内测试按钮文案应为「测试配置」')
  // 提交载荷包含完整配置字段
  assert.match(card, /provider: editing\.form\.provider/, '表单测试应提交生图模式')
  assert.match(card, /base_url: editing\.form\.base_url/, '表单测试应提交 Base URL')
  assert.match(card, /api_key: apiKeyInput\.trim\(\)/, '表单测试应提交当前填写的 API Key')
  assert.match(card, /model: editing\.form\.model/, '表单测试应提交默认模型')
})

test('测试结果展示成功/失败提示', () => {
  assert.match(card, /生图测试成功，已生成/, '成功提示应包含生成张数')
  assert.match(card, /res\.error \|\| '生图测试失败'/, '失败提示应展示后端错误信息')
  assert.match(card, /result\.ok \? 'saved-hint' : 'err-hint'/, '成功/失败用不同样式提示')
  assert.match(card, /testBusy \? '测试中…' : '测试'/, '测试中应有 busy 文案')
})

test('列表模式（非编辑）点击测试也展示结果（issue #149）', () => {
  // 复现：此前 testResult 只在编辑表单内渲染，列表行点「测试」无任何提示
  assert.match(card, /!editing && <TestResult result=\{testResult\} \/>/,
    '列表模式（非编辑）也应渲染测试结果')
})

test('生图成功返回并展示生成的图片（issue #149）', () => {
  // 复现：此前成功仅返回张数，前端无图片可展示；后端应回传首张图片
  // base64，前端拼 data URL 后 <img> 展示
  assert.match(card, /res\.image_base64/, '应读取后端返回的 image_base64')
  assert.match(card, /data:.*;base64,\$\(res\.image_base64\)|data:.*;base64,\$\{res\.image_base64\}/,
    '应把 image_base64 拼成 data URL')
  assert.match(card, /<img className="test-image" src=\{result\.image\}/,
    '应渲染生成图片')
})

test('providers.js 内置生图模型预设：gemini_nano_banana / openai_gpt_image / custom', () => {
  const keys = ['gemini_nano_banana', 'openai_gpt_image', 'custom']
  for (const key of keys) {
    assert.ok(
      new RegExp(`key: '${key}'`).test(providers),
      `生图模型预设清单应包含 ${key}`,
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

test('生图模型 logo 复用 gemini / openai 品牌图标', () => {
  // ImageModelsCard 通过 ProviderLogo 渲染，logo 定义在 providers.jsx 的 LOGOS
  assert.match(providers, /gemini: \{\s*\n\s*bg: '#4285F4'/, '应有 gemini logo')
  assert.match(providers, /openai: \{\s*\n\s*bg: '#10A37F'/, '应有 openai logo')
})
