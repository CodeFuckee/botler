// 概览页 issue 右边栏「任务执行引擎」测试（issue #118）：
// 概览页面弹出的 issue 右边栏展示任务执行引擎的类型——抽屉打开时从
// GET /api/settings 读取 worker.engine（issue #113 引入的全局配置：
// claude / hermes / dsh，默认 claude），KV 表格新增「执行引擎」行。
//
// 断言：
// 1. 源码：IssueDrawer 含「执行引擎」行、拉取 /api/settings、
//    ENGINE_META 三引擎映射（claude / hermes / dsh）；
// 2. 纯函数 engineDisplay：null → 加载中；空值 → 回退 claude；
//    未知值 → 原样展示兜底；
// 3. 渲染：settings 返回 dsh → 抽屉显示 deepseek-harness SDK 文案；
// 4. 渲染：settings 未返回 engine → 回退 Claude Code CLI；
// 5. 渲染：settings 请求失败 → 显示「—」不崩溃；
// 6. 渲染：未知引擎值 → 原样展示兜底。
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
const { default: IssueDrawer, ENGINE_META, engineDisplay } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 源码：新增「执行引擎」行并拉取 /api/settings', () => {
  assert.match(drawerSrc, /执行引擎/, '抽屉应含「执行引擎」行标题')
  assert.match(drawerSrc, /\/api\/settings/, '应拉取 /api/settings 获取引擎配置')
  assert.match(drawerSrc, /worker\s*\.\s*engine/, '应从 settings.worker.engine 取值')
})

test('ENGINE_META 映射覆盖 claude / hermes / dsh 三引擎', () => {
  assert.ok(ENGINE_META.claude, '应含 claude 映射')
  assert.ok(ENGINE_META.hermes, '应含 hermes 映射')
  assert.ok(ENGINE_META.dsh, '应含 dsh 映射')
})

test('engineDisplay 纯函数：null=加载中 / 空值回退 claude / 未知原样', () => {
  assert.equal(engineDisplay(null), '加载中…', 'null 应表示加载中')
  assert.equal(engineDisplay(undefined), '加载中…', 'undefined 应表示加载中')
  assert.equal(engineDisplay(''), ENGINE_META.claude.label, '空值应回退 claude 文案')
  assert.equal(engineDisplay('   '), ENGINE_META.claude.label, '纯空白应回退 claude 文案')
  assert.equal(engineDisplay('claude'), ENGINE_META.claude.label)
  assert.equal(engineDisplay('hermes'), ENGINE_META.hermes.label)
  assert.equal(engineDisplay('dsh'), ENGINE_META.dsh.label)
  assert.equal(engineDisplay('Dsh'), ENGINE_META.dsh.label, '大写应归一为小写匹配')
  assert.equal(engineDisplay('foo'), 'foo', '未知值应原样展示兜底')
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

async function renderOverview(settingsBody, settingsError) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10,
                         issues: [FULL_ISSUE] }], errors: [] }
    }
    if (pathname === '/api/issues/1/64/detail') return { notes: [] }
    if (pathname === '/api/settings') {
      if (settingsError) throw new Error(settingsError)
      return settingsBody
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
  // 打开抽屉（点击列表项按钮）
  let root = null
  await TestRenderer.act(async () => {
    const itemBtn = renderer.root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    if (itemBtn.length > 0) itemBtn[0].props.onClick()
  })
  root = renderer.root
  return { renderer, root, renderError }
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

function drawerText(root) {
  const drawers = findDrawer(root)
  return drawers.length > 0 ? toText(drawers[0].children) : ''
}

// 提取「执行引擎」行单元格文本
function engineRowText(root) {
  const drawer = findDrawer(root)[0]
  const rows = drawer.findAll((n) => n.type === 'tr')
  for (const row of rows) {
    const th = row.findAll((n) => n.type === 'th')
    if (th.length > 0 && toText(th[0].props.children).includes('执行引擎')) {
      const td = row.findAll((n) => n.type === 'td')
      return td.length > 0 ? toText(td[0].props.children).trim() : ''
    }
  }
  return null
}

test('渲染：settings 返回 dsh → 抽屉执行引擎行显示 deepseek-harness SDK', async () => {
  const { renderer, root, renderError } = await renderOverview(
    { worker: { engine: 'dsh' } })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(engineRowText(root), ENGINE_META.dsh.label,
                 '执行引擎行应显示 dsh 对应文案')
    assert.ok(drawerText(root).includes('执行引擎'), '抽屉应含执行引擎行')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：settings 未返回 engine → 回退 claude 文案', async () => {
  const { renderer, root } = await renderOverview({ worker: {} })
  try {
    assert.equal(engineRowText(root), ENGINE_META.claude.label,
                 '后端未返回 engine 时应回退默认 claude')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：settings 请求失败 → 执行引擎行显示「—」不崩溃', async () => {
  const { renderer, root, renderError } = await renderOverview(null, '网络错误')
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(engineRowText(root), '—', '拉取失败应显示「—」兜底')
    assert.ok(drawerText(root).includes('#64'), '其余抽屉信息应正常展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：未知引擎值 → 原样展示兜底', async () => {
  const { renderer, root } = await renderOverview({ worker: { engine: 'foo' } })
  try {
    assert.equal(engineRowText(root), 'foo', '未知引擎值应原样展示不崩溃')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
