// 复现测试（issue #68）：概览页板块排序不符合需求。
//
// 需求：「开放 issue 排在顶部，然后到运行中任务，然后到流水线」。
// 当前（修复前）：运行中任务 → CI/CD 流水线 → 开放 Issue，issue 板块在最底部。
// 期望（修复后）：开放 Issue → 运行中任务 → CI/CD 流水线。
//
// 断言分两层：
// 1. 源码级：Overview.jsx 中三个板块的 JSX 起始位置顺序
//    （issues-section < 任务区 < pipelines-section）；
// 2. 渲染级：mock 三个数据接口后渲染组件，序列化渲染树文本，
//    断言「开放 Issue」标题位于任务卡（或任务空状态文案）之前、
//    任务区位于「CI/CD 流水线」标题之前；覆盖空板块边界
//    （无任务 / 无流水线 / 无 issue 时顺序依然成立、页面不崩溃）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 源码级断言 ----

// 板块在源码中的起始位置：
// 任务区 = 活跃任务卡片网格（overview-grid），流水线 = pipelines-section，
// 开放 issue = issues-section。三个板块应是三个互不重叠的区间。
function sectionPositions(src) {
  const task = src.indexOf('className="overview-grid"')
  const pipes = src.indexOf('className="pipelines-section"')
  const issues = src.indexOf('className="issues-section"')
  return { task, pipes, issues }
}

test('源码：三个板块标记均存在', () => {
  const { task, pipes, issues } = sectionPositions(overview)
  assert.ok(task >= 0, '应存在任务卡片网格（overview-grid）')
  assert.ok(pipes >= 0, '应存在流水线板块（pipelines-section）')
  assert.ok(issues >= 0, '应存在开放 issue 板块（issues-section）')
})

test('源码：开放 issue 板块位于任务区与流水线板块之前（issue #68 需求顺序）', () => {
  const { task, pipes, issues } = sectionPositions(overview)
  assert.ok(
    issues < task,
    `开放 issue 板块（源码偏移 ${issues}）应位于任务区（偏移 ${task}）之前`,
  )
  assert.ok(
    task < pipes,
    `任务区（源码偏移 ${task}）应位于流水线板块（偏移 ${pipes}）之前`,
  )
})

// ---- 渲染级断言 ----

