// 概览页 issue 右边栏「关闭 issue」按钮测试（issue #94）：
// 点击按钮二次确认后调用后端关闭 GitLab issue；成功后按钮消失、
// 状态徽章变为「已关闭」并通知父组件刷新开放 issue 列表；失败时
// 显示错误信息且按钮保留可重试；请求进行中按钮禁用防重复点击。
//
// 断言：
// 1. opened 且带 project_id 的 issue 显示「关闭 issue」按钮；
//    closed / 缺 project_id（旧数据）不显示；
// 2. 点击 → 自定义对话框（confirmDialog）二次确认；取消则不调用接口；
// 3. 确认 → api.post /api/issues/{project_id}/{iid}/close；
// 4. 成功：按钮消失、徽章变「已关闭」、onIssueClosed 回调触发；
// 5. 失败：错误信息展示、按钮保留（可重试）、回调不触发；
// 6. 请求进行中（post 未返回）按钮 disabled 防重复点击。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-issue-drawer.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
// 与组件内 import 的同一 dialog.js 模块实例（同一 vite 实例），
// 测试注入 installAutoAnswer 直接作用于组件的确认调用（issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 渲染「关闭 issue」按钮并调用关闭接口', () => {
  assert.match(drawerSrc, /btn-danger/, '关闭按钮应使用危险操作样式')
  assert.match(drawerSrc, /关闭 issue/, '应渲染「关闭 issue」按钮文案')
  assert.match(drawerSrc, /confirm/, '点击应先二次确认')
  assert.match(drawerSrc, /api\.post/, '确认后调用 api.post')
  assert.match(drawerSrc, /\/close/, '请求路径应为关闭接口')
  assert.match(drawerSrc, /onIssueClosed/, '成功后应通知父组件刷新')
})

// ---- 组件渲染 ----

// 渲染 IssueDrawer：props 最小集合（SSR 环境 Esc 监听自动跳过）。
// issue #97：抽屉打开时按需拉取评论/活动详情，此处统一 mock 为空
// 列表（本文件只关注关闭按钮行为）
async function renderDrawer(issue, opts = {}) {
  mock.method(api, 'get', async () => ({ notes: [] }))
  // 默认注入「用户点确定」：单测环境未挂载 DialogHost，confirmDialog 由
  // autoAnswer 直接应答 true；取消路径用例单独覆盖为 false
  dialog.installAutoAnswer(() => true)
  const onIssueClosed = opts.onIssueClosed || (() => {})
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue,
        repoName: 'botler',
        onClose: () => {},
        onIssueClosed,
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  return { renderer, root: renderer.root, onIssueClosed }
}

const OPEN_ISSUE = {
  project_id: 42,
  iid: 94,
  title: '添加关闭 issue 按钮',
  state: 'opened',
  updated_at: '2026-08-15 17:03:00',
  created_at: '2026-08-15 17:03:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/94',
  description: '需求描述',
}

// 查找「关闭 issue」按钮：IssueDrawer 中唯一使用危险操作样式
// （btn-danger）的按钮（× 关闭按钮为 modal-close、跳转为 <a>）
function findCloseButton(root) {
  return root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('btn-danger'))
}

// 渲染树 → 纯文本（与 overview-issue-drawer.test.mjs 的 toText 一致，
// 断言抽屉子树文本用）
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') {
    const children = node.children ?? node.props?.children
    return toText(children)
  }
  return ''
}

function drawerText(root) {
  return toText(root.children)
}

test('opened issue 显示「关闭 issue」按钮；closed 不显示', async () => {
  const { renderer, root } = await renderDrawer(OPEN_ISSUE)
  try {
    assert.equal(findCloseButton(root).length, 1, 'opened 应显示关闭按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
  const { renderer: r2, root: root2 } = await renderDrawer(
    { ...OPEN_ISSUE, state: 'closed' })
  try {
    assert.equal(findCloseButton(root2).length, 0, 'closed 不应显示关闭按钮')
  } finally {
    await TestRenderer.act(() => r2.unmount())
  }
})

test('缺 project_id 的旧数据不显示关闭按钮（无法定位仓库）', async () => {
  const legacy = { ...OPEN_ISSUE }
  delete legacy.project_id
  const { renderer, root } = await renderDrawer(legacy)
  try {
    assert.equal(findCloseButton(root).length, 0, '无 project_id 不应显示按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('点击按钮：confirm 确认后调用关闭接口（参数正确）', async () => {
  const postMock = mock.method(api, 'post', async (pathname) => {
    assert.equal(pathname, '/api/issues/42/94/close')
    return { ok: true, state: 'closed' }
  })
  const onIssueClosed = mock.fn()
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, { onIssueClosed })
  try {
    await TestRenderer.act(async () => {
      // 注入的 autoAnswer 应答 true（等价于用户在对话框中点「确定」）；
      // setTimeout 推进微任务链：确认 resolve 后组件才继续调关闭接口
      findCloseButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postMock.mock.callCount(), 1, '确认后应调用一次关闭接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击按钮：confirm 取消则不调用接口', async () => {
  const postMock = mock.method(api, 'post', async () => {
    throw new Error('不应调用')
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE)
  // 注入 autoAnswer 应答 false（用户在对话框中点「取消」）——
  // 须在 renderDrawer 之后：渲染 helper 会先注入默认的「确定」应答
  dialog.installAutoAnswer(() => false)
  try {
    await TestRenderer.act(async () => {
      findCloseButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postMock.mock.callCount(), 0, '取消确认不应调用接口')
    assert.equal(findCloseButton(root).length, 1, '按钮应保留')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('关闭成功：按钮消失、徽章变「已关闭」、回调通知父组件', async () => {
  mock.method(api, 'post', async () => ({ ok: true, state: 'closed' }))
  const onIssueClosed = mock.fn()
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, { onIssueClosed })
  try {
    await TestRenderer.act(async () => {
      findCloseButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findCloseButton(root).length, 0, '成功后按钮应消失')
    const text = drawerText(root)
    assert.ok(text.includes('已关闭'), '状态徽章应变为「已关闭」')
    assert.ok(!text.includes('开放'), '「开放」徽章不应残留')
    assert.equal(onIssueClosed.mock.callCount(), 1, '应通知父组件刷新列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('关闭失败：显示错误信息、按钮保留可重试、回调不触发', async () => {
  mock.method(api, 'post', async () => {
    throw new Error('GitLab API 错误: 500')
  })
  const onIssueClosed = mock.fn()
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, { onIssueClosed })
  try {
    await TestRenderer.act(async () => {
      findCloseButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('GitLab API 错误: 500'),
              '应显示错误信息')
    assert.equal(findCloseButton(root).length, 1, '失败后按钮应保留')
    assert.equal(onIssueClosed.mock.callCount(), 0, '失败不应通知刷新')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('请求进行中按钮 disabled，防重复点击', async () => {
  // post 挂起不返回：模拟慢请求，此时按钮应处于禁用态
  let resolvePost
  mock.method(api, 'post', () => new Promise((resolve) => { resolvePost = resolve }))
  const { renderer, root } = await renderDrawer(OPEN_ISSUE)
  try {
    await TestRenderer.act(async () => {
      findCloseButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const btn = findCloseButton(root)[0]
    assert.equal(btn.props.disabled, true, '请求中按钮应禁用')
    assert.ok(drawerText(root).includes('关闭中'), '应显示进行中文案')
    // 释放请求完成收尾
    await TestRenderer.act(async () => {
      resolvePost({ ok: true, state: 'closed' })
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
