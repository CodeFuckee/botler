// 识图模型配置卡片测试（issue #152）：设置页「识图模型」卡片，
// 内置 Gemini 视觉 / OpenAI 视觉 两个预设 + 自定义（OpenAI 兼容接口），
// 用户可配置名称/识图模型类型/Base URL/API Key/默认模型/启用；
// 测试按钮：点击后用户上传一张图片，调用模型描述图片内容并展示描述。
//
// 本测试断言：
// 1. 设置页挂载「识图模型」卡片（生图模型卡片之后）；
// 2. 卡片提供增删改表单（名称/识图模型类型/Base URL/API Key/默认模型/启用）；
// 3. 保存走 PUT /api/settings 的 vision_models 段；API Key 留空 = 保持现有；
// 4. providers.js 内置识图模型预设（gemini_vision / openai_vision / custom）；
// 5. 测试按钮：上传图片后 POST /api/settings/vision-model-test（multipart
//    FormData），成功展示模型描述的文本内容。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const card = readFileSync(path.join(ROOT, 'src/components/VisionModelsCard.jsx'), 'utf8')
const providers = readFileSync(path.join(ROOT, 'src/providers.jsx'), 'utf8')
const api = readFileSync(path.join(ROOT, 'src/api.js'), 'utf8')

test('设置页挂载「识图模型」卡片组件', () => {
  assert.match(settings, /import VisionModelsCard from '\.\.\/components\/VisionModelsCard\.jsx'/, '应导入卡片组件')
  assert.match(settings, /<VisionModelsCard \/>/, '设置页应挂载 VisionModelsCard')
})

test('「识图模型」卡片位于「生图模型」卡片之后', () => {
  const imagePos = settings.search(/<ImageModelsCard \/>/)
  const visionPos = settings.search(/<VisionModelsCard \/>/)
  assert.ok(imagePos > 0, '应有 ImageModelsCard 挂载点')
  assert.ok(visionPos > imagePos, '识图模型卡片应在生图模型卡片之后')
})

test('卡片提供识图模型列表表格与增删改操作', () => {
  assert.match(card, /<h2>识图模型<\/h2>/, '应有卡片标题')
  assert.match(card, /添加模型/, '应有「添加模型」按钮')
  assert.match(card, />编辑<\/button>/, '列表行应有「编辑」按钮')
  assert.match(card, /confirmDialog\(\{ message: `删除识图模型/, '删除应有自定义确认对话框提示')
  assert.match(card, /ProviderLogo provider=/, '列表应渲染模型 logo')
})

test('编辑表单字段齐全（名称/识图模型类型/Base URL/API Key/默认模型/启用）', () => {
  assert.match(card, /placeholder="如：Gemini 视觉生产环境"/, '应有名称输入框')
  assert.match(card, /识图模型类型/, '应有「识图模型类型」标签（模型类型下拉）')
  assert.match(card, /VISION_MODEL_PRESETS\.map/, '识图模型类型下拉应遍历识图模型预设清单')
  assert.match(card, /placeholder="https:\/\/api\.example\.com\/v1"/, '应有 Base URL 输入框')
  assert.match(card, /type="password"/, 'API Key 应为密码输入框')
  assert.match(card, /placeholder="如：gemini-2\.5-flash"/, '应有默认模型输入框')
  assert.match(card, /启用该模型/, '应有启用开关')
})

test('保存走 PUT /api/settings 的 vision_models 段，API Key 留空 = 保持现有', () => {
  assert.match(card, /api\.put\('\/api\/settings', \{ vision_models: models \}\)/, '保存应提交 vision_models 段')
  assert.match(card, /留空 = 保持现有/, '编辑时 API Key 输入框应提示留空保持现有')
  assert.match(card, /留空 = 暂不配置/, '新增时 API Key 输入框应提示留空暂不配置')
  assert.match(card, /api\.get\('\/api\/settings'\)/, '列表加载走 GET /api/settings')
})

test('测试按钮：上传图片后 POST /api/settings/vision-model-test（multipart）', () => {
  // 点击「测试」→ 选择图片文件 → FormData 提交（含图片 + 当前表单配置）
  assert.match(card, /api\.post\('\/api\/settings\/vision-model-test'/, '应有识图测试调用')
  assert.match(card, /FormData/, '上传图片应使用 FormData（multipart）')
  assert.match(card, /input[^>]*type="file"[^>]*accept=/, '应有图片文件选择框（accept 图片类型）')
  assert.match(card, /type="file"/, '应有文件上传输入框')
  assert.match(card, /请上传一张图片/, '测试说明应提示先上传图片')
  assert.match(card, /provider: editing\.form\.provider/, '表单测试应提交识图模型类型')
  assert.match(card, /api_key: apiKeyInput\.trim\(\)/, '表单测试应提交当前填写的 API Key')
  assert.match(card, /model: editing\.form\.model/, '表单测试应提交默认模型')
  assert.match(card, /base_url: editing\.form\.base_url/, '表单测试应提交 Base URL')
})

test('测试结果展示模型描述文本（成功）/ 错误原因（失败）', () => {
  assert.match(card, /res\.description/, '成功应读取后端返回的描述文本')
  assert.match(card, /res\.error \|\| '识图测试失败'/, '失败提示应展示后端错误信息')
  assert.match(card, /testBusy \? '测试中…' : '测试'/, '测试中应有 busy 文案')
  assert.match(card, /'识别中…'/, '上传识别应有「识别中…」busy 文案')
})

test('providers.js 内置识图模型预设：gemini_vision / openai_vision / custom', () => {
  const keys = ['gemini_vision', 'openai_vision', 'custom']
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
  // 两个预设的默认模型与视觉理解接口一致
  assert.match(providers, /model: 'gemini-2\.5-flash'/, 'Gemini 视觉默认模型应为 gemini-2.5-flash')
  assert.match(providers, /model: 'gpt-4o'/, 'OpenAI 视觉默认模型应为 gpt-4o')
})

test('识图模型 logo 复用 gemini / openai 品牌图标', () => {
  assert.match(providers, /gemini: \{\s*\n\s*bg: '#4285F4'/, '应有 gemini logo')
  assert.match(providers, /openai: \{\s*\n\s*bg: '#10A37F'/, '应有 openai logo')
})

test('测试按钮触发隐藏文件选择框（点击测试 → 选图 → 自动识别）', () => {
  assert.match(card, /\.click\(\)/, '点击测试应触发文件选择框')
  assert.match(card, /testImageInput|fileInput|imageInput|fileRef/i, '应有文件输入引用')
})
