// 路由级代码分割测试（issue #202）：验证 React.lazy + Suspense 懒加载
// 1. 不影响正常渲染：路由目标页面按需渲染；
// 2. 未访问的路由页面组件不进入渲染树（懒加载生效，代码分割有效）；
// 3. 不同路由渲染各自页面 chunk，互不干扰（按需加载生效）。
// 机制与 app-default-page.test.mjs 同法：vite SSR 加载组件 +
// react-test-renderer + MemoryRouter；无后端 fetch reject → 主界面渲染。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import TestRenderer from 'react-test-renderer'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')
const { default: Plugins } = await vite.ssrLoadModule('/src/pages/Plugins.jsx')
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const lazyPages = await vite.ssrLoadModule('/src/pages/lazy.jsx')

after(() => vite.close())

// 以指定初始路径渲染 App，等待 auth 状态流转
async function renderAt(path) {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [path] }, React.createElement(App))
    )
    // 等微任务 flush：auth/status reject → 第二次渲染出主界面
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

// 路由懒加载等待（issue #202）：页面 chunk 异步加载完成后才渲染出页面组件
async function waitForPage(renderer, Page, timeout = 2000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    if (renderer.root.findAllByType(Page).length > 0) return true
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  return false
}

test('懒加载不影响渲染：/overview 渲染概览页，其余页面不进入渲染树', async () => {
  const renderer = await renderAt('/overview')
  try {
    assert.equal(await waitForPage(renderer, Overview), true, '概览页应正常渲染（懒加载后）')
    assert.equal(renderer.root.findAllByType(Settings).length, 0, '设置页不应渲染（未访问，懒加载未触发）')
    assert.equal(renderer.root.findAllByType(Plugins).length, 0, '插件页不应渲染（未访问，懒加载未触发）')
    assert.equal(renderer.root.findAllByType(Tasks).length, 0, '任务页不应渲染（未访问，懒加载未触发）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('不同路由按需渲染各自页面 chunk（懒加载按需生效）', async () => {
  const renderer = await renderAt('/settings')
  try {
    assert.equal(await waitForPage(renderer, Settings), true, '设置页应正常渲染（懒加载后）')
    assert.equal(renderer.root.findAllByType(Overview).length, 0, '概览页不应渲染（未访问）')
    assert.equal(renderer.root.findAllByType(Tasks).length, 0, '任务页不应渲染（未访问）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('pages/lazy.jsx 导出全部页面 lazy 包装（供 App 按路由懒加载）', () => {
  const names = ['Overview', 'Repos', 'Templates', 'Labels', 'Tasks', 'Stats',
    'TaskDetail', 'Settings', 'Plugins', 'Skills', 'Terminal', 'Login']
  for (const name of names) {
    assert.ok(lazyPages[name], `lazy.jsx 应导出 ${name}`)
    assert.equal(lazyPages[name].$$typeof, Symbol.for('react.lazy'), `${name} 应为 React.lazy 包装`)
  }
})
