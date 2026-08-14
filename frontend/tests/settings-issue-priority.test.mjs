// 设置页 issue 标签优先级配置测试（issue #76）：任务调度卡片增加
// worker.issue_priority 配置（逗号分隔的标签优先级顺序），默认 bug 最优先，
// 同仓库队列内调度按此顺序派发 issue。
//
// 断言：
// 1. 「任务调度」卡片含「issue 标签优先级」输入行（worker.issue_priority）；
// 2. 输入为逗号分隔文本，onChange 转为数组存储（trim + 过滤空项）；
// 3. 全局「保存」提交 worker 段含 issue_priority 数组；
// 4. 渲染时数组 join(', ') 回显；后端未返回时回退默认值；
// 5. 说明文字写明「默认 bug 最优先、未列出标签排最后、同优先级按更新时间」。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settingsSrc = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-sso-save-button.test.mjs 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名函数源码片段：从「const name =」到下一个顶层 const / return 之前
    （兼容块体与表达式体箭头函数） */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} =[\\s\\S]*?(?=\\n  const |\\n  return \\()`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('任务调度卡片含「issue 标签优先级」输入行', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /issue 标签优先级/, '卡片应含 issue 标签优先级输入行')
  assert.match(card, /worker\.issue_priority/, '行标题应标注配置键 worker.issue_priority')
  assert.match(card, /placeholder="bug, test, feature"/, '输入框应有默认顺序占位提示')
  assert.match(card, /setIssuePriority/, '输入框 onChange 应绑定 setIssuePriority')
})

test('说明文字写明默认顺序与排序规则', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /bug 最优先/, '说明应写明默认 bug 最优先')
  assert.match(card, /未列出的标签排在最后/, '说明应写明未列出标签排最后')
  assert.match(card, /更新时间/, '说明应写明同优先级按 issue 更新时间升序')
})

test('setIssuePriority 将逗号分隔文本转为数组（trim + 过滤空项）', () => {
  const body = fnBody(settingsSrc, 'setIssuePriority')
  assert.ok(body, '应存在 setIssuePriority 函数')
  assert.match(body, /split\(','\)/, '应按逗号拆分')
  assert.match(body, /\.trim\(\)/, '应去除每项首尾空白')
  assert.match(body, /filter\(Boolean\)/, '应过滤空项')
})

test('全局保存提交 worker.issue_priority 数组', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /issue_priority/, 'save 应携带 issue_priority')
  assert.match(
    body,
    /worker\.issue_priority = settings\.worker\.issue_priority \|\| \['bug', 'test', 'feature'\]/,
    '后端未返回时应回退默认顺序提交',
  )
})

/** 渲染用 fetch mock（与 settings-owner-token.test.mjs 同款，覆盖设置页全部接口） */
function mockFetch({ priority = ['feature', 'bug', 'test'] } = {}) {
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
          worker: { issue_priority: priority },
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

test('渲染：issue_priority 数组 join 回显，修改后全局保存提交过滤空项的数组', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const inputs = renderer.root.findAll(
      (node) => node.type === 'input' && node.props.value === 'feature, bug, test')
    assert.equal(inputs.length, 1, '输入框应回显数组 join 后的文本')

    // 修改输入（含空项）→ 触发 setIssuePriority → 点击全局「保存」
    await TestRenderer.act(async () => {
      inputs[0].props.onChange({ target: { value: 'bug, , feature' } })
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
    assert.deepEqual(worker.issue_priority, ['bug', 'feature'],
      '空项应被过滤，提交数组应为 [bug, feature]')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
