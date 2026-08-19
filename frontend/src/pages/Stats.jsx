// 统计看板页（issue #264）：任务执行数据聚合看板——总览卡片 / 引擎对比 /
// 仓库排行 / 来源分布 / 失败原因 Top。
// 数据来自本地任务表聚合接口 GET /api/stats/dashboard（与任务列表同表
// 同口径，验收标准 1「统计页各维度数字与任务列表一致」；后端 10s TTL
// 缓存，复用概览页缓存模式），无 GitLab 请求压力。
// - 总览卡片：任务总数 / 成功率 / 平均耗时 / 失败数；
// - 按引擎对比成功率与平均耗时（纯 CSS 条形图，避免引入 recharts 等
//   重依赖，验收标准「可用现有技术栈」）；
// - 按仓库排行任务量与成功率；
// - 按来源（webhook/手动/对账，#224 已做来源维度，本页聚焦成功率）；
// - 失败原因 Top 分布（failed/interrupted 任务 error_message，与 #40
//   失败分类口径联动）；
// - 时间段选择（最近 7 天 / 30 天 / 全部）持久化到 localStorage，刷新
//   后保持（验收标准 2/3）。
// issue #322：概览页「Issue 完成耗时」（issue #180/#288）与「Token 用量
// 统计」（issue #235）两个板块整体迁入本页——数据接口不变（
// GET /api/issues/completion-stats / GET /api/usage/stats），沿用 60 秒
// 低频轮询；统计页无任务数据（dashboard 空态）时两个板块仍展示各自的
// 空态，不随 dashboard 一起隐藏。

import { useCallback, useEffect, useState } from 'react'
import { api, fmtSeconds } from '../api.js'
import { Icon } from '../components/Icon.jsx'
import { fmtTokens, fmtCost } from '../components/UsageCard.jsx'
import { useI18n } from '../i18n.jsx'
import { failureCategoryClass, failureCategoryLabel } from '../failure-categories.js'

// Issue 完成耗时统计轮询间隔（issue #180）：平均完成耗时与走势图数据
// 来自本地 tasks 表成功终态任务（GET /api/issues/completion-stats），
// 无 GitLab 请求压力，低频轮询即可（任务完成后再等下一轮刷新）
const COMPLETION_STATS_POLL_MS = 60000

// Token 用量统计轮询间隔（issue #235）：数据来自本地 task_usage 表
// （GET /api/usage/stats），无 GitLab 请求压力，沿用 60 秒低频轮询
const USAGE_STATS_POLL_MS = 60000

// 时间段选项：value 为查询参数 days（0=全部），持久化存 value
const RANGE_OPTIONS = [
  { value: '7', label: '最近 7 天', days: 7 },
  { value: '30', label: '最近 30 天', days: 30 },
  { value: '0', label: '全部', days: 0 },
]

// 时间段偏好持久化键（issue #264 验收标准 3）
const RANGE_STORAGE_KEY = 'botler.stats.range'

function loadRange(storage) {
  try {
    const v = storage?.getItem(RANGE_STORAGE_KEY)
    if (RANGE_OPTIONS.some((o) => o.value === v)) return v
  } catch { /* 无存储环境（SSR/隐私模式）静默回默认 */ }
  return '7'
}

function saveRange(storage, value) {
  try { storage?.setItem(RANGE_STORAGE_KEY, value) } catch { /* 忽略 */ }
}

// 成功率 0~1 → 百分比文案；null（无任务）返回 '—'
function pct(rate) {
  if (rate == null) return '—'
  return `${Math.round(rate * 100)}%`
}

// 纯 CSS 条形图（issue #264）：宽度按 max 归一化（最小值 2% 保证可见），
// 配色由 styles.css 的 .stats-bar 变量控制（跟随深浅色主题）
function Bar({ value, max, title }) {
  const width = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0
  return (
    <span className="stats-bar-track" title={title}>
      <span className="stats-bar" style={{ width: `${width}%` }} />
    </span>
  )
}


