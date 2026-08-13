import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtTime, fmtDuration, shortSha, STATUS_META } from '../api.js'

export default function Tasks() {
  const [data, setData] = useState({ tasks: [], total: 0, stats: {} })
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [repos, setRepos] = useState([])
  const [repoId, setRepoId] = useState('')
  const [error, setError] = useState('')
  const [detailTask, setDetailTask] = useState(null) // 正在查看详细失败原因的任务
  const [stopMsg, setStopMsg] = useState('') // 一键停止成功提示（issue #35）
  const [stopping, setStopping] = useState(false) // 停止请求进行中
  const [retryMsg, setRetryMsg] = useState('') // 手动重试成功提示（issue #36）
  const [retryId, setRetryId] = useState(null) // 正在重试的任务 id（请求中禁用）
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

  // 活跃任务数（issue #35）：排队 + 执行 + 重试
  const activeCount =
    (data.stats?.queued || 0) + (data.stats?.running || 0) + (data.stats?.retrying || 0)

  // 一键停止所有任务（issue #35）：确认后调后端批量停止，刷新列表
  const stopAll = async () => {
    if (!window.confirm(
      `确定停止所有正在执行的任务吗？当前 ${activeCount} 个活跃任务（排队/执行/重试）将被标记为已中断，执行中的 claude 进程会被强制终止。`)) {
      return
    }
    setStopping(true)
    setStopMsg('')
    try {
      const r = await api.post('/api/tasks/stop-all')
      setStopMsg(`已停止 ${r.count} 个任务`)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setStopping(false)
    }
  }

  // 手动重试任务（issue #36）：确认后重新入队执行（接续上次 claude 会话），刷新列表
  const retryTask = async (t) => {
    if (!window.confirm(
      `确定重试任务 #${t.id}（issue #${t.issue_iid} ${t.issue_title || ''}）吗？任务将重新入队执行，并接续上次 claude 会话继续处理。`)) {
      return
    }
    setRetryId(t.id)
    setRetryMsg('')
    try {
      await api.post(`/api/tasks/${t.id}/retry`)
      setRetryMsg(`任务 #${t.id} 已重新入队，开始重试`)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setRetryId(null)
    }
  }

  return (
    <div>
      <h1>任务列表</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {stopMsg && <div className="alert alert-ok" onClick={() => setStopMsg('')}>{stopMsg}</div>}
      {retryMsg && <div className="alert alert-ok" onClick={() => setRetryMsg('')}>{retryMsg}</div>}

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
          {/* 一键停止所有任务（issue #35）：高危操作，需确认；无活跃任务或请求中禁用 */}
          <button
            className="btn btn-danger"
            onClick={stopAll}
            disabled={activeCount === 0 || stopping}
            title={activeCount === 0 ? '当前没有正在执行的任务' : '停止所有排队中、执行中、重试中的任务'}
          >
            ⏹ 停止所有任务{activeCount > 0 ? `（${activeCount}）` : ''}
          </button>
        </div>

        {/* 12 列表格最小宽度超容器，外包滚动容器防止窄视口下内容溢出卡片（issue #28） */}
        <div className="table-wrap">
          {/* table-layout: fixed 固定布局，表格宽度恒等于容器宽度，宽视口不出现水平滚动条（issue #28 第二轮） */}
          <table className="table tasks-table">
            <thead>
              <tr>
                <th>#</th><th>仓库</th><th>Issue</th><th>标题</th>
                <th>状态</th><th>尝试</th><th>来源</th><th>失败原因</th><th>提交</th><th>创建时间</th><th>用时</th><th>操作</th>
              </tr>
            </thead>
          <tbody>
            {data.tasks.length === 0 && (
              <tr><td colSpan={12} className="muted">暂无任务</td></tr>
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
                  <td className="ellipsis" title={t.repo_name}>{t.repo_name || '—'}</td>
                  <td><Link to={`/tasks/${t.id}`}>#{t.issue_iid}</Link></td>
                  <td className="ellipsis" title={t.issue_title}>{t.issue_title || '—'}</td>
                  <td><span className={'badge ' + meta.cls}>{meta.label}</span></td>
                  <td>
                    {t.attempt_count}
                    {t.resumed && (
                      <span className="badge resume" title="从上次中断的 claude 会话恢复执行（断点续跑）">恢复</span>
                    )}
                  </td>
                  <td>{t.triggered_by === 'reconcile' ? '对账' : t.triggered_by === 'manual' ? '手动' : 'webhook'}</td>
                  <td className="ellipsis" title={failedReason}>
                    {failedReason || <span className="muted">—</span>}
                    {hasDetail && (
                      <button className="btn btn-mini btn-gap-left" onClick={() => setDetailTask(t)}>详情</button>
                    )}
                  </td>
                  <td className="ellipsis" title={t.commit_sha ? `查看提交 ${t.commit_sha}` : undefined}>
                    {t.commit_url ? (
                      <a href={t.commit_url} target="_blank" rel="noreferrer"
                         title={`查看提交 ${t.commit_sha}`}>{shortSha(t.commit_sha)}</a>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{fmtTime(t.created_at)}</td>
                  <td>{fmtDuration(t.started_at || t.created_at, t.finished_at) || <span className="muted">—</span>}</td>
                  <td>
                    <Link to={`/tasks/${t.id}?live=1`} className="btn btn-mini"
                          title="实时查看 agent 执行进度与聊天记录（issue #20）">执行</Link>
                    {/* 手动重试（issue #36）：仅失败/中断任务可重试，请求中禁用防重复点击 */}
                    {(t.status === 'failed' || t.status === 'interrupted') && (
                      <button
                        className="btn btn-mini btn-gap-left"
                        onClick={() => retryTask(t)}
                        disabled={retryId === t.id}
                        title="手动重试：重新入队执行该任务（接续上次 claude 会话）"
                      >
                        {retryId === t.id ? '重试中…' : '重试'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          </table>
        </div>
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
