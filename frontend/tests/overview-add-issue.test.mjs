// 概览页「添加 Issue」按钮与弹窗测试（issue #92）：每个仓库卡片右上角
// 增加「添加 Issue」按钮，点击弹出表单（标题必填 / 描述选填 / 分配人
// 项目成员下拉必填默认 agent / 标签仓库已有标签多选必填），提交后调用
// 后端在 GitLab 对应仓库创建 issue，成功后关闭弹窗并立即刷新列表。
//
// 断言：
// 1. 渲染：每个仓库卡片头渲染「添加 Issue」按钮，点击打开弹窗并加载
//    成员/标签元数据；
// 2. 默认值：成员含 agent 时分配人默认选中 agent，否则不默认选择；
// 3. 表单校验：标题空、标签未选、分配人未选 → 显示错误且不调 POST；
// 4. 提交：POST /api/issues 参数正确；成功后弹窗关闭 + 重新拉取
//    overview（缓存失效后立即刷新）；失败时弹窗保持并显示错误；
// 5. 边界：仓库无标签显示空状态、元数据加载失败显示错误、点遮罩关闭。
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
const overviewSrc = readFileSync(path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
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
    { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
  ],
  errors: [], total: 1,
}

// 挂载 Overview：api.get 按路径分流（tasks/pipelines/issues/form-meta）。
// 返回 { renderer, getCalls }——getCalls 记录每次 api.get 的路径，
// 用于断言创建成功后 overview 被重新拉取。
async function renderOverview({ issuesPayload = ISSUES_PAYLOAD,
                                formMeta = FORM_META,
                                formMetaError = null } = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
    if (pathname.startsWith('/api/issues/form-meta/')) {
      if (formMetaError) throw new Error(formMetaError)
      return formMeta
    }
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
  return { renderer, renderError, getCalls }
}

