// 「一键对账所有仓库」按钮测试（issue #38）：任务页面新增对账按钮，
// 点击后同步扫描全部启用仓库，把「assignee 是 bot 但任务表无活跃记录」的
// open issues 补入任务队列，并即时展示扫描/补入队结果。
//
// 断言：
// 1. Tasks.jsx 渲染「对账所有仓库」按钮（普通 btn 样式），请求中禁用；
// 2. 点击直接调 POST /api/tasks/reconcile-all（低危操作，无需 confirm）；
// 3. 成功后显示「对账完成：扫描 X 个 issue，补入队 Y 个任务」提示（alert-ok）
//    并刷新列表；部分仓库失败（errors 非空）显示错误明细；接口失败显示错误；
// 4. 请求中禁用防重复点击（disabled={reconciling}）。
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

after(() => vite.close())

// ---- 源码断言 ----

test('任务页源码含「对账所有仓库」按钮与对账接口调用', () => {
  assert.match(tasksSrc, /对账所有仓库/, '应有对账按钮文案')
  assert.match(
    tasksSrc,
    /api\.post\('\/api\/tasks\/reconcile-all'\)/,
    '点击后应调 POST /api/tasks/reconcile-all',
  )
  assert.match(tasksSrc, /对账完成：扫描/, '成功后应显示对账结果提示')
  assert.match(tasksSrc, /disabled=\{reconciling\}/, '请求中应禁用按钮防重复点击')
})

test('对账为低危操作：无需确认对话框', () => {
  assert.ok(
    !tasksSrc.slice(tasksSrc.indexOf('对账所有仓库')).split('\n').slice(0, 20).some(
      (l) => l.includes('window.confirm') || l.includes('confirmDialog'),
    ),
    '对账按钮不应要求确认（与仓库页对账按钮一致）',
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

function findReconcileButton(renderer) {
  return renderer.root
    .findAllByType('button')
    .find((b) => textOf(b.props.children).includes('对账所有仓库'))
}

test('对账按钮渲染且默认可用', async () => {
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btn = findReconcileButton(renderer)
    assert.ok(btn, '应有对账按钮')
    assert.notEqual(btn.props.disabled, true, '默认应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击后调用对账接口并显示扫描与补入队结果', async () => {
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { ok: true, scanned: 3, enqueued: 2, errors: [] }
  })
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(() => findReconcileButton(renderer).props.onClick())
    assert.deepEqual(postCalls, ['/api/tasks/reconcile-all'], '应调用对账接口')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(
      text.includes('对账完成：扫描 3 个 issue，补入队 2 个任务'),
      '应显示成功提示（含扫描数与补入队数）',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('扫描无漏单时提示补入队 0 个任务', async () => {
  mock.method(api, 'post', async () => ({ ok: true, scanned: 2, enqueued: 0, errors: [] }))
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(() => findReconcileButton(renderer).props.onClick())
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('对账完成：扫描 2 个 issue，补入队 0 个任务'), '无漏单也应提示结果')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('部分仓库失败时显示错误明细', async () => {
  mock.method(api, 'post', async () => ({
    ok: true, scanned: 1, enqueued: 0, errors: ['仓库 a: 模拟 GitLab API 故障'],
  }))
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(() => findReconcileButton(renderer).props.onClick())
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('仓库 a'), '应展示失败仓库明细')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('对账接口失败时显示错误提示而不崩溃', async () => {
  mock.method(api, 'post', async () => {
    throw new Error('对账失败：内部错误')
  })
  const { renderer, renderError } = await renderAndSettle({ succeeded: 1 })
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    await TestRenderer.act(() => findReconcileButton(renderer).props.onClick())
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('对账失败：内部错误'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
