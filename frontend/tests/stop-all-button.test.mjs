// 「一键停止所有任务」按钮测试（issue #35）：任务页面新增停止按钮，
// 一键中断全部活跃任务（排队/执行/重试），执行中的 claude 进程被强制终止。
//
// 断言：
// 1. Tasks.jsx 渲染「停止所有任务」按钮（btn-danger），无活跃任务或请求中禁用；
// 2. 点击需自定义确认对话框（confirmDialog）确认，确认后才调 POST /api/tasks/stop-all；
// 3. 成功后显示「已停止 N 个任务」提示（alert-ok）并刷新列表；失败显示错误；
// 4. 活跃数 = stats.queued + running + retrying，展示在按钮文案上。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）。
// api 也经 vite 加载，与 Tasks 组件内 import 的是同一模块实例，可对 api 做 method mock。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  // react-router-dom 的 CJS 构建不能被 vite SSR 转译（module is not
  // defined），alias 到测试用最小 mock（MemoryRouter 透传 / Link 渲染 <a>）
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')
// 与组件内 import 的同一 dialog.js 模块实例，测试注入直接作用于确认调用（issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

after(() => vite.close())

// ---- 源码断言 ----

test('任务页源码含「停止所有任务」危险按钮与确认交互', () => {
  assert.match(tasksSrc, /停止所有任务/, '应有停止按钮文案')
  assert.match(
    tasksSrc,
    /className="btn btn-danger"/,
    '停止按钮应使用危险样式 btn-danger',
  )
  assert.match(tasksSrc, /confirmDialog/, '点击应先弹自定义确认对话框')
  assert.match(
    tasksSrc,
    /api\.post\('\/api\/tasks\/stop-all'\)/,
    '确认后应调 POST /api/tasks/stop-all',
  )
  assert.match(tasksSrc, /alert-ok/, '成功后应显示绿色成功提示')
})

test('按钮禁用条件：无活跃任务或请求进行中', () => {
  assert.match(
    tasksSrc,
    /disabled=\{activeCount === 0 \|\| stopping\}/,
    '活跃数为 0 或请求中应禁用按钮',
  )
})

test('活跃数取自 stats 的 queued + running + retrying', () => {
  assert.match(
    tasksSrc,
    /stats\?\.queued.*stats\?\.running.*stats\?\.retrying/s,
    '活跃任务数应统计排队/执行/重试三种状态',
  )
})

// ---- 组件渲染 ----

// Tasks 页含 react-router Link，渲染需包 MemoryRouter
function renderTasks() {
  return TestRenderer.create(
    React.createElement(MemoryRouter, null, React.createElement(Tasks)),
  )
}

async function renderAndSettle(stats) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [], total: 0, stats }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
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

function findStopButton(renderer) {
  return renderer.root
    .findAllByType('button')
    .find((b) => JSON.stringify(b.props.children).includes('停止所有任务'))
}

// 注入对话框自动应答（无 DialogHost 挂载时 confirmDialog 由 autoAnswer
// 直接结算）；应答后需推进微任务链，组件才继续调接口
function withConfirm(value) {
  dialog.installAutoAnswer(() => value)
}

async function flushMicrotasks() {
  await new Promise((resolve) => setTimeout(resolve, 10))
}

test('有活跃任务时按钮可用并显示数量', async () => {
  const { renderer, renderError } = await renderAndSettle({
    queued: 1, running: 2, retrying: 1, succeeded: 5,
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btn = findStopButton(renderer)
    assert.ok(btn, '应有停止按钮')
    assert.notEqual(btn.props.disabled, true, '有活跃任务时按钮应可用')
    assert.ok(
      JSON.stringify(btn.props.children).includes('（4）'),
      '按钮应显示活跃任务数 4（queued 1 + running 2 + retrying 1）',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无活跃任务时按钮禁用', async () => {
  const { renderer, renderError } = await renderAndSettle({ succeeded: 3 })
  try {
    assert.equal(renderError, null)
    const btn = findStopButton(renderer)
    assert.ok(btn, '无活跃任务时按钮仍应渲染')
    assert.equal(btn.props.disabled, true, '无活跃任务时按钮应禁用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认框取消时不调用停止接口', async () => {
  withConfirm(false)
  const postCalls = []
  mock.method(api, 'post', async (p) => { postCalls.push(p); return { stopped: [], count: 0 } })
  const { renderer, renderError } = await renderAndSettle({ running: 1 })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findStopButton(renderer).props.onClick()
      await flushMicrotasks()
    })
    assert.equal(postCalls.length, 0, '取消确认后不应调用停止接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认后调用停止接口并显示成功提示', async () => {
  withConfirm(true)
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { stopped: [11, 12], count: 2 }
  })
  const { renderer, renderError } = await renderAndSettle({ running: 1 })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findStopButton(renderer).props.onClick()
      await flushMicrotasks()
    })
    assert.deepEqual(postCalls, ['/api/tasks/stop-all'], '确认后应调停止接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已停止 2 个任务'), '应显示成功提示（含停止数量）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('停止接口失败时显示错误提示而不崩溃', async () => {
  withConfirm(true)
  mock.method(api, 'post', async () => {
    throw new Error('停止失败：内部错误')
  })
  const { renderer, renderError } = await renderAndSettle({ running: 1 })
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(async () => {
      findStopButton(renderer).props.onClick()
      await flushMicrotasks()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('停止失败：内部错误'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
