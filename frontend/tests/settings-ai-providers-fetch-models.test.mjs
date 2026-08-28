// AI API 供应商「获取模型」功能测试（issue #499）：
// 设置页供应商编辑表单新增「获取模型」按钮，点击后经后端代理
// POST /api/ai/list-models（OpenAI 兼容 GET {base_url}/models）获取
// 供应商全部模型，用户从中选择一个填入「默认模型」输入框。
//
// 断言：
// 1. 源码：按钮文案/端点/加载态/空 Base URL 校验/模型选择列表渲染；
// 2. 组件行为：编辑表单点「获取模型」→ 调 /api/ai/list-models →
//    渲染模型选择列表 → 点击模型填入「默认模型」；
// 3. 边界：Base URL 为空提示先填写；请求中按钮禁用防重复；
//    请求失败展示错误；切换编辑对象清空模型列表。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: AiProvidersCard } = await vite.ssrLoadModule('/src/components/AiProvidersCard.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const cardSrc = readFileSync(path.join(ROOT, 'src/components/AiProvidersCard.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

after(() => vite.close())

// ---- 夹具与 mock ----

const PROVIDERS = [
  {
    name: '生产 DeepSeek',
    provider: 'deepseek',
    base_url: 'https://api.deepseek.com/v1',
    api_key_masked: 'sk-sa********cret',
    model: 'deepseek-chat',
    enabled: true,
    priority: 100,
  },
]

