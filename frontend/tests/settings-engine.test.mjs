// 设置页「任务执行引擎」设置项测试（issue #113）：任务调度卡片增加
// worker.engine 下拉设置项，用来切换后端编写代码的 agent
// （claude / hermes / dsh），保存后写回 config.yaml 对后续任务生效。
//
// 断言：
// 1. 「任务调度」卡片含「任务执行引擎」行（worker.engine），select 三选项；
// 2. 渲染时按后端返回值回显当前引擎，后端未返回时回退 claude；
// 3. 修改选择后点击全局「保存」，提交 worker 段含 engine 值；
// 4. 说明文字写明三种引擎的含义与默认值。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：任务调度卡片 JSX 移到 components/settings/TasksCard.jsx，
// save 收敛到 hooks/useSettingsData.js——静态断言跟随新文件
const tasksCard = readFileSync(path.join(ROOT, 'src/components/settings/TasksCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsSrc = tasksCard + '\n' + hook

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

test('任务调度卡片含「任务执行引擎」下拉设置项', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /任务执行引擎/, '卡片应含任务执行引擎行')
  assert.match(card, /worker\.engine/, '行标题应标注配置键 worker.engine')
  assert.match(card, /<select/, '引擎选择应使用 select 下拉框')
  assert.match(card, /value="claude"/, '应含 claude 选项')
  assert.match(card, /value="hermes"/, '应含 hermes 选项')
  assert.match(card, /value="dsh"/, '应含 dsh 选项')
})

test('说明文字写明引擎含义与默认值', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /Claude Code CLI/, '说明应写明 claude = Claude Code CLI')
  assert.match(card, /hermes/, '说明应提及 hermes 引擎')
  assert.match(card, /deepseek-harness/, '说明应提及 dsh 引擎')
  assert.match(card, /默认/, '说明应写明默认引擎')
})

test('全局保存提交 worker.engine（后端未返回时回退 claude）', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /engine/, 'save 应携带 engine')
  assert.match(
    body,
    /worker\.engine = settings\.worker\?\.engine \|\| 'claude'/,
    '后端未返回 engine 时应回退默认 claude 提交',
  )
})

/** 渲染用 fetch mock（与 settings-issue-priority.test.mjs 同款，覆盖设置页全部接口） */
function mockFetch({ engine = 'claude' } = {}) {
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
          worker: { issue_priority: ['bug'], engine },
          sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '' }, notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {}, ai_providers: [],
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

test('渲染：select 回显当前引擎，切换后全局保存提交新引擎', async () => {
  const m = mockFetch({ engine: 'dsh' })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const selects = renderer.root.findAll(
      (node) => node.type === 'select' && node.props.value === 'dsh')
    assert.equal(selects.length, 1, 'select 应回显后端返回的引擎 dsh')

    // 切换为 hermes → 点击全局「保存」
    await TestRenderer.act(async () => {
      selects[0].props.onChange({ target: { value: 'hermes' } })
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
    const worker = m.puts[m.puts.length - 1].worker
    assert.equal(worker.engine, 'hermes', '提交 worker.engine 应为切换后的 hermes')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('渲染：后端未返回 engine 时回退 claude 展示与提交', async () => {
  const m = mockFetch({ engine: undefined })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const selects = renderer.root.findAll(
      (node) => node.type === 'select' && node.props.value === 'claude')
    assert.equal(selects.length, 1, '后端未返回 engine 时 select 应回退 claude')

    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find(
      (b) => Array.isArray(b.props.children)
        ? String(b.props.children.join('')).trim() === '保存'
        : String(b.props.children || '').trim() === '保存')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const worker = m.puts[m.puts.length - 1].worker
    assert.equal(worker.engine, 'claude', '提交 worker.engine 应回退默认 claude')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
