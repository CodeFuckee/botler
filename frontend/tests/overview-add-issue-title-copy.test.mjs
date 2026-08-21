// 添加 issue 弹窗「只输标题时描述复制标题」测试（issue #103）：
// 用户只输入标题、描述留空时，描述直接复制标题内容——输入时实时联动
// （描述为空则跟随标题更新，用户可见），提交时兜底（描述为空则用标题
// 填充，保证最终创建的 issue 描述等于标题）。
//
// 断言：
// 1. 联动：只输入标题 → 描述框值自动等于标题；继续改标题 → 描述跟随；
// 2. 不覆盖：描述已输入时改标题，描述保持用户输入不变；
// 3. 提交兜底：只输标题直接提交 → POST description = 标题；
//    标题（自动复制描述后）手动清空描述再提交 → POST description = 标题；
//    标题与描述都输入 → POST description = 用户输入的描述；
// 4. 回归：标题为空仍被「标题不能为空」校验拦截，不发起 POST。
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
const modalSrc = readFileSync(path.join(ROOT, 'src/components/AddIssueModal.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-add-issue.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const FORM_META = {
  members: [
    { id: 20, username: 'agent', name: 'Agent' },
    { id: 21, username: 'dev', name: 'Dev' },
  ],
  labels: [
    { name: 'bug', color: 'FF0000', text_color: 'FFFFFF' },
    { name: 'ui', color: '69D100', text_color: 'FFFFFF' },
  ],
}

const ISSUES_PAYLOAD = {
  repos: [
    { repo_id: 1, repo_name: 'botler', priority: 10, issues: [
      { iid: 11, title: '已有 issue',
        updated_at: '2026-08-15 01:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/11' },
    ] },
  ],
  errors: [], total: 1,
}

// 挂载 Overview 并打开第一个仓库的「添加 Issue」弹窗
async function renderAddIssueModal() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return ISSUES_PAYLOAD
    if (pathname.startsWith('/api/issues/form-meta/')) return FORM_META
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  const btns = renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('add-issue-btn'))
  await TestRenderer.act(async () => {
    btns[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return { renderer, renderError }
}

// 弹窗内表单元素定位辅助
function titleInput(renderer) {
  return renderer.root.find(
    (n) => n.props.className === 'input add-issue-title')
}
function descInput(renderer) {
  return renderer.root.find(
    (n) => n.props.className === 'input add-issue-desc')
}
function findSubmit(renderer) {
  return renderer.root.find(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('add-issue-submit'))
}
function errorText(renderer) {
  const alerts = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('alert-error'))
  return alerts.map((a) => textOf(a.props.children)).join('|')
}
// 输入标题 / 描述（触发受控组件 onChange）
async function setTitle(renderer, value) {
  await TestRenderer.act(async () => {
    titleInput(renderer).props.onChange({ target: { value } })
  })
}
async function setDesc(renderer, value) {
  await TestRenderer.act(async () => {
    descInput(renderer).props.onChange({ target: { value } })
  })
}
// 勾选第一个标签（表单校验必填）
async function checkFirstLabel(renderer) {
  const checkbox = renderer.root.findAll(
    (n) => n.type === 'input' && n.props.type === 'checkbox')[0]
  await TestRenderer.act(async () => { checkbox.props.onChange() })
}
// 点击提交并返回 POST 调用记录
async function submit(renderer) {
  const postCalls = []
  mock.method(api, 'post', async (pathname, body) => {
    postCalls.push({ pathname, body })
    if (pathname === '/api/issues') return { iid: 99, title: body.title }
    if (pathname === '/api/repos/1/reconcile') return { ok: true, scanned: 1, enqueued: 1 }
    throw new Error('unexpected ' + pathname)
  })
  await TestRenderer.act(async () => {
    findSubmit(renderer).props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return postCalls
}

async function cleanup(renderer) {
  await TestRenderer.act(() => renderer.unmount())
  mock.restoreAll()
}

// ---- 输入联动 ----

test('联动：只输入标题 → 描述框自动复制标题内容', async () => {
  const { renderer, renderError } = await renderAddIssueModal()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    await setTitle(renderer, '只填标题的 issue')
    assert.equal(descInput(renderer).props.value, '只填标题的 issue',
                 '描述应自动复制标题内容')
  } finally { await cleanup(renderer) }
})

test('联动：继续修改标题 → 描述跟随更新', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setTitle(renderer, '第一版标题')
    await setTitle(renderer, '第二版标题')
    assert.equal(descInput(renderer).props.value, '第二版标题',
                 '描述应跟随最新标题')
  } finally { await cleanup(renderer) }
})

test('不覆盖：描述已输入时修改标题，描述保持用户输入', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setDesc(renderer, '用户手写的描述')
    await setTitle(renderer, '新标题')
    assert.equal(descInput(renderer).props.value, '用户手写的描述',
                 '描述非空时不应被标题覆盖')
  } finally { await cleanup(renderer) }
})

test('源码数据流：标题 onChange 联动描述（为空才复制）', () => {
  assert.match(modalSrc, /setDescription\(/,
               'AddIssueModal 应包含描述联动逻辑')
})

// ---- 提交兜底 ----

test('提交：只输标题直接提交 → POST description 等于标题', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setTitle(renderer, '只有标题')
    await checkFirstLabel(renderer)
    const postCalls = await submit(renderer)
    assert.deepEqual(postCalls.map((call) => call.pathname),
      ['/api/issues', '/api/repos/1/reconcile'],
      '创建成功后应继续对账当前仓库')
    assert.equal(postCalls[0].body.title, '只有标题')
    assert.equal(postCalls[0].body.description, '只有标题',
                 '描述为空时 POST 应兜底复制标题')
  } finally { await cleanup(renderer) }
})

test('提交：自动复制后手动清空描述 → POST description 仍等于标题', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setTitle(renderer, '清空描述试试')
    assert.equal(descInput(renderer).props.value, '清空描述试试',
                 '前置条件：描述已被自动复制')
    await setDesc(renderer, '')
    await checkFirstLabel(renderer)
    const postCalls = await submit(renderer)
    assert.equal(postCalls[0].body.description, '清空描述试试',
                 '提交时描述为空应兜底复制标题（保证只输标题时描述=标题）')
  } finally { await cleanup(renderer) }
})

test('提交：标题与描述都输入 → POST description 为用户输入的描述', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setTitle(renderer, '有标题')
    await setDesc(renderer, '有描述')
    await checkFirstLabel(renderer)
    const postCalls = await submit(renderer)
    assert.equal(postCalls[0].body.description, '有描述',
                 '描述非空时提交应保留用户输入')
  } finally { await cleanup(renderer) }
})

// ---- 回归防护 ----

test('回归：标题为空仍被校验拦截，不发起 POST', async () => {
  const { renderer } = await renderAddIssueModal()
  try {
    await setTitle(renderer, '   ')
    await checkFirstLabel(renderer)
    let postCalled = false
    mock.method(api, 'post', async () => { postCalled = true })
    await TestRenderer.act(async () => { findSubmit(renderer).props.onClick() })
    assert.ok(errorText(renderer).includes('标题不能为空'), '应提示标题必填')
    assert.equal(postCalled, false, '校验失败不应发起 POST')
  } finally { await cleanup(renderer) }
})
