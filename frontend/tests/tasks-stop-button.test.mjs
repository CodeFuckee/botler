// 「单任务停止」按钮测试（issue #214）：任务列表操作列新增「停止」按钮，
// 仅执行中（running）任务可单独停止，确认后调 POST /api/tasks/{id}/stop
// 标记 interrupted 并强制终止引擎进程（停止不可逆）。
//
// 断言：
// 1. Tasks.jsx 源码含「停止」按钮与 POST /api/tasks/{id}/stop 调用、确认交互；
// 2. 仅 running 任务行渲染停止按钮，其他状态不渲染；
// 3. 点击需自定义确认对话框（confirmDialog，danger 危险样式）确认，
//    确认后才调接口；取消不调用；
// 4. 成功后显示提示（alert-ok）并刷新列表；失败显示错误；请求中按钮禁用。
import { after, mock, test } from 'node:test'

// 渲染树节点 → 纯文本（递归；Lucide 图标等元素无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tasksSrc = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 stop-all-button.test.mjs 一致）。
// api 也经 vite 加载，与 Tasks 组件内 import 的是同一模块实例，可对 api 做 method mock。
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
// 与组件内 import 的同一 dialog.js 模块实例，测试注入直接作用于确认调用（issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

after(() => vite.close())

// ---- 源码断言 ----

test('任务页源码含单任务停止按钮与确认交互', () => {
  assert.match(tasksSrc, /btn-danger/, '停止按钮应使用危险样式 btn-danger')
  assert.match(tasksSrc, /confirmDialog/, '点击应先弹自定义确认对话框')
  assert.match(
    tasksSrc,
    /api\.post\(`\/api\/tasks\/\$\{[^}]+\}\/stop`\)/,
    '确认后应调 POST /api/tasks/{id}/stop',
  )
  assert.match(tasksSrc, /alert-ok/, '成功后应显示绿色成功提示')
  assert.match(tasksSrc, /stopTaskMsg/, '应有单任务停止成功提示状态')
})

test('停止按钮仅对 running 状态渲染', () => {
  assert.match(
    tasksSrc,
    /t\.status === 'running'/,
    '停止按钮应按 running 状态条件渲染',
  )
})

// ---- 组件渲染 ----

function renderTasks() {
  return TestRenderer.create(
    React.createElement(MemoryRouter, null, React.createElement(Tasks)),
  )
}

function mkTask(overrides = {}) {
  return {
    id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
    issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
    status: 'running', attempt_count: 3, triggered_by: 'webhook',
    exit_code: null, error_message: null, error_detail: null,
    resumed: false, commit_sha: null, commit_url: null, log_path: null,
    started_at: '2026-08-13 10:00:00', finished_at: null,
    created_at: '2026-08-13 09:50:00',
    ...overrides,
  }
}

async function renderAndSettle(tasks, stats = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks, total: tasks.length, stats }
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

// 行内「停止」按钮（排除「停止所有任务」）
function findStopButtons(renderer) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => {
      const text = textOf(b.props.children)
      return text === '停止' || text === '停止中…'
    })
}

// 注入对话框自动应答（无 DialogHost 挂载时 confirmDialog 由 autoAnswer
// 直接结算）；应答后推进微任务链，组件才继续调接口
function withConfirm(value) {
  dialog.installAutoAnswer(() => value)
}

async function flushMicrotasks() {
  await new Promise((resolve) => setTimeout(resolve, 10))
}

test('running 任务行渲染停止按钮，其他状态不渲染', async () => {
  const { renderer, renderError } = await renderAndSettle([
    mkTask({ id: 1, status: 'running' }),
    mkTask({ id: 2, status: 'queued' }),
    mkTask({ id: 3, status: 'retrying' }),
    mkTask({ id: 4, status: 'succeeded' }),
    mkTask({ id: 5, status: 'failed' }),
  ])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = findStopButtons(renderer)
    assert.equal(btns.length, 1, '仅 running 任务应显示停止按钮')
    assert.notEqual(btns[0].props.disabled, true, '无停止请求时按钮应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认框取消时不调用停止接口', async () => {
  withConfirm(false)
  const postCalls = []
  mock.method(api, 'post', async (p) => { postCalls.push(p); return { task_id: 3 } })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findStopButtons(renderer)[0].props.onClick()
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
    return { task_id: 3, status: 'interrupted' }
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findStopButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.deepEqual(postCalls, ['/api/tasks/3/stop'], '确认后应调用停止接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已停止'), '应显示成功提示（含停止结果）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('停止接口失败时显示错误提示而不崩溃', async () => {
  withConfirm(true)
  mock.method(api, 'post', async () => {
    throw new Error('仅排队中（queued）、执行中（running）或重试中（retrying）的任务可停止')
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(async () => {
      findStopButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('仅排队中'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('停止请求进行中按钮禁用', async () => {
  withConfirm(true)
  let resolvePost = null
  mock.method(api, 'post', async () => {
    await new Promise((resolve) => { resolvePost = resolve })
    return { task_id: 3, status: 'interrupted' }
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    let clickPromise = null
    await TestRenderer.act(async () => {
      // 不把 onClick 的 promise 交给 act——接口挂起时 act 会一直等待；
      // onClick 在确认应答后进入请求段（置 stopId + post 挂起），
      // flush 微任务推进到挂起点即可断言按钮禁用
      clickPromise = findStopButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(findStopButtons(renderer)[0].props.disabled, true)
    resolvePost()
    await TestRenderer.act(() => clickPromise)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
