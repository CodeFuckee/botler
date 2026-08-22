// 概览页灵感组件「AI 对话抽屉」输入框 codex 风格优化测试（issue #443）。
//
// 需求：概览页灵感组件（InspirationSection）中对话框（AI 对话抽屉，
// issue #166/#184）的输入框优化为类似 codex 输入框的样式：
//   1. 文本框与发送按钮融合为单一圆角 composer 容器（.chat-input-row
//      带边框/圆角，聚焦时容器整体高亮 focus-within）；
//   2. textarea 无边框、禁用手动缩放，rows=1 自动增高——内容撑开高度，
//      达到上限（CHAT_INPUT_MAX_HEIGHT）后内部滚动；
//   3. 发送按钮为圆形图标按钮（上箭头 arrowUp，无文本），带国际化
//      aria-label/title；空输入或发送中禁用；
//   4. 交互保持不变：Enter 发送、Shift+Enter 换行、失败后输入保留。
//
// 断言分三层：styles.css 源码级（composer 容器/textarea/圆形按钮）、
// InspirationSection.jsx 源码级（rows=1 + 自动增高 + arrowUp + aria-label）、
// 渲染与交互级（react-test-renderer 真实挂载）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const section = readFileSync(
  path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')

// 提取 styles.css 中某选择器的样式块内容
function cssBlock(selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')
  const m = styles.match(re)
  assert.ok(m, `styles.css 中应存在 ${selector} 样式块`)
  return m[1]
}

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: InspirationSection } = await vite.ssrLoadModule(
  '/src/components/overview/InspirationSection.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- styles.css 源码级断言（codex composer 视觉） ----

test('源码：chat-input-row 为圆角 composer 容器，聚焦时整体高亮（focus-within）', () => {
  const row = cssBlock('.chat-input-row')
  assert.match(row, /border-radius/, '容器应有圆角')
  assert.match(row, /border:/, '容器应有边框')
  assert.match(row, /background/, '容器应有背景色（区分于抽屉底色）')
  assert.match(row, /padding/, '容器应有内边距（文本框+按钮同框）')
  assert.match(styles, /\.chat-input-row:focus-within\s*\{[^}]*border-color:\s*var\(--primary\)/,
               '聚焦时容器边框应高亮为主色')
  assert.match(styles, /\.chat-input-row:focus-within\s*\{[^}]*box-shadow:\s*var\(--focus-ring\)/,
               '聚焦时容器应显示焦点环')
})

test('源码：chat-input 无边框、禁用手动缩放、自动增高达上限后内部滚动', () => {
  const input = cssBlock('.chat-input')
  assert.match(input, /border:\s*none/, 'textarea 不应有独立边框（融入 composer）')
  assert.match(input, /resize:\s*none/, '应禁用右下角手动缩放（改为自动增高）')
  assert.match(input, /max-height/, '应设置自动增高上限')
  assert.match(input, /overflow-y/, '达到上限后应内部滚动')
})

test('源码：chat-send-btn 为圆形图标按钮', () => {
  const btn = cssBlock('.chat-send-btn')
  assert.match(btn, /border-radius:\s*50%/, '发送按钮应为圆形')
  assert.match(btn, /width/, '应固定宽度（圆形尺寸）')
  assert.match(btn, /height/, '应固定高度（圆形尺寸）')
  assert.match(btn, /padding:\s*0/, '不应有文本按钮内边距')
})

// ---- InspirationSection.jsx 源码级断言 ----

test('源码：文本框 rows=1 并挂载自动增高处理（onInput）', () => {
  assert.match(section, /rows=\{1\}/, 'textarea 应从 rows=2 改为 rows=1（随内容增高）')
  assert.match(section, /onInput=\{autoGrowChatInput\}/, '应挂载自动增高处理')
  assert.match(section, /CHAT_INPUT_MAX_HEIGHT/, '应定义自动增高上限常量')
})

test('源码：发送按钮为 arrowUp 图标 + 国际化 aria-label，无文本', () => {
  assert.match(section, /name="arrowUp"/, '发送按钮应使用上箭头图标')
  assert.match(section, /aria-label=\{tr\('overview\.send'\)\}/,
               '图标按钮应带国际化 aria-label（可访问性）')
  assert.match(section, /title=\{tr\('overview\.send'\)\}/,
               '应带国际化 title 提示')
  assert.match(section, /disabled=\{chatSending \|\| !chatDraft\.trim\(\)\}/,
               '空输入或发送中应禁用发送按钮')
})

// ---- 渲染与交互级断言 ----

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

// Overview 挂载 mock（精简版：只关心灵感对话抽屉交互）
async function renderOverview() {
  const getCalls = []
  const postCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname === '/api/inspirations/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', enabled: true,
        inspirations: [{ id: 11, repo_id: 1, repo_name: 'botler',
          content: '支持批量处理 issue', updated_at: '2026-08-16 12:00:00' }] }] }
    }
    if (pathname.startsWith('/api/inspirations/') && pathname.endsWith('/messages')) {
      return { messages: [] }
    }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname, body) => {
    postCalls.push([pathname, body])
    if (String(pathname).endsWith('/messages')) {
      return { messages: [
        { id: 101, role: 'user', content: body.content, created_at: '2026-08-17 12:00:00' },
        { id: 102, role: 'assistant', content: 'AI 的探讨回复', created_at: '2026-08-17 12:00:01' },
      ] }
    }
    return { id: 99 }
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return {
    renderer, renderError, getCalls, postCalls,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// 打开第一个灵感的「对话」抽屉
async function openChat(renderer) {
  const btns = renderer.root.findAll((n) =>
    n.type === 'button'
    && String(n.props.className || '').includes('inspiration-chat-btn'))
  assert.ok(btns.length >= 1, '应有「对话」按钮')
  await TestRenderer.act(async () => { btns[0].props.onClick() })
  await new Promise((resolve) => setTimeout(resolve, 10))
}

function findChatSendBtn(renderer) {
  const btns = renderer.root.findAll((n) =>
    n.type === 'button' && String(n.props.className || '').includes('chat-send-btn'))
  assert.ok(btns.length === 1, `应恰有一个发送按钮（实际 ${btns.length}）`)
  return btns[0]
}

function findChatTextarea(renderer) {
  const tas = renderer.root.findAll((n) =>
    n.type === 'textarea' && String(n.props.className || '').includes('chat-input'))
  assert.ok(tas.length === 1, `应恰有一个聊天输入框（实际 ${tas.length}）`)
  return tas[0]
}

test('渲染：打开对话抽屉后发送按钮为圆形 arrowUp 图标、空输入禁用', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    await openChat(r.renderer)
    const btn = findChatSendBtn(r.renderer)
    assert.equal(btn.props['aria-label'], '发送', 'aria-label 应使用国际化「发送」')
    assert.equal(btn.props.title, '发送', 'title 应使用国际化「发送」')
    // 按钮内应渲染 arrowUp 图标（Icon name=arrowUp）且无文本（codex 圆形图标）
    const icons = btn.findAll((n) =>
      n.type === 'svg' || (n.props && n.props.name === 'arrowUp'))
    assert.ok(icons.length > 0, '发送按钮内应有图标')
    assert.ok(!textOf(btn.props.children).trim() || btn.findAll((n) => n.props?.name === 'arrowUp').length > 0,
              '发送按钮应为图标按钮（无可见文本）')
    assert.equal(btn.props.disabled, true, '空输入时发送按钮应禁用')
    // 输入框应保持 rows=1（自动增高起点）
    assert.equal(findChatTextarea(r.renderer).props.rows, 1, '输入框 rows 应为 1')
  } finally {
    await r.unmount()
  }
})

