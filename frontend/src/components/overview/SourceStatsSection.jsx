// 概览页「来源分布」统计卡片（issue #224）：按任务来源（webhook/手动/对账）
// 聚合展示任务量、成功率与平均耗时——数据来自本地任务表聚合接口
// GET /api/stats/dashboard?days=30（后端 10s TTL 缓存，与任务列表同表同
// 口径），组件自持低频轮询（60 秒，复用 usePolling——页面隐藏自动暂停，
// 后台 0 请求），不依赖 GitLab API、不给远端加压力。
import { useCallback, useState } from 'react'
import { usePolling } from '../../hooks/usePolling.js'
import { api, fmtSeconds } from '../../api.js'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'

// 来源分布轮询间隔（issue #224）：数据来自本地 tasks 表聚合，后端另有
// 10s TTL 缓存，低频轮询即可（任务完成后再等下一轮刷新）
const SOURCE_STATS_POLL_MS = 60000
// 概览页来源分布默认统计窗口：近 30 天（与统计页「最近 30 天」口径一致）
const SOURCE_STATS_DAYS = 30

// 成功率 0~1 → 百分比文案；null（无任务）返回 '—'
function pct(rate) {
  if (rate == null) return '—'
  return `${Math.round(rate * 100)}%`
}

export default function SourceStatsSection() {
  const { tr } = useI18n()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  // 拉取来源分布聚合（近 30 天）：失败保留上次数据并展示错误提示，
  // 不影响概览页其他板块（轮询类接口 silent，不弹 toast 骚扰）
  const load = useCallback(async () => {
    try {
      const d = await api.get(
        `/api/stats/dashboard?days=${SOURCE_STATS_DAYS}`, { silent: true })
      setData(d || { overview: {}, by_source: [] })
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [])

  usePolling(load, SOURCE_STATS_POLL_MS)

  const bySource = data?.by_source || []
  const taskCount = data?.overview?.task_count || 0

  return (
    <section className="overview-source-stats-section">
      <h2>{tr('overview.sourceStatsTitle')}</h2>
      <p className="muted">
        {tr('overview.sourceStatsDesc', { seconds: SOURCE_STATS_POLL_MS / 1000 })}
      </p>
      {error && (
        <div className="alert alert-error" role="alert" onClick={() => setError('')}>
          {error}
        </div>
      )}
      {!data ? (
        <div className="loading-hint">
          <span className="spinner" aria-hidden="true" />
          <span className="muted">加载中…</span>
        </div>
      ) : taskCount === 0 ? (
        <div className="empty-state small">
          <span className="empty-icon" aria-hidden="true"><Icon name="chart" /></span>
          <p className="muted">{tr('overview.sourceStatsEmpty')}</p>
        </div>
      ) : (
        <div className="overview-source-stats-cards">
          {bySource.map((s) => (
            <div className="overview-source-stats-card" key={s.key}>
              <span className="overview-source-stats-name"
                    title={tr('overview.sourceStatsTitle')}>{s.name || '—'}</span>
              <span className="overview-source-stats-count">{s.task_count}</span>
              <span className="muted small">
                {tr('overview.sourceTaskCount', { n: s.task_count })}
              </span>
              <span className="overview-source-stats-rate">
                <b>{pct(s.success_rate)}</b>
                <span className="muted small">{tr('overview.sourceSuccessRate')}</span>
              </span>
              <span className="muted small">
                {tr('overview.sourceAvgDuration',
                    { duration: fmtSeconds(s.avg_duration_seconds) || '—' })}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