// 渲染整个组件树为扁平文本（深度优先，与视觉自上而下顺序一致），
// 返回文本与辅助函数：pos(key) = key 在渲染文本中的位置（找不到返回 -1）
async function renderText(impl, waitMs = 30) {
  mock.method(api, 'get', impl)
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 等待首轮三个数据接口（任务/流水线/issue）的 promise flush
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    } catch (e) {
      renderError = e
    }
  })
  const text = renderer ? JSON.stringify(renderer.toJSON()) : ''
  return {
    text,
    renderError,
    pos: (key) => text.indexOf(key),
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// 三个接口各返回一条数据（正常路径）
async function implAllPopulated(pathname) {
  if (pathname.startsWith('/api/tasks?')) {
    return {
      tasks: [{
        id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
        issue_iid: 7, issue_title: '修复登录问题', status: 'running',
        issue_url: 'https://gitlab.example.com/group/shipyard/-/issues/7',
      }],
      total: 1, stats: { running: 1 },
    }
  }
  if (pathname === '/api/pipelines/overview') {
    return {
      pipelines: [{
        repo_id: 1, repo_name: 'shipyard', enabled: true,
        pipeline: { status: 'running', ref: 'main', sha: 'abc12345', web_url: 'https://x/p' },
        stages: [], commit_time: '2026-08-14T12:00:00+08:00',
      }],
      errors: [],
    }
  }
  if (pathname === '/api/issues/overview') {
    return {
      repos: [{
        repo_id: 1, repo_name: 'shipyard', priority: 1,
        issues: [{ iid: 9, title: '数据备份失败', web_url: 'https://x/i/9', updated_at: '2026-08-14T12:00:00+08:00' }],
      }],
      errors: [],
    }
  }
  throw new Error('unexpected ' + pathname)
}

test('渲染：三板块均有时自上而下为 开放 Issue → 运行中任务 → CI/CD 流水线', async () => {
  const r = await renderText(implAllPopulated)
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const issueTitle = r.pos('开放 Issue')
    const taskMark = r.pos('shipyard') // 任务卡中的仓库名
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueTitle >= 0, '应渲染「开放 Issue」板块标题')
    assert.ok(taskMark >= 0, '应渲染任务卡（仓库名 shipyard）')
    assert.ok(pipeTitle >= 0, '应渲染「CI/CD 流水线」板块标题')
    assert.ok(issueTitle < taskMark, '「开放 Issue」板块应位于任务卡之前')
    assert.ok(taskMark < pipeTitle, '任务卡应位于「CI/CD 流水线」板块之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：无运行中任务时，开放 Issue 仍在顶部、任务空状态居中、流水线在底部', async () => {
  const r = await renderText(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') {
      return { pipelines: [{ repo_id: 1, repo_name: 'shipyard', enabled: true, pipeline: null }], errors: [] }
    }
    if (pathname === '/api/issues/overview') {
      return {
        repos: [{ repo_id: 1, repo_name: 'shipyard', priority: 1, issues: [{ iid: 9, title: 'x', web_url: 'https://x', updated_at: null }] }],
        errors: [],
      }
    }
    throw new Error('unexpected ' + pathname)
  })
  try {
    assert.equal(r.renderError, null)
    const issueTitle = r.pos('开放 Issue')
    const taskEmpty = r.pos('当前没有正在执行的任务')
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueTitle >= 0 && taskEmpty >= 0 && pipeTitle >= 0,
              `三个板块标记都应存在（issue=${issueTitle} task=${taskEmpty} pipe=${pipeTitle}）`)
    assert.ok(issueTitle < taskEmpty, '「开放 Issue」应位于任务空状态文案之前')
    assert.ok(taskEmpty < pipeTitle, '任务空状态文案应位于「CI/CD 流水线」之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：无流水线时任务区仍在流水线空状态文案之前', async () => {
  const r = await renderText(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return {
        tasks: [{ id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
                  issue_iid: 7, issue_title: 't', status: 'running', issue_url: null }],
        total: 1, stats: { running: 1 },
      }
    }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    throw new Error('unexpected ' + pathname)
  })
  try {
    assert.equal(r.renderError, null)
    const taskMark = r.pos('shipyard')
    const pipeEmpty = r.pos('暂无流水线')
    assert.ok(taskMark >= 0 && pipeEmpty >= 0, '任务卡与流水线空状态文案都应存在')
    assert.ok(taskMark < pipeEmpty, '任务卡应位于「暂无流水线」之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：无开放 issue 时板块空状态文案仍在最顶部（任务与流水线之前）', async () => {
  const r = await renderText(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return {
        tasks: [{ id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
                  issue_iid: 7, issue_title: 't', status: 'running', issue_url: null }],
        total: 1, stats: { running: 1 },
      }
    }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    throw new Error('unexpected ' + pathname)
  })
  try {
    assert.equal(r.renderError, null)
    const issueEmpty = r.pos('暂无开放 issue')
    const taskMark = r.pos('shipyard')
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueEmpty >= 0 && taskMark >= 0 && pipeTitle >= 0,
              `三个板块标记都应存在（issue=${issueEmpty} task=${taskMark} pipe=${pipeTitle}）`)
    assert.ok(issueEmpty < taskMark, '「暂无开放 issue」应位于任务卡之前（板块在顶部）')
    assert.ok(taskMark < pipeTitle, '任务卡应位于「CI/CD 流水线」之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：三个数据接口全部失败时页面不崩溃、板块标题仍渲染', async () => {
  const r = await renderText(async () => {
    throw new Error('网络错误')
  })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const issueTitle = r.pos('开放 Issue')
    const pipeTitle = r.pos('CI/CD 流水线')
    const errMark = r.pos('网络错误')
    assert.ok(issueTitle >= 0 && pipeTitle >= 0, '失败时三个板块骨架仍应渲染')
    assert.ok(errMark >= 0, '应显示 API 错误信息')
    assert.ok(issueTitle < pipeTitle, '「开放 Issue」板块仍应位于「CI/CD 流水线」之前')
  } finally {
    await r.unmount()
  }
})
