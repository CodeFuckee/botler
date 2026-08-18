import { useEffect, useState } from 'react'
import { api, fmtSeconds } from '../api.js'
import { Icon } from '../components/Icon.jsx'
import { failureCategoryClass, failureCategoryLabel } from '../failure-categories.js'

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

export default function Stats() {
  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  const [range, setRange] = useState(() => loadRange(storage))
  // 看板数据：{overview, by_engine, by_repo, by_source, failure_reasons}
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

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
    </div>
  )
}
