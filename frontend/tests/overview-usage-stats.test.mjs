// 概览页 Token 用量统计板块测试（issue #235）。
//
// 需求：统计页（概览页板块）按仓库/引擎/时间段聚合——本地 task_usage 表
// （GET /api/usage/stats），无 GitLab 请求压力；展示合计 token 数与估算
// 费用（未配置单价只展示 token 数），按引擎/仓库分组明细；空数据显示
// 「暂无用量数据」空态；过滤器（仓库/引擎/时间范围）变化立即重拉。
//
// 断言：
// 1. Overview 请求 /api/usage/stats 并低频轮询（60 秒）；
// 2. 板块含过滤器（仓库/引擎/时间范围），过滤器变化带查询参数重拉；
// 3. 有数据时渲染合计 tokens + 任务数 + 估算费用 + 按引擎/仓库分组表格；
// 4. 无数据时渲染空状态（页面不崩溃）；
// 5. 接口失败显示错误提示、不崩溃；
// 6. styles.css 提供板块与分组表格样式类。
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
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

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
  // toJSON 宿主树用 node.children；ReactTestInstance 用 props.children
  return textOf(node.children ?? node.props?.children)
}

// ---- 源码断言 ----

test('源码：请求用量统计接口并低频轮询（60 秒）', () => {
  assert.match(overview, /\/api\/usage\/stats/,
               '应请求 GET /api/usage/stats')
  assert.match(overview, /USAGE_STATS_POLL_MS\s*=\s*60000/,
               '用量统计轮询间隔应为 60 秒')
  assert.match(overview, /setInterval\(loadUsageStats/,
               '应独立定时轮询用量统计接口')
})

test('源码：板块含仓库/引擎/时间范围过滤器', () => {
  assert.match(overview, /Token 用量统计/, '板块应有标题')
  assert.match(overview, /全部仓库/, '应有仓库过滤器')
  assert.match(overview, /全部引擎/, '应有引擎过滤器')
  assert.match(overview, /最近 7 天/, '应有时间范围过滤器（7 天）')
  assert.match(overview, /usage-stats-section/, '应使用 usage-stats-section 容器')
})

test('styles.css 提供统计板块样式', () => {
  assert.match(styles, /\.usage-stats-section\s*\{/, '应有板块容器样式')
  assert.match(styles, /\.usage-stats-value\s*\{/, '应有合计数字样式')
  assert.match(styles, /\.usage-stats-grid\s*\{/, '应有分组表格网格样式')
})

// ---- 渲染断言 ----

function mockOverviewApi(stats = null, error = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/usage/stats')) {
      if (error) throw new Error(error)
      return stats
    }
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'demo', issues: [] }], errors: [], total: 0 }
    }
    if (pathname === '/api/pipelines/overview') {
      return { pipelines: [], errors: [] }
    }
    if (pathname === '/api/inspirations/overview') {
      return { repos: [], errors: [] }
    }
    if (pathname === '/api/tasks') {
      return { tasks: [], total: 0, stats: { queued: 0, running: 0, retrying: 0 } }
    }
    if (pathname === '/api/settings/deepseek-balance') {
      return { configured: false, balance: null, error: null }
    }
    if (pathname === '/api/settings') {
      return { gitlab: { owner_token_masked: '' } }
    }
    if (pathname === '/api/issues/completion-stats') {
      return { completed_count: 0, avg_seconds: null, trend: [] }
    }
    return { repos: [], errors: [] }
  })
}

async function renderOverview() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 50))
    } catch (e) { renderError = e }
  })
  return { renderer, renderError }
}

const STATS = {
  summary: { task_count: 2, prompt_tokens: 3000, completion_tokens: 1500,
             total_tokens: 4500, estimated_cost: 0.03, costed_count: 2 },
  currency: 'USD',
  by_repo: [{ repo_id: 1, repo_name: 'demo', task_count: 2,
              prompt_tokens: 3000, completion_tokens: 1500,
              total_tokens: 4500, estimated_cost: 0.03 }],
  by_engine: [{ engine: 'claude', task_count: 2, prompt_tokens: 3000,
                completion_tokens: 1500, total_tokens: 4500,
                estimated_cost: 0.03 }],
  by_date: [{ date: '2026-08-18', task_count: 2, total_tokens: 4500,
              estimated_cost: 0.03 }],
}

test('渲染：有数据时展示合计 tokens、任务数与分组表格', async () => {
  mockOverviewApi(STATS)
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /Token 用量统计/, '板块应有标题')
    assert.match(text, /4,500 tokens/, '应展示合计 total tokens')
    assert.match(text, /2 个任务/, '应展示任务数')
    assert.match(text, /USD 0\.0300/, '应展示估算费用')
    assert.match(text, /claude/, '按引擎表格应有 claude 行')
    assert.match(text, /demo/, '按仓库表格应有 demo 行')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：无数据时显示空状态不崩溃', async () => {
  mockOverviewApi({
    summary: { task_count: 0, prompt_tokens: 0, completion_tokens: 0,
               total_tokens: 0, estimated_cost: 0, costed_count: 0 },
    currency: 'USD', by_repo: [], by_engine: [], by_date: [],
  })
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /暂无用量数据/, '应有空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：接口失败显示错误提示不崩溃', async () => {
  mockOverviewApi(null, 'boom')
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /boom/, '应显示错误提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
