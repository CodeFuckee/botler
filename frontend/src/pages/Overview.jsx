import { useCallback, useEffect, useRef, useState } from 'react'
import { api, STATUS_META, shortSha } from '../api.js'

// 概览页展示的活跃任务状态（issue #32）：执行中 + 重试中
export const LIVE_STATUSES = ['running', 'retrying']

// 每张卡片保留的实时输出行数（超出丢弃最旧行，防止卡片无限增长）
export const MAX_CARD_LINES = 40

// 任务列表与实时输出轮询间隔（与任务详情页实时执行一致）
export const OVERVIEW_POLL_MS = 3000

// 流水线状态轮询间隔（issue #39）：比任务轮询慢——流水线变化不频繁，
// 且后端有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const PIPELINE_POLL_MS = 15000

// 流水线整体状态 → 徽章映射（issue #39）。样式类复用任务状态徽章
// status-*（视觉语义一致：成功绿 / 失败红 / 运行蓝 / 其余灰）
export const PIPELINE_STATUS_META = {
  success: { label: '成功', cls: 'status-succeeded' },
  failed: { label: '失败', cls: 'status-failed' },
  running: { label: '运行中', cls: 'status-running' },
  pending: { label: '等待中', cls: 'status-queued' },
  created: { label: '已创建', cls: 'status-queued' },
  canceled: { label: '已取消', cls: 'status-interrupted' },
  skipped: { label: '已跳过', cls: 'status-interrupted' },
  manual: { label: '手动', cls: 'status-queued' },
}

// stage 状态 → 节点样式类（参考 GitLab CI/CD 阶段图颜色语义）
export function stageClass(status) {
  switch (status) {
    case 'success': return 'st-success'
    case 'failed': return 'st-failed'
    case 'running': return 'st-running'
    case 'canceled': return 'st-canceled'
    case 'skipped': return 'st-skipped'
    default: return 'st-pending' // pending/created/未知统一按待运行展示
  }
}

// 日志行尾部截取：总行数超过 max 时只保留最后 max 行
export function trimLogTail(lines, max) {
  if (!Array.isArray(lines)) return []
  if (!Number.isFinite(max) || max <= 0) return []
  return lines.length > max ? lines.slice(lines.length - max) : lines
}

