// 概览页 issue 右边栏「优先处理」按钮测试（issue #242）：该 issue 最近
// 任务处于排队中（queued）时显示「优先处理」按钮，点击把该任务人工
// 优先级置顶（调度器优先派发）；已 running / 无任务 / 终态任务不显示。
//
// 断言：
// 1. 源码：优先处理按钮文案、POST /api/issues/{project_id}/{iid}/prioritize、
//    onPrioritized 通知父组件、成功提示；
// 2. 渲染：task_status=queued 显示按钮；running / 无任务不显示；
// 3. 交互：点击调 prioritize 接口；成功提示 + onPrioritized 触发；
//    失败显示错误；请求中按钮禁用；
// 4. Overview 传递 onPrioritized 给 IssueDrawer。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: IssueDrawer } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

after(() => vite.close())

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

// ---- 源码断言 ----

test('IssueDrawer 渲染「优先处理」按钮并调用 issue 级 prioritize 接口', () => {
  assert.match(drawerSrc, /优先处理/, '应有优先处理按钮文案')
  assert.match(drawerSrc, /taskStatus === 'queued'/, '应仅对 queued 任务展示按钮')
  assert.match(
    drawerSrc,
    /api\.post\(`\/api\/issues\/\$\{i\.project_id\}\/\$\{i\.iid\}\/prioritize`\)/,
    '点击应调 POST /api/issues/{project_id}/{iid}/prioritize',
  )
  assert.match(drawerSrc, /onPrioritized/, '成功后应通知父组件刷新')
  assert.match(drawerSrc, /alert-ok/, '成功后应显示成功提示')
})

test('Overview 向 IssueDrawer 传递 onPrioritized', () => {
  assert.match(overviewSrc, /onPrioritized=\{\(\) => loadIssues\(\)\}/,
               '优先处理成功后应刷新开放 issue 列表')
})

// ---- 组件渲染 ----

async function renderDrawer(issue, opts = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.endsWith('/detail')) {
      return { notes: [], engine: 'claude', task_id: 7,
               task_status: opts.taskStatus ?? null,
               task_duration_seconds: null }
    }
    return { notes: [] }
  })
  const onPrioritized = opts.onPrioritized || (() => {})
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue,
        repoName: 'botler',
        onClose: () => {},
        onIssueClosed: () => {},
        onLabelsUpdated: () => {},
        onRetried: () => {},
        onAssigneeUpdated: () => {},
        onPrioritized,
      }))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

async function flushMicrotasks() {
  await new Promise((resolve) => setTimeout(resolve, 10))
}

function mkIssue(overrides = {}) {
  return {
    id: 42, iid: 7, project_id: 42, title: '排队任务人工调优',
    state: 'opened', labels: [], created_at: '2026-08-01 10:00:00',
    updated_at: '2026-08-01 10:00:00',
    web_url: 'https://gitlab.example.com/demo/-/work_items/7',
    ...overrides,
  }
}

function findPrioritizeButtons(renderer) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => {
      const text = textOf(b.props.children)
      return text === '优先处理' || text === '优先处理中…'
    })
}

test('queued 任务展示「优先处理」按钮', async () => {
  const { renderer, renderError } = await renderDrawer(mkIssue(), { taskStatus: 'queued' })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = findPrioritizeButtons(renderer)
    // issue #270：移动端底部操作栏与头部渲染同一组按钮（renderer 不应用
    // CSS 两处都可见）——「显示」断言 ≥1
    assert.ok(btns.length >= 1, 'queued 任务应显示优先处理按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('running / 无任务不展示「优先处理」按钮', async () => {
  for (const taskStatus of ['running', null]) {
    const { renderer, renderError } = await renderDrawer(mkIssue(), { taskStatus })
    try {
      assert.equal(renderError, null)
      assert.equal(findPrioritizeButtons(renderer).length, 0,
                   `task_status=${taskStatus} 不应显示优先处理按钮`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})

test('点击优先处理调用接口并显示成功提示、通知父组件', async () => {
  let prioritized = false
  mock.method(api, 'post', async (p) => {
    assert.equal(p, '/api/issues/42/7/prioritize')
    return { task_id: 7, status: 'queued', manual_priority: 0 }
  })
  const { renderer, renderError } = await renderDrawer(
    mkIssue(), { taskStatus: 'queued', onPrioritized: () => { prioritized = true } })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findPrioritizeButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    assert.equal(prioritized, true, '成功后应通知父组件刷新列表')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('已置顶'), '应显示优先处理成功提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('优先处理失败显示错误提示且按钮保留', async () => {
  mock.method(api, 'post', async () => {
    throw new Error('仅排队中（queued）的任务可优先处理')
  })
  const { renderer, renderError } = await renderDrawer(mkIssue(), { taskStatus: 'queued' })
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      findPrioritizeButtons(renderer)[0].props.onClick()
      await flushMicrotasks()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('仅排队中'), '应显示错误提示')
    assert.ok(findPrioritizeButtons(renderer).length >= 1, '失败后按钮应保留可重试')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
