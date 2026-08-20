// 统计看板页测试（issue #264 + #322）。
//
// 需求：新增独立「统计」页（导航入口），数据来自本地任务表聚合接口
// GET /api/stats/dashboard（后端 10s TTL 缓存，与任务列表同表同口径）：
// 总览卡片（任务总数/成功率/平均耗时/失败数）+ 按引擎/仓库/来源分组对比
// （纯 CSS 条形图）+ 失败原因 Top 分布；时间段选择（最近 7 天/30 天/全部）
// 持久化到 localStorage；无任务数据时显示空态不报错。
//
// 断言：
// 1. Stats 请求 /api/stats/dashboard 并按 days 参数传时间段；
// 2. 页面含时间段过滤器（7/30/全部）并持久化 localStorage；
// 3. 有数据时渲染总览卡片、引擎/仓库/来源分组与失败原因列表；
// 4. 无数据时渲染空状态（页面不崩溃）；
// 5. 接口失败显示错误提示、不崩溃；
// 6. App.jsx 提供导航入口与路由；styles.css 提供统计页样式类。
// issue #322：概览页「Issue 完成耗时」与「Token 用量统计」板块迁入——
// 请求 /api/issues/completion-stats 与 /api/usage/stats 并 60 秒低频轮询；
// dashboard 无数据（空态）时两个板块仍各自渲染（独立空态不互相干扰）。
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
const appSrc = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const lazyPages = readFileSync(path.join(ROOT, 'src/pages/lazy.jsx'), 'utf8')
const iconSrc = readFileSync(path.join(ROOT, 'src/components/Icon.jsx'), 'utf8')
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

test('源码：请求统计看板接口并按 days 传时间段', () => {
  assert.match(statsSrc, /\/api\/stats\/dashboard/, '应请求 GET /api/stats/dashboard')
  assert.match(statsSrc, /days=/, '应携带 days 查询参数（0=全部）')
})

test('源码：时间段选择（7 天/30 天/全部）并持久化 localStorage', () => {
  assert.match(statsSrc, /最近 7 天/, '应有 7 天选项')
  assert.match(statsSrc, /最近 30 天/, '应有 30 天选项')
  assert.match(statsSrc, /全部/, '应有全部选项')
  assert.match(statsSrc, /localStorage/, '应使用 localStorage')
  assert.match(statsSrc, /setItem/, '应保存时间段偏好')
  assert.match(statsSrc, /getItem/, '应恢复时间段偏好')
})

test('源码：页面包含统计看板各板块', () => {
  assert.match(statsSrc, /任务总数/, '应有任务总数卡片')
  assert.match(statsSrc, /成功率/, '应有成功率卡片')
  assert.match(statsSrc, /平均耗时/, '应有平均耗时卡片')
  assert.match(statsSrc, /失败数/, '应有失败数卡片')
  assert.match(statsSrc, /引擎对比/, '应有引擎对比板块')
  assert.match(statsSrc, /仓库排行/, '应有仓库排行板块')
  assert.match(statsSrc, /SourceStatsSection/, '应有来源分布板块（迁入组件，issue #361）')
  assert.match(statsSrc, /失败原因 Top 分布/, '应有失败原因 Top 板块')
})

