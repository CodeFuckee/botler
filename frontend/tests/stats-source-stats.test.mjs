// 统计页「来源分布」统计卡片测试（issue #224 + #361）：
// 概览页「来源分布」统计卡片整体迁入统计页（issue #361）——按任务来源
// （webhook/手动/对账）展示任务量、成功率与平均耗时（近 30 天）。数据来自
// 本地任务表聚合接口 GET /api/stats/dashboard?days=30（后端 10s TTL 缓存，
// 与任务列表同表同口径，无 GitLab 请求压力），组件自持 60 秒低频轮询
// （usePolling，页面隐藏自动暂停，后台 0 请求）。
// 统计页原有「来源分布」表格展示同源同口径数据（来源/任务量/成功率/平均
// 耗时），迁入后由卡片替换表格，避免同页重复展示同一份数据。
//
// 断言：
// 1. Stats.jsx 引入并渲染 SourceStatsSection；Overview.jsx 不再渲染；
// 2. 组件请求 /api/stats/dashboard?days=30 并 60 秒轮询；
// 3. 有数据时渲染来源卡片（来源名/任务量/成功率/平均耗时）；
// 4. 无数据时渲染空态不报错；
// 5. i18n key 与中文文案；6. styles.css 提供卡片样式。
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
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const sectionSrc = readFileSync(path.join(ROOT, 'src/components/stats/SourceStatsSection.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: SourceStatsSection } =
  await vite.ssrLoadModule('/src/components/stats/SourceStatsSection.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.children ?? node.props?.children)
}

// ---- 源码断言 ----

test('源码：Stats 引入并渲染来源分布卡片（issue #361），Overview 不再渲染', () => {
  assert.match(statsSrc, /SourceStatsSection/, '统计页应引入来源分布组件')
  assert.match(statsSrc, /<SourceStatsSection \/>/, '统计页应渲染来源分布组件')
  assert.match(statsSrc, /issue #361/, '应有 issue #361 注释说明')
  assert.doesNotMatch(overviewSrc, /SourceStatsSection/, '概览页不应再渲染来源分布组件')
})

test('源码：组件请求统计看板接口（近 30 天）并事件驱动刷新（issue #478）', () => {
  assert.match(sectionSrc, /\/api\/stats\/dashboard\?days=\$\{SOURCE_STATS_DAYS\}/,
               '应请求 /api/stats/dashboard 并带 days 窗口参数')
  assert.match(sectionSrc, /SOURCE_STATS_DAYS\s*=\s*30/, '组件默认窗口应为近 30 天')
  assert.match(sectionSrc, /useGlobalEvents\(/,
               '应订阅全局事件流（issue #478 替代 60 秒轮询）')
  assert.match(sectionSrc, /ev\.type === 'task'\).*load\(\)/s,
               'task 事件（任务状态变化）驱动刷新来源分布')
  assert.match(sectionSrc, /silent: true/, '接口应 silent（失败不弹 toast）')
})

test('源码：文案经 i18n 国际化且中文文案保留', () => {
  assert.match(sectionSrc, /tr\('stats\.sourceStatsTitle'\)/, '标题应经 t() 国际化')
  assert.equal(zhCN['stats.sourceStatsTitle'], '来源分布', '中文标题应为「来源分布」')
  assert.match(sectionSrc, /tr\('stats\.sourceStatsDesc'/, '说明应经 t() 国际化')
  assert.match(sectionSrc, /tr\('stats\.sourceTaskCount'/, '任务量文案应经 t() 国际化')
  assert.equal(zhCN['stats.sourceTaskCount'], '{n} 个任务', '中文任务量文案应保留')
  assert.match(sectionSrc, /tr\('stats\.sourceSuccessRate'\)/, '成功率文案应经 t() 国际化')
  assert.equal(zhCN['stats.sourceSuccessRate'], '成功率', '中文成功率文案应为「成功率」')
  assert.match(sectionSrc, /tr\('stats\.sourceAvgDuration'/, '平均耗时文案应经 t() 国际化')
  assert.match(sectionSrc, /tr\('stats\.sourceStatsEmpty'\)/, '空态文案应经 t() 国际化')
})

test('styles.css 提供来源分布卡片样式', () => {
  assert.match(styles, /\.stats-source-section\s*\{/, '应有板块容器样式')
  assert.match(styles, /\.stats-source-cards\s*\{/, '应有卡片网格样式')
  assert.match(styles, /\.stats-source-card\s*\{/, '应有卡片样式')
  assert.match(styles, /\.stats-source-count\s*\{/, '应有任务量数字样式')
  assert.match(styles, /\.stats-source-rate\s*\{/, '应有成功率样式')
})

// ---- 渲染断言 ----

const DASHBOARD = {
  overview: { task_count: 5, succeeded_count: 3, failed_count: 2,
              interrupted_count: 0, success_rate: 0.6,
              avg_duration_seconds: 150 },
  by_source: [
    { key: 'webhook', name: 'webhook', task_count: 3, succeeded_count: 2,
      failed_count: 1, interrupted_count: 0, success_rate: 0.6667,
      avg_duration_seconds: 120 },
    { key: 'manual', name: '手动', task_count: 2, succeeded_count: 1,
      failed_count: 1, interrupted_count: 0, success_rate: 0.5,
      avg_duration_seconds: 195 },
  ],
  by_engine: [], by_repo: [], failure_reasons: [], by_source_daily: [],
}

function mockApi(dashboard) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/stats/dashboard')) return dashboard
    return {}
  })
}

async function renderSection() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(SourceStatsSection))
      await new Promise((resolve) => setTimeout(resolve, 50))
    } catch (e) { renderError = e }
  })
  return { renderer, renderError }
}

test('渲染：有数据时展示来源卡片（来源/任务量/成功率/平均耗时）', async () => {
  mockApi(DASHBOARD)
  const { renderer, renderError } = await renderSection()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /来源分布/, '应有板块标题')
    assert.match(text, /webhook/, '应有 webhook 来源卡片')
    assert.match(text, /手动/, '应有「手动」来源卡片')
    assert.match(text, /3 个任务/, 'webhook 应展示任务量')
    assert.match(text, /67%/, 'webhook 成功率 66.67% → 67%')
    assert.match(text, /2 分钟/, 'webhook 平均耗时 120 秒 → 2 分钟')
    assert.match(text, /50%/, '手动成功率 50%')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：无数据时显示空态不报错', async () => {
  mockApi({ overview: { task_count: 0 }, by_source: [] })
  const { renderer, renderError } = await renderSection()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /暂无任务数据（近 30 天没有任务记录）/, '应有空态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：接口失败显示错误提示不崩溃', async () => {
  mock.method(api, 'get', async () => { throw new Error('source boom') })
  const { renderer, renderError } = await renderSection()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /source boom/, '应显示错误提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
