// 默认页面路由测试（issue #54：默认页面从仓库页改为概览页）
// 现状（修复前）：App.jsx 路由 / → Repos，打开根路径显示仓库页。
// 期望（修复后）：/ 重定向到 /overview 显示概览页，仓库页迁至 /repos。
// 本测试沿 app-hooks.test.mjs 风格：vite SSR 加载组件 + react-test-renderer
// + MemoryRouter；无后端环境 fetch reject → auth 回退非 SSO，主界面渲染。
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
const { default: Repos } = await vite.ssrLoadModule('/src/pages/Repos.jsx')
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')

after(() => vite.close())

// 以指定初始路径渲染 App，等待 auth 状态流转（与 app-hooks.test.mjs 同法）
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

test('访问 / 时默认显示概览页而非仓库页（issue #54）', async () => {
  const renderer = await renderAt('/')
  try {
    assert.equal(
      renderer.root.findAllByType(Overview).length,
      1,
      '根路径应重定向渲染概览页'
    )
    assert.equal(
      renderer.root.findAllByType(Repos).length,
      0,
      '根路径不应再渲染仓库页'
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('仓库页仍可通过 /repos 访问（issue #54 迁址）', async () => {
  const renderer = await renderAt('/repos')
  try {
    assert.equal(
      renderer.root.findAllByType(Repos).length,
      1,
      '/repos 应渲染仓库页'
    )
    assert.equal(
      renderer.root.findAllByType(Overview).length,
      0,
      '/repos 不应渲染概览页'
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
