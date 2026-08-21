// 概览页批量关闭 Issue 测试（issue #412）：
// 通过当前列表勾选一个或多个开放 issue，二次确认后复用单条关闭接口
// 逐条提交；部分失败继续处理并显示失败原因，成功后刷新列表。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const issueListSrc = readFileSync(
  path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')
let helperSrc = ''

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const dialog = await vite.ssrLoadModule('/src/dialog.js')
let closeIssuesInBatch
try {
  const helper = await vite.ssrLoadModule('/src/lib/batchCloseIssues.js')
  closeIssuesInBatch = helper.closeIssuesInBatch
  helperSrc = readFileSync(path.join(ROOT, 'src/lib/batchCloseIssues.js'), 'utf8')
} catch {
  // TDD 阶段模块尚未实现，后续测试应明确报告功能缺失。
}

after(() => vite.close())

const ISSUES_PAYLOAD = {
  repos: [{
    repo_id: 1,
    project_id: 42,
    repo_name: 'botler',
    priority: 10,
    issues: [
      { project_id: 42, iid: 101, title: '第一个 issue', state: 'opened', labels: [] },
      { project_id: 42, iid: 102, title: '第二个 issue', state: 'opened', labels: [] },
    ],
  }],
  errors: [],
  total: 2,
}



async function renderOverview(payload = ISSUES_PAYLOAD) {
  mockedIssueOverviewCalls = 0
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      mockedIssueOverviewCalls += 1
      return payload
    }
    if (pathname === '/api/settings') return { gitlab: { owner_token_masked: '***' } }
    throw new Error('unexpected GET ' + pathname)
  })
  dialog.installAutoAnswer(() => true)
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(Overview))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return { renderer, root: renderer.root }
}

function issueCheckboxes(root) {
  return root.findAll((n) => n.type === 'input'
    && n.props.type === 'checkbox'
    && String(n.props.className || '').includes('issue-select-checkbox'))
}

function batchButton(root) {
  return root.find((n) => n.type === 'button'
    && String(n.props.className || '').includes('batch-close-btn'))
}

// ---- 测试先行：接口契约与边界 ----

test('批量关闭实现应提供去重、异常参数和全量结算结果', () => {
  assert.match(issueListSrc, /issue-select-checkbox/, '每条 issue 应提供勾选框')
  assert.match(issueListSrc, /batch-close-btn/, '列表应提供批量关闭按钮')
  assert.match(issueListSrc, /confirmDialog/, '批量关闭前应二次确认')
  assert.match(helperSrc, /Promise\.allSettled/, '批量请求应允许部分失败后继续')
  assert.equal(typeof closeIssuesInBatch, 'function', '应提供批量关闭纯函数')
  assert.match(helperSrc, /project_id/, '关闭请求应使用 project_id 定位仓库')
  assert.match(helperSrc, /iid/, '关闭请求应使用 iid 定位 issue')
})

test('批量关闭：成功、失败和重复 issue 均应结算且重复项只请求一次', async () => {
  const calls = []
  const result = await closeIssuesInBatch([
    { project_id: 42, iid: 101, title: '成功' },
    { project_id: 42, iid: 102, title: '失败' },
    { project_id: 42, iid: 101, title: '重复' },
  ], async (issue) => {
    calls.push(issue.iid)
    if (issue.iid === 102) throw new Error('无权限')
  })
  assert.deepEqual(calls, [101, 102], '重复 issue 不应重复请求')
  assert.deepEqual(result.succeeded.map((i) => i.iid), [101])
  assert.equal(result.failed.length, 1)
  assert.equal(result.failed[0].issue.iid, 102)
  assert.equal(result.failed[0].error.message, '无权限')
})

test('批量关闭：空输入、null 和异常参数不应调用关闭函数', async () => {
  let calls = 0
  const close = async () => { calls += 1 }
  for (const input of [[], null, [{ iid: 1 }], [{ project_id: 42, iid: 0 }]]) {
    const result = await closeIssuesInBatch(input, close)
    assert.equal(result.succeeded.length, 0)
    assert.equal(result.failed.length, input && input.length ? 1 : 0)
  }
  assert.equal(calls, 0, '无效输入不应发起关闭请求')
})

test('概览页：全选后批量关闭应逐条调用单条关闭接口并刷新列表', async () => {
  const postMock = mock.method(api, 'post', async (pathname) => {
    assert.match(pathname, /^\/api\/issues\/42\/10[12]\/close$/)
    return { ok: true, state: 'closed' }
  })
  const { renderer, root } = await renderOverview()
  try {
    const selectAll = root.find((n) => n.type === 'input'
      && n.props.type === 'checkbox'
      && n.props.className === 'issue-select-all')
    await TestRenderer.act(async () => {
      selectAll.props.onChange({ target: { checked: true } })
    })
    assert.equal(issueCheckboxes(root).filter((n) => n.props.checked).length, 2)
    await TestRenderer.act(async () => {
      batchButton(root).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    assert.deepEqual(postMock.mock.calls.map((c) => c.arguments[0]).sort(), [
      '/api/issues/42/101/close', '/api/issues/42/102/close',
    ])
    const rendered = JSON.stringify(renderer.toJSON())
    assert.ok(rendered.includes('成功关闭') || rendered.includes('Successfully closed'), '应显示批量结果')
    assert.ok(mockedIssueOverviewCalls >= 2, '成功后应重新加载概览列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

let mockedIssueOverviewCalls = 0

// 给上一个用例的 GET mock 计数注入一个独立的行为验证，避免依赖实现细节。
test('概览页：空选择点击批量关闭应提示先选择 issue', async () => {
  const { renderer, root } = await renderOverview()
  try {
    await TestRenderer.act(async () => {
      batchButton(root).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const rendered = JSON.stringify(renderer.toJSON())
    assert.ok(rendered.includes('请先选择') || rendered.includes('Select at least one'), '应提示先选择 issue')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('概览页：批量请求进行中禁止重复提交', async () => {
  let resolvePost
  const postMock = mock.method(api, 'post', () => new Promise((resolve) => {
    resolvePost = resolve
  }))
  const { renderer, root } = await renderOverview()
  try {
    const first = issueCheckboxes(root)[0]
    await TestRenderer.act(async () => {
      first.props.onChange({ target: { checked: true } })
      await new Promise((resolve) => setTimeout(resolve, 5))
      batchButton(root).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(batchButton(root).props.disabled, true)
    batchButton(root).props.onClick()
    assert.equal(postMock.mock.callCount(), 1, '进行中重复点击不应增加请求')
    await TestRenderer.act(async () => {
      resolvePost({ ok: true, state: 'closed' })
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
