// App 组件渲染回归测试（issue #27 第二轮：SSO 部署后页面空白）
// 根因：App.jsx 的通知轮询 useEffect 曾位于条件 return 之后——auth 为
// null 时只渲染 4 个 hooks，auth 加载完成后渲染 5 个，hook 数量变化触发
// React error #310「Rendered more hooks than during the previous render」，
// 整棵组件树崩溃 → 白屏。
// 本测试渲染 App 并驱动 auth 状态流转（加载中 → 加载完成回退未启用 SSO），
// 断言两次渲染后主界面正常出现，防止回归。
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

// 测试结束后关闭 vite server，否则进程不退出
after(() => vite.close())

test('App 在 auth 状态流转后不崩溃且渲染主界面（回归 issue #27 白屏）', async () => {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(App))
      )
      // auth/status 与 settings 请求在无后端环境下 reject → App 回退
      // auth={enabled:false,user:null} 并重渲染主界面。等待微任务
      // flush，让第二次渲染（此前会崩溃的一次）完成。
      await new Promise((resolve) => setTimeout(resolve, 20))
    } catch (e) {
      renderError = e
    }
  })

  try {
    assert.equal(
      renderError,
      null,
      `渲染期间抛错（修复前 bug 复现）：${renderError?.message || renderError}`
    )
    assert.ok(renderer.toJSON(), '渲染结果为 null')
    assert.ok(
      renderer.root.findAllByType('nav').length > 0,
      '未渲染出主界面导航栏（可能仍停留在加载态）'
    )
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})
