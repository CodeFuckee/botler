// 概览页 issue 右边栏「完成耗时」展示测试（issue #300）：概览页弹出的
// issue 详情侧边栏，如果任务完成了（该 issue 最近任务成功终态
// succeeded），显示完成耗时（finished_at - created_at，与 issue #180
// 完成耗时统计语义一致）；未完成/从未执行显示「—」。完成耗时随抽屉
// 打开时拉取的 GET /api/issues/{project_id}/{iid}/detail 一起返回
// （后端 detail 接口新增 task_duration_seconds 字段，仅成功终态任务
// 返回秒数，其余为 null）。
//
// 断言：
// 1. 源码：IssueDrawer 含「完成耗时」行标题、从 detail 响应
//    d.task_duration_seconds 取值并经 fmtSeconds 展示；
// 2. 渲染：detail 返回 task_duration_seconds=3600 → 「完成耗时」行
//    显示「1 小时」；
// 3. 渲染：detail 返回 null（任务未完成/从未执行）→ 「—」；
// 4. 渲染：detail 请求失败 → 「完成耗时」行显示「—」不崩溃；
// 5. 渲染：task_duration_seconds 非法值（负数/NaN/字符串/null）→
//    「—」兜底。
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
const { api } = await vite.ssrLoadModule('/src/api.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 源码：含「完成耗时」行并从 detail 响应读取 d.task_duration_seconds', () => {
  assert.match(drawerSrc, /<th>完成耗时<\/th>|完成耗时/, '抽屉应含「完成耗时」行标题')
  assert.match(drawerSrc, /d\s*\.\s*task_duration_seconds/, '应从 detail 响应 d.task_duration_seconds 取值')
  assert.match(drawerSrc, /fmtSeconds/, '完成耗时应经 fmtSeconds 人类可读展示')
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

// 提取「完成耗时」行单元格文本
function durationRowText(root) {
  const drawer = findDrawer(root)[0]
  const rows = drawer.findAll((n) => n.type === 'tr')
  for (const row of rows) {
    const th = row.findAll((n) => n.type === 'th')
    if (th.length > 0 && toText(th[0].props.children).includes('完成耗时')) {
      const td = row.findAll((n) => n.type === 'td')
      return td.length > 0 ? toText(td[0].props.children).trim() : ''
    }
  }
  return null
}

test('渲染：detail 返回 task_duration_seconds → 「完成耗时」行显示人类可读耗时', async () => {
  const { renderer, root, renderError } =
    await renderOverview({ task_duration_seconds: 3600, task_id: 123, engine: 'dsh' })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(durationRowText(root), '1 小时', '已完成任务应显示完成耗时（3600s → 1 小时）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：detail 未返回 task_duration_seconds（任务未完成/从未执行）→ 「—」', async () => {
  const { renderer, root, renderError } = await renderOverview({})
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(durationRowText(root), '—', '未完成应显示「—」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：detail 请求失败 → 「完成耗时」行显示「—」不崩溃', async () => {
  const { renderer, root, renderError } = await renderOverview(null, '网络错误')
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(durationRowText(root), '—', '拉取失败应显示「—」兜底')
    assert.ok(findDrawer(root).length > 0, '其余抽屉信息应正常展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：task_duration_seconds 异常值（负数/NaN/字符串/null）→ 「—」兜底', async () => {
  for (const bad of [-1, NaN, 'abc', null]) {
    const { renderer, root } = await renderOverview({ task_duration_seconds: bad })
    try {
      assert.equal(durationRowText(root), '—',
                   `task_duration_seconds=${JSON.stringify(bad)} 应显示「—」`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})
