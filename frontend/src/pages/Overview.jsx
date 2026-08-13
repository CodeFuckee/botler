import { useCallback, useEffect, useRef, useState } from 'react'
import { api, STATUS_META } from '../api.js'

// 概览页展示的活跃任务状态（issue #32）：执行中 + 重试中
export const LIVE_STATUSES = ['running', 'retrying']

// 每张卡片保留的实时输出行数（超出丢弃最旧行，防止卡片无限增长）
export const MAX_CARD_LINES = 40

// 任务列表与实时输出轮询间隔（与任务详情页实时执行一致）
export const OVERVIEW_POLL_MS = 3000

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

  useEffect(() => {
    load()
    const t = setInterval(load, OVERVIEW_POLL_MS)
    return () => clearInterval(t)
  }, [load])

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
    </div>
  )
}
