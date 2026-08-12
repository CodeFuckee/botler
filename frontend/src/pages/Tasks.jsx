import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtTime, shortSha, STATUS_META } from '../api.js'

export default function Tasks() {
  const [data, setData] = useState({ tasks: [], total: 0, stats: {} })
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [repos, setRepos] = useState([])
  const [repoId, setRepoId] = useState('')
  const [error, setError] = useState('')
  const [detailTask, setDetailTask] = useState(null) // 正在查看详细失败原因的任务
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
              <th>状态</th><th>尝试</th><th>来源</th><th>失败原因</th><th>提交</th><th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            {data.tasks.length === 0 && (
              <tr><td colSpan={10} className="muted">暂无任务</td></tr>
            )}
            {data.tasks.map((t) => {
              const meta = STATUS_META[t.status] || { label: t.status, cls: '' }
              // 仅失败/中断的任务展示失败原因
              const failedReason =
                (t.status === 'failed' || t.status === 'interrupted') && t.error_message
                  ? t.error_message
                  : ''
              // 详细失败原因（每次尝试 + trace）不直接展示，通过按钮弹窗查看
              const hasDetail = Array.isArray(t.error_detail?.attempts) && t.error_detail.attempts.length > 0
              return (
                <tr key={t.id}>
                  <td><Link to={`/tasks/${t.id}`}>#{t.id}</Link></td>
                  <td>{t.repo_name || '—'}</td>
                  <td><Link to={`/tasks/${t.id}`}>#{t.issue_iid}</Link></td>
                  <td className="ellipsis" title={t.issue_title}>{t.issue_title || '—'}</td>
                  <td><span className={'badge ' + meta.cls}>{meta.label}</span></td>
                  <td>
                    {t.attempt_count}
                    {t.resumed && (
                      <span className="badge resume" title="从上次中断的 claude 会话恢复执行（断点续跑）">恢复</span>
                    )}
                  </td>
                  <td>{t.triggered_by === 'reconcile' ? '对账' : 'webhook'}</td>
                  <td className="ellipsis" title={failedReason}>
                    {failedReason || <span className="muted">—</span>}
                    {hasDetail && (
                      <button className="btn btn-mini" onClick={() => setDetailTask(t)}>详情</button>
                    )}
                  </td>
                  <td>
                    {t.commit_url ? (
                      <a href={t.commit_url} target="_blank" rel="noreferrer"
                         title={`查看提交 ${t.commit_sha}`}>{shortSha(t.commit_sha)}</a>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{fmtTime(t.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="muted small">共 {data.total} 条（最多显示 50 条）</p>
      </div>

      {detailTask && (
        <div className="modal-overlay" onClick={() => setDetailTask(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong>失败详细原因 — #{detailTask.issue_iid} {detailTask.issue_title}</strong>
              <button className="btn modal-close" onClick={() => setDetailTask(null)} title="关闭">×</button>
            </div>
            {detailTask.error_message && (
              <div className="error-summary">
                <strong>摘要：</strong>
                <span className="pre-wrap">{detailTask.error_message}</span>
              </div>
            )}
            <div className="error-detail-body">
              {(detailTask.error_detail?.attempts || []).map((a) => (
                <div key={a.attempt} className="error-attempt">
                  <div className="error-attempt-head">
                    <span>第 {a.attempt} 次尝试</span>
                    <code>退出码: {a.exit_code ?? '—'}</code>
                  </div>
                  <pre className="error-attempt-trace">{a.error || '（无输出）'}</pre>
                </div>
              ))}
              {!detailTask.error_detail?.attempts?.length && (
                <p className="muted">该任务没有更详细的失败记录</p>
              )}
            </div>
            <div className="modal-footer">
              {detailTask.log_path && <code className="muted small">日志文件: {detailTask.log_path}</code>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
