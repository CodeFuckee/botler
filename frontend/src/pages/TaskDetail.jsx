import { failureCategoryClass, failureCategoryLabel } from '../failure-categories.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import { usePolling } from '../hooks/usePolling.js'
import { Icon } from '../components/Icon.jsx'
import { Link, useLocation, useParams } from 'react-router-dom'
import { api, openTaskEventStream, fmtTime, fmtDuration, shortSha, STATUS_META, summarizeToolInput } from '../api.js'
import { confirmDialog } from '../dialog.js'
import UsageCard from '../components/UsageCard.jsx'

// 任务仍可能产出新日志/聊天的状态（活跃期间持续轮询）
const LIVE_STATUSES = ['queued', 'running', 'retrying']

function LiveMsg({ m }) {
  // 聊天消息渲染：用户 / 助手文本 / 工具调用 / 工具结果（issue #20）。
  // 被后端截断的长消息渲染显式标记（issue #90），不再被误认为数据丢失
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

// ---- 实时事件流（SSE 推送）：逐事件展示引擎执行过程 ----

function EventRow({ e, showThinking }) {
  // 归一化事件渲染：thinking（默认隐藏，勾选「显示思考过程」后展开显示，
  // issue #176）/ 文本 / 工具调用 / 工具结果 / 状态（session/model 等）/ 结果摘要
  if (e.kind === 'thinking') {
    // 思考过程默认隐藏（issue #176）：未勾选时整条不渲染
    if (!showThinking) return null
    return (
      <details open className="event-row event-thinking">
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

function EventList({ events, showThinking }) {
  // 事件流完整渲染（issue #52）：不做虚拟化、无内部垂直滚动条，
  // 所有事件直线完整展示，滚动交给页面最外层；thinking 事件是否
  // 渲染由「显示思考过程」开关（issue #176）控制
  return (
    <div className="event-list">
      {events.length === 0 && <p className="muted">暂无事件（任务尚未开始执行）</p>}
      {events.map((e, i) => <EventRow key={e.seq ?? i} e={e} showThinking={showThinking} />)}
    </div>
  )
}

// 区块折叠标题（issue #52）：事件流/聊天记录/执行日志标题为可点击
// 切换按钮，区块内容直线完整展示，滚动交给页面最外层
function SectionToggle({ open, onClick, level, children }) {
  return (
    <button type="button" className={'section-toggle section-toggle-' + level}
            aria-expanded={open} onClick={onClick}>
      <span className="chevron">{open ? <Icon name="chevronDown" /> : <Icon name="chevronRight" />}</span>
      {children}
    </button>
  )
}

// 执行环境快照（issue #276）：任务开始时采集的执行环境信息——引擎版本 /
// 模型 / 起始提交（分支 + sha）/ 平台版本（VersionBadge 同源）/ config 关键项
// hash / 采集时间。元信息区折叠面板展示；无快照显示「暂无环境快照」，采集
// 失败显示「环境快照获取失败」（任务照常执行，不阻塞）
function EnvSnapshot({ env }) {
  if (!env) {
    return <p className="muted">暂无环境快照（任务执行前未采集到环境信息）</p>
  }
  if (env.error) {
    return <p className="muted"><Icon name="warning" /> {env.error}（采集失败不影响任务执行）</p>
  }
  const rows = []
  if (env.engine) {
    rows.push(['引擎', `${env.engine.name || '—'}${env.engine.version ? ' ' + env.engine.version : ''}`])
  }
  if (env.model && env.model.name) {
    rows.push(['模型', env.model.name + (env.model.provider ? `（${env.model.provider}）` : '')])
  }
  if (env.git && (env.git.commit_sha || env.git.branch)) {
    rows.push(['起始提交', `${env.git.branch || '—'} · ${env.git.commit_sha ? shortSha(env.git.commit_sha) : '—'}`])
  }
  if (env.platform && env.platform.version) {
    rows.push(['平台版本', `v${env.platform.version}`])
  }
  if (env.config_hash) {
    rows.push(['配置哈希', <code key="config-hash">{env.config_hash}</code>])
  }
  if (env.captured_at) {
    rows.push(['采集时间', fmtTime(env.captured_at)])
  }
  if (rows.length === 0) return <p className="muted">环境快照无可用字段</p>
  return (
    <table className="table kv">
      <tbody>
        {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}
      </tbody>
    </table>
  )
}

export default function TaskDetail() {
  const { id } = useParams()
  const location = useLocation()
  const [task, setTask] = useState(null)
  const [error, setError] = useState('')
  // 单任务停止/重试操作反馈（issue #214）：成功/失败提示独立于加载错误
  // （加载错误走整页替换，操作错误走行内横幅不打断页面）
  const [actionMsg, setActionMsg] = useState('') // 操作成功提示
  const [actionErr, setActionErr] = useState('') // 操作失败提示
  const [stopping, setStopping] = useState(false) // 停止请求进行中
  const [retrying, setRetrying] = useState(false) // 重试请求进行中
  const [showPrompt, setShowPrompt] = useState(false)
  const [showFileLog, setShowFileLog] = useState(true)
  // 区块折叠状态（issue #52）：事件流/聊天记录/执行日志默认展开
  const [showEvents, setShowEvents] = useState(true)
  // 思考过程显示开关（issue #176）：事件流默认隐藏思考过程，勾选
  // 「显示思考过程」后以展开态显示 thinking 事件
  const [showThinking, setShowThinking] = useState(false)
  // 执行环境快照折叠面板（issue #276）：元信息区展示，默认展开可收起
  const [showEnv, setShowEnv] = useState(true)
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

  // 任务详情拉取（useCallback 稳定引用供 usePolling 使用；issue #200 起
  // 由 usePolling 统一管理 5s 轮询与页面可见性——隐藏暂停、恢复可见即刷）
  const load = useCallback(async () => {
    try {
      setTask(await api.get(`/api/tasks/${id}`, { silent: true }))
    } catch (e) { setError(e.message) }
  }, [id])

  usePolling(load, 5000)

  // 单任务停止（issue #214）：仅执行中（running）任务可停止；确认后调
  // 后端 POST /api/tasks/{id}/stop——状态落库 interrupted 并强制终止执行
  // 中的引擎进程（停止不可逆），成功后刷新详情。请求中禁用防重复点击。
  const stopTask = async () => {
    if (!task) return
    if (!(await confirmDialog({
      message: `确定停止任务 #${task.id}（issue #${task.issue_iid} ${task.issue_title || ''}）吗？执行中的引擎进程将被强制终止，任务状态标记为已中断，停止不可逆。`,
      danger: true,
    }))) {
      return
    }
    setStopping(true)
    setActionMsg('')
    setActionErr('')
    try {
      await api.post(`/api/tasks/${task.id}/stop`)
      setActionMsg(`任务 #${task.id} 已停止`)
      await load()
    } catch (e) {
      setActionErr(e.message)
    } finally {
      setStopping(false)
    }
  }

  // 手动重试（issue #214）：失败/中断任务重新入队执行，复用任务列表页
  // 手动重试（issue #36）的 POST /api/tasks/{id}/retry 逻辑（接续上次
  // claude 会话断点续跑）；确认后调用，成功后刷新详情。
  const retryTask = async () => {
    if (!task) return
    if (!(await confirmDialog({
      message: `确定重试任务 #${task.id}（issue #${task.issue_iid} ${task.issue_title || ''}）吗？任务将重新入队执行，并接续上次 claude 会话继续处理。`,
    }))) {
      return
    }
    setRetrying(true)
    setActionMsg('')
    setActionErr('')
    try {
      await api.post(`/api/tasks/${task.id}/retry`)
      setActionMsg(`任务 #${task.id} 已重新入队，开始重试`)
      await load()
    } catch (e) {
      setActionErr(e.message)
    } finally {
      setRetrying(false)
    }
  }

  // 实时执行轮询（3s）：日志增量（字节偏移续读）+ 聊天记录（全量替换）。
  // 事件流改走 SSE（下方独立 effect），此处仅保留 transcript 部分
  const pollLive = useCallback(async () => {
    const cur = liveRef.current
    try {
      const d = await api.get(`/api/tasks/${id}/execution?after_byte=${cur.offset}`, { silent: true })
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

  // 实时执行面板 3s 轮询（issue #200 起经 usePolling 统一管理可见性）：
  // liveDone 为 true（任务终态/拉取失败）时停止轮询
  usePolling(pollLive, 3000, { enabled: !liveDone })

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
        <Link to="/tasks"><Icon name="arrowLeft" /> 返回任务列表</Link>
      </p>

      {actionErr && <div className="alert alert-error" onClick={() => setActionErr('')}>{actionErr}</div>}
      {actionMsg && <div className="alert alert-ok" onClick={() => setActionMsg('')}>{actionMsg}</div>}

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
                    {shortSha(task.commit_sha)} <Icon name="externalLink" />
                  </a>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
            <tr>
              {/* issue #281：dsh 会话 id 任务开始即落库，中断恢复凭此 id
                  经 DeepSeek Harness SDK resume；仅 dsh 引擎任务有值 */}
              <th>dsh 会话</th>
              <td>
                {task.dsh_session_id ? (
                  <code title={`完整会话 id: ${task.dsh_session_id}`}>{task.dsh_session_id.slice(0, 12)}…</code>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
            {/* issue #274：失败任务展示失败原因分类徽章 + 处理建议
                 （env/engine/unsolvable/unknown；仅 failed/interrupted 且有
                 分类时显示，旧任务无分类不显示，不报错） */}
            {task.failure_category && (task.status === 'failed' || task.status === 'interrupted') && (
              <tr>
                <th>失败分类</th>
                <td>
                  <span className={`badge failure-cat ${failureCategoryClass(task.failure_category)}`}>
                    {failureCategoryLabel(task.failure_category)}
                  </span>
                  {task.failure_advice && (
                    <span className="muted failure-advice">{task.failure_advice}</span>
                  )}
                </td>
              </tr>
            )}
            {task.error_message && (
              <tr><th>错误信息</th><td className="pre-wrap">{task.error_message}</td></tr>
            )}
            {/* 操作（issue #214）：执行中任务可停止（不可逆，需确认）；
                失败/中断任务可重试（重新入队执行）；其余状态无操作 */}
            <tr>
              <th>操作</th>
              <td>
                {task.status === 'running' && (
                  <button
                    className="btn btn-mini btn-danger"
                    onClick={stopTask}
                    disabled={stopping}
                    title="手动停止：强制终止该任务的执行进程并标记为已中断（停止不可逆）"
                  >
                    {stopping ? '停止中…' : '停止'}
                  </button>
                )}
                {(task.status === 'failed' || task.status === 'interrupted') && (
                  <button
                    className="btn btn-mini"
                    onClick={retryTask}
                    disabled={retrying}
                    title="手动重试：重新入队执行该任务（接续上次 claude 会话）"
                  >
                    {retrying ? '重试中…' : '重试'}
                  </button>
                )}
                {task.status !== 'running' &&
                 task.status !== 'failed' && task.status !== 'interrupted' && (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Token 用量（issue #235）：引擎采集的模型调用 token 用量与估算
          费用卡片；无用量数据时显示「无数据」而不是报错 */}
      <UsageCard usage={task.usage} />

      {/* 执行环境快照（issue #276）：元信息区折叠面板，展示任务开始时
          采集的执行环境（引擎/模型/起始提交/平台版本/配置哈希） */}
      <div className="card">
        <SectionToggle open={showEnv} level="h2"
                       onClick={() => setShowEnv(!showEnv)}>
          执行环境快照
          {task.environment && !task.environment.error &&
            <span className="muted small">（引擎 / 模型 / 起始提交 / 配置）</span>}
        </SectionToggle>
        {showEnv && <EnvSnapshot env={task.environment} />}
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
        <div className="event-stream-header">
          <SectionToggle open={showEvents} level="h3"
                         onClick={() => setShowEvents(!showEvents)}>
            事件流
            {!eventDone && <span className="muted small">（实时推送）</span>}
          </SectionToggle>
          <label className="checkbox-label thinking-toggle">
            <input
              type="checkbox"
              checked={showThinking}
              onChange={(e) => setShowThinking(e.target.checked)}
            />
            显示思考过程
          </label>
        </div>
        {showEvents && <EventList events={events} showThinking={showThinking} />}
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
                <p className="muted small"><Icon name="warning" /> 聊天记录过多，仅显示首条提示词与最近消息</p>
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
            {showFileLog ? <><Icon name="chevronDown" /> 收起</> : <><Icon name="chevronRight" /> 展开</>} claude 输出尾部
          </button>
          {showFileLog && <pre className="log-view log-view-flat">{task.log_file_tail}</pre>}
        </div>
      )}
    </div>
  )
}
