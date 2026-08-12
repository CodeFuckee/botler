// App 组件 SSO 无限刷新回归测试（issue #27 第五轮）
// 现象：配置（启用）Synology SSO 后，页面一直刷新。
// 根因：App.jsx 的「时区加载」useEffect 无条件请求 /api/settings，没有像
// 通知轮询那样加 auth 守卫——SSO 启用未登录时该请求被 SsoGuardMiddleware
// 401，api.js 兜底 window.location.href='/login' 整页重载；重载后渲染登录页
// 时该 effect 再次无条件发起 /api/settings → 又 401 → 又重载，无限循环。
// 本测试渲染 App 并模拟后端：/api/auth/status 返回 {enabled:true,user:null}
// （SSO 启用未登录），其余 API 一律 401（中间件行为）。断言：登录页状态下
// 除 /api/auth/ 前缀外不得发起任何受保护 API 请求、不得触发登录页跳转。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import TestRenderer from 'react-test-renderer'

// node --test 原生不支持 jsx，用 vite SSR 转译加载 App 组件
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')

// 模拟浏览器 window（api.js 401 兜底写 window.location.href；node 环境无 window）
globalThis.window = { location: { href: '' } }

// 测试结束后关闭 vite server，否则进程不退出
after(() => vite.close())

test('SSO 启用未登录时登录页不得发起受保护请求（回归 issue #27 无限刷新）', async () => {
  // mock fetch：记录所有请求路径；/api/auth/status 返回 SSO 启用未登录，
  // 其余 API 一律 401（与后端 SsoGuardMiddleware 行为一致）
  const requested = []
  const originalFetch = global.fetch
  global.fetch = async (path) => {
    requested.push(String(path))
    if (String(path).startsWith('/api/auth/status')) {
      return { ok: true, status: 200, json: async () => ({ enabled: true, user: null }) }
    }
    return { ok: false, status: 401, json: async () => ({ error: '未登录（SSO 已启用）' }) }
  }
  try {
    let renderer = null
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(App))
      )
      // 等待 auth 状态加载完成（/api/auth/status → setAuth → 重渲染登录页）
      await new Promise((resolve) => setTimeout(resolve, 30))
    })

    try {
      assert.ok(
        requested.every((p) => p.startsWith('/api/auth/')),
        `登录页发起了受保护 API 请求（修复前 bug 复现）：${requested.filter((p) => !p.startsWith('/api/auth/')).join(', ')}`
      )
      assert.equal(
        window.location.href,
        '',
        `触发登录页整页跳转（401 循环，修复前 bug 复现）：href='${window.location.href}'`
      )
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    global.fetch = originalFetch
  }
})
