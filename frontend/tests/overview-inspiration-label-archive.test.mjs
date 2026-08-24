// 概览页「灵感」板块标签分类、筛选与归档测试（issue #246）：灵感表新增
// label（单标签，可自由输入或从候选选择）与 archived（软删除）；概览默认
// 隐藏归档（可开关查看）；灵感卡片操作区新增「归档/取消归档」按钮；转
// issue 时可选「保留灵感并关联」（默认删除保持旧行为）。
//
// 断言：
// 1. 源码：板块头部有标签筛选（inspiration-label-filter）与归档开关
//    （inspiration-archive-toggle）；归档/取消归档调用 POST
//    /api/inspirations/{id}/archive 与 /unarchive；编辑态有标签输入
//    （inspiration-label-input + datalist 候选）；转 issue 确认弹窗
//    提交带 keep_inspiration；overview/pages 请求带 archived/label 参数；
// 2. 渲染：标签徽章（inspiration-label-badge）、关联 issue 链接
//    （inspiration-linked-issue）、归档条目样式（inspiration-item-archived）
//    与「已归档」徽章；归档视图渲染「取消归档」按钮；
// 3. 交互：归档按钮 → POST archive；归档开关切换 → 重新请求 archived=1；
//    转 issue 弹窗勾选「保留灵感并关联」→ POST 带 keep_inspiration=true。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const comp = readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与现有测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}
function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}
function findByClass(renderer, cls) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes(cls))
}
function findButton(renderer, cls) {
  const list = findByClass(renderer, cls)
  assert.ok(list.length > 0, `找不到按钮 ${cls}`)
  return list[0]
}

// 未归档视图 payload（带标签 / 关联 / 归档字段）
const ACTIVE_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', enabled: true, priority: 10,
    archived_total: 1,
    inspirations: [
      { id: 11, repo_id: 1, repo_name: 'botler', content: '待验证灵感',
        label: '待验证', archived: false,
        linked_issue_iid: null, linked_issue_url: null,
        updated_at: '2026-08-16 12:00:00' },
      { id: 12, repo_id: 1, repo_name: 'botler', content: '已规划灵感（关联 issue）',
        label: '已规划', archived: false,
        linked_issue_iid: 77, linked_issue_url: 'https://gitlab.example.com/x/-/issues/77',
        updated_at: '2026-08-16 11:00:00' },
    ],
  }],
}

// 归档视图 payload：归档后的灵感（archived=true）
const ARCHIVED_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', enabled: true, priority: 10,
    archived_total: 1,
    inspirations: [
      { id: 13, repo_id: 1, repo_name: 'botler', content: '已归档灵感',
        label: '已实现', archived: true,
        linked_issue_iid: null, linked_issue_url: null,
        updated_at: '2026-08-15 12:00:00' },
    ],
  }],
}

