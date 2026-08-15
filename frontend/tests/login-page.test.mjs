// Synology SSO 登录页测试（issue #104 补测）：登录页此前无任何测试覆盖。
//
// 断言：
// 1. 源码：点击登录按钮跳转 /api/auth/login；内置 login_failed /
//    access_denied 错误文案映射；
// 2. 渲染：无 error 参数不渲染错误提示；error=login_failed /
//    access_denied 显示映射文案；未知 error 原样透传展示；
// 3. 交互：点击「使用群晖账号登录」设置 window.location.href。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// 用 Login 专用 router mock（useSearchParams 查询参数可注入，
// 见 tests/helpers/mock-router-login.jsx）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router-login.jsx'),
    },
  },
})
const { default: Login } = await vite.ssrLoadModule('/src/pages/Login.jsx')

const loginSrc = readFileSync(path.join(ROOT, 'src/pages/Login.jsx'), 'utf8')

after(() => vite.close())

// ---- 源码断言 ----

test('登录页点击按钮跳转 SSO 登录端点 /api/auth/login', () => {
  assert.match(loginSrc, /使用群晖账号登录/, '应有登录按钮文案')
  assert.match(loginSrc, /window\.location\.href = '\/api\/auth\/login'/, '点击应跳转 SSO 登录端点')
})

test('内置 login_failed / access_denied 错误文案映射', () => {
  assert.match(loginSrc, /login_failed:\s*'登录失败：与群晖 SSO 服务器通信出错/, '应有通信失败映射文案')
  assert.match(loginSrc, /access_denied:\s*'已在群晖 SSO 登录页取消授权'/, '应有取消授权映射文案')
})

// ---- 渲染断言 ----

// 注入查询参数并渲染登录页（__LOGIN_SEARCH_PARAMS 由 mock-router-login.jsx 读取）
async function renderLogin(search = '') {
  globalThis.__LOGIN_SEARCH_PARAMS = search
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Login))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

// 节点内所有可读文本（TestInstance / 元素 / 数组通用）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

// 错误提示节点（alert-error），无则 null
function findAlert(renderer) {
  return renderer.root.findAll((node) =>
    typeof node.props?.className === 'string'
    && node.props.className.includes('alert'))
}

test('无 error 参数：不渲染错误提示，仅登录按钮', async () => {
  const { renderer, renderError } = await renderLogin('')
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  assert.equal(findAlert(renderer).length, 0, '无 error 时不应有错误提示')
  const text = textOf(renderer.root)
  assert.match(text, /登录 Botler/, '应渲染标题')
  assert.match(text, /使用群晖账号登录/, '应渲染登录按钮')
})

test('error=login_failed：显示「与群晖 SSO 服务器通信出错」映射文案', async () => {
  const { renderer } = await renderLogin('error=login_failed')
  const text = textOf(findAlert(renderer)[0])
  assert.match(text, /登录失败：与群晖 SSO 服务器通信出错，请检查配置后重试/)
})

test('error=access_denied：显示「已在群晖 SSO 登录页取消授权」映射文案', async () => {
  const { renderer } = await renderLogin('error=access_denied')
  const text = textOf(findAlert(renderer)[0])
  assert.match(text, /已在群晖 SSO 登录页取消授权/)
})

test('未知 error：原样透传展示', async () => {
  const { renderer } = await renderLogin('error=sso_unknown_xyz')
  const text = textOf(findAlert(renderer)[0])
  assert.match(text, /sso_unknown_xyz/, '未知错误应原样展示')
})

// ---- 交互断言 ----

test('点击登录按钮 → window.location.href = /api/auth/login', async () => {
  const { renderer } = await renderLogin('')
  // 模拟浏览器 window.location（node 环境无 window）
  const savedWindow = globalThis.window
  globalThis.window = { location: {} }
  try {
    const btn = renderer.root.findAllByType('button')
      .find((b) => textOf(b.props.children).includes('使用群晖账号登录'))
    assert.ok(btn, '应有登录按钮')
    await TestRenderer.act(async () => { btn.props.onClick() })
    assert.equal(globalThis.window.location.href, '/api/auth/login')
  } finally {
    if (savedWindow === undefined) delete globalThis.window
    else globalThis.window = savedWindow
  }
})
