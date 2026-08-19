// 设置页「dsh 引擎」推理等级设置项测试（issue #123）：新增 dsh 引擎卡片，
// 下拉选择 deepseek-harness SDK 推理等级（off / high / max，空 = 不设置），
// 保存后写回 config.yaml，dsh 引擎执行时自动派生 Cordis 注入 reasoningEffort。
//
// 断言：
// 1. 设置页存在「dsh 引擎」卡片，含「推理等级」行（dsh.reasoning_effort）；
// 2. select 含默认 / off / high / max 四个选项，默认值为空串；
// 3. 说明文字写明三档含义与默认值；
// 4. 全局「保存」提交 dsh 段含 reasoning_effort；
// 5. 渲染时按后端返回值回显，切换后保存提交新值。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：dsh 引擎卡片 JSX 移到 components/settings/DshCard.jsx，
// save 收敛到 hooks/useSettingsData.js——静态断言跟随新文件
const dshCard = readFileSync(path.join(ROOT, 'src/components/settings/DshCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsSrc = dshCard + '\n' + hook

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-issue-priority.test.mjs 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名函数源码片段（与 settings-issue-priority.test.mjs 同款工具） */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} =[\\s\\S]*?(?=\\n  const |\\n  return \\()`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('设置页存在「dsh 引擎」卡片与推理等级下拉设置项', () => {
  const card = cardSource(settingsSrc, 'dsh 引擎')
  assert.ok(card, '设置页应存在「dsh 引擎」卡片')
  assert.match(card, /推理等级/, '卡片应含推理等级行')
  assert.match(card, /reasoning_effort/, '行标题应标注配置键 dsh.reasoning_effort')
  assert.match(card, /<select/, '推理等级应使用 select 下拉框')
  for (const value of ['off', 'high', 'max']) {
    assert.match(card, new RegExp(`value="${value}"`), `应含 ${value} 选项`)
  }
  assert.match(card, /value="">/, '应含默认（空值）选项')
})

test('说明文字写明推理等级含义与默认值', () => {
  const card = cardSource(settingsSrc, 'dsh 引擎')
  assert.ok(card, '设置页应存在「dsh 引擎」卡片')
  assert.match(card, /deepseek-harness/, '说明应提及 dsh 引擎')
  assert.match(card, /关闭推理/, '说明应写明 off = 关闭推理')
  assert.match(card, /high/, '说明应写明 high 档')
  assert.match(card, /max/, '说明应写明 max 档')
  assert.match(card, /默认/, '说明应写明默认值')
})

test('全局保存提交 dsh.reasoning_effort（空值回退空串）', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /dsh:/, 'save 应携带 dsh 段')
  assert.match(
    body,
    /reasoning_effort: settings\.dsh\?\.reasoning_effort \|\| ''/,
    'save 应提交 dsh.reasoning_effort（后端未返回时回退空串）',
  )
})

/** 渲染用 fetch mock（与 settings-engine.test.mjs 同款，覆盖设置页全部接口） */
function mockFetch({ reasoningEffort = '' } = {}) {
  const puts = []
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    if (opts?.method === 'PUT') {
      puts.push(JSON.parse(opts.body))
      return { ok: true, status: 200, json: async () => ({}) }
    }
    const pathname = String(p)
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          worker: { issue_priority: ['bug'], engine: 'dsh' },
          sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '' }, notifications: {}, gitlab: {}, env: {},
          dsh: { reasoning_effort: reasoningEffort },
          backup: {}, browse: {}, templates: {}, ai_providers: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-13 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { puts, restore: () => { global.fetch = originalFetch } }
}

test('渲染：下拉回显当前推理等级，切换后全局保存提交新值', async () => {
  const m = mockFetch({ reasoningEffort: 'max' })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const selects = renderer.root.findAll(
      (node) => node.type === 'select' && node.props.value === 'max')
    assert.equal(selects.length, 1, 'select 应回显后端返回的推理等级 max')

    // 切换为 off → 点击全局「保存」
    await TestRenderer.act(async () => {
      selects[0].props.onChange({ target: { value: 'off' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find(
      (b) => Array.isArray(b.props.children)
        ? String(b.props.children.join('')).trim() === '保存'
        : String(b.props.children || '').trim() === '保存')
    assert.ok(saveBtn, '应存在全局「保存」按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.ok(m.puts.length >= 1, '点击保存应发出 PUT 请求')
    const dsh = m.puts[m.puts.length - 1].dsh
    assert.ok(dsh, '提交应含 dsh 段')
    assert.equal(dsh.reasoning_effort, 'off', '提交 dsh.reasoning_effort 应为切换后的 off')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('渲染：后端未返回推理等级时回退空串展示', async () => {
  const m = mockFetch({ reasoningEffort: undefined })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const selects = renderer.root.findAll(
      (node) => node.type === 'select' && node.props.value === '')
    assert.ok(selects.length >= 1, '后端未返回推理等级时 select 应回退空串（默认）')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
