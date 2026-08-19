// 导航栏用户区测试（issue #271）：SSO 登录后右上角展示昵称/头像与「退出
// 登录」按钮，未启用 SSO 显示「未登录（开放模式）」弱提示，会话过期时间
// tooltip 展示，头像加载失败回退首字母占位，用户信息复用 /api/auth/me。
//
// 断言：
// 1. 源码：UserMenu 复用 /api/auth/me 获取用户信息、退出调 POST
//    /api/auth/logout、头像 img onError 回退首字母；i18n key 中英齐全；
// 2. 渲染：SSO 启用登录 → 昵称 + 头像 + 退出按钮；无 picture → 首字母
//    占位；picture 加载失败 → 回退首字母；tooltip 含会话过期时间；
// 3. 边界：未启用 SSO（user null）→ 弱提示不报错；/api/auth/me 失败 →
//    保持初始用户；退出按钮 → 调用 logout 接口并跳登录页。
import { after, beforeEach, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))
const src = readFileSync(path.join(ROOT, 'src/components/UserMenu.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
after(() => vite.close())
beforeEach(() => toastMod.clearToasts())
const { default: UserMenu } = await vite.ssrLoadModule('/src/components/UserMenu.jsx')
const { api, fmtTime, fmtSeconds } = await vite.ssrLoadModule('/src/api.js')
// 同一 vite 实例加载 toast 模块（与 UserMenu 内部 import 同一实例，
// 保证 currentToasts 能看到 showToast 入队的临期提醒）
const toastMod = await vite.ssrLoadModule('/src/toast.js')

// 模拟浏览器 window（退出登录跳转写 window.location.href）
globalThis.window = { location: { href: '' } }

// 用户信息：含 OIDC picture（头像）与 exp（会话过期，unix 秒）
const LOGGED_IN_USER = {
  sub: 'uid-1',
  username: 'zhangsan',
  name: '张三',
  email: 'zs@example.com',
  picture: 'https://nas.example.com/avatar/zhangsan.png',
  exp: 1800000000, // 2027-01-15 附近，unix 秒
}

// 渲染树节点 → 纯文本（与现有测试同法）：只收叶子文本，不重复计数。
// 兼容两种节点：renderer.toJSON() 的 JSON 节点（children 为顶层字段）与
// renderer.root 的 TestInstance（children 在 props 下）
function texts(node) {
  if (node == null || typeof node === 'boolean') return []
  if (typeof node === 'string' || typeof node === 'number') return [String(node)]
  if (Array.isArray(node)) return node.flatMap(texts)
  const children = node.children ?? node.props?.children
  return children == null ? [] : texts(children)
}

/** 以指定 props 渲染 UserMenu，等待 /api/auth/me 异步刷新完成 */
async function renderMenu(props) {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(UserMenu, props))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

// ---- 1. 源码断言 ----

test('源码：用户信息复用 /api/auth/me，退出调用 POST /api/auth/logout', () => {
  assert.match(src, /api\.get\('\/api\/auth\/me'\)/, '应复用 /api/auth/me 获取用户信息')
  assert.match(src, /api\.post\('\/api\/auth\/logout'\)/, '退出应调用 POST /api/auth/logout')
  assert.match(src, /window\.location\.href = '\/login'/, '退出成功后应跳转登录页')
})

test('源码：头像加载失败 onError 回退首字母占位', () => {
  assert.match(src, /onError=\{\(\) => setAvatarFailed\(true\)\}/, '图片 onError 应触发回退')
  assert.match(src, /user-avatar-fallback/, '应有首字母占位样式类')
  assert.match(src, /user\.name \|\| user\.username \|\| user\.sub/, '昵称应按 name→username→sub 兜底')
})

test('i18n：nav.notLoggedIn / nav.sessionExpiry 中英字典齐全', () => {
  for (const k of ['nav.notLoggedIn', 'nav.sessionExpiry', 'nav.userTitle', 'nav.logout']) {
    assert.equal(typeof zhCN[k], 'string', `zh-CN 缺 ${k}`)
    assert.equal(typeof enUS[k], 'string', `en-US 缺 ${k}`)
  }
})

// ---- 2. 渲染 ----

test('SSO 启用登录：显示昵称、头像图片与退出按钮，并复用 /api/auth/me', async () => {
  const requested = []
  mock.method(api, 'get', async (p) => {
    requested.push(String(p))
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    const flat = texts(renderer.toJSON())
    assert.ok(flat.includes('张三'), '应显示昵称 name')
    assert.ok(flat.includes('退出'), '应显示退出按钮文案')
    const img = renderer.root.findByType('img')
    assert.equal(img.props.src, LOGGED_IN_USER.picture, '头像应显示 OIDC picture')
    assert.ok(requested.includes('/api/auth/me'), '应调用 /api/auth/me 获取用户信息')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('无 picture：直接回退首字母占位，不渲染 img', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER, picture: null }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: { ...LOGGED_IN_USER, picture: null }, ssoEnabled: true })
    assert.equal(renderer.root.findAllByType('img').length, 0, '无 picture 不应渲染 img')
    const fallback = renderer.root.findByProps({ className: 'user-avatar user-avatar-fallback' })
    assert.equal(texts(fallback).join(''), '张', '首字母占位应为昵称首字「张」')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('头像加载失败（onError）→ 回退首字母占位（验收标准 3）', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    assert.equal(renderer.root.findAllByType('img').length, 1, 'picture 正常时应渲染 img')
    // 模拟图片加载失败：触发 onError
    await TestRenderer.act(() => {
      renderer.root.findByType('img').props.onError()
    })
    assert.equal(renderer.root.findAllByType('img').length, 0, '加载失败后应移除 img')
    const fallback = renderer.root.findByProps({ className: 'user-avatar user-avatar-fallback' })
    assert.equal(texts(fallback).join(''), '张', '应回退为昵称首字「张」')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('会话过期时间 tooltip 展示（与 #221 联动）', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    const chip = renderer.root.findByProps({ className: 'user-chip' })
    const expected = fmtTime(LOGGED_IN_USER.exp)
    assert.ok(expected !== '—', 'exp 应能被 fmtTime 格式化')
    assert.ok(
      chip.props.title.includes(expected),
      `tooltip 应包含会话过期时间（期望含 ${expected}，实际 ${chip.props.title}）`
    )
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 3. 边界场景 ----

test('未启用 SSO：显示「未登录（开放模式）」弱提示，不报错（验收标准 2）', async () => {
  const requested = []
  mock.method(api, 'get', async (p) => {
    requested.push(String(p))
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  let renderError = null
  try {
    await TestRenderer.act(async () => {
      try {
        renderer = TestRenderer.create(React.createElement(UserMenu, { user: null, ssoEnabled: false }))
        await new Promise((resolve) => setTimeout(resolve, 20))
      } catch (e) {
        renderError = e
      }
    })
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const flat = texts(renderer.toJSON())
    assert.ok(flat.includes('未登录（开放模式）'), '应显示开放模式弱提示')
    assert.equal(renderer.root.findAllByType('button').length, 0, '未登录不应有退出按钮')
    assert.equal(requested.length, 0, '未登录不应发起 /api/auth/me 请求')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('/api/auth/me 失败（401 等）→ 保持初始用户展示，不报错', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') throw new Error('未登录（SSO 已启用）')
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    const flat = texts(renderer.toJSON())
    assert.ok(flat.includes('张三'), 'me 失败后仍应展示初始用户昵称')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('退出按钮：调用 POST /api/auth/logout 并跳转登录页', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER }
    throw new Error('unexpected ' + p)
  })
  const posted = []
  mock.method(api, 'post', async (p) => {
    posted.push(String(p))
    if (p === '/api/auth/logout') return { ok: true }
    throw new Error('unexpected ' + p)
  })
  window.location.href = ''
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    await TestRenderer.act(() => {
      renderer.root.findByType('button').props.onClick()
    })
    assert.ok(posted.includes('/api/auth/logout'), '应调用 POST /api/auth/logout')
    assert.equal(window.location.href, '/login', '退出后应跳转登录页')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})


// ---- issue #221：会话剩余时间 / 临近过期提示续期 ----

// 会话即将过期用户：12 小时后过期（剩余 12 小时，≤ 1 天阈值）
function expiringUser(exp = Math.floor(Date.now() / 1000) + 12 * 3600) {
  return { ...LOGGED_IN_USER, exp }
}

test('源码：剩余时间经 fmtSeconds 计算，续期跳 /api/auth/login，i18n 词条齐全', () => {
  assert.match(src, /fmtSeconds/, '应使用 fmtSeconds 计算会话剩余时间')
  assert.match(src, /window\.location\.href = '\/api\/auth\/login'/, '续期按钮应跳转 SSO 登录端点重新登录')
  for (const k of ['nav.sessionRemain', 'nav.sessionExpiring', 'nav.renew']) {
    assert.equal(typeof zhCN[k], 'string', `zh-CN 缺 ${k}`)
    assert.equal(typeof enUS[k], 'string', `en-US 缺 ${k}`)
  }
})

test('会话剩余时间：tooltip 展示「剩余 X」相对时间（非临期）', async () => {
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...LOGGED_IN_USER }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: LOGGED_IN_USER, ssoEnabled: true })
    const chip = renderer.root.findByProps({ className: 'user-chip' })
    const remainSec = Math.floor((LOGGED_IN_USER.exp * 1000 - Date.now()) / 1000)
    const remainText = fmtSeconds(remainSec)
    assert.ok(remainText, '剩余时间应可计算')
    assert.ok(
      chip.props.title.includes(remainText),
      `tooltip 应包含剩余时间（期望含 ${remainText}，实际 ${chip.props.title}）`
    )
    // 非临期不渲染续期按钮
    const buttons = renderer.root.findAllByType('button').map((b) => texts(b.props.children).join(''))
    assert.ok(!buttons.includes('续期'), '非临期不应显示续期按钮')
    // 非临期不弹临期 toast
    assert.equal(toastMod.currentToasts().length, 0, '非临期不应弹临期提醒 toast')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('临近过期（剩 ≤1 天）：chip 高亮、tooltip 提示续期、显示续期按钮并弹提醒 toast', async () => {
  const u = expiringUser()
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...u }
    throw new Error('unexpected ' + p)
  })
  let renderer = null
  try {
    renderer = await renderMenu({ user: u, ssoEnabled: true })
    // chip 高亮 class
    const chip = renderer.root.findByProps({ className: 'user-chip user-chip-expiring' })
    // tooltip 提示续期
    assert.match(chip.props.title, /即将过期/, '临期 tooltip 应提示即将过期')
    assert.match(chip.props.title, /续期/, '临期 tooltip 应引导续期')
    // 续期按钮
    const buttons = renderer.root.findAllByType('button').map((b) => texts(b.props.children).join(''))
    assert.ok(buttons.includes('续期'), '临期应显示续期按钮')
    // 临期提醒 toast
    const toasts = toastMod.currentToasts()
    assert.equal(toasts.length, 1, '临期应弹一次提醒 toast')
    assert.match(String(toasts[0].message), /即将过期/, 'toast 应提示会话即将过期')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('临近过期：点击续期按钮 → 跳转 /api/auth/login 重新登录', async () => {
  const u = expiringUser()
  mock.method(api, 'get', async (p) => {
    if (p === '/api/auth/me') return { ...u }
    throw new Error('unexpected ' + p)
  })
  window.location.href = ''
  let renderer = null
  try {
    renderer = await renderMenu({ user: u, ssoEnabled: true })
    const renewBtn = renderer.root.findAllByType('button')
      .find((b) => texts(b.props.children).join('').includes('续期'))
    assert.ok(renewBtn, '应有续期按钮')
    await TestRenderer.act(() => { renewBtn.props.onClick() })
    assert.equal(window.location.href, '/api/auth/login', '点击续期应跳转 SSO 登录端点')
  } finally {
    mock.restoreAll()
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})
