// 统计页 Issue 完成耗时测试（issue #180 + #288 + #322）。
// issue #322：该板块自概览页迁入统计页——显示平均每个 issue 完成
// 所需的时间（成功任务的处理用时：系统接收时间 → bot-done 打标时间，
// 与任务详情「处理用时」issue #49 语义一致）以及该时间的逐日平均走势图。
// 数据来自本地 tasks 表成功终态任务（GET /api/issues/completion-stats），
// 无 GitLab 请求压力，低频轮询（60 秒）。
// issue #288：板块内新增「每个开启仓库的平均耗时与走势」拆分——接口
// 返回 repos 数组，前端按仓库渲染平均耗时 + 紧凑迷你走势图。
//
// 断言：
// 1. Stats 请求 /api/issues/completion-stats 并低频轮询（60 秒）；
// 2. 板块位于统计页 dashboard 各板块之后（页面最下方）；
// 3. 有数据时渲染平均完成耗时（fmtSeconds 人类可读）+ 完成数量 +
//    走势图（SVG 折线 + 数据点 + 日期/数值标注）；
// 4. 无已完成 issue 时渲染空状态（页面不崩溃）；
// 5. 接口失败时显示错误提示、不崩溃；
// 6. 有 repos 数据时按仓库渲染平均耗时 + 迷你走势图，无数据仓库
//    渲染「暂无数据」；
// 7. styles.css 提供板块、走势图与各仓库明细样式类；
// 8. 概览页（Overview.jsx）不再包含该板块（issue #322 迁移完成）。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 stats-*.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Stats, CompletionTrendChart } = await vite.ssrLoadModule('/src/pages/Stats.jsx')
const { api, fmtSeconds } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('源码：统计页请求完成耗时接口并低频轮询（60 秒）', () => {
  assert.match(statsSrc, /\/api\/issues\/completion-stats/,
               '应请求 GET /api/issues/completion-stats')
  assert.match(statsSrc, /COMPLETION_STATS_POLL_MS\s*=\s*60000/,
               '完成耗时轮询间隔应为 60 秒')
  assert.match(statsSrc, /usePolling\(loadCompletionStats, COMPLETION_STATS_POLL_MS\)/,
               '应经 usePolling 独立定时轮询完成耗时接口（issue #200 统一管理）')
})

test('源码：板块位于统计页 dashboard 各板块之后（页面最下方）', () => {
  const cards = statsSrc.indexOf('className="stats-cards"')
  const stats = statsSrc.indexOf('className="completion-stats-section"')
  assert.ok(cards >= 0, '应存在统计页总览卡片板块')
  assert.ok(stats >= 0, '应存在完成耗时板块（completion-stats-section）')
  assert.ok(stats > cards,
            `完成耗时板块（源码偏移 ${stats}）应位于总览卡片板块（偏移 ${cards}）之后`)
})

