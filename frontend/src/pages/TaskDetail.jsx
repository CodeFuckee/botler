import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { api, openTaskEventStream, fmtTime, fmtDuration, shortSha, STATUS_META, summarizeToolInput } from '../api.js'

// 任务仍可能产出新日志/聊天的状态（活跃期间持续轮询）
const LIVE_STATUSES = ['queued', 'running', 'retrying']

function LiveMsg({ m }) {
  // 聊天消息渲染：用户 / 助手文本 / 工具调用 / 工具结果（issue #20）。
  // 被后端截断的长消息渲染显式标记（issue #90），不再被误认为数据丢失
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
        {m.truncated && <span className="muted small">（内容过长，已截断）</span>}
      </div>
    )
  }
  const cls = m.role === 'assistant' ? 'chat-assistant' : 'chat-user'
  return (
    <div className={'chat-msg ' + cls}>
      <span className="pre-wrap">{m.text}</span>
      {m.truncated && <span className="muted small">（内容过长，已截断）</span>}
      {m.ts && <span className="chat-ts">{fmtTime(m.ts)}</span>}
    </div>
  )
}

// ---- 实时事件流（SSE 推送）：逐事件展示引擎执行过程 ----

function EventRow({ e }) {
  // 归一化事件渲染：thinking（默认折叠）/ 文本 / 工具调用 / 工具结果 /
  // 状态（session/model 等）/ 结果摘要
  if (e.kind === 'thinking') {
    return (
      <details className="event-row event-thinking">
        <summary>💭 思考过程</summary>
        <span className="pre-wrap">{e.text}</span>
      </details>
    )
  }
  if (e.kind === 'tool') {
    return (
      <div className="event-row chat-msg chat-tool">
        <span className="chat-tool-name">🔧 {e.tool}</span>
        <code>{summarizeToolInput(e.input, e.tool)}</code>
      </div>
    )
  }
  if (e.kind === 'tool_result') {
    return (
      <div className={'event-row chat-msg chat-tool-result' + (e.is_error ? ' chat-tool-error' : '')}>
        {e.is_error && <span className="badge chat-err-badge">失败</span>}
        <span className="pre-wrap">{e.text || '（无输出）'}</span>
      </div>
    )
  }
  if (e.kind === 'status') {
    const parts = []
    if (e.session_id) parts.push(`session ${e.session_id.slice(0, 8)}…`)
    if (e.model) parts.push(e.model)
    if (e.cwd) parts.push(e.cwd)
    if (e.message) parts.push(e.message)
    return <div className="event-row event-status muted small">{parts.join(' · ')}</div>
  }
  if (e.kind === 'result') {
    return <div className="event-row event-result pre-wrap">🏁 {e.result}</div>
  }
  return <div className="event-row pre-wrap">{e.text}</div>
}

function EventList({ events }) {
  // 事件流完整渲染（issue #52）：不做虚拟化、无内部垂直滚动条，
  // 所有事件直线完整展示，滚动交给页面最外层
  return (
    <div className="event-list">
      {events.length === 0 && <p className="muted">暂无事件（任务尚未开始执行）</p>}
      {events.map((e, i) => <EventRow key={e.seq ?? i} e={e} />)}
    </div>
  )
}

// 区块折叠标题（issue #52）：事件流/聊天记录/执行日志标题为可点击
// 切换按钮，区块内容直线完整展示，滚动交给页面最外层
function SectionToggle({ open, onClick, level, children }) {
  return (
    <button type="button" className={'section-toggle section-toggle-' + level}
            aria-expanded={open} onClick={onClick}>
      <span className="chevron">{open ? '▾' : '▸'}</span>
      {children}
    </button>
  )
}

