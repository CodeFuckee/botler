// 复现测试（issue #49）：任务「用时」应显示系统处理该问题的完整周期——
// 从系统接收到问题（任务入队 created_at）到系统给 issue 打上 bot-done 标记
// （finished_at）的时长，而不是 Claude 执行时长（started_at → finished_at）。
//
// 修复前行为：Tasks 列表「用时」列与 TaskDetail「执行用时」行均用
// fmtDuration(started_at || created_at, finished_at)——任务排队等待的
// 时间不计入，且终点语义是执行结束而非 bot-done 打标。
//
// 修复目标：
// 1. 用时起点固定为 created_at（系统接收时间），终点为 finished_at
//    （后端已保证 = bot-done 打标时间，见 backend/tests/test_task_duration.py）；
// 2. TaskDetail 字段名由「执行用时」改为「处理用时」，与语义一致；
// 3. 动态计算，不新增数据库字段。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const taskDetailSrc = readFileSync(path.join(ROOT, 'src/pages/TaskDetail.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 stop-all-button.test.mjs 一致）。
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
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { default: TaskDetail } = await vite.ssrLoadModule('/src/pages/TaskDetail.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// 任务数据：入队（接收）09:50，开始执行 10:00，收尾（bot-done 打标）10:30。
// 期望用时 = 40 分钟（09:50 → 10:30）；修复前显示 30 分钟（10:00 → 10:30）。
const TASK = {
  id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
  status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
  exit_code: 0, error_message: null, error_detail: null,
  resumed: false, commit_sha: null, commit_url: null, log_path: null,
  created_at: '2026-08-13 09:50:00',
  started_at: '2026-08-13 10:00:00',
  finished_at: '2026-08-13 10:30:00',
}

async function renderTasks() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [TASK], total: 1, stats: { queued: 0, running: 0, retrying: 0, succeeded: 1 } }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Tasks)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

async function renderTaskDetail() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/tasks/3') {
      return { ...TASK, logs: [], log_file_tail: null }
    }
    if (pathname.startsWith('/api/tasks/3/execution')) {
      return { status: 'succeeded', session_id: null, log_offset: 0,
               log_delta: [], transcript: [], transcript_truncated: false }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(TaskDetail)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

test('任务列表「用时」= 系统接收时间 → bot-done 打标时间（40 分钟），不含排队外的执行时长', async () => {
  const { renderer, renderError } = await renderTasks()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const tree = JSON.stringify(renderer.toJSON())
    assert.ok(
      tree.includes('40 分钟'),
      '用时列应显示 40 分钟（created_at 09:50 → finished_at 10:30）；' +
        '修复前用 started_at 起点显示 30 分钟（排队 10 分钟未计入）',
    )
    assert.ok(
      !tree.includes('30 分钟'),
      '用时列不应显示 30 分钟（started_at → finished_at 的执行时长）',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('任务详情「处理用时」= 系统接收时间 → bot-done 打标时间（40 分钟）', async () => {
  const { renderer, renderError } = await renderTaskDetail()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const tree = JSON.stringify(renderer.toJSON())
    assert.ok(
      tree.includes('处理用时'),
      '详情页字段名应为「处理用时」（整个处理周期语义）；修复前为「执行用时」',
    )
    assert.ok(
      tree.includes('40 分钟'),
      '处理用时行应显示 40 分钟（created_at 09:50 → finished_at 10:30）',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('TaskDetail 源码的用时计算不依赖 started_at（起点固定 created_at）', () => {
  assert.ok(
    !/fmtDuration\(task\.started_at/.test(taskDetailSrc),
    '详情页用时起点不应再回退 started_at（执行开始时刻），应固定为 created_at（系统接收时间）',
  )
  assert.match(
    taskDetailSrc,
    /fmtDuration\(task\.created_at,\s*task\.finished_at\)/,
    '详情页应用时应为 fmtDuration(task.created_at, task.finished_at)',
  )
})