test('源码：板块渲染平均完成耗时与走势图', () => {
  assert.match(statsSrc, /tr\('stats\.completionTitle'\)/, '板块标题应经 t() 国际化')
  assert.equal(zhCN['stats.completionTitle'], 'Issue 完成耗时', '中文标题应为「Issue 完成耗时」')
  assert.match(statsSrc, /tr\('stats\.avgCompletion'/, '平均耗时文案应经 t() 国际化')
  assert.ok(zhCN['stats.avgCompletion'].includes('平均完成耗时'), '中文平均耗时文案应保留')
  assert.match(statsSrc, /fmtSeconds\(completionStats\.avg_seconds\)/,
               '平均耗时应经 fmtSeconds 人类可读格式化')
  assert.match(statsSrc, /CompletionTrendChart/, '应渲染走势图组件')
  assert.match(statsSrc, /tr\('stats\.noCompletedIssues'\)/, '空状态文案应经 t() 国际化')
  assert.equal(zhCN['stats.noCompletedIssues'], '暂无已完成 issue', '中文空状态文案应保留')
})

test('源码：板块渲染每个仓库的平均耗时与走势（issue #288）', () => {
  assert.match(statsSrc, /completionStats\.repos/, '应遍历接口返回的 repos 拆分')
  assert.match(statsSrc, /tr\('stats\.completionPerRepoTitle'\)/,
               '各仓库明细标题应经 t() 国际化')
  assert.equal(zhCN['stats.completionPerRepoTitle'], '各仓库平均耗时与走势',
               '中文各仓库明细标题应为「各仓库平均耗时与走势」')
  assert.match(statsSrc, /tr\('stats\.repoNoData'\)/, '仓库无数据文案应经 t() 国际化')
  assert.equal(zhCN['stats.repoNoData'], '暂无数据', '中文仓库无数据文案应为「暂无数据」')
  assert.match(statsSrc, /completion-repo-row/, '应按仓库渲染明细行')
  assert.match(statsSrc, /compact/, '走势图组件应支持紧凑模式（迷你走势图）')
})

test('源码：概览页不再包含该板块（issue #322 迁移完成）', () => {
  assert.ok(!overviewSrc.includes('completion-stats-section'),
            '概览页应移除 completion-stats-section 板块')
  assert.ok(!overviewSrc.includes('loadCompletionStats'),
            '概览页应移除完成耗时轮询逻辑')
  assert.ok(!overviewSrc.includes('/api/issues/completion-stats'),
            '概览页应不再请求完成耗时接口')
})

test('styles.css 提供板块与走势图样式', () => {
  assert.match(styles, /\.completion-stats-section\s*\{/, '应有板块容器样式')
  assert.match(styles, /\.completion-stats-value\s*\{/, '应有平均耗时数字样式')
  assert.match(styles, /\.completion-trend-chart\s*\{/, '应有走势图 SVG 样式')
  assert.match(styles, /\.completion-trend-line\s*\{/, '应有折线样式')
  assert.match(styles, /\.completion-trend-dot\s*\{/, '应有数据点样式')
  assert.match(styles, /\.completion-repo-list\s*\{/, '应有各仓库明细列表样式')
  assert.match(styles, /\.completion-repo-row\s*\{/, '应有各仓库明细行样式')
  assert.match(styles, /\.completion-repo-name\s*\{/, '应有仓库名称样式')
  assert.match(styles, /\.completion-repo-value\s*\{/, '应有仓库平均耗时样式')
})

// ---- fmtSeconds 纯函数（issue #180：秒数 → 人类可读）----

test('fmtSeconds：秒/分钟/小时/天换算与 fmtDuration 输出一致', () => {
  assert.equal(fmtSeconds(0), '0 秒')
  assert.equal(fmtSeconds(30), '30 秒')
  assert.equal(fmtSeconds(60), '1 分钟')
  assert.equal(fmtSeconds(119), '1 分钟')
  assert.equal(fmtSeconds(3600), '1 小时')
  assert.equal(fmtSeconds(3660), '1 小时 1 分钟')
  assert.equal(fmtSeconds(90000), '1 天 1 小时')
  assert.equal(fmtSeconds(172800), '2 天')
})

test('fmtSeconds：非法输入返回 null', () => {
  assert.equal(fmtSeconds(null), null)
  assert.equal(fmtSeconds(undefined), null)
  assert.equal(fmtSeconds('abc'), null)
  assert.equal(fmtSeconds(NaN), null)
  assert.equal(fmtSeconds(-5), null)
})

// ---- 走势图组件纯函数 ----

test('走势图：空/非法 trend 返回 null（不渲染）', () => {
  const rendered = TestRenderer.create(React.createElement(CompletionTrendChart, { trend: [] }))
  assert.equal(rendered.toJSON(), null)
  rendered.unmount()
  const rendered2 = TestRenderer.create(React.createElement(CompletionTrendChart, { trend: null }))
  assert.equal(rendered2.toJSON(), null)
  rendered2.unmount()
})

test('走势图：单数据点也能渲染（不除零崩溃）', () => {
  const r = TestRenderer.create(React.createElement(
    CompletionTrendChart, { trend: [{ date: '2026-08-12', count: 1, avg_seconds: 3600 }] }))
  const root = r.root
  assert.equal(root.findAllByType('polyline').length, 1, '应渲染折线')
  assert.equal(root.findAll((n) => String(n.props.className || '').includes('completion-trend-dot')).length, 1,
               '应渲染数据点')
  assert.ok(JSON.stringify(r.toJSON()).includes('2026-08-12'), '应渲染日期标注')
  r.unmount()
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// 完成耗时接口返回数据（两日：08-12 平均 2700s，08-13 平均 7200s，
// 总体平均 4200s）
const STATS_PAYLOAD = {
  completed_count: 3,
  avg_seconds: 4200,
  trend: [
    { date: '2026-08-12', count: 2, avg_seconds: 2700 },
    { date: '2026-08-13', count: 1, avg_seconds: 7200 },
  ],
  // issue #288：每个已开启仓库的平均耗时与走势拆分
  repos: [
    { repo_id: 1, repo_name: 'alpha', completed_count: 2, avg_seconds: 2700,
      trend: [{ date: '2026-08-12', count: 2, avg_seconds: 2700 }] },
    { repo_id: 2, repo_name: 'beta', completed_count: 1, avg_seconds: 7200,
      trend: [{ date: '2026-08-13', count: 1, avg_seconds: 7200 }] },
    { repo_id: 3, repo_name: 'gamma', completed_count: 0, avg_seconds: null,
      trend: [] },
  ],
}

const DASHBOARD = {
  overview: { task_count: 1, succeeded_count: 1, failed_count: 0,
              interrupted_count: 0, success_rate: 1,
              avg_duration_seconds: 100 },
  by_engine: [], by_repo: [], by_source: [], failure_reasons: [],
  failure_categories: [],
}

async function renderStats({
  statsPayload = STATS_PAYLOAD,
  statsFail = false,
} = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/stats/dashboard')) return DASHBOARD
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname.startsWith('/api/usage/stats')) {
      return { summary: { task_count: 0 }, by_engine: [], by_repo: [], by_date: [] }
    }
    if (pathname === '/api/issues/completion-stats') {
      if (statsFail) throw new Error('完成耗时接口网络错误')
      return statsPayload
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Stats))
      // 等待首轮各数据接口的 promise flush
      await new Promise((resolve) => setTimeout(resolve, 50))
    } catch (e) {
      renderError = e
    }
  })
  return {
    renderer, renderError, getCalls,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// 渲染整棵树为扁平文本（深度优先，与视觉顺序一致）
function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}

// ---- 渲染级断言 ----

test('渲染：有已完成 issue 时展示平均耗时、数量与走势图', async () => {
  const r = await renderStats()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    assert.ok(r.getCalls.includes('/api/issues/completion-stats'), '应请求完成耗时接口')
    const text = treeText(r.renderer)
    assert.ok(text.includes('Issue 完成耗时'), '应渲染板块标题')
    // 平均耗时 4200 秒 = 1 小时 10 分钟
    assert.ok(text.includes('1 小时 10 分钟'), '应渲染人类可读的平均完成耗时')
    assert.ok(text.includes('个已完成 issue'), '应渲染已完成 issue 数量文案')
    assert.ok(text.includes('"3"') || text.includes(',"3",'), '应渲染已完成 issue 数量 3')
    // 走势图：SVG 折线 + 数据点 + 日期标注
    const root = r.renderer.root
    assert.equal(root.findAllByType('polyline').length, 3,
                 '应渲染 3 条折线（全局 1 + alpha/beta 各 1）')
    assert.equal(root.findAll((n) => String(n.props.className || '').includes('completion-trend-dot')).length, 4,
                 '应渲染 4 个数据点（全局 2 + alpha 1 + beta 1）')
    assert.ok(text.includes('2026-08-12'), '应渲染起始日期标注')
    assert.ok(text.includes('2026-08-13'), '应渲染结束日期标注')
    // issue #288：各仓库明细——仓库名 + 平均耗时 + 无数据仓库「暂无数据」
    assert.ok(text.includes('各仓库平均耗时与走势'), '应渲染各仓库明细标题')
    assert.ok(text.includes('alpha'), '应渲染仓库 alpha 行')
    assert.ok(text.includes('beta'), '应渲染仓库 beta 行')
    assert.ok(text.includes('45 分钟'), 'alpha 平均 2700 秒 = 45 分钟')
    assert.ok(text.includes('2 小时'), 'beta 平均 7200 秒 = 2 小时')
    assert.ok(text.includes('gamma'), '应渲染无数据仓库 gamma 行')
    assert.ok(text.includes('暂无数据'), '无数据仓库应显示「暂无数据」')
    // 板块位于统计页 dashboard 之后（页面最下方）
    assert.ok(text.indexOf('任务总数') < text.indexOf('Issue 完成耗时'),
              '完成耗时板块应位于统计页 dashboard 板块之后')
  } finally {
    await r.unmount()
  }
})

test('渲染：无已完成 issue 时展示空状态，不崩溃', async () => {
  const r = await renderStats({
    statsPayload: { completed_count: 0, avg_seconds: null, trend: [], repos: [] },
  })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('Issue 完成耗时'), '板块标题仍应渲染')
    assert.ok(text.includes('暂无已完成 issue'), '应渲染空状态文案')
    assert.ok(!text.includes('completion-trend-chart'), '无数据时不应渲染走势图')
  } finally {
    await r.unmount()
  }
})

test('渲染：完成耗时接口失败时显示错误提示，页面其他板块不崩溃', async () => {
  const r = await renderStats({ statsFail: true })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('完成耗时接口网络错误'), '应显示接口错误信息')
    assert.ok(text.includes('任务总数'), '统计页总览卡片仍应渲染')
    assert.ok(text.includes('引擎对比'), '引擎对比板块仍应渲染')
  } finally {
    await r.unmount()
  }
})
