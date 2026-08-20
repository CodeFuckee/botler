// 任务详情页「任务执行前预检」面板测试（issue #238）。
//
// 需求：执行器领取任务后、消耗模型调用前对环境做快速检查（git 凭据/token /
// 本地路径 / 磁盘剩余空间 / 工作区可用），检查明细（✓/✗）落库
// tasks.precheck_result；任务详情页「元信息」区以折叠面板展示——预检失败
// 时任务已在预检阶段判定失败，面板展示各检查项结果与整体状态；无记录
// （旧任务 / 未启用预检）显示「暂无预检记录」。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom mock（与 task-detail-env-snapshot.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: TaskDetail } = await vite.ssrLoadModule('/src/pages/TaskDetail.jsx')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.onerror = null
    this.closed = false
    FakeEventSource.instances.push(this)
  }
  close() { this.closed = true }
  emit(event) { if (this.onmessage) this.onmessage({ data: JSON.stringify(event) }) }
}

const BASE_TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '任务执行前预检', issue_url: null,
  status: 'failed', attempt_count: 0, triggered_by: 'webhook',
  exit_code: null, error_message: null, error_detail: null,
  commit_sha: null, commit_url: null, log_path: null,
  log_file_tail: null, logs: [],
  created_at: '2026-08-20 09:00:00', started_at: null,
  finished_at: '2026-08-20 09:00:02', prompt: null,
}

const PRECHECK_PASS = {
  ok: true,
  checked_at: '2026-08-20T09:00:01+08:00',
  checks: [
    { name: 'git_token', label: 'Git 凭据/Token', ok: true, detail: 'git ls-remote origin 探测通过（仓库可克隆）' },
    { name: 'local_path', label: '本地路径', ok: null, detail: '未配置 local_path，跳过' },
    { name: 'disk_space', label: '磁盘剩余空间', ok: true, detail: '磁盘剩余 128.0 GB ≥ 阈值 2048 MB' },
    { name: 'workspace', label: '工作区可用', ok: true, detail: '工作区可用' },
  ],
}

const PRECHECK_FAIL = {
  ok: false,
  checked_at: '2026-08-20T09:00:01+08:00',
  checks: [
    { name: 'git_token', label: 'Git 凭据/Token', ok: false, detail: 'git ls-remote https://x.git 失败（exit 128）: fatal: Authentication failed' },
    { name: 'local_path', label: '本地路径', ok: null, detail: '未配置 local_path，跳过' },
    { name: 'disk_space', label: '磁盘剩余空间', ok: true, detail: '磁盘剩余 128.0 GB ≥ 阈值 2048 MB' },
    { name: 'workspace', label: '工作区可用', ok: true, detail: '工作区可用' },
  ],
}

function mockTaskApi(taskOverrides = {}, execution = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') return { ...BASE_TASK, ...taskOverrides }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return execution || { status: 'failed', session_id: null,
                            log_offset: 0, log_delta: [], transcript: [],
                            transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
}

function withEventSource(fn) {
  return async () => {
    FakeEventSource.instances = []
    const saved = globalThis.EventSource
    globalThis.EventSource = FakeEventSource
    try { await fn() } finally {
      globalThis.EventSource = saved
      FakeEventSource.instances = []
    }
  }
}

async function renderDetail() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, null, React.createElement(TaskDetail)),
    )
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return renderer
}

function thTexts(renderer) {
  return renderer.root.findAllByType('th').map((n) => {
    const parts = []
    const walk = (c) => {
      if (c == null) return
      if (typeof c === 'string') { parts.push(c); return }
      if (Array.isArray(c)) { c.forEach(walk); return }
      if (typeof c === 'object' && c.props) walk(c.props.children)
    }
    walk(n.props.children)
    return parts.join('')
  })
}

function tdTexts(renderer) {
  return renderer.root.findAllByType('td').map((n) => {
    const parts = []
    const walk = (c) => {
      if (c == null) return
      if (typeof c === 'string') { parts.push(c); return }
      if (Array.isArray(c)) { c.forEach(walk); return }
      if (typeof c === 'object' && c.props) walk(c.props.children)
    }
    walk(n.props.children)
    return parts.join('')
  })
}

function allText(renderer) {
  const parts = []
  const walk = (n) => {
    if (n == null) return
    if (typeof n === 'string') { parts.push(n); return }
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.children) walk(n.children)
  }
  walk(renderer.toJSON())
  return parts.join(' ')
}

test('预检通过：元信息区展示 4 个检查项与通过标记', withEventSource(async () => {
  mockTaskApi({ precheck_result: PRECHECK_PASS })
  const renderer = await renderDetail()
  try {
    const text = allText(renderer)
    assert.ok(text.includes('任务执行前预检'), '应展示「任务执行前预检」折叠面板')
    // 检查项 label 在 kv 表 <th> 单元格
    const headers = thTexts(renderer)
    assert.ok(headers.includes('Git 凭据/Token'), '应展示 Git 凭据/Token 检查项')
    assert.ok(headers.includes('本地路径'), '应展示本地路径检查项')
    assert.ok(headers.includes('磁盘剩余空间'), '应展示磁盘剩余空间检查项')
    assert.ok(headers.includes('工作区可用'), '应展示工作区可用检查项')

    const cells = tdTexts(renderer).join(' ')
    assert.ok(cells.includes('✓ 通过'), '通过的检查项应展示 ✓ 通过')
    assert.ok(cells.includes('— 跳过'), '未配置的检查项应展示 — 跳过')
    assert.ok(cells.includes('探测通过'), '应展示检查明细')
    assert.ok(text.includes('全部检查通过'), '整体状态应展示「全部检查通过」')
  } finally {
    renderer.unmount()
  }
}))

test('预检失败：元信息区展示 ✗ 未通过与失败原因，任务已判失败', withEventSource(async () => {
  mockTaskApi({ precheck_result: PRECHECK_FAIL })
  const renderer = await renderDetail()
  try {
    const cells = tdTexts(renderer).join(' ')
    assert.ok(cells.includes('✗ 未通过'), '未通过的检查项应展示 ✗ 未通过')
    assert.ok(cells.includes('Authentication failed'), '应展示失败具体原因')
    const text = allText(renderer)
    // 未通过项数量为 JSX 表达式插值（独立文本节点，allText 以空格拼接），
    // 这里只断言文案片段
    assert.ok(text.includes('项检查未通过'), '整体状态应提示未通过项数量')
    assert.ok(text.includes('预检阶段判定失败'), '应提示任务已在预检阶段判定失败')
  } finally {
    renderer.unmount()
  }
}))

test('无预检记录（旧任务/未启用预检）：显示提示文案', withEventSource(async () => {
  mockTaskApi({ precheck_result: null })
  const renderer = await renderDetail()
  try {
    const text = allText(renderer)
    assert.ok(text.includes('暂无预检记录'), '无预检记录应显示提示文案')
  } finally {
    renderer.unmount()
  }
}))
