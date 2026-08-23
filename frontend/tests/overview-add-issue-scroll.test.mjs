// 概览页「添加 Issue」弹窗滚动布局测试（issue #458）：弹窗内容超过
// 视口高度时——头部标题固定在顶部、取消/创建 Issue 按钮固定在底部、
// 中间表单区（标题/描述/图片附件/分配人/标签）独立上下滚动。
//
// 断言：
// 1. styles.css 源码：.add-issue-body 规则存在且可滚动（overflow-y: auto /
//    min-height: 0 / flex 撑满剩余空间），规则限定 .modal.add-issue 作用域
//    （裸 .modal 不被改成滚动、裸 .modal-footer 不被改成 sticky/absolute，
//    避免波及仓库设置等其他弹窗）；
// 2. 渲染结构：标题/描述/图片附件/分配人/标签字段全部位于 .add-issue-body
//    内；.modal-header 与 .modal-footer 是弹窗直接子节点、位于滚动区外
//    （头部固定顶部、取消/创建按钮固定底部不随内容滚动）；
// 3. 边界：元数据加载中不渲染表单（无 .add-issue-body）；元数据加载失败
//    显示错误不崩溃。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview 系测试一致）。
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

// 挂载 Overview：api.get 按路径分流。formMetaPending=true 时 form-meta
// 永不 resolve（验证加载中状态）；formMetaError 非空时模拟加载失败。
async function renderOverview({ issuesPayload = ISSUES_PAYLOAD,
                                formMeta = FORM_META,
                                formMetaError = null,
                                formMetaPending = false } = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
    if (pathname.startsWith('/api/issues/form-meta/')) {
      if (formMetaPending) return new Promise(() => {}) // 永不 resolve：加载中
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

function modalOf(renderer) {
  return renderer.root.find(
    (n) => String(n.props.className || '').includes('modal add-issue'))
}

// 节点是否位于带指定 class 的祖先内（滚动区归属断言）
function hasAncestor(node, cls) {
  let cur = node
  while (cur) {
    if (String(cur.props?.className || '').includes(cls)) return true
    cur = cur.parent
  }
  return false
}

// 提取某选择器首个规则体（feature 特征非空时只返回含该特征的规则）
function ruleBody(css, selector, feature) {
  const re = new RegExp(`${selector}\\s*\\{([^}]*)\\}`, 'g')
  let m
  while ((m = re.exec(css))) {
    if (!feature || m[1].includes(feature)) return m[1]
  }
  assert.ok(false, `styles.css 应存在 ${selector} 规则（特征：${feature || '任意'}）`)
}

// ---- styles.css 源码断言 ----

test('styles.css：添加 Issue 弹窗中间表单区可独立滚动（issue #458）', () => {
  const body = ruleBody(styles, '\\.modal\\.add-issue\\s+\\.add-issue-body', 'overflow-y')
  assert.match(body, /overflow-y:\s*auto/, '中间表单区应 overflow-y: auto 可滚动')
  assert.match(body, /min-height:\s*0/, '滚动区应有 min-height: 0（flex 子项允许收缩触发滚动）')
  assert.match(body, /flex/, '滚动区应 flex 撑满弹窗剩余高度')
  assert.match(body, /overscroll-behavior/, '滚动区应隔离滚动（overscroll-behavior，避免滚穿到页面）')
})

test('styles.css：滚动规则限定 .modal.add-issue，不波及其他弹窗', () => {
  // 裸 .modal 不应被改成 overflow-y: auto（会波及仓库设置/对话框等全部弹窗）
  assert.ok(!/^\s*\.modal\s*\{[^}]*overflow-y/m.test(styles),
            '裸 .modal 不应新增 overflow-y（限定 .modal.add-issue 作用域）')
  // 裸 .modal-footer 不应被改成 sticky/absolute（固定底部靠结构实现——
  // footer 在滚动区外是弹窗 flex 列的直接子节点，天然固定）
  assert.ok(!/^\s*\.modal-footer\s*\{[^}]*position:\s*(sticky|absolute)/m.test(styles),
            '裸 .modal-footer 不应改为 sticky/absolute（避免影响其他弹窗尾部）')
  // 滚动区规则必须带 .add-issue 限定选择器
  assert.ok(/\.modal\.add-issue\s+\.add-issue-body\s*\{/.test(styles),
            '滚动区规则应限定 .modal.add-issue 作用域')
})

// ---- 渲染结构断言 ----

test('渲染：表单字段位于 .add-issue-body 内，头/尾在滚动区外固定', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    await openAddIssueModal(renderer, 0)
    const modal = modalOf(renderer)
    assert.ok(modal, '应渲染弹窗')

    // 弹窗直接子节点：header / add-issue-body / footer 三者并列
    const children = modal.children
    const hasDirectChild = (cls) => children.some(
      (c) => String(c.props?.className || '').includes(cls))
    assert.ok(hasDirectChild('modal-header'), '弹窗应含直接子节点 .modal-header')
    assert.ok(hasDirectChild('add-issue-body'), '弹窗应含直接子节点 .add-issue-body')
    assert.ok(hasDirectChild('modal-footer'), '弹窗应含直接子节点 .modal-footer（滚动区外）')

    // 五个字段全部在滚动区内
    const titleInput = renderer.root.find(
      (n) => String(n.props.className || '').includes('add-issue-title'))
    const desc = renderer.root.find(
      (n) => String(n.props.className || '').includes('add-issue-desc'))
    const attachment = renderer.root.find(
      (n) => String(n.props.className || '').includes('add-issue-attachment'))
    const assignee = renderer.root.find(
      (n) => String(n.props.className || '').includes('add-issue-assignee'))
    const labelPicker = renderer.root.find(
      (n) => String(n.props.className || '').includes('label-picker'))
    for (const field of [titleInput, desc, attachment, assignee, labelPicker]) {
      assert.ok(hasAncestor(field, 'add-issue-body'),
                '标题/描述/图片附件/分配人/标签字段应位于滚动区 .add-issue-body 内')
      assert.ok(!hasAncestor(field, 'modal-footer'),
                '表单字段不应位于 .modal-footer 内（尾部按钮区只放按钮）')
    }

    // 头部与尾部不在滚动区内（头部固定顶部、取消/创建按钮固定底部）
    const header = renderer.root.find(
      (n) => String(n.props.className || '').includes('modal-header'))
    const footer = renderer.root.find(
      (n) => String(n.props.className || '').includes('modal-footer'))
    assert.ok(!hasAncestor(header, 'add-issue-body'), '头部不应在滚动区内')
    assert.ok(!hasAncestor(footer, 'add-issue-body'), '尾部（取消/创建按钮）不应在滚动区内')

    // 尾部按钮齐全：取消 + 创建 Issue
    const footerButtons = footer.findAll((n) => n.type === 'button')
    const footerText = footerButtons.map((b) => {
      let t = ''
      const walk = (n) => {
        if (typeof n === 'string') { t += n; return }
        if (n?.props?.children != null) walk(n.props.children)
      }
      walk(b.props.children)
      return t.trim()
    })
    assert.ok(footerText.some((t) => t.includes('取消')), '尾部应含「取消」按钮')
    assert.ok(footerText.some((t) => t.includes('创建 Issue')), '尾部应含「创建 Issue」按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 边界 ----

test('边界：元数据加载中不渲染表单滚动区（无 .add-issue-body）', async () => {
  const { renderer, renderError } = await renderOverview({ formMetaPending: true })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const bodies = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('add-issue-body'))
    assert.equal(bodies.length, 0, '加载中不应渲染表单滚动区')
    assert.ok(modalOf(renderer), '加载中弹窗仍应渲染')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：元数据加载失败显示错误且不崩溃', async () => {
  const { renderer, renderError } = await renderOverview({
    formMetaError: '加载成员失败',
  })
  try {
    assert.equal(renderError, null)
    await openAddIssueModal(renderer, 0)
    const alerts = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('alert-error'))
    assert.ok(alerts.length > 0, '加载失败应显示错误提示')
    const bodies = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('add-issue-body'))
    assert.equal(bodies.length, 0, '加载失败不渲染表单滚动区')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
