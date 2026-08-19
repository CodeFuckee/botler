// 统计页 Token 用量统计板块测试（issue #235 + #322）。
// issue #322：该板块自概览页迁入统计页。
//
// 需求：统计页按仓库/引擎/时间段聚合——本地 task_usage 表
// （GET /api/usage/stats），无 GitLab 请求压力；展示合计 token 数与估算
// 费用（未配置单价只展示 token 数），按引擎/仓库分组明细；空数据显示
// 「暂无用量数据」空态；过滤器（仓库/引擎/时间范围）变化立即重拉。
//
// 断言：
// 1. Stats 请求 /api/usage/stats 并低频轮询（60 秒）；
// 2. 板块含过滤器（仓库/引擎/时间范围），过滤器变化带查询参数重拉；
// 3. 有数据时渲染合计 tokens + 任务数 + 估算费用 + 按引擎/仓库分组表格；
// 4. 无数据时渲染空状态（页面不崩溃）；
// 5. 接口失败显示错误提示、不崩溃；
// 6. styles.css 提供板块与分组表格样式类；
// 7. 概览页（Overview.jsx）不再包含该板块（issue #322 迁移完成）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const statsSrc = readFileSync(path.join(ROOT, 'src/pages/Stats.jsx'), 'utf8')
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Stats } = await vite.ssrLoadModule('/src/pages/Stats.jsx')
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
  assert.match(statsSrc, /\/api\/usage\/stats/,
               '应请求 GET /api/usage/stats')
  assert.match(statsSrc, /USAGE_STATS_POLL_MS\s*=\s*60000/,
               '用量统计轮询间隔应为 60 秒')
  assert.match(statsSrc, /setInterval\(loadUsageStats/,
               '应独立定时轮询用量统计接口')
})

test('源码：板块含仓库/引擎/时间范围过滤器', () => {
  assert.match(statsSrc, /tr\('stats\.usageTitle'\)/, '板块标题应经 t() 国际化')
  assert.equal(zhCN['stats.usageTitle'], 'Token 用量统计', '中文标题应保留')
  assert.match(statsSrc, /tr\('stats\.allRepos'\)/, '「全部仓库」应经 t() 国际化')
  assert.equal(zhCN['stats.allRepos'], '全部仓库', '中文文案应保留')
  assert.match(statsSrc, /tr\('stats\.allEngines'\)/, '「全部引擎」应经 t() 国际化')
  assert.equal(zhCN['stats.allEngines'], '全部引擎', '中文文案应保留')
  assert.match(statsSrc, /tr\('stats\.last7Days'\)/, '「最近 7 天」应经 t() 国际化')
  assert.equal(zhCN['stats.last7Days'], '最近 7 天', '中文文案应保留')
  assert.match(statsSrc, /usage-stats-section/, '应使用 usage-stats-section 容器')
})

test('源码：概览页不再包含该板块（issue #322 迁移完成）', () => {
  assert.ok(!overviewSrc.includes('usage-stats-section'),
            '概览页应移除 usage-stats-section 板块')
  assert.ok(!overviewSrc.includes('loadUsageStats'),
            '概览页应移除用量统计轮询逻辑')
  assert.ok(!overviewSrc.includes('/api/usage/stats'),
            '概览页应不再请求用量统计接口')
})

test('styles.css 提供统计板块样式', () => {
  assert.match(styles, /\.usage-stats-section\s*\{/, '应有板块容器样式')
  assert.match(styles, /\.usage-stats-value\s*\{/, '应有合计数字样式')
  assert.match(styles, /\.usage-stats-grid\s*\{/, '应有分组表格网格样式')
})

// ---- 渲染断言 ----

const DASHBOARD = {
  overview: { task_count: 1, succeeded_count: 1, failed_count: 0,
              interrupted_count: 0, success_rate: 1,
              avg_duration_seconds: 100 },
  by_engine: [], by_repo: [], by_source: [], failure_reasons: [],
  failure_categories: [],
}

function mockStatsApi(stats = null, error = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/usage/stats')) {
      if (error) throw new Error(error)
      return stats
    }
    if (pathname.startsWith('/api/stats/dashboard')) return DASHBOARD
    if (pathname === '/api/repos') {
      return { repos: [{ id: 1, name: 'demo' }, { id: 2, name: 'other' }] }
    }
    if (pathname === '/api/issues/completion-stats') {
      return { completed_count: 0, avg_seconds: null, trend: [] }
    }
    return { repos: [] }
  })
}

async function renderStats() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Stats))
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
  mockStatsApi(STATS)
  const { renderer, renderError } = await renderStats()
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

test('渲染：过滤器切换触发带参数重拉（仓库过滤）', async () => {
  const calls = []
  mock.method(api, 'get', async (pathname) => {
    calls.push(pathname)
    if (pathname.startsWith('/api/usage/stats')) return STATS
    if (pathname.startsWith('/api/stats/dashboard')) return DASHBOARD
    if (pathname === '/api/repos') {
      return { repos: [{ id: 1, name: 'demo' }, { id: 2, name: 'other' }] }
    }
    if (pathname === '/api/issues/completion-stats') {
      return { completed_count: 0, avg_seconds: null, trend: [] }
    }
    return { repos: [] }
  })
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    // usage-stats-filter 第一个为仓库过滤器（value='' → 切换为 1）
    const filters = renderer.root.findAll((n) => n.type === 'select'
      && String(n.props.className || '').includes('usage-stats-filter'))
    assert.ok(filters.length >= 3, '应有仓库/引擎/时间范围三个过滤器')
    await TestRenderer.act(() => filters[0].props.onChange({ target: { value: '1' } }))
    assert.ok(calls.some((p) => p.startsWith('/api/usage/stats?') && p.includes('repo_id=1')),
              `切换仓库后应携带 repo_id=1 重拉，实际调用：${calls.filter((p) => p.startsWith('/api/usage/stats')).join(', ')}`)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：无数据时显示空状态不崩溃', async () => {
  mockStatsApi({
    summary: { task_count: 0, prompt_tokens: 0, completion_tokens: 0,
               total_tokens: 0, estimated_cost: 0, costed_count: 0 },
    currency: 'USD', by_repo: [], by_engine: [], by_date: [],
  })
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /暂无用量数据/, '应有空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：接口失败显示错误提示不崩溃', async () => {
  mockStatsApi(null, 'boom')
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /boom/, '应显示错误提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
