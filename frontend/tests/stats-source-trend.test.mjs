// 统计页「来源按天趋势」测试（issue #224）：
// 概览页新增来源分布统计卡片（issue #224 验收标准 2），统计页在「来源分布」
// 卡片（issue #361 迁入）下方新增「来源按天趋势」板块——各来源每日任务量
// 走势（近 7/30 天或
// 全部），数据来自同一 dashboard 响应的 by_source_daily（后端按来源×日期
// 聚合：days>0 窗口内逐日零填充、days=0 仅返回有数据日期），随上方时间段
// 选择联动，无需额外接口请求。
//
// 断言：
// 1. Stats.jsx 渲染「来源按天趋势」板块（i18n key + 中文值）；
// 2. 数据来自 dashboard 响应的 by_source_daily（同一接口无新增请求）；
// 3. 有数据时按来源分组渲染迷你折线图（来源名 + SVG）；
// 4. by_source_daily 为空时显示空态文案不报错；
// 5. styles.css 提供趋势板块样式。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const statsSrc = readFileSync(path.join(ROOT, 'src/pages/Stats.jsx'), 'utf8')
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
  return textOf(node.children ?? node.props?.children)
}

// ---- 源码断言 ----

test('源码：统计页渲染「来源按天趋势」板块（issue #224）', () => {
  assert.match(statsSrc, /stats\.sourceTrendTitle/, '标题应经 t() 国际化')
  assert.equal(zhCN['stats.sourceTrendTitle'], '来源按天趋势', '中文标题应为「来源按天趋势」')
  assert.match(statsSrc, /stats\.sourceTrendDesc/, '说明文案应经 t() 国际化')
  assert.match(statsSrc, /by_source_daily/, '趋势数据应来自 dashboard 的 by_source_daily')
  assert.match(statsSrc, /groupSourceDaily/, '应按来源分组 by_source_daily')
  assert.match(statsSrc, /SourceDailyChart/, '应渲染来源趋势迷你折线图组件')
  assert.match(statsSrc, /stats\.sourceTrendEmpty/, '空态文案应经 t() 国际化')
})

test('源码：趋势复用轻量 SVG 折线（无第三方图表库依赖）', () => {
  assert.match(statsSrc, /<svg/, '应使用原生 SVG 折线图')
  assert.match(statsSrc, /completion-trend-line/, '应复用 completion-trend 折线样式')
  assert.doesNotMatch(statsSrc, /from ['"]recharts['"]/, '不应引入 recharts 依赖')
})

test('styles.css 提供来源按天趋势样式', () => {
  assert.match(styles, /\.stats-source-trend-list\s*\{/, '应有趋势列表容器样式')
  assert.match(styles, /\.stats-source-trend-item\s*\{/, '应有趋势条目样式')
  assert.match(styles, /\.stats-source-trend-chart\s*\{/, '应有趋势图样式')
})

// ---- 渲染断言 ----

const TREND = [
  { date: '2026-08-18', source: 'webhook', name: 'webhook', task_count: 3,
    succeeded_count: 2, failed_count: 1, interrupted_count: 0,
    success_rate: 0.6667, avg_duration_seconds: 120 },
  { date: '2026-08-19', source: 'webhook', name: 'webhook', task_count: 0,
    succeeded_count: 0, failed_count: 0, interrupted_count: 0,
    success_rate: null, avg_duration_seconds: null },
  { date: '2026-08-18', source: 'manual', name: '手动', task_count: 1,
    succeeded_count: 1, failed_count: 0, interrupted_count: 0,
    success_rate: 1, avg_duration_seconds: 60 },
  { date: '2026-08-19', source: 'manual', name: '手动', task_count: 2,
    succeeded_count: 0, failed_count: 2, interrupted_count: 0,
    success_rate: 0, avg_duration_seconds: 90 },
]

function makeDashboard(withTrend = true) {
  return {
    overview: { task_count: 6, succeeded_count: 3, failed_count: 3,
                interrupted_count: 0, success_rate: 0.5,
                avg_duration_seconds: 100 },
    by_engine: [], by_repo: [],
    by_source: [
      { key: 'webhook', name: 'webhook', task_count: 3, succeeded_count: 2,
        failed_count: 1, interrupted_count: 0, success_rate: 0.6667,
        avg_duration_seconds: 120 },
      { key: 'manual', name: '手动', task_count: 3, succeeded_count: 1,
        failed_count: 2, interrupted_count: 0, success_rate: 0.3333,
        avg_duration_seconds: 80 },
    ],
    failure_reasons: [],
    failure_categories: [],
    by_source_daily: withTrend ? TREND : [],
  }
}

function mockStatsApi(dashboard) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/stats/dashboard')) return dashboard
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname === '/api/issues/completion-stats') {
      return { completed_count: 0, avg_seconds: null, trend: [] }
    }
    if (pathname.startsWith('/api/usage/stats')) {
      return { summary: { task_count: 0 }, by_engine: [], by_repo: [], by_date: [] }
    }
    return {}
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

test('渲染：有趋势数据时按来源分组展示迷你折线图', async () => {
  mockStatsApi(makeDashboard(true))
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /来源按天趋势/, '应有趋势板块标题')
    assert.match(text, /webhook/, '趋势应有 webhook 来源')
    assert.match(text, /手动/, '趋势应有「手动」来源')
    const svgs = renderer.root.findAllByType('svg')
    // webhook + manual 各一个迷你折线图（另有完成耗时图可能渲染，>=2 即可）
    assert.ok(svgs.length >= 2, `应渲染各来源迷你折线图，实际 ${svgs.length} 个`)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：趋势数据为空时显示空态文案不报错', async () => {
  mockStatsApi(makeDashboard(false))
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /暂无任务数据（当前时间段内没有任务记录）/,
                 '应有趋势空态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