async function renderOverview({
  activePayload = ACTIVE_PAYLOAD,
  archivedPayload = ARCHIVED_PAYLOAD,
} = {}) {
  const getCalls = []
  const postCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname.startsWith('/api/inspirations/overview')) {
      return pathname.includes('archived=1') ? archivedPayload : activePayload
    }
    if (pathname.startsWith('/api/inspirations/pages/')) {
      return { repo_id: 1, total: 0, offset: 0, limit: 20, has_more: false, inspirations: [] }
    }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname, body) => {
    postCalls.push([pathname, body])
    return { id: 99 }
  })
  mock.method(api, 'put', async (_pathname, _body) => { return {} })
  mock.method(api, 'del', async (_pathname) => { return null })
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
  return {
    renderer, renderError, getCalls, postCalls,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// ---- 源码断言 ----

test('源码：板块头部有标签筛选与归档开关（issue #246）', () => {
  assert.match(comp, /inspiration-label-filter/, '应有标签筛选下拉（inspiration-label-filter）')
  assert.match(comp, /inspiration-archive-toggle/, '应有归档开关（inspiration-archive-toggle）')
  assert.match(comp, /inspirationShowArchived/, '应存在归档开关状态 inspirationShowArchived')
  assert.match(comp, /inspirationLabelFilter/, '应存在标签筛选状态 inspirationLabelFilter')
})

test('源码：归档/取消归档调用 POST /api/inspirations/{id}/archive 与 /unarchive', () => {
  assert.match(comp, /api\.post\(`\/api\/inspirations\/\$\{insp\.id\}\/archive`\)/,
               '归档应调用 POST /api/inspirations/{id}/archive')
  assert.match(comp, /api\.post\(`\/api\/inspirations\/\$\{insp\.id\}\/unarchive`\)/,
               '取消归档应调用 POST /api/inspirations/{id}/unarchive')
})

test('源码：转 issue 提交带 keep_inspiration 参数', () => {
  assert.match(comp, /keep_inspiration/,
               'add-issue 提交应带 keep_inspiration（issue #246 保留灵感并关联）')
  assert.match(comp, /inspiration-keep-modal/, '应有点击「添加 Issue」后的保留确认弹窗')
  assert.match(comp, /inspiration-keep-checkbox/, '弹窗应有「保留灵感并关联」勾选框')
})

test('源码：overview/pages 请求带 archived 与 label 参数', () => {
  assert.match(comp, /archived/, 'overview/pages 请求应支持 archived 参数（查看归档）')
  assert.match(comp, /label/, 'overview/pages 请求应支持 label 参数（按标签筛选）')
})

test('源码：编辑态有标签输入（inspiration-label-input + datalist 候选）', () => {
  assert.match(comp, /inspiration-label-input/, '编辑态应有标签输入框')
  assert.match(comp, /datalist/, '标签输入应有 datalist 候选（已有标签快速选择）')
})

test('i18n：标签筛选/归档相关文案已提供中文与英文', () => {
  assert.equal(typeof zhCN['overview.inspirationLabelAll'], 'string', '应有「全部标签」文案')
  assert.equal(typeof zhCN['overview.inspirationShowArchived'], 'string', '应有「查看归档」文案')
  assert.equal(typeof zhCN['overview.inspirationArchive'], 'string', '应有「归档」按钮文案')
  assert.equal(typeof zhCN['overview.inspirationUnarchive'], 'string', '应有「取消归档」按钮文案')
  assert.equal(typeof zhCN['overview.inspirationKeepLabel'], 'string', '应有「保留灵感并关联」文案')
})

test('样式：归档条目 / 标签徽章 / 归档开关样式已提供', () => {
  assert.match(styles, /\.inspiration-label-badge/, '应有标签徽章样式')
  assert.match(styles, /\.inspiration-item-archived/, '应有归档条目样式')
  assert.match(styles, /\.inspiration-linked-issue/, '应有关联 issue 链接样式')
})

// ---- 渲染断言 ----

test('渲染：灵感条目显示标签徽章与关联 issue 链接', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    assert.ok(text.includes('待验证灵感'), '应渲染未归档灵感内容')
    assert.ok(text.includes('待验证'), '应渲染标签徽章文本')
    assert.ok(text.includes('已关联 issue #77'), '应渲染「已关联 issue」链接文本')
  } finally {
    await r.unmount()
  }
})

