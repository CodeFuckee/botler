// 「刷新」按钮测试（issue #59）：任务页面新增手动刷新按钮，点击重新拉取
// /api/tasks 更新所有任务的显示状态。背景：页面仅在存在活跃任务时每 5s
// 自动轮询，全部任务结束后轮询停止，列表状态可能陈旧；手动刷新补上缺口。
//
// 断言：
// 1. Tasks.jsx 渲染「↻ 刷新」按钮（普通 btn 样式），无活跃任务时也可用；
// 2. 点击直接重新 GET /api/tasks（低危操作，无需 confirm），且保持当前
//    筛选（状态/仓库/搜索）与页码；
// 3. 请求中禁用防重复点击（disabled={refreshing}），完成后恢复可用；
// 4. 接口失败显示错误提示而不崩溃。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// tasks-reconcile-all-button.test.mjs 一致）。api 也经 vite 加载，
// 与 Tasks 组件内 import 的是同一模块实例，可对 api 做 method mock。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// ---- 源码断言 ----

test('任务页源码含「刷新」按钮与防重复点击禁用逻辑', () => {
  assert.match(tasksSrc, /↻ 刷新/, '应有刷新按钮文案')
  assert.match(tasksSrc, /disabled=\{refreshing\}/, '请求中应禁用按钮防重复点击')
})

test('刷新为低危操作：无需 window.confirm 确认', () => {
  assert.ok(tasksSrc.includes('↻ 刷新'), '源码应先含刷新按钮（前置条件）')
  const tail = tasksSrc.slice(tasksSrc.indexOf('↻ 刷新'))
  assert.ok(
    !tail.split('\n').slice(0, 25).some((l) => l.includes('window.confirm')),
    '刷新按钮不应要求确认（与对账按钮一致）',
  )
})

// ---- 组件渲染 ----

// Tasks 页含 react-router Link，渲染需包 MemoryRouter
function renderTasks() {
  return TestRenderer.create(
    React.createElement(MemoryRouter, null, React.createElement(Tasks)),
  )
}

async function renderAndSettle(stats, getImpl) {
  mock.method(api, 'get', getImpl || (async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [], total: 0, stats }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  }))
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = renderTasks()
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function findRefreshButton(renderer) {
  return renderer.root
    .findAllByType('button')
    .find((b) => JSON.stringify(b.props.children).includes('刷新'))
}

test('刷新按钮渲染且无活跃任务时也可用', async () => {
  // 无活跃任务（仅 succeeded）：页面无自动轮询，手动刷新是唯一更新途径
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btn = findRefreshButton(renderer)
    assert.ok(btn, '应有刷新按钮')
    assert.notEqual(btn.props.disabled, true, '默认应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无活跃任务时点击刷新重新拉取列表（页面无自动轮询）', async () => {
  const tasksCalls = []
  const getImpl = async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      tasksCalls.push(pathname)
      return { tasks: [], total: 0, stats: { succeeded: 1 } }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  }
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 }, getImpl)
  try {
    assert.equal(renderError, null)
    assert.equal(tasksCalls.length, 1, '挂载后应只有一次初始加载')
    await TestRenderer.act(async () => {
      await findRefreshButton(renderer).props.onClick()
    })
    assert.equal(tasksCalls.length, 2, '点击刷新应再次拉取任务列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('刷新保持当前筛选（状态/仓库/搜索）与页码', async () => {
  const tasksCalls = []
  const getImpl = async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      tasksCalls.push(pathname)
      return { tasks: [], total: 0, stats: { succeeded: 1 } }
    }
    if (pathname === '/api/repos') return { repos: [{ id: 3, name: 'r3' }] }
    throw new Error('unexpected ' + pathname)
  }
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 }, getImpl)
  try {
    assert.equal(renderError, null)
    const [statusSel, repoSel] = renderer.root.findAllByType('select')
    const searchInput = renderer.root.findAllByType('input')
      .find((i) => i.props.placeholder?.includes('搜索'))
    // 一次 act 内批量设置筛选 → 合并为一次 effect 触发的 load
    await TestRenderer.act(() => {
      statusSel.props.onChange({ target: { value: 'failed' } })
      repoSel.props.onChange({ target: { value: '3' } })
      searchInput.props.onChange({ target: { value: 'abc' } })
    })
    const before = tasksCalls.at(-1)
    await TestRenderer.act(async () => {
      await findRefreshButton(renderer).props.onClick()
    })
    const after = tasksCalls.at(-1)
    assert.equal(after, before, '刷新应使用与筛选后加载完全一致的查询参数')
    assert.ok(before.includes('status=failed'), '应保留状态筛选')
    assert.ok(before.includes('repo_id=3'), '应保留仓库筛选')
    assert.ok(before.includes('search=abc'), '应保留搜索关键词')
    assert.ok(before.includes('offset=0'), '应停留在第 1 页')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('刷新请求进行中按钮禁用，完成后恢复可用', async () => {
  let tasksCalls = 0
  let resolveSecond = null
  const getImpl = async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      tasksCalls += 1
      if (tasksCalls === 2) {
        // 第二次拉取（手动刷新）挂起，模拟请求进行中
        await new Promise((resolve) => { resolveSecond = resolve })
      }
      return { tasks: [], total: 0, stats: { succeeded: 1 } }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  }
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 }, getImpl)
  try {
    assert.equal(renderError, null)
    let clickPromise = null
    await TestRenderer.act(() => {
      // 不把 onClick 的 promise 交给 act——接口挂起时 act 会一直等待；
      // onClick 同步段（置 refreshing）在 act 内执行完即可断言按钮禁用
      clickPromise = findRefreshButton(renderer).props.onClick()
    })
    assert.equal(findRefreshButton(renderer).props.disabled, true, '请求中应禁用')
    resolveSecond()
    await TestRenderer.act(() => clickPromise)
    assert.notEqual(findRefreshButton(renderer).props.disabled, true, '完成后应恢复可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('刷新接口失败显示错误提示而不崩溃', async () => {
  const getImpl = async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      throw new Error('刷新失败：内部错误')
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  }
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 }, getImpl)
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(async () => {
      await findRefreshButton(renderer).props.onClick()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('刷新失败：内部错误'), '应显示 API 错误信息')
    assert.notEqual(findRefreshButton(renderer).props.disabled, true, '失败后按钮应恢复可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
