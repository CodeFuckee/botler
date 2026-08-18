// 概览页 Issue 完成耗时测试（issue #180）：
// 概览页面最下方新增「Issue 完成耗时」板块——显示平均每个 issue 完成
// 所需的时间（成功任务的处理用时：系统接收时间 → bot-done 打标时间，
// 与任务详情「处理用时」issue #49 语义一致）以及该时间的逐日平均走势图。
// 数据来自本地 tasks 表成功终态任务（GET /api/issues/completion-stats），
// 无 GitLab 请求压力，低频轮询（60 秒）。
//
// 断言：
// 1. Overview 请求 /api/issues/completion-stats 并低频轮询（60 秒）；
// 2. 板块位于概览页最下方（源码位置在 CI/CD 流水线板块之后）；
// 3. 有数据时渲染平均完成耗时（fmtSeconds 人类可读）+ 完成数量 +
//    走势图（SVG 折线 + 数据点 + 日期/数值标注）；
// 4. 无已完成 issue 时渲染空状态（页面不崩溃）；
// 5. 接口失败时显示错误提示、不崩溃；
// 6. styles.css 提供板块与走势图样式类。
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
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-*.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, CompletionTrendChart } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api, fmtSeconds } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('源码：请求完成耗时接口并低频轮询（60 秒）', () => {
  assert.match(overview, /\/api\/issues\/completion-stats/,
               '应请求 GET /api/issues/completion-stats')
  assert.match(overview, /COMPLETION_STATS_POLL_MS\s*=\s*60000/,
               '完成耗时轮询间隔应为 60 秒')
  assert.match(overview, /setInterval\(loadCompletionStats/,
               '应独立定时轮询完成耗时接口')
})

test('源码：板块位于概览页最下方（CI/CD 流水线板块之后）', () => {
  const pipes = overview.indexOf('className="pipelines-section"')
  const stats = overview.indexOf('className="completion-stats-section"')
  assert.ok(pipes >= 0, '应存在流水线板块')
  assert.ok(stats >= 0, '应存在完成耗时板块（completion-stats-section）')
  assert.ok(stats > pipes,
            `完成耗时板块（源码偏移 ${stats}）应位于流水线板块（偏移 ${pipes}）之后`)
})

test('源码：板块渲染平均完成耗时与走势图', () => {
  assert.match(overview, /tr\('overview\.completionTitle'\)/, '板块标题应经 t() 国际化')
  assert.equal(zhCN['overview.completionTitle'], 'Issue 完成耗时', '中文标题应为「Issue 完成耗时」')
  assert.match(overview, /tr\('overview\.avgCompletion'/, '平均耗时文案应经 t() 国际化')
  assert.ok(zhCN['overview.avgCompletion'].includes('平均完成耗时'), '中文平均耗时文案应保留')
  assert.match(overview, /fmtSeconds\(completionStats\.avg_seconds\)/,
               '平均耗时应经 fmtSeconds 人类可读格式化')
  assert.match(overview, /CompletionTrendChart/, '应渲染走势图组件')
  assert.match(overview, /tr\('overview\.noCompletedIssues'\)/, '空状态文案应经 t() 国际化')
  assert.equal(zhCN['overview.noCompletedIssues'], '暂无已完成 issue', '中文空状态文案应保留')
})

test('styles.css 提供板块与走势图样式', () => {
  assert.match(styles, /\.completion-stats-section\s*\{/, '应有板块容器样式')
  assert.match(styles, /\.completion-stats-value\s*\{/, '应有平均耗时数字样式')
  assert.match(styles, /\.completion-trend-chart\s*\{/, '应有走势图 SVG 样式')
  assert.match(styles, /\.completion-trend-line\s*\{/, '应有折线样式')
  assert.match(styles, /\.completion-trend-dot\s*\{/, '应有数据点样式')
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
}

async function renderOverview({
  statsPayload = STATS_PAYLOAD,
  statsFail = false,
} = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    if (pathname === '/api/settings') {
      return { gitlab: { owner_token_masked: 'test-****' } }
    }
    if (pathname === '/api/settings/deepseek-balance') {
      return { configured: false, balance: null, error: null }
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
      renderer = TestRenderer.create(React.createElement(Overview))
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
  const r = await renderOverview()
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
    assert.equal(root.findAllByType('polyline').length, 1, '应渲染走势折线')
    assert.equal(root.findAll((n) => String(n.props.className || '').includes('completion-trend-dot')).length, 2,
                 '应渲染 2 个数据点（两日）')
    assert.ok(text.includes('2026-08-12'), '应渲染起始日期标注')
    assert.ok(text.includes('2026-08-13'), '应渲染结束日期标注')
    // 板块位于 CI/CD 流水线之后（页面最下方）
    assert.ok(text.indexOf('CI/CD 流水线') < text.indexOf('Issue 完成耗时'),
              '完成耗时板块应位于流水线板块之后')
  } finally {
    await r.unmount()
  }
})

test('渲染：无已完成 issue 时展示空状态，不崩溃', async () => {
  const r = await renderOverview({
    statsPayload: { completed_count: 0, avg_seconds: null, trend: [] },
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
  const r = await renderOverview({ statsFail: true })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('完成耗时接口网络错误'), '应显示接口错误信息')
    assert.ok(text.includes('开放 Issue'), '开放 Issue 板块仍应渲染')
    assert.ok(text.includes('CI/CD 流水线'), 'CI/CD 流水线板块仍应渲染')
  } finally {
    await r.unmount()
  }
})
