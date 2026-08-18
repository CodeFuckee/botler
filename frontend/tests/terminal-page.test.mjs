// Web 终端页面测试（issue #183）：
// - App.jsx 顶部导航「终端」入口 + /terminal 路由注册；
// - Icon.jsx 注册 terminal 图标、styles.css 提供终端样式；
// - 页面交互（mock fetch /api/terminal/token）：新建/关闭/切换标签、上限、
//   token 获取失败提示、后端 API 定义（token 签发 + ws/health 反向代理）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const page = readFileSync(path.join(ROOT, 'src/pages/Terminal.jsx'), 'utf8')
const view = readFileSync(path.join(ROOT, 'src/terminal/TerminalView.jsx'), 'utf8')
const icons = readFileSync(path.join(ROOT, 'src/components/Icon.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiTerminal = readFileSync(path.join(ROOT, '../backend/botler/api/terminal.py'), 'utf8')
const termService = readFileSync(path.join(ROOT, '../backend/terminal_service.py'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: TerminalPage } = await vite.ssrLoadModule('/src/pages/Terminal.jsx')

after(() => vite.close())

// ---- 静态断言：入口与资源 ----
test('App.jsx 顶部导航含「终端」入口并注册路由', () => {
  assert.match(app, /NavLink to="\/terminal"/, '导航应有到 /terminal 的 NavLink')
  assert.match(app, /终端/, '导航链接文案应为「终端」')
  assert.match(app, /Route path="\/terminal" element={<Terminal \/>}/, '应有 /terminal 路由')
  assert.match(app, /import Terminal from '\.\/pages\/Terminal\.jsx'/, '应导入 Terminal 页面')
})

test('Icon.jsx 注册 terminal 图标（Lucide Terminal）', () => {
  assert.match(icons, /Terminal as TerminalIcon/, '应导入 Lucide Terminal 图标')
  assert.match(icons, /terminal: TerminalIcon/, 'ICONS 映射应含 terminal')
})

test('styles.css 提供终端页样式', () => {
  for (const cls of ['terminal-tab', 'terminal-tabs', 'terminal-add', 'terminal-hint',
    'terminal-error', 'terminal-host', 'terminal-empty']) {
    assert.ok(new RegExp(`\\.${cls}\\s*\\{`).test(styles), `styles.css 应包含 .${cls} 规则`)
  }
})

// ---- 页面交互（mock fetch）----
function mockFetch({ ok = true } = {}) {
  const calls = []
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    calls.push({ p: String(p), opts })
    if (String(p) === '/api/terminal/token' && ok) {
      return { ok: true, status: 200, json: async () => ({ token: 'tok-abc', expires_in: 60 }) }
    }
    return { ok: false, status: 401, json: async () => ({ error: '未登录' }) }
  }
  return { calls, restore: () => { global.fetch = originalFetch } }
}

async function renderPage() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(TerminalPage))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

test('渲染：标题、快捷键提示与空状态', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await renderPage()
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /Web 终端/, '应渲染页面标题')
    assert.match(text, /Alt\+T/, '应提示新建标签快捷键')
    assert.match(text, /Ctrl\+Shift\+V/, '应提示粘贴快捷键')
    assert.match(text, /新建/, '应有新建按钮')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：点击新建获取 token 并新增标签（终端 1/终端 2）', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await renderPage()
    const findBtn = (label) => renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes(label))
    await TestRenderer.act(async () => {
      findBtn('新建').props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    let text = JSON.stringify(renderer.toJSON())
    assert.match(text, /终端 1/, '应新建「终端 1」标签')
    assert.match(text, /连接中/, '新标签初始状态应为连接中')
    assert.ok(m.calls.some((c) => c.p === '/api/terminal/token'), '应请求 /api/terminal/token')

    await TestRenderer.act(async () => {
      findBtn('新建').props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    text = JSON.stringify(renderer.toJSON())
    assert.match(text, /终端 2/, '应新建「终端 2」标签')
    // token 只请求一次（复用）
    const tokenCalls = m.calls.filter((c) => c.p === '/api/terminal/token')
    assert.equal(tokenCalls.length, 1, 'token 应只获取一次')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：关闭标签移除标签页', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await renderPage()
    const findBtn = (label) => renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes(label))
    for (let i = 0; i < 2; i += 1) {
      await TestRenderer.act(async () => {
        findBtn('新建').props.onClick()
        await new Promise((resolve) => setTimeout(resolve, 20))
      })
    }
    let text = JSON.stringify(renderer.toJSON())
    assert.match(text, /终端 1/, '应有终端 1')
    assert.match(text, /终端 2/, '应有终端 2')
    // 关闭「终端 1」：点击其关闭按钮（span.terminal-tab-close）
    const closeBtn = renderer.root.findAllByType('span')
      .find((s) => String(s.props['aria-label'] || '') === '关闭 终端 1')
    assert.ok(closeBtn, '终端 1 应有关闭按钮')
    await TestRenderer.act(async () => {
      closeBtn.props.onClick({ stopPropagation() {} })
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    text = JSON.stringify(renderer.toJSON())
    assert.doesNotMatch(text, /终端 1/, '关闭后不应再有终端 1')
    assert.match(text, /终端 2/, '终端 2 应保留')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：token 获取失败展示错误且不新增标签', async () => {
  const m = mockFetch({ ok: false })
  let renderer = null
  try {
    renderer = await renderPage()
    const findBtn = (label) => renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes(label))
    await TestRenderer.act(async () => {
      findBtn('新建').props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /无法获取终端访问凭证/, '应展示获取凭证失败提示')
    assert.doesNotMatch(text, /终端 1/, 'token 失败不应新增标签')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('TerminalView 渲染容器并携带数据属性', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await renderPage()
    const findBtn = (label) => renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes(label))
    await TestRenderer.act(async () => {
      findBtn('新建').props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    const host = renderer.root.findByProps({ 'data-tab': '终端 1' })
    assert.ok(host, '应渲染 terminal-host 容器')
    assert.equal(host.props.className, 'terminal-host')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

// ---- 后端 API 定义（与前端约定的契约）----
test('后端提供 token 签发 + ws/health 反向代理端点', () => {
  assert.match(apiTerminal, /APIRouter\(prefix="\/terminal"/, '终端 API 前缀应为 /terminal')
  assert.match(apiTerminal, /@router\.post\("\/token"\)/, '应有 POST /api/terminal/token')
  assert.match(apiTerminal, /@router\.get\("\/health"\)/, '应有 GET /api/terminal/health')
  assert.match(apiTerminal, /@router\.websocket\("\/ws\/\{name\}"\)/, '应有 WS /api/terminal/ws/{name}')
  assert.match(apiTerminal, /websockets\.connect/, '代理应使用 websockets 客户端连接上游')
  assert.match(termService, /terminado/, '终端服务应基于 terminado')
  assert.match(termService, /AuthTermSocket/, '终端服务应有认证 WebSocket 处理器')
  assert.match(termService, /verify_terminal_token/, '终端服务应校验终端 token')
})
