// 任务执行详情右边栏（issue #167）：概览页 issue 右边栏「查看执行的
// 详情」按钮点击后弹出的第二个右边栏——展示该 issue 的任务执行详情。
//
// 数据流：
// - 打开时拉取 GET /api/issues/{project_id}/{iid}/tasks（该 issue 的
//   全部任务记录，id 倒序最新在前），默认选中最新一条，可切换查看
//   历史任务（重新指派/对账补入队/手动重试会产生多条任务记录）；
// - 选中任务后拉取 GET /api/tasks/{task_id}（详情 + 日志行 + 执行
//   日志文件尾部）、GET /api/tasks/{task_id}/execution（聊天记录 +
//   实时输出增量，任务活跃期间每 3 秒轮询刷新）、SSE 事件流
//   /api/tasks/{task_id}/events（逐事件展示执行过程，终态任务回放
//   历史后 done 收尾，seq 去重防断线重连重复渲染）。
//
// 交互约定（与 IssueDrawer 一致）：
// - 关闭方式：右上角 × / 点击遮罩 / Esc 键（Esc 只关本层，不误关
//   下层 issue 抽屉——IssueDrawer 检测到本层打开时不再响应 Esc）；
// - 底部「查看完整任务页」跳转既有任务详情路由（/tasks/{id}）。
import { useCallback, useEffect, useRef, useState } from 'react'
import { Icon } from './Icon.jsx'
import { Link } from 'react-router-dom'
import { api, fmtTime, fmtDuration, shortSha, STATUS_META, summarizeToolInput } from '../api.js'

// 任务仍可能产出新日志/聊天的活跃状态（与任务详情页一致，活跃期间轮询）
const LIVE_STATUSES = ['queued', 'running', 'retrying']

// 任务状态 → 徽章映射兜底（复用 api.STATUS_META）
export function taskStatusMeta(status) {
  return STATUS_META[status] || { label: status || '—', cls: '' }
}