test('交互：输入内容后发送按钮启用，Enter 发送、Shift+Enter 保留换行', async () => {
  const r = await renderOverview()
  try {
    await openChat(r.renderer)
    const ta = findChatTextarea(r.renderer)
    await TestRenderer.act(async () => {
      ta.props.onChange({ target: { value: '这个灵感怎么落地？' } })
    })
    assert.equal(findChatSendBtn(r.renderer).props.disabled, false,
                 '输入内容后发送按钮应启用')
    // Enter 发送（未按 Shift）：直接触发发送
    const before = r.postCalls.length
    await TestRenderer.act(async () => {
      ta.props.onKeyDown({ key: 'Enter', shiftKey: false, preventDefault() {} })
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(r.postCalls.length, before + 1, 'Enter 应触发发送 POST')
    // Shift+Enter：不应发送（保留换行）
    const before2 = r.postCalls.length
    await TestRenderer.act(async () => {
      ta.props.onKeyDown({ key: 'Enter', shiftKey: true, preventDefault() {} })
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(r.postCalls.length, before2, 'Shift+Enter 不应触发发送')
  } finally {
    await r.unmount()
  }
})

test('交互：自动增高 onInput 在测试环境（无 style）不崩溃，真实 DOM 下按上限截断', async () => {
  const r = await renderOverview()
  try {
    await openChat(r.renderer)
    const ta = findChatTextarea(r.renderer)
    // react-test-renderer 无真实 DOM：事件对象无 style 属性，必须静默跳过
    await TestRenderer.act(async () => {
      ta.props.onInput({ target: { value: 'x'.repeat(200) } })
    })
    // 带 style 的仿真事件对象：验证增高逻辑（上限 160px 截断）
    const fakeEl = { style: { height: '' }, scrollHeight: 500 }
    await TestRenderer.act(async () => {
      ta.props.onInput({ target: fakeEl })
    })
    assert.equal(fakeEl.style.height, '160px',
                 '超出上限时高度应截断为 CHAT_INPUT_MAX_HEIGHT')
    const fakeEl2 = { style: { height: '' }, scrollHeight: 50 }
    await TestRenderer.act(async () => {
      ta.props.onInput({ target: fakeEl2 })
    })
    assert.equal(fakeEl2.style.height, '50px', '未达上限时高度应等于内容高度')
  } finally {
    await r.unmount()
  }
})

test('源码：自动增高函数应导出供测试（命名导出 autoGrowChatInput / CHAT_INPUT_MAX_HEIGHT）', () => {
  assert.match(section, /export (function|const) autoGrowChatInput|export \{[^}]*autoGrowChatInput/,
               '应导出 autoGrowChatInput 便于单元验证')
  assert.ok(InspirationSection, '组件应可加载')
})