export default function Overview() {
  const [tasks, setTasks] = useState([])
  const [liveLines, setLiveLines] = useState({}) // taskId -> 实时输出行数组
  const [error, setError] = useState('')
  // 流水线状态（issue #39）：所有配置仓库（含未启用，第二轮）的最新 CI/CD 流水线
  const [pipelines, setPipelines] = useState([])
  const [pipeErrors, setPipeErrors] = useState([])
  const [pipeError, setPipeError] = useState('')
  const liveRef = useRef(new Map()) // taskId -> { offset }（日志字节偏移续读）
  // 任务集合签名：任务增删 / 状态变化时重跑实时输出轮询 effect
  const tasksKey = tasks.map((t) => `${t.id}:${t.status}`).sort().join('|')

  // 拉取全部正在执行的任务（running+retrying 多值过滤，issue #32）
  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ status: 'running,retrying', limit: '200' })
      const d = await api.get('/api/tasks?' + q)
      setTasks(d.tasks || [])
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [])

  // 单个任务的实时输出增量轮询（after_byte 续读 + trimLogTail 截尾）
  const pollLive = useCallback(async (taskId) => {
    const cur = liveRef.current.get(taskId) || { offset: 0 }
    try {
      const d = await api.get(`/api/tasks/${taskId}/execution?after_byte=${cur.offset}`)
      cur.offset = d.log_offset
      liveRef.current.set(taskId, cur)
      setLiveLines((prev) => {
        const lines = trimLogTail((prev[taskId] || []).concat(d.log_delta || []), MAX_CARD_LINES)
        return { ...prev, [taskId]: lines }
      })
    } catch {
      // 单个卡片拉取失败忽略（列表轮询下一轮兜底重试，不阻塞页面）
    }
  }, [])

  // 所有配置仓库的最新流水线状态（issue #39，独立慢轮询）
  const loadPipelines = useCallback(async () => {
    try {
      const d = await api.get('/api/pipelines/overview')
      setPipelines(d.pipelines || [])
      setPipeErrors(d.errors || [])
      setPipeError('')
    } catch (e) {
      setPipeError(e.message)
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, OVERVIEW_POLL_MS)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    loadPipelines()
    const t = setInterval(loadPipelines, PIPELINE_POLL_MS)
    return () => clearInterval(t)
  }, [loadPipelines])

  useEffect(() => {
    if (tasks.length === 0) return
    const ids = tasks.map((t) => t.id)
    ids.forEach(pollLive)
    const t = setInterval(() => ids.forEach(pollLive), OVERVIEW_POLL_MS)
    return () => clearInterval(t)
  }, [tasksKey, pollLive])

  // 各卡片实时输出自动滚动到底部（SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    document.querySelectorAll('.overview-log').forEach((el) => {
      el.scrollTop = el.scrollHeight
    })
  }, [liveLines])

  return (
    <div>
      <h1>概览</h1>
      <p className="muted">正在执行的任务（每 {OVERVIEW_POLL_MS / 1000} 秒自动刷新）</p>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {tasks.length === 0 && !error ? (
        <p className="muted">当前没有正在执行的任务</p>
      ) : (
        <div className="overview-grid">
          {tasks.map((t) => {
            const lines = liveLines[t.id] || []
            const meta = STATUS_META[t.status] || { label: t.status, cls: '' }
            return (
              <div key={t.id} className="card overview-card">
                <div className="overview-card-head">
                  <span className="overview-repo" title="仓库">📁 {t.repo_name || '（已删除）'}</span>
                  <span className={'badge ' + meta.cls}>{meta.label}</span>
                </div>
                <div className="overview-issue">
                  {t.issue_url ? (
                    <a href={t.issue_url} target="_blank" rel="noreferrer"
                       title="在 GitLab 中打开 issue">
                      #{t.issue_iid} — {t.issue_title || '—'}
                    </a>
                  ) : (
                    <span>#{t.issue_iid} — {t.issue_title || '—'}</span>
                  )}
                </div>
                <pre className="log-view overview-log">
                  {lines.join('\n') || '（暂无输出）'}
                </pre>
              </div>
            )
          })}
        </div>
      )}

      <section className="pipelines-section">
        <h2>CI/CD 流水线</h2>
        <p className="muted">所有配置仓库的最新流水线（每 {PIPELINE_POLL_MS / 1000} 秒自动刷新）</p>
        {pipeError && (
          <div className="alert alert-error" onClick={() => setPipeError('')}>{pipeError}</div>
        )}
        {pipeErrors.length > 0 && (
          <div className="alert alert-error">
            {pipeErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        {pipelines.length === 0 ? (
          <p className="muted">暂无流水线</p>
        ) : (
          <div className="pipelines-list">
            {pipelines.map((p) => {
              const pl = p.pipeline
              const meta = pl
                ? (PIPELINE_STATUS_META[pl.status] || { label: pl.status, cls: '' })
                : null
              return (
                <div key={p.repo_id} className="card pipeline-card">
                  <div className="pipeline-head">
                    <span className="pipeline-repo" title="仓库">📁 {p.repo_name || '（已删除）'}</span>
                    {p.enabled === false && (
                      <span className="badge badge-muted" title="该仓库在 Botler 中未启用">未启用</span>
                    )}
                    {meta ? (
                      <span className={'badge ' + meta.cls}>{meta.label}</span>
                    ) : (
                      <span className="muted">暂无流水线</span>
                    )}
                  </div>
                  {pl && (
                    <a className="pipeline-link" href={pl.web_url} target="_blank"
                       rel="noreferrer" title="在 GitLab 中打开流水线">
                      <span className="pipeline-ref" title={`分支 ${pl.ref} · 提交 ${pl.sha}`}>
                        {pl.ref} · {shortSha(pl.sha)}
                      </span>
                      <div className="pipeline-stages">
                        {(p.stages || []).map((s, i) => (
                          <span key={i}
                                className={`pipeline-stage ${stageClass(s.status)}`}
                                title={`${s.name}: ${s.status}`}>
                            <span className="pipeline-stage-name">{s.name}</span>
                            <span className="pipeline-stage-dot" />
                          </span>
                        ))}
                      </div>
                    </a>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}