// 走势图（issue #180）：轻量 SVG 折线图，无第三方图表库依赖——
// 横轴为完成日（数据本身是逐日序列，等距排布即可），纵轴为当日平均
// 完成耗时（秒），范围 0 → 最大值留 10% 余量；折线 + 数据点，每个点
// 带 <title> 悬浮提示（日期 / 平均耗时 / 当日完成数）。trend 非数组
// 或为空时返回 null（不渲染）。
// issue #288：compact 紧凑模式——各仓库明细行的迷你走势图（更小画布、
// 更小数据点，隐藏日期/刻度文字避免缩小后不可读，仍保留 <title> 提示）。
// issue #322：组件随板块从概览页迁入统计页。
export function CompletionTrendChart({ trend, compact = false }) {
  const { tr } = useI18n()
  if (!Array.isArray(trend) || trend.length === 0) return null
  const W = compact ? 240 : 640
  const H = compact ? 48 : 180
  const PAD_L = compact ? 2 : 8
  const PAD_R = compact ? 2 : 8
  const PAD_T = compact ? 4 : 14
  const PAD_B = compact ? 4 : 24
  const n = trend.length
  const maxSec = Math.max(...trend.map((t) => Number(t.avg_seconds) || 0))
  const yMax = maxSec > 0 ? maxSec * 1.1 : 1
  const innerW = W - PAD_L - PAD_R
  const innerH = H - PAD_T - PAD_B
  const px = (i) => (n === 1 ? PAD_L + innerW / 2 : PAD_L + (innerW * i) / (n - 1))
  const py = (v) => H - PAD_B - (innerH * (Number(v) || 0)) / yMax
  const points = trend
    .map((t, i) => `${px(i).toFixed(2)},${py(t.avg_seconds).toFixed(2)}`)
    .join(' ')
  const first = trend[0]
  const last = trend[n - 1]
  return (
    <svg className={compact ? 'completion-trend-chart compact' : 'completion-trend-chart'}
         viewBox={`0 0 ${W} ${H}`}
         role="img" aria-label={compact
           ? tr('stats.repoTrendAria')
           : tr('stats.trendAria')}>
      <line className="completion-trend-axis" x1={PAD_L} y1={H - PAD_B}
            x2={W - PAD_R} y2={H - PAD_B} />
      <polyline className="completion-trend-line" points={points} fill="none" />
      {trend.map((t, i) => (
        <circle key={t.date || i} className="completion-trend-dot"
                cx={px(i).toFixed(2)} cy={py(t.avg_seconds).toFixed(2)}
                r={compact ? '2' : '3'}>
          <title>{tr('stats.trendPoint', { date: t.date, avg: fmtSeconds(t.avg_seconds) || '—', n: t.count })}</title>
        </circle>
      ))}
      {!compact && (
        <>
          <text className="completion-trend-label" x={PAD_L} y={H - PAD_B + 16}>{first.date}</text>
          <text className="completion-trend-label" x={W - PAD_R} y={H - PAD_B + 16}
                textAnchor="end">{last.date}</text>
          <text className="completion-trend-label" x={PAD_L} y={PAD_T - 4}>{fmtSeconds(yMax) || ''}</text>
        </>
      )}
    </svg>
  )
}