// 在已挂载的 Overview 中点击第 index 个仓库卡片的「添加 Issue」按钮
// （等待弹窗元数据加载完成）
async function openAddIssueModal(renderer, index = 0) {
  const btns = renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('add-issue-btn'))
  assert.ok(btns.length > index, `找不到第 ${index} 个添加 Issue 按钮`)
  await TestRenderer.act(async () => {
    btns[index].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return btns
}

// 弹窗内表单元素定位辅助
function modalOf(renderer) {
  return renderer.root.find(
    (n) => String(n.props.className || '').includes('modal add-issue'))
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
// 输入标题并（可选）勾选第一个标签
async function fillForm(renderer, title, withLabel = true) {
  const titleInput = renderer.root.find(
    (n) => n.props.className === 'input add-issue-title')
  await TestRenderer.act(async () => {
    titleInput.props.onChange({ target: { value: title } })
  })
  if (withLabel) {
    const checkbox = renderer.root.findAll(
      (n) => n.type === 'input' && n.props.type === 'checkbox')[0]
    await TestRenderer.act(async () => { checkbox.props.onChange() })
  }
}

// ---- 渲染与按钮 ----

test('渲染：每个仓库卡片头渲染「添加 Issue」按钮', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = renderer.root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('add-issue-btn'))
    assert.equal(btns.length, 2, '两个仓库卡片各应有一个添加按钮')
    assert.ok(overviewSrc.includes('添加 Issue'), '源码应含按钮文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击按钮打开弹窗：加载成员下拉与标签多选，默认选中 agent', async () => {
  const { renderer, renderError, getCalls } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)

    assert.ok(getCalls.some((p) => p === '/api/issues/form-meta/1'),
              '应请求仓库 1 的 form-meta')
    const modal = modalOf(renderer)
    assert.ok(modal, '应渲染弹窗')
    // 分配人下拉默认选中 agent（username=agent 的 user_id=20）
    const select = renderer.root.find(
      (n) => n.props.className === 'input add-issue-assignee')
    assert.equal(select.props.value, '20', '分配人应默认选中 agent')
    // 标签多选渲染全部标签
    const choices = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('label-choice'))
    assert.equal(choices.length, 2, '应渲染两个标签选项')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('标签胶囊显示 GitLab 标签颜色（issue #100）', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)

    // 后端归一化后传无 # 的 6 位 hex，前端拼 # 着色（background 背景色 +
    // text_color 文字色，与 GitLab 标签外观一致）
    const pills = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('label-pill'))
    assert.equal(pills.length, 2, '应渲染两个标签胶囊')
    assert.deepEqual(pills[0].props.style,
      { background: '#FF0000', color: '#FFFFFF' },
      'bug 胶囊应使用 GitLab 标签背景色与文字色')
    assert.deepEqual(pills[1].props.style,
      { background: '#69D100', color: '#FFFFFF' },
      'ui 胶囊应使用 GitLab 标签背景色与文字色')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：标签无颜色时胶囊无内联样式（中性降级）', async () => {
  const { renderer, renderError } = await renderOverview({
    formMeta: {
      members: FORM_META.members,
      labels: [{ name: 'bug', color: null, text_color: null }],
    },
  })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)

    const pills = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('label-pill'))
    assert.equal(pills.length, 1, '应渲染一个标签胶囊')
    assert.equal(pills[0].props.style, undefined,
                 '无色标签不应携带内联颜色样式（CSS 中性灰兜底）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：成员不含 agent 时分配人不默认选择（提交时必填校验拦截）', async () => {
  const { renderer, renderError } = await renderOverview({
    formMeta: {
      members: [{ id: 21, username: 'dev', name: 'Dev' }],
      labels: FORM_META.labels,
    },
  })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const select = renderer.root.find(
      (n) => n.props.className === 'input add-issue-assignee')
    assert.equal(select.props.value, '', '无 agent 时不应默认选中任何成员')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 表单校验（不触发 POST）----

test('校验：标题为空提交 → 显示错误且不调 POST', async () => {
  const { renderer } = await renderOverview()
  try {
    await openAddIssueModal(renderer, 0)
    let postCalled = false
    mock.method(api, 'post', async () => { postCalled = true })

    await fillForm(renderer, '   ', true)
    await TestRenderer.act(async () => { findSubmit(renderer).props.onClick() })

    assert.ok(errorText(renderer).includes('标题不能为空'), '应提示标题必填')
    assert.equal(postCalled, false, '校验失败不应发起 POST')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('校验：未选择标签提交 → 显示错误且不调 POST', async () => {
  const { renderer } = await renderOverview()
  try {
    await openAddIssueModal(renderer, 0)
    let postCalled = false
    mock.method(api, 'post', async () => { postCalled = true })

    await fillForm(renderer, '有标题', false)
    await TestRenderer.act(async () => { findSubmit(renderer).props.onClick() })

    assert.ok(errorText(renderer).includes('请至少选择一个标签'), '应提示标签必填')
    assert.equal(postCalled, false, '校验失败不应发起 POST')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('校验：成员不含 agent 且未手动选择 → 提示分配人必填', async () => {
  const { renderer } = await renderOverview({
    formMeta: {
      members: [{ id: 21, username: 'dev', name: 'Dev' }],
      labels: FORM_META.labels,
    },
  })
  try {
    await openAddIssueModal(renderer, 0)
    let postCalled = false
    mock.method(api, 'post', async () => { postCalled = true })

    await fillForm(renderer, '有标题', true)
    await TestRenderer.act(async () => { findSubmit(renderer).props.onClick() })

    assert.ok(errorText(renderer).includes('请选择分配人'), '应提示分配人必填')
    assert.equal(postCalled, false, '校验失败不应发起 POST')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 提交 ----

test('提交成功：创建后自动对账对应仓库，再关闭弹窗并刷新 overview', async () => {
  const { renderer, renderError, getCalls } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const postCalls = []
    mock.method(api, 'post', async (pathname, body) => {
      postCalls.push({ pathname, body })
      if (pathname === '/api/issues') return { iid: 99, title: body.title }
      if (pathname === '/api/repos/1/reconcile') return { ok: true, scanned: 1, enqueued: 1 }
      throw new Error('unexpected ' + pathname)
    })

    await fillForm(renderer, '新 issue 标题', true)
    await TestRenderer.act(async () => {
      findSubmit(renderer).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })

    assert.deepEqual(postCalls.map((call) => call.pathname),
      ['/api/issues', '/api/repos/1/reconcile'],
      '创建成功后应串行对账被创建 issue 所在的仓库')
    assert.equal(postCalls[0].body.repo_id, 1, 'repo_id 应对应被点击的仓库')
    assert.equal(postCalls[0].body.title, '新 issue 标题')
    assert.equal(postCalls[0].body.assignee_id, 20, '分配人应传 agent 的 user_id')
    assert.deepEqual(postCalls[0].body.labels, ['bug'], '标签应传选中的标签名')
    const modals = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('modal add-issue'))
    assert.equal(modals.length, 0, '创建成功后弹窗应关闭')
    const overviewCalls = getCalls.filter((p) => p === '/api/issues/overview')
    assert.equal(overviewCalls.length, 2, '创建和对账完成后应重新拉取 overview')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：创建成功但自动对账失败时保留创建结果、提示失败并刷新列表', async () => {
  const { renderer, renderError, getCalls } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const postCalls = []
    mock.method(api, 'post', async (pathname, body) => {
      postCalls.push(pathname)
      if (pathname === '/api/issues') return { iid: 99, title: body.title }
      if (pathname === '/api/repos/1/reconcile') throw new Error('自动对账失败：网络暂不可用')
      throw new Error('unexpected ' + pathname)
    })

    await fillForm(renderer, '对账失败仍已创建', true)
    await TestRenderer.act(async () => {
      findSubmit(renderer).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })

    assert.deepEqual(postCalls, ['/api/issues', '/api/repos/1/reconcile'],
      '创建已成功后仍应尝试一次指定仓库的自动对账')
    assert.equal(renderer.root.findAll(
      (n) => String(n.props.className || '').includes('modal add-issue')).length, 0,
    '对账失败不应把已创建的 issue 误报为创建失败')
    assert.ok(errorText(renderer).includes('自动对账失败：网络暂不可用'),
      '应在仓库卡片提示自动对账失败')
    const overviewCalls = getCalls.filter((p) => p === '/api/issues/overview')
    assert.equal(overviewCalls.length, 2, '对账失败也应刷新列表以展示已创建的 issue')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('提交失败：弹窗保持打开并显示错误', async () => {
  const { renderer } = await renderOverview()
  try {
    await openAddIssueModal(renderer, 0)
    mock.method(api, 'post', async () => {
      throw new Error('创建 issue 失败: token 无效或已过期（401）')
    })

    await fillForm(renderer, '会失败的标题', true)
    await TestRenderer.act(async () => {
      findSubmit(renderer).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })

    assert.ok(errorText(renderer).includes('创建 issue 失败'), '应显示创建失败错误')
    assert.ok(modalOf(renderer), '失败时弹窗应保持打开')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 边界与关闭 ----

test('边界：仓库无标签时显示空状态提示', async () => {
  const { renderer, renderError } = await renderOverview({
    formMeta: { members: FORM_META.members, labels: [] },
  })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('该仓库暂无标签'), '应显示标签空状态提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：元数据加载失败时弹窗显示错误', async () => {
  const { renderer, renderError } = await renderOverview({
    formMetaError: '获取仓库成员/标签失败: 模拟故障',
  })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    assert.ok(errorText(renderer).includes('获取仓库成员/标签失败'),
              '应显示元数据加载失败错误')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点遮罩关闭弹窗', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const overlay = renderer.root.find(
      (n) => n.props.className === 'modal-overlay')
    await TestRenderer.act(async () => { overlay.props.onClick() })
    const modals = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('modal add-issue'))
    assert.equal(modals.length, 0, '点击遮罩后弹窗应关闭')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