export default function TaskDetail() {
  const { id } = useParams()
  const location = useLocation()
  const [task, setTask] = useState(null)
  const [error, setError] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [showFileLog, setShowFileLog] = useState(true)
  // 区块折叠状态（issue #52）：事件流/聊天记录/执行日志默认展开
  const [showEvents, setShowEvents] = useState(true)
  const [showChat, setShowChat] = useState(true)
  const [showLogs, setShowLogs] = useState(true)
  // 实时执行面板（issue #20）：live 为 null 表示尚未拉取过
  const [live, setLive] = useState(null)
  const [liveDone, setLiveDone] = useState(false)
  const liveRef = useRef({ offset: 0, lines: [], transcript: [], sessionId: null,
                           prompt: null, transcriptTruncated: false })
  // 事件流（SSE 实时输出）：逐事件展示引擎执行过程；终态任务连接后
  // 后端回放全部历史事件再发 done，前端按 seq 去重（断线重连回放
  // 重叠不重复渲染）
  const [events, setEvents] = useState([])
  const [eventDone, setEventDone] = useState(false)
  const lastSeqRef = useRef(0)

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

  // 实时执行轮询（3s）：日志增量（字节偏移续读）+ 聊天记录（全量替换）。
  // 事件流改走 SSE（下方独立 effect），此处仅保留 transcript 部分
  const pollLive = useCallback(async () => {
    const cur = liveRef.current
    try {
      const d = await api.get(`/api/tasks/${id}/execution?after_byte=${cur.offset}`)
      cur.offset = d.log_offset
      cur.lines = cur.lines.concat(d.log_delta)
      if (d.transcript?.length) cur.transcript = d.transcript
      if (d.prompt) cur.prompt = d.prompt // issue #90：提示词全文懒加载
      cur.transcriptTruncated = d.transcript_truncated
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

  // 事件流 SSE 订阅：挂载即连接（终态任务由后端回放历史后 done 收尾）。
  // EventSource 断线自动重连，后端重新回放，seq 去重保证不重复渲染
  useEffect(() => {
    lastSeqRef.current = 0
    setEvents([])
    setEventDone(false)
    const es = openTaskEventStream(Number(id), {
      onEvent: (ev) => {
        setEvents((prev) => {
          if (typeof ev.seq === 'number') {
            if (ev.seq <= lastSeqRef.current) return prev
            lastSeqRef.current = ev.seq
          }
          return prev.concat(ev)
        })
      },
      onDone: () => setEventDone(true),
    })
    return () => es.close()
  }, [id])

  // 从列表页「执行」按钮跳转（?live=1）时滚动到实时面板
  useEffect(() => {
    if (new URLSearchParams(location.search).get('live') === '1' && live) {
      document.getElementById('live-panel')?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [location.search, live])

  if (error) return <div className="alert alert-error">{error}</div>
  // HIG 匠心：加载态用 spinner，非裸文本
  if (!task) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

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
            {/* 处理用时（issue #49）：系统接收时间 created_at → bot-done 打标时间
                finished_at 的动态计算，不再用执行开始时间 started_at 作起点 */}
            <tr><th>处理用时</th><td>{fmtDuration(task.created_at, task.finished_at) || <span className="muted">—</span>}</td></tr>
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
          // issue #90：提示词全文来自 execution 懒加载（会话文件首条 user
          // 消息），会话文件不可读时回退占位文案
          <pre className="log-view log-view-flat">{live?.prompt || '（提示词未持久化，见执行日志）'}</pre>
        )}
      </div>

      <div className="card" id="live-panel">
        <h2>
          实时执行
          {live?.sessionId && <code className="muted small live-session">session {live.sessionId.slice(0, 8)}…</code>}
        </h2>
        <SectionToggle open={showEvents} level="h3"
                       onClick={() => setShowEvents(!showEvents)}>
          事件流
          {!eventDone && <span className="muted small">（实时推送）</span>}
        </SectionToggle>
        {showEvents && <EventList events={events} />}
        <SectionToggle open={showChat} level="h3"
                       onClick={() => setShowChat(!showChat)}>
          聊天记录
          {!liveDone && live && <span className="muted small">（每 3 秒自动刷新）</span>}
        </SectionToggle>
        {showChat && (
          !live ? (
            <p className="muted">加载中…</p>
          ) : (
            <div className="chat-list">
              {live.transcript.length === 0 && (
                <p className="muted">
                  {live.sessionId ? '暂无聊天消息' : '暂无聊天记录（会话尚未开始或会话文件不可读）'}
                </p>
              )}
              {live.transcriptTruncated && (
                // issue #90：消息数量超上限时后端只保留首条提示词与最近消息
                <p className="muted small">⚠️ 聊天记录过多，仅显示首条提示词与最近消息</p>
              )}
              {live.transcript.map((m, i) => <LiveMsg key={i} m={m} />)}
            </div>
          )
        )}
      </div>

      <div className="card">
        <SectionToggle open={showLogs} level="h2"
                       onClick={() => setShowLogs(!showLogs)}>
          执行日志
        </SectionToggle>
        {showLogs && (
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
        )}
      </div>

      {task.log_file_tail && (
        <div className="card">
          <button className="btn" aria-expanded={showFileLog}
                  onClick={() => setShowFileLog(!showFileLog)}>
            {showFileLog ? '▾ 收起' : '▸ 展开'} claude 输出尾部
          </button>
          {showFileLog && <pre className="log-view log-view-flat">{task.log_file_tail}</pre>}
        </div>
      )}
    </div>
  )
}
