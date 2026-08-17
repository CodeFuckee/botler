// 「手动重试任务」按钮测试（issue #36）：任务列表操作列新增重试按钮，
// 失败（failed）/已中断（interrupted）的任务可一键重新入队执行。
//
// 断言：
// 1. Tasks.jsx 源码含「重试」按钮与 POST /api/tasks/{id}/retry 调用；
// 2. 仅失败/中断任务行渲染重试按钮，成功/执行中任务不渲染；
// 3. 点击需自定义确认对话框（confirmDialog）确认，确认后才调接口；取消不调用；
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

test('任务页源码含重试按钮与确认交互', () => {
  assert.match(tasksSrc, /重试/, '应有重试按钮文案')
  assert.match(tasksSrc, /confirmDialog/, '点击应先弹自定义确认对话框')
  assert.match(
    tasksSrc,
    /api\.post\(`\/api\/tasks\/\$\{[^}]+\}\/retry`\)/,
    '确认后应调 POST /api/tasks/{id}/retry',
  )
  assert.match(tasksSrc, /alert-ok/, '成功后应显示绿色成功提示')
})

test('重试按钮仅对 failed / interrupted 状态渲染', () => {
  assert.match(
    tasksSrc,
    /t\.status === 'failed' \|\| t\.status === 'interrupted'/,
    '重试按钮应按失败/中断状态条件渲染',
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
    status: 'failed', attempt_count: 3, triggered_by: 'webhook',
    exit_code: 1, error_message: '重试耗尽后仍失败', error_detail: null,
    resumed: false, commit_sha: null, commit_url: null, log_path: null,
    started_at: '2026-08-13 10:00:00', finished_at: '2026-08-13 10:30:00',
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

function findRetryButtons(renderer) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => textOf(b.props.children).includes('重试'))
}

// 注入对话框自动应答（无 DialogHost 挂载时 confirmDialog 由 autoAnswer
// 直接结算）；应答后推进微任务链，组件才继续调接口
function withConfirm(value) {
  dialog.installAutoAnswer(() => value)
}

async function flushMicrotasks() {
  await new Promise((resolve) => setTimeout(resolve, 10))
}

test('失败任务行渲染重试按钮，成功/执行中任务不渲染', async () => {
  const { renderer, renderError } = await renderAndSettle([
    mkTask({ id: 1, status: 'failed' }),
    mkTask({ id: 2, status: 'succeeded' }),
    mkTask({ id: 3, status: 'interrupted', error_message: '用户手动停止' }),
    mkTask({ id: 4, status: 'running' }),
  ])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = findRetryButtons(renderer)
    assert.equal(btns.length, 2, '仅失败/中断任务应显示重试按钮')
    assert.notEqual(btns[0].props.disabled, true, '无重试请求时按钮应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认框取消时不调用重试接口', async () => {
  withConfirm(false)
  const postCalls = []
  mock.method(api, 'post', async (p) => { postCalls.push(p); return { task_id: 3 } })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findRetryButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(postCalls.length, 0, '取消确认后不应调用重试接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认后调用重试接口并显示成功提示', async () => {
  withConfirm(true)
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { task_id: 3, status: 'queued' }
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findRetryButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.deepEqual(postCalls, ['/api/tasks/3/retry'], '确认后应调用重试接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已重新入队'), '应显示成功提示（含重试结果）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('重试接口失败时显示错误提示而不崩溃', async () => {
  withConfirm(true)
  mock.method(api, 'post', async () => {
    throw new Error('该 issue 已有活跃任务')
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(async () => {
      findRetryButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('该 issue 已有活跃任务'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('重试请求进行中按钮禁用', async () => {
  withConfirm(true)
  let resolvePost = null
  mock.method(api, 'post', async () => {
    await new Promise((resolve) => { resolvePost = resolve })
    return { task_id: 3, status: 'queued' }
  })
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null)
    let clickPromise = null
    await TestRenderer.act(async () => {
      // 不把 onClick 的 promise 交给 act——接口挂起时 act 会一直等待；
      // onClick 在确认应答后进入请求段（置 retryId + post 挂起），
      // flush 微任务推进到挂起点即可断言按钮禁用
      clickPromise = findRetryButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(findRetryButtons(renderer)[0].props.disabled, true)
    resolvePost()
    await TestRenderer.act(() => clickPromise)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