export default function Stats() {
  const { tr } = useI18n()
  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  const [range, setRange] = useState(() => loadRange(storage))
  // 看板数据：{overview, by_engine, by_repo, by_source, failure_reasons}
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  // issue #322：迁入的统计板块状态——Issue 完成耗时（issue #180/#288）：
  // null=加载中；{completed_count, avg_seconds, trend, repos} 为
  // /api/issues/completion-stats 返回
  const [completionStats, setCompletionStats] = useState(null)
  const [completionStatsError, setCompletionStatsError] = useState('')
  // Token 用量统计（issue #235）：null=加载中；{summary, by_repo,
  // by_engine, by_date, currency} 为 /api/usage/stats 返回；过滤器
  // usageRepoId（''=全部仓库）/ usageEngine（''=全部引擎）/
  // usageRange（'7'|'30'|'0'=最近 7/30 天或全部）
  const [usageStats, setUsageStats] = useState(null)
  const [usageStatsError, setUsageStatsError] = useState('')
  const [usageRepoId, setUsageRepoId] = useState('')
  const [usageEngine, setUsageEngine] = useState('')
  const [usageRange, setUsageRange] = useState('7')
  // 仓库列表（GET /api/repos，用量过滤器「按仓库过滤」下拉数据源；
  // 与概览页原过滤器同构——id/name 映射 repo_id/repo_name）
  const [repos, setRepos] = useState([])

  const load = async (days) => {
    const d = await api.get('/api/stats/dashboard?days=' + days)
    setData(d || {
      overview: { task_count: 0, succeeded_count: 0, failed_count: 0,
                  interrupted_count: 0, success_rate: null,
                  avg_duration_seconds: null },
      by_engine: [], by_repo: [], by_source: [], failure_reasons: [],
    })
  }

  // 时间段变化：立即重拉 + 持久化（刷新后保持）
  useEffect(() => {
    const days = RANGE_OPTIONS.find((o) => o.value === range)?.days ?? 0
    setError('')
    load(days).catch((e) => setError(e.message))
  }, [range])

  const changeRange = (e) => {
    const v = e.target.value
    setRange(v)
    saveRange(storage, v)
  }

  // issue #322：仓库列表一次拉取（用量过滤器用；失败静默，过滤器仅剩
  // 「全部仓库」选项，不影响板块主体）
  useEffect(() => {
    api.get('/api/repos')
      .then((d) => setRepos(d?.repos || []))
      .catch(() => {})
  }, [])

  // 已完成 issue 平均耗时与逐日走势（issue #180，独立低频轮询）：数据
  // 来自本地 tasks 表成功终态任务，无 GitLab 请求压力；接口失败保留
  // 上次数据并展示错误提示，不影响页面其他板块
  const loadCompletionStats = useCallback(async () => {
    try {
      const d = await api.get('/api/issues/completion-stats', { silent: true })
      setCompletionStats(d || { completed_count: 0, avg_seconds: null, trend: [] })
      setCompletionStatsError('')
    } catch (e) {
      setCompletionStatsError(e.message)
    }
  }, [])

  useEffect(() => {
    loadCompletionStats()
    const t = setInterval(loadCompletionStats, COMPLETION_STATS_POLL_MS)
    return () => clearInterval(t)
  }, [loadCompletionStats])

  // Token 用量统计（issue #235）：按仓库/引擎/时间段聚合，数据来自本地
  // task_usage 表（GET /api/usage/stats），无 GitLab 请求压力，沿用 60 秒
  // 低频轮询；过滤器变化时立即重拉（不清空旧数据避免闪烁）
  const loadUsageStats = useCallback(async () => {
    const q = new URLSearchParams()
    if (usageRepoId) q.set('repo_id', usageRepoId)
    if (usageEngine) q.set('engine', usageEngine)
    if (usageRange && usageRange !== '0') {
      q.set('since', new Date(Date.now() - Number(usageRange) * 86400000)
        .toISOString().slice(0, 10))
    }
    try {
      const d = await api.get('/api/usage/stats?' + q, { silent: true })
      setUsageStats(d || { summary: {}, by_repo: [], by_engine: [], by_date: [] })
      setUsageStatsError('')
    } catch (e) {
      setUsageStatsError(e.message)
    }
  }, [usageRepoId, usageEngine, usageRange])

  useEffect(() => {
    loadUsageStats()
    const t = setInterval(loadUsageStats, USAGE_STATS_POLL_MS)
    return () => clearInterval(t)
  }, [loadUsageStats])

  const o = data?.overview || {}
  const maxEngine = Math.max(1, ...((data?.by_engine) || []).map((e) => e.task_count))
  const maxRepo = Math.max(1, ...((data?.by_repo) || []).map((r) => r.task_count))
  const maxReason = Math.max(1, ...((data?.failure_reasons) || []).map((r) => r.count))
  const rangeLabel = RANGE_OPTIONS.find((x) => x.value === range)?.label || '全部'
  const empty = (o.task_count || 0) === 0

  return (
    <div className="stats-page">
      <div className="stats-header">
        <h1>统计看板</h1>
        <select className="input stats-range" value={range} onChange={changeRange}
                title="统计时间段（选择持久化，刷新后保持）">
          {RANGE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>
      <p className="muted">任务执行数据聚合（本地任务表，与任务列表同口径 · {rangeLabel}）</p>

      {error && (
        <div className="alert alert-error" onClick={() => setError('')}>{error}</div>
      )}

      {!data ? (
        <div className="loading-hint">
          <span className="spinner" aria-hidden="true" />
          <span className="muted">加载中…</span>
        </div>
      ) : empty ? (
        <div className="empty-state">
          <span className="empty-icon" aria-hidden="true"><Icon name="chart" /></span>
          <p className="muted">暂无任务数据（当前时间段内没有任务记录）</p>
        </div>
      ) : (
        <>
          {/* 总览卡片：任务总数 / 成功率 / 平均耗时 / 失败数 */}
          <section className="stats-cards">
            <div className="stats-card">
              <span className="stats-card-label"><Icon name="clipboard" /> 任务总数</span>
              <span className="stats-card-value">{o.task_count}</span>
            </div>
            <div className="stats-card">
              <span className="stats-card-label"><Icon name="check" /> 成功率</span>
              <span className="stats-card-value">{pct(o.success_rate)}</span>
              <span className="muted">成功 {o.succeeded_count || 0}</span>
            </div>
            <div className="stats-card">
              <span className="stats-card-label"><Icon name="hourglass" /> 平均耗时</span>
              <span className="stats-card-value">{fmtSeconds(o.avg_duration_seconds) || '—'}</span>
              <span className="muted">完成 {o.succeeded_count || 0} 个任务</span>
            </div>
            <div className="stats-card">
              <span className="stats-card-label"><Icon name="xCircle" /> 失败数</span>
              <span className="stats-card-value">{o.failed_count || 0}</span>
              <span className="muted">中断 {o.interrupted_count || 0}</span>
            </div>
          </section>

          {/* 按引擎对比成功率与平均耗时（条形图） */}
          <section className="stats-section">
            <h2>引擎对比</h2>
            <table className="table stats-table">
              <thead>
                <tr><th>引擎</th><th>任务数</th><th>成功率</th><th>平均耗时</th></tr>
              </thead>
              <tbody>
                {(data.by_engine || []).map((e) => (
                  <tr key={e.key}>
                    <td className="stats-name">{e.name || '—'}</td>
                    <td>
                      <span className="stats-bar-cell">
                        <Bar value={e.task_count} max={maxEngine} title={`${e.task_count} 个任务`} />
                        <span className="muted">{e.task_count}</span>
                      </span>
                    </td>
                    <td>
                      <span className="stats-bar-cell">
                        <Bar value={e.succeeded_count} max={maxEngine} title={`成功 ${e.succeeded_count} / ${e.task_count}`} />
                        <b>{pct(e.success_rate)}</b>
                      </span>
                    </td>
                    <td>{fmtSeconds(e.avg_duration_seconds) || <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* 按仓库排行任务量与成功率 */}
          <section className="stats-section">
            <h2>仓库排行</h2>
            <table className="table stats-table">
              <thead>
                <tr><th>仓库</th><th>任务量</th><th>成功率</th><th>成功 / 失败</th></tr>
              </thead>
              <tbody>
                {(data.by_repo || []).map((r) => (
                  <tr key={r.key}>
                    <td className="stats-name ellipsis" title={r.name}>{r.name || '（已删除）'}</td>
                    <td>
                      <span className="stats-bar-cell">
                        <Bar value={r.task_count} max={maxRepo} title={`${r.task_count} 个任务`} />
                        <span className="muted">{r.task_count}</span>
                      </span>
                    </td>
                    <td><b>{pct(r.success_rate)}</b></td>
                    <td className="muted">{r.succeeded_count} / {r.failed_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* 按来源（webhook/手动/对账）分布 */}
          <section className="stats-section">
            <h2>来源分布</h2>
            <table className="table stats-table">
              <thead>
                <tr><th>来源</th><th>任务数</th><th>成功率</th><th>平均耗时</th></tr>
              </thead>
              <tbody>
                {(data.by_source || []).map((s) => (
                  <tr key={s.key}>
                    <td className="stats-name">{s.name || '—'}</td>
                    <td>{s.task_count}</td>
                    <td><b>{pct(s.success_rate)}</b></td>
                    <td>{fmtSeconds(s.avg_duration_seconds) || <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* 失败原因 Top 分布（与 #40 失败分类口径联动） */}
          <section className="stats-section">
            <h2>失败原因 Top 分布</h2>
            {(data.failure_reasons || []).length === 0 ? (
              <p className="muted">无失败任务（当前时间段内失败原因为空）</p>
            ) : (
              <ul className="stats-reasons">
                {(data.failure_reasons || []).map((r, i) => (
                  <li key={i}>
                    <span className="stats-reason-rank">{i + 1}</span>
                    {/* issue #274：失败原因条目附分类徽章（后端按失败原因
                        文本规则分类，与详情页/失败评论同口径） */}
                    {r.category && (
                      <span className={`badge failure-cat ${failureCategoryClass(r.category)}`}>
                        {failureCategoryLabel(r.category)}
                      </span>
                    )}
                    <span className="stats-reason-text" title={r.reason}>{r.reason}</span>
                    <span className="stats-bar-cell">
                      <Bar value={r.count} max={maxReason} title={`${r.count} 次`} />
                      <span className="muted">{r.count} 次</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* issue #274：失败原因分类分布——与「失败原因 Top」同源
              （failed/interrupted 任务），按分类聚合展示数量与占比，
              数据驱动判断失败大头在环境/引擎还是任务本身不可解 */}
          <section className="stats-section">
            <h2>失败原因分类分布</h2>
            {(data.failure_categories || []).length === 0 ? (
              <p className="muted">无失败任务（当前时间段内分类分布为空）</p>
            ) : (
              <ul className="stats-reasons">
                {data.failure_categories.map((c, i) => (
                  <li key={c.category}>
                    <span className="stats-reason-rank">{i + 1}</span>
                    <span className={`badge failure-cat ${failureCategoryClass(c.category)}`}>
                      {c.name}
                    </span>
                    <span className="stats-reason-text">{c.name}（{c.category}）</span>
                    <span className="stats-bar-cell">
                      <Bar value={c.count} max={maxReason} title={`${c.count} 次`} />
                      <span className="muted">{c.count} 次</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

        </>
      )}

      {/* issue #322：迁入板块——dashboard 加载中/空态时仍独立渲染（各自空态） */}
      {/* issue #322：Issue 完成耗时板块自概览页迁入——平均每个 issue
          完成所需的时间（成功任务的处理用时：系统接收时间 → bot-done
          打标时间，与任务详情「处理用时」issue #49 语义一致）与逐日
          平均走势图。数据来自本地 tasks 表成功终态任务
          （GET /api/issues/completion-stats），无 GitLab 请求压力 */}
      <section className="completion-stats-section">
        <h2>{tr('stats.completionTitle')}</h2>
        <p className="muted">{tr('stats.completionDesc', { seconds: COMPLETION_STATS_POLL_MS / 1000 })}</p>
        {completionStatsError && (
          <div className="alert alert-error" onClick={() => setCompletionStatsError('')}>{completionStatsError}</div>
        )}
        {completionStats && completionStats.completed_count === 0 ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="hourglass" /></span>
            <p className="muted">{tr('stats.noCompletedIssues')}</p>
          </div>
        ) : completionStats ? (
          <>
            <div className="completion-stats-summary">
              <span className="completion-stats-value"
                    title={tr('stats.avgCompletionTitle')}>
                {fmtSeconds(completionStats.avg_seconds) || <span className="muted">—</span>}
              </span>
              <span className="muted">{tr('stats.avgCompletion', { n: completionStats.completed_count })}</span>
            </div>
            <CompletionTrendChart trend={completionStats.trend} />
            {/* issue #288：每个开启仓库的平均耗时与走势拆分——接口
                repos 数组（仅已启用仓库，按配置优先级升序，无已完成
                任务仓库 avg_seconds=null/trend=[]），逐仓库渲染平均
                耗时 + 紧凑迷你走势图 */}
            {Array.isArray(completionStats.repos) && completionStats.repos.length > 0 && (
              <div className="completion-repo-list">
                <h3 className="completion-repo-title">{tr('stats.completionPerRepoTitle')}</h3>
                {completionStats.repos.map((r) => (
                  <div className="completion-repo-row" key={r.repo_id}>
                    <div className="completion-repo-info">
                      <span className="completion-repo-name"
                            title={r.repo_name}>{r.repo_name}</span>
                      <span className="completion-repo-value"
                            title={r.completed_count > 0 ? tr('stats.avgCompletionTitle') : undefined}>
                        {r.completed_count > 0
                          ? (fmtSeconds(r.avg_seconds) || <span className="muted">—</span>)
                          : <span className="muted">{tr('stats.repoNoData')}</span>}
                      </span>
                      <span className="muted">{tr('stats.avgCompletion', { n: r.completed_count })}</span>
                    </div>
                    {r.completed_count > 0 ? (
                      <div className="completion-repo-chart">
                        <CompletionTrendChart trend={r.trend} compact />
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </>
        ) : null}
      </section>

      {/* issue #322：Token 用量统计板块自概览页迁入——按仓库/引擎/
          时间段聚合（本地 task_usage 表，GET /api/usage/stats），无
          GitLab 请求压力；展示合计 token 数与估算费用（未配置单价只
          展示 token 数）；过滤器变化立即重拉，沿用 60 秒低频轮询 */}
      <section className="usage-stats-section">
        <h2>{tr('stats.usageTitle')}</h2>
        <p className="muted">{tr('stats.usageDesc', { seconds: USAGE_STATS_POLL_MS / 1000 })}</p>
        <div className="form-row wrap">
          <select className="input usage-stats-filter" value={usageRepoId}
                  onChange={(e) => setUsageRepoId(e.target.value)}
                  title={tr('stats.filterByRepo')}>
            <option value="">{tr('stats.allRepos')}</option>
            {(repos || []).map((r) => (
              <option key={r.id} value={r.id}>{r.name}</option>
            ))}
          </select>
          <select className="input usage-stats-filter" value={usageEngine}
                  onChange={(e) => setUsageEngine(e.target.value)}
                  title={tr('stats.filterByEngine')}>
            <option value="">{tr('stats.allEngines')}</option>
            <option value="claude">claude</option>
            <option value="hermes">hermes</option>
            <option value="dsh">dsh</option>
          </select>
          <select className="input usage-stats-filter" value={usageRange}
                  onChange={(e) => setUsageRange(e.target.value)}
                  title={tr('stats.filterByRange')}>
            <option value="7">{tr('stats.last7Days')}</option>
            <option value="30">{tr('stats.last30Days')}</option>
            <option value="0">{tr('stats.all')}</option>
          </select>
        </div>
        {usageStatsError && (
          <div className="alert alert-error" onClick={() => setUsageStatsError('')}>{usageStatsError}</div>
        )}
        {usageStats && (usageStats.summary?.task_count || 0) === 0 ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="coins" /></span>
            <p className="muted">{tr('stats.noUsage')}</p>
          </div>
        ) : usageStats ? (
          <>
            <div className="usage-stats-summary">
              <span className="usage-stats-value">
                {fmtTokens(usageStats.summary?.total_tokens || 0)} tokens
              </span>
              <span className="muted">
                {tr('stats.usageTaskCount', { n: usageStats.summary?.task_count || 0 })}{' '}
                {fmtCost(usageStats.summary?.estimated_cost, usageStats.currency)
                  ? <>{tr('stats.estimatedCost')} <b>{fmtCost(usageStats.summary?.estimated_cost, usageStats.currency)}</b></>
                  : tr('stats.noUnitPrice')}
              </span>
            </div>
            <div className="usage-stats-grid">
              <table className="table usage-stats-table">
                <thead>
                  <tr><th>{tr('stats.engine')}</th><th>{tr('stats.taskCount')}</th><th>{tr('stats.totalTokens')}</th><th>{tr('stats.estimatedCost')}</th></tr>
                </thead>
                <tbody>
                  {(usageStats.by_engine || []).map((e) => (
                    <tr key={e.engine}>
                      <td>{e.engine || '—'}</td>
                      <td>{e.task_count}</td>
                      <td>{fmtTokens(e.total_tokens)}</td>
                      <td>{fmtCost(e.estimated_cost, usageStats.currency) || <span className="muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table className="table usage-stats-table">
                <thead>
                  <tr><th>{tr('stats.repo')}</th><th>{tr('stats.taskCount')}</th><th>{tr('stats.totalTokens')}</th><th>{tr('stats.estimatedCost')}</th></tr>
                </thead>
                <tbody>
                  {(usageStats.by_repo || []).map((r) => (
                    <tr key={r.repo_id}>
                      <td className="ellipsis" title={r.repo_name}>{r.repo_name || tr('common.deleted')}</td>
                      <td>{r.task_count}</td>
                      <td>{fmtTokens(r.total_tokens)}</td>
                      <td>{fmtCost(r.estimated_cost, usageStats.currency) || <span className="muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>
    </div>
  )
}
