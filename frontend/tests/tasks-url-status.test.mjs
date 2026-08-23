// 任务列表页 URL 状态过滤参数测试（issue #257）：
//
// 需求：导航栏任务水位徽章点击跳转 /tasks?status=xxx（运行=running,
// retrying / 排队=queued / 今日完成=succeeded），任务列表页应读取 URL
// 参数并应用对应状态过滤——「点击徽章跳到过滤后的任务列表」（验收标准 2）。
//
// 断言：
// 1. URL 带 status 参数挂载 → 首次 /api/tasks 请求携带该 status 过滤；
// 2. URL 无 status 参数挂载 → 请求不携带 status（既有行为不变）；
// 3. 源码断言：Tasks.jsx 使用 useSearchParams 初始化/同步 status 状态。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tasksSrc = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router-url-status.jsx'),
    },
  },
})
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => {
  vite.close()
  mock.restoreAll()
})

/** 渲染 Tasks，记录 /api/tasks 请求；返回 { renderer, calls, renderError } */
async function renderAndSettle() {
  const calls = []
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      calls.push(pathname)
      return { tasks: [], total: 0, stats: {} }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Tasks)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError, calls }
}

function statusParamOf(url) {
  return new URLSearchParams(url.split('?')[1]).get('status')
}

test('URL 带 status=queued 挂载：首次请求携带该状态过滤（徽章点击跳转）', async () => {
  globalThis.__URL_STATUS_PARAMS = 'status=queued'
  const { renderer, renderError, calls } = await renderAndSettle()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.ok(calls.length >= 1, '应至少发起一次任务列表请求')
    assert.equal(statusParamOf(calls[0]), 'queued', '首次请求应带 status=queued 过滤')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    globalThis.__URL_STATUS_PARAMS = ''
  }
})

test('URL 带 status=running,retrying 挂载：多状态过滤透传（运行徽章跳转）', async () => {
  globalThis.__URL_STATUS_PARAMS = 'status=running,retrying'
  const { renderer, renderError, calls } = await renderAndSettle()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(statusParamOf(calls[0]), 'running,retrying', '首次请求应透传多状态过滤')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    globalThis.__URL_STATUS_PARAMS = ''
  }
})

test('URL 无 status 参数：请求不携带状态过滤（既有行为不变）', async () => {
  globalThis.__URL_STATUS_PARAMS = ''
  const { renderer, renderError, calls } = await renderAndSettle()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(statusParamOf(calls[0]), null, '无 URL 参数时不应携带 status 过滤')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('源码断言：Tasks.jsx 经 useSearchParams 初始化并同步 URL status 参数', () => {
  assert.match(tasksSrc, /useSearchParams\(\)/, '应使用 useSearchParams 读取 URL 参数')
  assert.match(tasksSrc, /searchParams\.get\('status'\)/, '应读取 status 查询参数')
  assert.match(tasksSrc, /setStatus\(urlStatus\)/, 'URL 变化应同步到 status 状态')
})