const calls = { get: [], post: [] }
const modelResult = { models: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-coder'] }

function mockAll(opts = {}) {
  calls.get.length = 0
  calls.post.length = 0
  mock.method(api, 'get', async (p) => {
    calls.get.push(p)
    if (opts.getImpl) return opts.getImpl(p)
    return { ai_providers: PROVIDERS }
  })
  mock.method(api, 'post', async (p, body) => {
    calls.post.push([p, body])
    if (opts.postImpl) return opts.postImpl(p, body)
    return { ...modelResult }
  })
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

function findButtons(renderer, text) {
  return renderer.root.findAllByType('button')
    .filter((b) => textOf(b.props.children).includes(text))
}

async function renderCard() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(AiProvidersCard))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

async function openEdit(renderer) {
  // 进入编辑表单（点击列表中第一行「编辑」）
  await TestRenderer.act(async () => {
    findButtons(renderer, '编辑')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
}

// ---- 源码断言 ----

test('源码：编辑表单提供「获取模型」按钮，走 POST /api/ai/list-models', () => {
  assert.match(cardSrc, /获取模型/, '应有「获取模型」按钮文案')
  assert.match(cardSrc, /api\.post\('\/api\/ai\/list-models'/, '应调后端代理端点 /api/ai/list-models')
  assert.match(cardSrc, /base_url:/, '请求体应携带 base_url')
  assert.match(cardSrc, /api_key:/, '请求体应携带 api_key（掩码时后端按 name 匹配已保存）')
  assert.match(cardSrc, /name:/, '请求体应携带供应商 name（用于掩码 Key 回退）')
  assert.match(cardSrc, /获取中…/, '请求中按钮应显示「获取中…」')
  assert.match(cardSrc, /请先填写 Base URL 再获取模型/, 'Base URL 为空时应提示先填写')
})

test('源码：模型选择列表渲染与选中填入「默认模型」', () => {
  assert.match(cardSrc, /modelOptions\.map/, '应遍历模型列表渲染选择项')
  assert.match(cardSrc, /setForm\('model'/, '选择模型应写入 form.model（默认模型输入框）')
  assert.match(cardSrc, /setModelOptions\(null\)/, '选中后应收起模型列表')
})

test('源码：切换编辑对象 / 取消时清空模型列表状态', () => {
  assert.match(cardSrc, /setModelOptions\(null\)/, '取消/切换编辑对象应清空模型列表')
})

test('styles.css 提供模型行与模型选择列表样式', () => {
  for (const cls of ['provider-model-row', 'model-picker']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

// ---- 组件行为 ----

test('点击「获取模型」调用后端代理并渲染模型列表，选择后填入默认模型', async () => {
  mockAll()
  const renderer = await renderCard()
  await openEdit(renderer)

  assert.equal(findButtons(renderer, '获取模型').length, 1, '编辑表单应有「获取模型」按钮')
  await TestRenderer.act(async () => {
    findButtons(renderer, '获取模型')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })

  assert.deepEqual(calls.post[0][0], '/api/ai/list-models', '应调后端代理端点')
  assert.equal(calls.post[0][1].base_url, 'https://api.deepseek.com/v1', '请求体应携带编辑中的 base_url')
  assert.equal(calls.post[0][1].name, '生产 DeepSeek', '请求体应携带供应商 name')

  // 模型选择列表渲染
  const picker = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('model-picker'))
  assert.equal(picker.length, 1, '应渲染模型选择列表')
  const pickerText = textOf(picker[0])
  for (const m of modelResult.models) {
    assert.ok(pickerText.includes(m), `选择列表应包含模型 ${m}`)
  }

  // 点击第二个模型 → 填入默认模型输入框并收起列表
  await TestRenderer.act(async () => {
    findButtons(renderer, 'deepseek-reasoner')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  const modelInput = renderer.root.findAllByType('input')
    .find((i) => i.props.placeholder?.includes('deepseek-chat'))
  assert.ok(modelInput, '应有默认模型输入框')
  assert.equal(modelInput.props.value, 'deepseek-reasoner', '选择后应填入默认模型输入框')
  const pickerAfter = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('model-picker'))
  assert.equal(pickerAfter.length, 0, '选中后应收起模型选择列表')
})

test('Base URL 为空时点击「获取模型」提示先填写，不调接口', async () => {
  mockAll()
  const renderer = await renderCard()
  await openEdit(renderer)

  // 清空 Base URL
  const urlInput = renderer.root.findAllByType('input')
    .find((i) => i.props.placeholder === 'https://api.example.com/v1')
  assert.ok(urlInput, '应有 Base URL 输入框')
  await TestRenderer.act(async () => {
    urlInput.props.onChange({ target: { value: '' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
  })

  await TestRenderer.act(async () => {
    findButtons(renderer, '获取模型')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.equal(calls.post.length, 0, 'Base URL 为空不应调接口')
  assert.match(textOf(renderer.root), /请先填写 Base URL 再获取模型/, '应提示先填写 Base URL')
})

test('请求中按钮禁用防重复点击', async () => {
  let release = null
  mockAll({ postImpl: () => new Promise((resolve) => { release = resolve }) })
  const renderer = await renderCard()
  await openEdit(renderer)

  await TestRenderer.act(async () => {
    findButtons(renderer, '获取模型')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  const fetchingBtn = findButtons(renderer, '获取中…')[0]
  assert.ok(fetchingBtn, '请求中按钮应显示「获取中…」')
  assert.equal(fetchingBtn.props.disabled, true, '请求中按钮应禁用')
  assert.equal(calls.post.length, 1, '请求中重复点击不应再次调接口')

  await TestRenderer.act(async () => {
    release({ models: ['m1'] })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
})

test('获取模型失败展示后端错误信息', async () => {
  mockAll({ postImpl: async () => { throw new Error('HTTP 401 认证失败') } })
  const renderer = await renderCard()
  await openEdit(renderer)

  await TestRenderer.act(async () => {
    findButtons(renderer, '获取模型')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.match(textOf(renderer.root), /HTTP 401 认证失败/, '应展示后端错误信息')
  assert.equal(findButtons(renderer, '获取中…').length, 0, '请求结束后按钮应恢复')
})

test('取消编辑后模型列表状态清空（再次进入编辑不残留）', async () => {
  mockAll()
  const renderer = await renderCard()
  await openEdit(renderer)

  await TestRenderer.act(async () => {
    findButtons(renderer, '获取模型')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.equal(renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('model-picker')).length, 1)

  // 取消编辑 → 列表清空
  await TestRenderer.act(async () => {
    findButtons(renderer, '取消')[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  await openEdit(renderer)
  const pickerAfter = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('model-picker'))
  assert.equal(pickerAfter.length, 0, '重新进入编辑不应残留上次模型列表')
})