test('源码：迁入「Issue 完成耗时」与「Token 用量统计」板块（issue #322）', () => {
  assert.match(statsSrc, /completion-stats-section/, '应有完成耗时板块容器')
  assert.match(statsSrc, /usage-stats-section/, '应用量统计板块容器')
  assert.match(statsSrc, /\/api\/issues\/completion-stats/,
               '应请求完成耗时接口')
  assert.match(statsSrc, /COMPLETION_STATS_POLL_MS\s*=\s*60000/,
               '完成耗时轮询间隔应为 60 秒')
  assert.match(statsSrc, /\/api\/usage\/stats/,
               '应请求用量统计接口')
  assert.match(statsSrc, /USAGE_STATS_POLL_MS\s*=\s*60000/,
               '用量统计轮询间隔应为 60 秒')
  assert.match(statsSrc, /stats\.completionTitle/, '完成耗时标题应经 t() 国际化')
  assert.equal(zhCN['stats.completionTitle'], 'Issue 完成耗时', '中文标题应为「Issue 完成耗时」')
  assert.match(statsSrc, /stats\.usageTitle/, '用量标题应经 t() 国际化')
  assert.equal(zhCN['stats.usageTitle'], 'Token 用量统计', '中文标题应为「Token 用量统计」')
  // dashboard 空态（empty）分支外仍渲染两个迁入板块——独立空态互不干扰
  assert.match(statsSrc, /empty \? \(/, '应保留 dashboard 空态分支')
})

test('源码：纯 CSS 条形图（不引入 recharts 等重依赖）', () => {
  assert.match(statsSrc, /stats-bar/, '应使用 stats-bar CSS 条形图')
  assert.doesNotMatch(statsSrc, /from ['"]recharts['"]/, '不应引入 recharts 依赖')
})

test('App.jsx 提供统计页导航入口与路由', () => {
  assert.match(appSrc, /from '\.\/pages\/lazy\.jsx'/, 'App 页面应统一经 lazy.jsx 按路由懒加载')
  assert.match(lazyPages, /export const Stats = lazy\(\(\) => import\('\.\/Stats\.jsx'\)\)/, 'lazy.jsx 应包装 Stats 页面')
  assert.match(appSrc, /to="\/stats"/, '应有 /stats 导航入口')
  assert.match(appSrc, /t\('nav\.stats'\)/, '导航文案应经 t() 国际化')
  assert.equal(zhCN['nav.stats'], '统计', '中文文案应为「统计」')
  assert.match(appSrc, /path="\/stats" element=\{<Stats \/>\}/, '应注册 /stats 路由')
})

test('Icon.jsx 注册 chart 图标（空态用）', () => {
  assert.match(iconSrc, /BarChart3/, '应导入 BarChart3 图标')
  assert.match(iconSrc, /chart: BarChart3/, '应注册 chart 语义图标')
})

test('styles.css 提供统计页样式', () => {
  assert.match(styles, /\.stats-page\s*\{/, '应有页面容器样式')
  assert.match(styles, /\.stats-card\s*\{/, '应有卡片样式')
  assert.match(styles, /\.stats-bar-track\s*\{/, '应有条形图轨道样式')
  assert.match(styles, /\.stats-bar\s*\{/, '应有条形图样式')
  assert.match(styles, /\.stats-reasons\s*\{/, '应有失败原因列表样式')
})

// ---- 渲染断言 ----

const DASHBOARD = {
  overview: { task_count: 4, succeeded_count: 3, failed_count: 1,
              interrupted_count: 0, success_rate: 0.75,
              avg_duration_seconds: 127.5 },
  by_engine: [
    { key: 'claude', name: 'claude', task_count: 3, succeeded_count: 3,
      failed_count: 0, interrupted_count: 0, success_rate: 1,
      avg_duration_seconds: 150 },
    { key: 'hermes', name: 'hermes', task_count: 1, succeeded_count: 0,
      failed_count: 1, interrupted_count: 0, success_rate: 0,
      avg_duration_seconds: 60 },
  ],
  by_repo: [
    { key: 1, name: 'repo-a', task_count: 3, succeeded_count: 3,
      failed_count: 0, interrupted_count: 0, success_rate: 1,
      avg_duration_seconds: 150 },
    { key: 2, name: 'repo-b', task_count: 1, succeeded_count: 0,
      failed_count: 1, interrupted_count: 0, success_rate: 0,
      avg_duration_seconds: 60 },
  ],
  by_source: [
    { key: 'webhook', name: 'webhook', task_count: 2, succeeded_count: 2,
      failed_count: 0, interrupted_count: 0, success_rate: 1,
      avg_duration_seconds: 120 },
    { key: 'manual', name: '手动', task_count: 2, succeeded_count: 1,
      failed_count: 1, interrupted_count: 0, success_rate: 0.5,
      avg_duration_seconds: 135 },
  ],
  failure_reasons: [
    { reason: '网络超时', count: 1 },
  ],
}

function mockStatsApi(dashboard = null, error = null) {
  const calls = []
  mock.method(api, 'get', async (pathname) => {
    calls.push(pathname)
    if (pathname.startsWith('/api/stats/dashboard')) {
      if (error) throw new Error(error)
      return dashboard
    }
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname === '/api/issues/completion-stats') {
      return { completed_count: 0, avg_seconds: null, trend: [] }
    }
    if (pathname.startsWith('/api/usage/stats')) {
      return { summary: { task_count: 0 }, by_engine: [], by_repo: [], by_date: [] }
    }
    return {}
  })
  return calls
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

test('渲染：默认请求最近 7 天（days=7）', async () => {
  const calls = mockStatsApi(DASHBOARD)
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    assert.ok(calls.some((p) => p.includes('/api/stats/dashboard?days=7')),
              `应请求 days=7，实际调用：${calls.join(', ')}`)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：有数据时展示总览卡片与各分组', async () => {
  mockStatsApi(DASHBOARD)
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /统计看板/, '应有页面标题')
    assert.match(text, /4/, '应展示任务总数')
    assert.match(text, /75%/, '应展示成功率 75%')
    assert.match(text, /2 分钟/, '应展示平均耗时（127.5 秒 → 2 分钟）')
    assert.match(text, /claude/, '引擎对比应有 claude 行')
    assert.match(text, /hermes/, '引擎对比应有 hermes 行')
    assert.match(text, /repo-a/, '仓库排行应有 repo-a')
    assert.match(text, /手动/, '来源分布应有「手动」')
    assert.match(text, /网络超时/, '失败原因应有「网络超时」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：无数据时显示空状态不崩溃', async () => {
  mockStatsApi({
    overview: { task_count: 0, succeeded_count: 0, failed_count: 0,
                interrupted_count: 0, success_rate: null,
                avg_duration_seconds: null },
    by_engine: [], by_repo: [], by_source: [], failure_reasons: [],
  })
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /暂无任务数据/, '应有空状态文案')
    // issue #322：dashboard 空态时迁入板块仍渲染各自空态（互不干扰）
    assert.match(text, /Issue 完成耗时/, '空态下完成耗时板块标题仍应渲染')
    assert.match(text, /暂无已完成 issue/, '空态下完成耗时板块应显示独立空态')
    assert.match(text, /Token 用量统计/, '空态下用量统计板块标题仍应渲染')
    assert.match(text, /暂无用量数据/, '空态下用量统计板块应显示独立空态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：接口失败显示错误提示不崩溃', async () => {
  mockStatsApi(null, 'stats boom')
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = textOf(renderer.root)
    assert.match(text, /stats boom/, '应显示错误提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：切换时间段触发重拉（days 参数变化）', async () => {
  const calls = mockStatsApi(DASHBOARD)
  const { renderer, renderError } = await renderStats()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    // 找到 dashboard 时间段 select（stats-range）并模拟切换为 30 天
    const select = renderer.root.find((n) => n.type === 'select'
      && String(n.props.className || '').includes('stats-range'))
    await TestRenderer.act(() => select.props.onChange({ target: { value: '30' } }))
    assert.ok(calls.some((p) => p.includes('/api/stats/dashboard?days=30')),
              `切换后应请求 days=30，实际调用：${calls.join(', ')}`)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
