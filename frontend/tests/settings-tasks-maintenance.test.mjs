// 设置页「任务调度」卡片维护模式开关测试（issue #241）：
//
// 需求：任务调度卡片新增「维护模式」开关（config worker.maintenance_mode +
// 内存态），开启后调度器停止派发新任务（新事件入队保留，恢复后自动执行），
// 运行中任务不中断；可配置「维护期间新事件」处理方式（入队保留 / 直接忽略）；
// 开启时卡片内显示醒目警示条。
//
// 断言：
// 1. 卡片含「维护模式」区块（worker.maintenance_mode / 处理方式标注）；
// 2. 开启时渲染警示条（「维护模式已开启」）；
// 3. 全局「保存」提交 maintenance_mode / maintenance_hold_events 字段；
// 4. 渲染：勾选开关后警示条出现，保存请求携带正确字段。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
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

/** 提取指定标题卡片的源码片段（与 settings-issue-priority 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名函数源码片段（与 settings-issue-priority 同款工具） */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} =[\\s\\S]*?(?=\\n  const |\\n  return \\()`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('任务调度卡片含「维护模式」区块（配置键与说明齐全）', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /维护模式/, '卡片应含维护模式区块')
  assert.match(card, /worker\.maintenance_mode/, '应标注配置键 worker.maintenance_mode')
  assert.match(card, /worker\.maintenance_hold_events/, '应标注新事件处理方式配置键')
  assert.match(card, /紧急情况下一键暂停全部任务派发/, '应有功能说明')
  assert.match(card, /webhook 事件照常接收/, '应说明 webhook 照常接收')
  assert.match(card, /已运行任务不中断/, '应说明运行中任务不中断')
})

test('维护模式开启时渲染警示条', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(
    card,
    /settings\.worker\?\.maintenance_mode &&/,
    '警示条应仅在维护模式开启时渲染',
  )
  assert.match(card, /维护模式已开启：新任务暂停派发/, '警示条应含状态文案')
})

test('save 函数提交 maintenance_mode 与 maintenance_hold_events', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /worker\.maintenance_mode = settings\.worker\?\.maintenance_mode === true/,
    'save 应提交维护模式开关（布尔）')
  assert.match(body, /worker\.maintenance_hold_events = settings\.worker\?\.maintenance_hold_events !== false/,
    'save 应提交新事件处理方式（默认 true）')
})

/** 递归收集节点下全部文本 */
function collectText(node) {
  let out = ''
  for (const c of node.children || []) {
    if (typeof c === 'string') out += c
    else out += collectText(c)
  }
  return out
}

/** 渲染用 fetch mock（与 settings-issue-priority 同款，覆盖设置页全部接口） */
function mockFetch({ maintenanceMode = false } = {}) {
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
          worker: {
            issue_priority: ['bug', 'test', 'feature'],
            maintenance_mode: maintenanceMode,
            maintenance_hold_events: true,
          },
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

test('渲染：勾选维护模式开关 → 警示条出现，全局保存提交正确字段', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })

    // 维护模式开关：按 label 文本「开启维护模式」定位（任务调度卡片内）
    const maintLabels = renderer.root.findAll((n) => {
      if (n.type !== 'label') return false
      return collectText(n).includes('开启维护模式')
    })
    assert.equal(maintLabels.length, 1, '应渲染「开启维护模式」开关')
    const maintInput = maintLabels[0].children.find((c) => c.type === 'input')
    assert.ok(maintInput, '开关应为 input 元素')

    // 勾选维护模式开关 → 警示条出现
    await TestRenderer.act(async () => {
      maintInput.props.onChange({ target: { checked: true } })
    })
    const warnings = renderer.root.findAll(
      (node) => String(node.props.className || '').includes('alert-warning'))
    assert.ok(warnings.length >= 1, '勾选后应出现警示条')

    // 点击全局「保存」→ PUT 提交 maintenance_mode: true
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
    assert.equal(worker.maintenance_mode, true, '保存应提交 maintenance_mode=true')
    assert.equal(worker.maintenance_hold_events, true, '默认应提交 maintenance_hold_events=true')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