// 事件流归一化渲染（与任务详情页 EventRow 同语义的精简版）：
// thinking（默认折叠）/ 文本 / 工具调用 / 工具结果 / 状态 / 结果摘要
export function renderEvent(e) {
  if (!e || typeof e !== 'object' || !e.kind) return null
  if (e.kind === 'thinking') {
    return (
      <details className="event-row event-thinking">
        <summary><Icon name="brain" /> 思考过程</summary>
        <span className="pre-wrap">{e.text}</span>
      </details>
    )
  }
  if (e.kind === 'tool') {
    return (
      <div className="event-row chat-msg chat-tool">
        <span className="chat-tool-name"><Icon name="wrench" /> {e.tool}</span>
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
    return <div className="event-row event-result pre-wrap"><Icon name="flag" /> {e.result}</div>
  }
  return <div className="event-row pre-wrap">{e.text}</div>
}

// 聊天消息渲染（与任务详情页 LiveMsg 同语义的精简版）：
// 用户/助手文本、工具调用、工具结果（截断标记沿用 issue #90 约定）
export function renderChatMessage(m) {
  if (!m || typeof m !== 'object' || !m.role) return null
  if (m.role === 'tool') {
    return (
      <div className="chat-msg chat-tool">
        <span className="chat-tool-name"><Icon name="wrench" /> {m.tool}</span>
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

export default function TaskDetailDrawer({ projectId, issueIid, issueTitle,
                                           repoName, onClose }) {
  // 任务列表：tasks null=加载中；listErr 非空=加载失败（错误横幅 + 重试）
  const [tasks, setTasks] = useState(null)
  const [listErr, setListErr] = useState('')
  // 选中任务 id：null 未选（默认自动选最新一条）；task 选中任务详情；
  // taskErr 详情加载失败；loadingTask 详情请求中
  const [selectedId, setSelectedId] = useState(null)
  const [task, setTask] = useState(null)
  const [taskErr, setTaskErr] = useState('')
  // 实时执行数据（聊天记录/实时输出，轮询增量续读）：live null=未拉取
  const [live, setLive] = useState(null)
  // 事件流（SSE）：events 事件数组、eventDone 是否已 done 收尾
  const [events, setEvents] = useState([])
  const [eventDone, setEventDone] = useState(false)
  const lastSeqRef = useRef(0)
  const liveRef = useRef({ offset: 0, transcript: [], prompt: null, sessionId: null })

  // 打开/重试时加载该 issue 的任务列表，默认选中最新一条
  const loadList = useCallback(async () => {
    setTasks(null)
    setListErr('')
    try {
      const d = await api.get(`/api/issues/${projectId}/${issueIid}/tasks`)
      const list = Array.isArray(d && d.tasks) ? d.tasks : []
      setTasks(list)
      // 默认选中最新一条（列表已按 id 倒序）；无任务保持未选中
      setSelectedId((prev) => prev ?? (list.length > 0 ? list[0].id : null))
    } catch (e) {
      setListErr(e.message || '加载失败')
      setTasks([])
    }
  }, [projectId, issueIid])

  useEffect(() => { loadList() }, [loadList])

  // 选中任务变化：拉详情 + 执行数据，重置事件流并重连 SSE
  const taskId = selectedId
  const loadTask = useCallback(async () => {
    if (taskId == null) return
    setTask(null)
    setTaskErr('')
    try {
      setTask(await api.get(`/api/tasks/${taskId}`))
    } catch (e) {
      setTaskErr(e.message || '加载失败')
    }
  }, [taskId])

  const pollExecution = useCallback(async () => {
    if (taskId == null) return
    const cur = liveRef.current
    try {
      const d = await api.get(`/api/tasks/${taskId}/execution?after_byte=${cur.offset}`)
      cur.offset = d.log_offset
      if (Array.isArray(d.transcript) && d.transcript.length > 0) {
        cur.transcript = d.transcript
      }
      if (d.prompt) cur.prompt = d.prompt
      if (d.session_id) cur.sessionId = d.session_id
      setLive({ ...cur })
    } catch {
      // 执行数据尽力而为：拉取失败不阻塞其他区块（与任务详情页一致）
    }
  }, [taskId])

  // 任务切换时重置轮询续读游标与执行数据
  useEffect(() => {
    if (taskId == null) return
    liveRef.current = { offset: 0, transcript: [], prompt: null, sessionId: null }
    setLive(null)
    loadTask()
    pollExecution()
    // 活跃任务每 3 秒续读（增量）；任务终态后停止（下一轮拉取发现
    // 非活跃状态即不再起新轮询——由下方依赖 task 状态的 effect 决定）
    // 这里先立即拉一次，轮询频率交由 task.status 决定
  }, [taskId, loadTask, pollExecution])

  // 按任务状态起轮询：活跃任务每 3 秒续读执行数据，终态/加载失败即停
  useEffect(() => {
    if (taskId == null) return
    const status = task && task.status
    if (!status || !LIVE_STATUSES.includes(status)) return
    const t = setInterval(pollExecution, 3000)
    return () => clearInterval(t)
  }, [taskId, task && task.status, pollExecution])

  // 事件流 SSE：切换任务重连；终态任务由后端回放历史后 done 收尾，
  // 前端按 seq 去重（断线重连回放重叠不重复渲染）
  useEffect(() => {
    if (taskId == null) return
    lastSeqRef.current = 0
    setEvents([])
    setEventDone(false)
    const es = api.openTaskEventStream(Number(taskId), {
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
  }, [taskId])

  // Esc 关闭本层抽屉（SSR 测试环境无 document 时跳过，与 IssueDrawer 一致）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (e && e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const meta = task ? taskStatusMeta(task.status) : null
  const activeTask = task && LIVE_STATUSES.includes(task.status)

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer task-detail-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong className="issue-drawer-title">
            任务执行详情 — #{issueIid} {issueTitle || '—'}
          </strong>
          <span className="issue-drawer-actions">
            <button className="btn modal-close" onClick={onClose} title="关闭"
                    aria-label="关闭任务执行详情右边栏"><Icon name="x" /></button>
          </span>
        </div>

        {listErr && (
          <div className="issue-drawer-error" role="alert">
            {listErr}
            <button type="button" className="btn btn-small tasks-retry"
                    onClick={loadList} title="重新加载任务列表">重试</button>
          </div>
        )}

        {/* 任务列表：null=加载中；空=该 issue 从未执行；非空=可切换查看 */}
        <div className="task-detail-list">
          {tasks === null ? (
            <p className="muted">加载任务记录…</p>
          ) : tasks.length === 0 ? (
            <p className="muted">该 issue 暂无任务执行记录</p>
          ) : (
            <ul className="task-detail-list-items">
              {tasks.map((t) => {
                const m = taskStatusMeta(t.status)
                const selected = t.id === selectedId
                return (
                  <li key={t.id}>
                    <button type="button"
                            className={'task-detail-item' + (selected ? ' task-detail-item-selected' : '')}
                            onClick={() => setSelectedId(t.id)}
                            title={`查看任务 #${t.id} 执行详情`}>
                      <span className="task-detail-item-id">#{t.id}</span>
                      <span className={'badge ' + m.cls}>{m.label}</span>
                      {t.engine && (
                        <span className="task-detail-item-engine"
                              title="任务执行引擎">{t.engine}</span>
                      )}
                      <span className="muted small task-detail-item-time"
                            title="任务创建时间">{fmtTime(t.created_at)}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        {/* 选中任务详情 */}
        {selectedId != null && (
          <div className="task-detail-body">
            {taskErr && (
              <div className="issue-drawer-error" role="alert">
                {taskErr}
                <button type="button" className="btn btn-small tasks-retry"
                        onClick={loadTask} title="重新加载任务详情">重试</button>
              </div>
            )}
            {!task && !taskErr && (
              <p className="muted">加载任务详情…</p>
            )}
            {task && (
              <>
                <div className="task-detail-nav">
                  <span className={'badge ' + meta.cls}>{meta.label}</span>
                  {activeTask && <span className="muted small">（实时刷新中）</span>}
                  <Link className="btn btn-mini task-detail-full-link"
                        to={`/tasks/${task.id}`}
                        title="打开完整任务详情页">查看完整任务页</Link>
                </div>
                <table className="table kv">
                  <tbody>
                    <tr><th>任务</th><td>#{task.id}</td></tr>
                    <tr><th>仓库</th><td>{task.repo_name || repoName || '—'}</td></tr>
                    <tr><th>Issue</th><td>#{task.issue_iid} — {task.issue_title || '—'}</td></tr>
                    <tr><th>执行引擎</th><td>{task.engine || '—'}</td></tr>
                    <tr><th>来源</th><td>{task.triggered_by === 'reconcile' ? '对账兜底' : task.triggered_by === 'manual' ? '手动' : 'Webhook'}</td></tr>
                    <tr><th>尝试次数</th><td>{task.attempt_count}</td></tr>
                    <tr><th>退出码</th><td>{task.exit_code ?? '—'}</td></tr>
                    <tr><th>创建时间</th><td>{fmtTime(task.created_at)}</td></tr>
                    <tr><th>开始时间</th><td>{fmtTime(task.started_at)}</td></tr>
                    <tr><th>完成时间</th><td>{fmtTime(task.finished_at)}</td></tr>
                    <tr><th>处理用时</th><td>{fmtDuration(task.created_at, task.finished_at) || <span className="muted">—</span>}</td></tr>
                    <tr><th>提交</th><td>
                      {task.commit_url ? (
                        <a href={task.commit_url} target="_blank" rel="noreferrer"
                           title={`完整 sha: ${task.commit_sha}`}>
                          {shortSha(task.commit_sha)} <Icon name="externalLink" />
                        </a>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td></tr>
                    {/* issue #281：dsh 会话 id 任务开始即落库，中断恢复凭此 id
                       经 DeepSeek Harness SDK resume；仅 dsh 引擎任务有值 */}
                    <tr><th>dsh 会话</th><td>{task.dsh_session_id ? <code title={`完整会话 id: ${task.dsh_session_id}`}>{task.dsh_session_id.slice(0, 12)}…</code> : <span className="muted">—</span>}</td></tr>
                    {task.error_message && (
                      <tr><th>错误信息</th><td className="pre-wrap">{task.error_message}</td></tr>
                    )}
                  </tbody>
                </table>

                <div className="issue-notes-block">
                  <h3>事件流
                    {!eventDone && activeTask && <span className="muted small">（实时推送）</span>}
                  </h3>
                  <div className="event-list">
                    {events.length === 0 && (
                      <p className="muted">暂无事件（任务尚未开始执行）</p>
                    )}
                    {events.map((e, i) => (
                      <div key={e.seq ?? i}>{renderEvent(e)}</div>
                    ))}
                  </div>
                </div>

                <div className="issue-notes-block">
                  <h3>聊天记录
                    {activeTask && <span className="muted small">（每 3 秒自动刷新）</span>}
                  </h3>
                  {live === null ? (
                    <p className="muted">加载中…</p>
                  ) : live.transcript.length === 0 ? (
                    <p className="muted">
                      {live.sessionId ? '暂无聊天消息' : '暂无聊天记录（会话尚未开始或会话文件不可读）'}
                    </p>
                  ) : (
                    <div className="chat-list">
                      {live.transcript.map((m, i) => (
                        <div key={i}>{renderChatMessage(m)}</div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="issue-notes-block">
                  <h3>执行日志</h3>
                  {task.logs && task.logs.length === 0 ? (
                    <p className="muted">暂无日志</p>
                  ) : (
                    <div className="log-list">
                      {(task.logs || []).map((l) => (
                        <div key={l.id} className={'log-line log-' + l.level}>
                          <span className="log-ts">{fmtTime(l.ts)}</span>
                          <span className="log-level">{l.level}</span>
                          <span>{l.message}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {task.log_file_tail && (
                    <details className="task-detail-file-tail">
                      <summary><Icon name="chevronRight" /> 展开执行日志文件尾部</summary>
                      <pre className="log-view log-view-flat">{task.log_file_tail}</pre>
                    </details>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
