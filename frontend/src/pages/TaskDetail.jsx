import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, fmtTime, STATUS_META } from '../api.js'

export default function TaskDetail() {
  const { id } = useParams()
  const [task, setTask] = useState(null)
  const [error, setError] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [showFileLog, setShowFileLog] = useState(true)

  const load = async () => {
    try {
      setTask(await api.get(`/api/tasks/${id}`))
    } catch (e) { setError(e.message) }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5000) // 执行中自动刷新
    return () => clearInterval(t)
  }, [id])

  if (error) return <div className="alert alert-error">{error}</div>
  if (!task) return <p className="muted">加载中…</p>

  const meta = STATUS_META[task.status] || { label: task.status, cls: '' }

  return (
    <div>
      <h1>
        任务 #{task.id}
        <span className={'badge ' + meta.cls}>{meta.label}</span>
      </h1>
      <p className="muted">
        <Link to="/tasks">← 返回任务列表</Link>
      </p>

      <div className="card">
        <table className="table kv">
          <tbody>
            <tr><th>仓库</th><td>{task.repo_name || '（已删除）'}（project_id={task.project_id}）</td></tr>
            <tr><th>Issue</th><td>#{task.issue_iid} — {task.issue_title || '—'}</td></tr>
            <tr><th>来源</th><td>{task.triggered_by === 'reconcile' ? '对账兜底' : 'Webhook'}</td></tr>
            <tr><th>尝试次数</th><td>{task.attempt_count}</td></tr>
            <tr><th>退出码</th><td>{task.exit_code ?? '—'}</td></tr>
            <tr><th>创建时间</th><td>{fmtTime(task.created_at)}</td></tr>
            <tr><th>开始时间</th><td>{fmtTime(task.started_at)}</td></tr>
            <tr><th>完成时间</th><td>{fmtTime(task.finished_at)}</td></tr>
            {task.error_message && (
              <tr><th>错误信息</th><td className="pre-wrap">{task.error_message}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card">
        <button className="btn" onClick={() => setShowPrompt(!showPrompt)}>
          {showPrompt ? '收起' : '查看'}提示词
        </button>
        {showPrompt && (
          <pre className="log-view">{task.prompt || '（提示词未持久化，见执行日志）'}</pre>
        )}
      </div>

      <div className="card">
        <h2>执行日志</h2>
        <div className="log-list">
          {task.logs.length === 0 && <p className="muted">暂无日志</p>}
          {task.logs.map((l) => (
            <div key={l.id} className={'log-line log-' + l.level}>
              <span className="log-ts">{fmtTime(l.ts)}</span>
              <span className="log-level">{l.level}</span>
              <span>{l.message}</span>
            </div>
          ))}
        </div>
      </div>

      {task.log_file_tail && (
        <div className="card">
          <button className="btn" onClick={() => setShowFileLog(!showFileLog)}>
            {showFileLog ? '收起' : '展开'} claude 输出尾部
          </button>
          {showFileLog && <pre className="log-view">{task.log_file_tail}</pre>}
        </div>
      )}
    </div>
  )
}