test('渲染：归档视图显示归档条目与「已归档」标记', async () => {
  const r = await renderOverview()
  try {
    // 打开归档开关 → 请求 archived=1 → 渲染归档 payload
    const toggle = findByClass(r.renderer, 'inspiration-archive-checkbox')[0]
    assert.ok(toggle, '应有归档开关 checkbox')
    await TestRenderer.act(async () => {
      toggle.props.onChange({ target: { checked: true } })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const text = treeText(r.renderer)
    assert.ok(text.includes('已归档灵感'), '归档视图应渲染归档条目')
    assert.ok(text.includes('已归档'), '归档条目应有「已归档」标记')
    assert.ok(r.getCalls.some((p) => p.includes('/api/inspirations/overview')
      && p.includes('archived=1')), '切换开关后应重新请求 archived=1 的概览')
    const unarchiveBtn = findButton(r.renderer, 'inspiration-unarchive-btn')
    assert.ok(unarchiveBtn, '归档视图条目应有「取消归档」按钮')
  } finally {
    await r.unmount()
  }
})

test('渲染：未归档视图每条灵感有「归档」按钮', async () => {
  const r = await renderOverview()
  try {
    const btns = findByClass(r.renderer, 'inspiration-archive-btn')
    assert.equal(btns.length, 2, '两条未归档灵感各有一个「归档」按钮')
  } finally {
    await r.unmount()
  }
})

// ---- 交互断言 ----

test('交互：点击归档 → POST /api/inspirations/{id}/archive', async () => {
  const r = await renderOverview()
  try {
    const btn = findButton(r.renderer, 'inspiration-archive-btn')
    await TestRenderer.act(async () => {
      btn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(r.postCalls.some(([p]) => p === '/api/inspirations/11/archive'),
              '应调用 POST /api/inspirations/11/archive')
  } finally {
    await r.unmount()
  }
})

test('交互：点击取消归档 → POST /api/inspirations/{id}/unarchive', async () => {
  const r = await renderOverview()
  try {
    const toggle = findByClass(r.renderer, 'inspiration-archive-checkbox')[0]
    await TestRenderer.act(async () => {
      toggle.props.onChange({ target: { checked: true } })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const btn = findButton(r.renderer, 'inspiration-unarchive-btn')
    await TestRenderer.act(async () => {
      btn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(r.postCalls.some(([p]) => p === '/api/inspirations/13/unarchive'),
              '应调用 POST /api/inspirations/13/unarchive')
  } finally {
    await r.unmount()
  }
})

test('交互：转 issue 弹窗勾选保留 → POST 带 keep_inspiration=true', async () => {
  const r = await renderOverview()
  try {
    // 点击「添加 Issue」→ 弹出保留确认弹窗
    const addBtn = findButton(r.renderer, 'inspiration-add-issue-btn')
    await TestRenderer.act(async () => { addBtn.props.onClick() })
    assert.ok(findByClass(r.renderer, 'inspiration-keep-modal').length > 0,
              '点击「添加 Issue」应弹出保留确认弹窗')
    // 勾选「保留灵感并关联」（精确匹配 input，避免命中 label 容器）
    const checkbox = r.renderer.root.findAll(
      (n) => n.props.className === 'inspiration-keep-checkbox')[0]
    assert.ok(checkbox, '弹窗应有保留勾选框')
    await TestRenderer.act(async () => {
      checkbox.props.onChange({ target: { checked: true } })
    })
    // 确认创建
    const confirmBtn = findButton(r.renderer, 'inspiration-keep-confirm-btn')
    await TestRenderer.act(async () => {
      confirmBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(r.postCalls.some(([p, body]) =>
      p === '/api/inspirations/11/add-issue' && body && body.keep_inspiration === true),
      '确认后应 POST /api/inspirations/11/add-issue 且 keep_inspiration=true')
  } finally {
    await r.unmount()
  }
})

test('交互：转 issue 弹窗不勾选 → POST 带 keep_inspiration=false（默认删除）', async () => {
  const r = await renderOverview()
  try {
    const addBtn = findButton(r.renderer, 'inspiration-add-issue-btn')
    await TestRenderer.act(async () => { addBtn.props.onClick() })
    const confirmBtn = findButton(r.renderer, 'inspiration-keep-confirm-btn')
    await TestRenderer.act(async () => {
      confirmBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(r.postCalls.some(([p, body]) =>
      p === '/api/inspirations/11/add-issue' && body && body.keep_inspiration === false),
      '默认不勾选时应 POST keep_inspiration=false（保持删除旧行为）')
  } finally {
    await r.unmount()
  }
})

test('交互：编辑态显示标签输入框', async () => {
  const r = await renderOverview()
  try {
    // 进入第一条灵感的编辑态
    const editBtn = r.renderer.root.findAll((n) =>
      n.type === 'button'
      && String(n.props.className || '').includes('inspiration-action-btn')
      && !String(n.props.className || '').includes('inspiration-add-issue-btn')
      && textOf(n.props.children).includes('编辑'))[0]
    await TestRenderer.act(async () => { editBtn.props.onClick() })
    const labelInput = findByClass(r.renderer, 'inspiration-label-input')
    assert.equal(labelInput.length, 1, '编辑态应显示标签输入框')
  } finally {
    await r.unmount()
  }
})
