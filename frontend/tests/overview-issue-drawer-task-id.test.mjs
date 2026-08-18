// 概览页 issue 右边栏「任务id」展示测试（issue #290）：概览页弹出的
// issue 详情侧边栏，如果该 issue 已经执行过（有任务记录），显示对应
// 的任务 id（#id）；从未执行（无任务记录）显示「—」。任务 id 随抽屉
// 打开时拉取的 GET /api/issues/{project_id}/{iid}/detail 一起返回
// （后端 detail 接口新增 task_id 字段，取该 issue 最近一条任务 id，
// 无任务为 null）。
//
// 断言：
// 1. 源码：IssueDrawer 含「任务」行标题、从 detail 响应 d.task_id 取值；
// 2. 渲染：detail 返回 task_id=123 → 「任务」行显示 #123；
// 3. 渲染：detail 未返回 task_id（从未执行）→ 「任务」行显示「—」；
// 4. 渲染：detail 请求失败 → 「任务」行显示「—」不崩溃；
// 5. 渲染：task_id 非正数（0/字符串/负数等异常数据）→ 「—」兜底。
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
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: IssueDrawer } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 源码：含「任务」行并从 detail 响应读取 d.task_id', () => {
  assert.match(drawerSrc, /<th>任务<\/th>|任务/, '抽屉应含「任务」行标题')
  assert.match(drawerSrc, /d\s*\.\s*task_id/, '应从 detail 响应 d.task_id 取值')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

const FULL_ISSUE = {
  iid: 64, title: '概览页面增加读取已启用的仓库issue',
  state: 'opened',
  updated_at: '2026-08-14 10:20:00',
  created_at: '2026-08-10 09:00:00',
  project_id: 1,
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
  description: '**需求**\n\n- 要点一\n- 要点二',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
  milestone: 'v1.0',
  assignees: [{ name: 'Agent', username: 'agent' }],
  user_notes_count: 3,
}

async function renderOverview(detailBody, detailError) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10,
                         issues: [FULL_ISSUE] }], errors: [] }
    }
    if (pathname === '/api/issues/1/64/detail') {
      if (detailError) throw new Error(detailError)
      return { notes: [], ...detailBody }
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
  await TestRenderer.act(async () => {
    const itemBtn = renderer.root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    if (itemBtn.length > 0) itemBtn[0].props.onClick()
  })
  return { renderer, root: renderer.root, renderError }
}

function findDrawer(root) {
  return root.findAll(
    (n) => String(n.props.className || '').includes('issue-drawer')
      && n.props.onClick)
}

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

// 提取「任务」行单元格文本
function taskRowText(root) {
  const drawer = findDrawer(root)[0]
  const rows = drawer.findAll((n) => n.type === 'tr')
  for (const row of rows) {
    const th = row.findAll((n) => n.type === 'th')
    if (th.length > 0 && toText(th[0].props.children).includes('任务')) {
      const td = row.findAll((n) => n.type === 'td')
      return td.length > 0 ? toText(td[0].props.children).trim() : ''
    }
  }
  return null
}

test('渲染：detail 返回 task_id → 「任务」行显示 #id', async () => {
  const { renderer, root, renderError } = await renderOverview({ task_id: 123, engine: 'dsh' })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(taskRowText(root), '#123', '已执行 issue 应显示对应任务 id')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：detail 未返回 task_id（从未执行）→ 「任务」行显示「—」', async () => {
  const { renderer, root, renderError } = await renderOverview({})
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(taskRowText(root), '—', '从未执行应显示「—」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：detail 请求失败 → 「任务」行显示「—」不崩溃', async () => {
  const { renderer, root, renderError } = await renderOverview(null, '网络错误')
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(taskRowText(root), '—', '拉取失败应显示「—」兜底')
    assert.ok(findDrawer(root).length > 0, '其余抽屉信息应正常展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：task_id 异常值（0/字符串/负数）→ 「—」兜底', async () => {
  for (const bad of [0, 'abc', -1, null]) {
    const { renderer, root } = await renderOverview({ task_id: bad })
    try {
      assert.equal(taskRowText(root), '—', `task_id=${JSON.stringify(bad)} 应显示「—」`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})
