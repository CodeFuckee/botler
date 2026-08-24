// 统计页「来源分布」统计卡片（issue #224 + #361）：按任务来源
// （webhook/手动/对账）聚合展示任务量、成功率与平均耗时——数据来自本地任务
// 表聚合接口 GET /api/stats/dashboard?days=30（后端 10s TTL 缓存，与任务
// 列表同表同口径），组件自持低频轮询（60 秒，复用 usePolling——页面隐藏
// 自动暂停，后台 0 请求），不依赖 GitLab API、不给远端加压力。
// issue #361：本卡片由概览页整体迁入统计页，替换统计页原有「来源分布」表格
// （同源同口径数据，避免同页重复展示）；组件行为保持不变（近 30 天独立轮询）。
import { useCallback, useEffect, useState } from 'react'
import { useGlobalEvents } from '../../hooks/useGlobalEvents.js'
import { api, fmtSeconds } from '../../api.js'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'

// issue #478：来源分布由低频轮询改为 SSE 事件驱动刷新（数据来自本地
// tasks 表聚合，task 事件即数据变化来源），不再需要固定轮询间隔常量
// 来源分布默认统计窗口：近 30 天（与统计页「最近 30 天」口径一致）
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
  // 不影响统计页其他板块（轮询类接口 silent，不弹 toast 骚扰）
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

  // issue #478：来源分布统计由 60s 轮询改为 SSE 事件驱动刷新——数据
  // 来自本地 tasks 表聚合（后端 10s TTL 缓存），task 事件即数据唯一
  // 变化来源；连接建立/断线重连（onOpen）全量兜底刷新一次
  useGlobalEvents((ev) => {
    if (ev && ev.type === 'task') load()
  }, { onOpen: () => load() })

  // 挂载即加载一次（初始数据；事件驱动负责后续变化，onOpen 重连兜底）
  useEffect(() => {
    load()
  }, [load])

  const bySource = data?.by_source || []
  const taskCount = data?.overview?.task_count || 0

  return (
    <section className="stats-source-section">
      <h2>{tr('stats.sourceStatsTitle')}</h2>
      <p className="muted">
        {tr('stats.sourceStatsDesc')}
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
          <p className="muted">{tr('stats.sourceStatsEmpty')}</p>
        </div>
      ) : (
        <div className="stats-source-cards">
          {bySource.map((s) => (
            <div className="stats-source-card" key={s.key}>
              <span className="stats-source-name"
                    title={tr('stats.sourceStatsTitle')}>{s.name || '—'}</span>
              <span className="stats-source-count">{s.task_count}</span>
              <span className="muted small">
                {tr('stats.sourceTaskCount', { n: s.task_count })}
              </span>
              <span className="stats-source-rate">
                <b>{pct(s.success_rate)}</b>
                <span className="muted small">{tr('stats.sourceSuccessRate')}</span>
              </span>
              <span className="muted small">
                {tr('stats.sourceAvgDuration',
                    { duration: fmtSeconds(s.avg_duration_seconds) || '—' })}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
