import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtTime, STATUS_META } from '../api.js'

export default function Tasks() {
  const [data, setData] = useState({ tasks: [], total: 0, stats: {} })
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [repos, setRepos] = useState([])
  const [repoId, setRepoId] = useState('')
  const [error, setError] = useState('')
  const timer = useRef(null)

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ limit: '50' })
      if (status) q.set('status', status)
      if (search.trim()) q.set('search', search.trim())
      if (repoId) q.set('repo_id', repoId)
      const d = await api.get('/api/tasks?' + q)
      setData(d)
    } catch (e) {
      setError(e.message)
    }
  }, [status, search, repoId])

  // 有活跃任务时每 5s 自动刷新
  useEffect(() => {
    load()
    const active = data.stats?.queued + data.stats?.running + data.stats?.retrying
    if (active > 0 && !timer.current) {
      timer.current = setInterval(load, 5000)
    } else if (active === 0 && timer.current) {
      clearInterval(timer.current)
      timer.current = null
    }
    return () => { if (timer.current) { clearInterval(timer.current); timer.current = null } }
  }, [load, data.stats?.queued, data.stats?.running, data.stats?.retrying])

  useEffect(() => {
    api.get('/api/repos').then((d) => setRepos(d.repos)).catch(() => {})
  }, [])

  return (
    <div>
      <h1>任务列表</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}

      <div className="stats-row">
        {Object.entries(data.stats || {}).map(([k, v]) => (
          <div key={k} className="stat-chip">
            <span className={'status-dot ' + (STATUS_META[k]?.cls || '')} />
            {STATUS_META[k]?.label || k}: <b>{v}</b>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="form-row wrap">
          <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部状态</option>
            {Object.entries(STATUS_META).map(([k, v]) => (
              <option key={k} value={k}>{v.label}</option>
            ))}
          </select>
          <select className="input" value={repoId} onChange={(e) => setRepoId(e.target.value)}>
            <option value="">全部仓库</option>
            {repos.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <input
            className="input grow"
            placeholder="搜索 issue 标题或编号…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <table className="table">
          <thead>
            <tr>
              <th>#</th><th>仓库</th><th>Issue</th><th>标题</th>
              <th>状态</th><th>尝试</th><th>来源</th><th>失败原因</th><th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            {data.tasks.length === 0 && (
              <tr><td colSpan={9} className="muted">暂无任务</td></tr>
            )}
            {data.tasks.map((t) => {
              const meta = STATUS_META[t.status] || { label: t.status, cls: '' }
              // 仅失败/中断的任务展示失败原因
              const failedReason =
                (t.status === 'failed' || t.status === 'interrupted') && t.error_message
                  ? t.error_message
                  : ''
              return (
                <tr key={t.id}>
                  <td><Link to={`/tasks/${t.id}`}>#{t.id}</Link></td>
                  <td>{t.repo_name || '—'}</td>
                  <td><Link to={`/tasks/${t.id}`}>#{t.issue_iid}</Link></td>
                  <td className="ellipsis" title={t.issue_title}>{t.issue_title || '—'}</td>
                  <td><span className={'badge ' + meta.cls}>{meta.label}</span></td>
                  <td>{t.attempt_count}</td>
                  <td>{t.triggered_by === 'reconcile' ? '对账' : 'webhook'}</td>
                  <td className="ellipsis" title={failedReason}>
                    {failedReason || <span className="muted">—</span>}
                  </td>
                  <td>{fmtTime(t.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="muted small">共 {data.total} 条（最多显示 50 条）</p>
      </div>
    </div>
  )
}
