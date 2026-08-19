// 复现测试（issue #68，issue #114 适配）：概览页板块排序。
//
// 原需求（issue #68）：「开放 issue 排在顶部，然后到运行中任务，然后到流水线」。
// issue #114 起独立任务板块删除，任务信息整合进「开放 Issue」板块
// running 组的 issue 项内，概览页仅剩两个板块：开放 Issue → CI/CD 流水线。
// 期望（修复后）：开放 Issue → CI/CD 流水线（任务信息随 running 组
// 位于开放 Issue 板块内）。
//
// 断言分两层：
// 1. 源码级：Overview.jsx 中两板块组件（IssueListSection / PipelineSection）
//    的挂载位置顺序（issues < pipelines），且不再有任务板块标记；
// 2. 渲染级：mock 三个数据接口后渲染组件，序列化渲染树文本，
//    断言「开放 Issue」标题位于「CI/CD 流水线」标题之前；覆盖
//    有/无运行中任务、无流水线、无 issue 等边界（顺序依然成立、
//    页面不崩溃）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：三个板块改为独立组件，板块顺序由 Overview.jsx
// 组合顺序（组件挂载顺序）保证；源码级断言改为校验组件挂载顺序。
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

// 板块在源码中的起始位置：流水线 = pipelines-section，
// 开放 issue = issues-section。两板块应是互不重叠的区间。
function sectionPositions(src) {
  const pipes = src.indexOf('<PipelineSection')
  const issues = src.indexOf('<IssueListSection')
  return { pipes, issues }
}

test('源码：两板块标记均存在，独立任务板块已删除（issue #114）', () => {
  const { pipes, issues } = sectionPositions(overview)
  assert.ok(pipes >= 0, '应存在流水线板块（pipelines-section）')
  assert.ok(issues >= 0, '应存在开放 issue 板块（issues-section）')
  assert.ok(!overview.includes('tasks-section'), '不应再有独立任务板块（tasks-section）')
  assert.ok(!overview.includes('className="overview-grid"'),
            '不应再有任务卡片网格（overview-grid）')
})

test('源码：开放 issue 板块位于流水线板块之前（issue #68 需求顺序）', () => {
  const { pipes, issues } = sectionPositions(overview)
  assert.ok(
    issues < pipes,
    `开放 issue 板块（源码偏移 ${issues}）应位于流水线板块（偏移 ${pipes}）之前`,
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

// 三个接口各返回一条数据（正常路径，任务命中 issue → running 组任务块）
async function implAllPopulated(pathname) {
  if (pathname.startsWith('/api/tasks?')) {
    return {
      tasks: [{
        id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
        issue_iid: 9, issue_title: '数据备份失败', status: 'running',
        issue_url: 'https://gitlab.example.com/group/shipyard/-/issues/9',
        engine: '',
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

test('渲染：开放 Issue 板块位于 CI/CD 流水线板块之前（issue #114 起仅两板块）', async () => {
  const r = await renderText(implAllPopulated)
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const issueTitle = r.pos('开放 Issue')
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueTitle >= 0, '应渲染「开放 Issue」板块标题')
    assert.ok(pipeTitle >= 0, '应渲染「CI/CD 流水线」板块标题')
    assert.ok(issueTitle < pipeTitle, '「开放 Issue」板块应位于「CI/CD 流水线」板块之前')
    // issue #114：任务板块删除，任务信息进入开放 issue 列表项内
    assert.ok(!r.text.includes('正在执行的任务'), '不应渲染独立任务板块标题')
    assert.ok(r.text.includes('运行中'), '运行中任务应在开放 issue 板块内展示')
  } finally {
    await r.unmount()
  }
})

test('渲染：无运行中任务时开放 Issue 仍在顶部、流水线在底部', async () => {
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
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueTitle >= 0 && pipeTitle >= 0,
              `两板块标记都应存在（issue=${issueTitle} pipe=${pipeTitle}）`)
    assert.ok(issueTitle < pipeTitle, '「开放 Issue」应位于「CI/CD 流水线」之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：无流水线时开放 Issue 板块正常展示运行中任务（流水线空状态垫底）', async () => {
  const r = await renderText(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return {
        tasks: [{ id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
                  issue_iid: 7, issue_title: 't', status: 'running',
                  issue_url: null, engine: '' }],
        total: 1, stats: { running: 1 },
      }
    }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      return {
        repos: [{ repo_id: 1, repo_name: 'shipyard', priority: 1,
                  issues: [{ iid: 7, title: 't', web_url: 'https://x', updated_at: null }] }],
        errors: [],
      }
    }
    throw new Error('unexpected ' + pathname)
  })
  try {
    assert.equal(r.renderError, null)
    const issueTitle = r.pos('开放 Issue')
    const runningGroup = r.pos('issue-group-title')
    const pipeEmpty = r.pos('暂无流水线')
    assert.ok(issueTitle >= 0 && runningGroup >= 0 && pipeEmpty >= 0,
              '开放 Issue 板块、运行中组与流水线空状态都应存在')
    assert.ok(issueTitle < runningGroup, '「开放 Issue」标题应位于运行中组之前')
    assert.ok(runningGroup < pipeEmpty, '运行中组（开放 issue 板块内）应位于流水线空状态之前')
  } finally {
    await r.unmount()
  }
})

test('渲染：无开放 issue 时板块空状态文案仍在最顶部（流水线之前）', async () => {
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
    const pipeTitle = r.pos('CI/CD 流水线')
    assert.ok(issueEmpty >= 0 && pipeTitle >= 0,
              `两板块标记都应存在（issue=${issueEmpty} pipe=${pipeTitle}）`)
    assert.ok(issueEmpty < pipeTitle, '「暂无开放 issue」应位于「CI/CD 流水线」之前')
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
    assert.ok(issueTitle >= 0 && pipeTitle >= 0, '失败时两板块骨架仍应渲染')
    assert.ok(errMark >= 0, '应显示 API 错误信息')
    assert.ok(issueTitle < pipeTitle, '「开放 Issue」板块仍应位于「CI/CD 流水线」之前')
  } finally {
    await r.unmount()
  }
})
