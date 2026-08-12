import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { api, fmtTime, fmtDuration, shortSha, STATUS_META, summarizeToolInput } from '../api.js'

// 任务仍可能产出新日志/聊天的状态（活跃期间持续轮询）
const LIVE_STATUSES = ['queued', 'running', 'retrying']

function LiveMsg({ m }) {
  // 聊天消息渲染：用户 / 助手文本 / 工具调用 / 工具结果（issue #20）
  if (m.role === 'tool') {
    return (
      <div className="chat-msg chat-tool">
        <span className="chat-tool-name">🔧 {m.tool}</span>
        <code>{summarizeToolInput(m.input, m.tool)}</code>
      </div>
    )
  }
  if (m.role === 'tool_result') {
    return (
      <div className={'chat-msg chat-tool-result' + (m.tool_error ? ' chat-tool-error' : '')}>
        {m.tool_error && <span className="badge chat-err-badge">失败</span>}
        <span className="pre-wrap">{m.text || '（无输出）'}</span>
      </div>
    )
  }
  const cls = m.role === 'assistant' ? 'chat-assistant' : 'chat-user'
  return (
    <div className={'chat-msg ' + cls}>
      <span className="pre-wrap">{m.text}</span>
      {m.ts && <span className="chat-ts">{fmtTime(m.ts)}</span>}
    </div>
  )
}

export default function TaskDetail() {
  const { id } = useParams()
  const location = useLocation()
  const [task, setTask] = useState(null)
  const [error, setError] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [showFileLog, setShowFileLog] = useState(true)
  // 实时执行面板（issue #20）：live 为 null 表示尚未拉取过
  const [live, setLive] = useState(null)
  const [liveDone, setLiveDone] = useState(false)
  const liveRef = useRef({ offset: 0, lines: [], transcript: [], sessionId: null })
  const logRef = useRef(null)

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

  // 实时执行轮询（3s）：日志增量（字节偏移续读）+ 聊天记录（全量替换）
  const pollLive = useCallback(async () => {
    const cur = liveRef.current
    try {
      const d = await api.get(`/api/tasks/${id}/execution?after_byte=${cur.offset}`)
      cur.offset = d.log_offset
      cur.lines = cur.lines.concat(d.log_delta)
      if (d.transcript?.length) cur.transcript = d.transcript
      if (d.session_id) cur.sessionId = d.session_id
      setLive({ ...cur })
      if (!LIVE_STATUSES.includes(d.status)) setLiveDone(true)
    } catch {
      setLiveDone(true) // 拉取失败停止轮询（不阻塞页面其他内容）
    }
  }, [id])

  useEffect(() => {
    if (liveDone) return
    pollLive()
    const t = setInterval(pollLive, 3000)
    return () => clearInterval(t)
  }, [id, liveDone, pollLive])

  // 实时日志自动滚动到底部
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [live?.lines])

  // 从列表页「执行」按钮跳转（?live=1）时滚动到实时面板
  useEffect(() => {
    if (new URLSearchParams(location.search).get('live') === '1' && live) {
      document.getElementById('live-panel')?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [location.search, live])

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
            <tr><th>执行用时</th><td>{fmtDuration(task.started_at || task.created_at, task.finished_at) || <span className="muted">—</span>}</td></tr>
            <tr>
              <th>提交</th>
              <td>
                {task.commit_url ? (
                  <a href={task.commit_url} target="_blank" rel="noreferrer"
                     title={`完整 sha: ${task.commit_sha}`}>
                    {shortSha(task.commit_sha)} ↗
                  </a>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
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

      <div className="card" id="live-panel">
        <h2>
          实时执行
          {live?.sessionId && <code className="muted small live-session">session {live.sessionId.slice(0, 8)}…</code>}
          {!liveDone && live && <span className="muted small">（每 3 秒自动刷新）</span>}
        </h2>
        {!live ? (
          <p className="muted">加载中…</p>
        ) : (
          <>
            <div className="chat-list">
              {live.transcript.length === 0 && (
                <p className="muted">
                  {live.sessionId ? '暂无聊天消息' : '暂无聊天记录（会话尚未开始或会话文件不可读）'}
                </p>
              )}
              {live.transcript.map((m, i) => <LiveMsg key={i} m={m} />)}
            </div>
            <details className="live-log-block">
              <summary>实时输出（{live.lines.length} 行）</summary>
              <pre className="log-view live-log" ref={logRef}>
                {live.lines.join('\n') || '（暂无输出）'}
              </pre>
            </details>
          </>
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
