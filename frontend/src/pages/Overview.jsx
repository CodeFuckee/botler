import { useCallback, useEffect, useState } from 'react'
import { api, STATUS_META, shortSha, fmtTime, fmtAgo, summarizeToolInput } from '../api.js'
import IssueDrawer from '../components/IssueDrawer.jsx'

// 概览页展示的活跃任务状态（issue #32）：执行中 + 重试中
export const LIVE_STATUSES = ['running', 'retrying']

// 每张卡片保留的实时输出行数（超出丢弃最旧行，防止卡片无限增长）
export const MAX_CARD_LINES = 40

// 任务列表轮询间隔
export const OVERVIEW_POLL_MS = 3000

// 流水线状态轮询间隔（issue #39）：比任务轮询慢——流水线变化不频繁，
// 且后端有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const PIPELINE_POLL_MS = 15000

// 开放 issue 聚合轮询间隔（issue #64）：与流水线板块同频，后端同样
// 有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const ISSUE_POLL_MS = 15000

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

// ---- issue #80：开放 issue 按 bot 终态标签分组 + 状态徽章 ----
// bot-done = bot 已完成开发待用户确认；bot-failed = bot 处理失败待人工
// 介入。判定优先级 bot-done 高于 bot-failed：失败后重试成功时两个标签
// 会并存（executor 幂等 add_label 不移除旧标签），成功为最终态。
export const BOT_STATUS_NAMES = new Set(['bot-done', 'bot-failed'])

// bot 状态 → 标题旁徽章文案与样式类（复用任务状态徽章的弱底语义色风格）
export const BOT_STATUS_META = {
  done: { label: '✅ bot-done', cls: 'issue-status-done',
          hint: 'bot 已完成开发，待人工确认关闭' },
  failed: { label: '❌ bot-failed', cls: 'issue-status-failed',
            hint: 'bot 处理失败，需人工介入' },
}

// 组显示顺序：用户指定 bot-failed → bot-done → 其他（issue #80 评论区）
export const ISSUE_GROUPS = [
  { key: 'failed', title: '❌ bot-failed', hint: 'bot 处理失败，需人工介入' },
  { key: 'done', title: '✅ bot-done', hint: 'bot 已完成开发，待人工确认关闭' },
  { key: 'other', title: '📋 其他', hint: '尚未处理或处理中的 issue' },
]

// 提取 issue 的 bot 终态键（done/failed），无则 null。labels 元素可能
// 缺 name 或非对象（旧缓存/异常数据），逐一防御
export function botStatusKey(issue) {
  const labels = issue && issue.labels
  if (!Array.isArray(labels)) return null
  let hasFailed = false
  for (const l of labels) {
    const name = l && typeof l === 'object' ? l.name : null
    if (name === 'bot-done') return 'done'
    if (name === 'bot-failed') hasFailed = true
  }
  return hasFailed ? 'failed' : null
}

// 按 bot 终态标签分组：{ failed, done, other }。组内保持原始相对顺序
// （后端已按 updated_at 降序），前端不重排
export function groupIssuesByBotLabel(issues) {
  const groups = { failed: [], done: [], other: [] }
  for (const i of Array.isArray(issues) ? issues : []) {
    groups[botStatusKey(i) || 'other'].push(i)
  }
  return groups
}

// 日志行尾部截取：总行数超过 max 时只保留最后 max 行
export function trimLogTail(lines, max) {
  if (!Array.isArray(lines)) return []
  if (!Number.isFinite(max) || max <= 0) return []
  return lines.length > max ? lines.slice(lines.length - max) : lines
}

// 事件 → 卡片单行文本（实时输出 SSE 事件流；status 事件跳过，卡片空间有限）
export function eventToLine(e) {
  if (!e || typeof e !== 'object') return ''
  if (e.kind === 'thinking') return `💭 ${e.text || ''}`
  if (e.kind === 'tool') return `🔧 ${e.tool} ${summarizeToolInput(e.input, e.tool)}`
  if (e.kind === 'tool_result') return e.text || '（无输出）'
  if (e.kind === 'result') return `🏁 ${e.result || ''}`
  if (e.kind === 'status') return ''
  return e.text || ''
}

export default function Overview() {
  const [tasks, setTasks] = useState([])
  const [liveLines, setLiveLines] = useState({}) // taskId -> 实时输出行数组
  const [error, setError] = useState('')
  // 流水线状态（issue #39）：所有配置仓库（含未启用，第二轮）的最新 CI/CD 流水线
  const [pipelines, setPipelines] = useState([])
  const [pipeErrors, setPipeErrors] = useState([])
  const [pipeError, setPipeError] = useState('')
  // 开放 issue 聚合（issue #64）：已启用仓库的开放 issue，按仓库优先级排序
  const [repoIssues, setRepoIssues] = useState([])
  const [issueErrors, setIssueErrors] = useState([])
  const [issueError, setIssueError] = useState('')
  // 详情右边栏选中的 issue（issue #85）：{issue, repoName}，null 表示关闭
  const [selectedIssue, setSelectedIssue] = useState(null)
  // 任务集合签名：任务增删 / 状态变化时重建事件流连接
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

  // 各卡片事件流（SSE 实时输出）：每个活跃任务一个 EventSource，事件
  // 转单行文本 append 到卡片（trimLogTail 截尾）。seq 去重防断线重连
  // 回放重复；任务集合变化（tasksKey）时重建全部连接
  useEffect(() => {
    if (tasks.length === 0) return
    const streams = tasks.map((t) => {
      let lastSeq = 0
      const es = api.openTaskEventStream(t.id, {
        onEvent: (ev) => {
          if (typeof ev.seq === 'number') {
            if (ev.seq <= lastSeq) return
            lastSeq = ev.seq
          }
          const line = eventToLine(ev)
          if (!line) return
          setLiveLines((prev) => ({
            ...prev,
            [t.id]: trimLogTail((prev[t.id] || []).concat(line), MAX_CARD_LINES),
          }))
        },
      })
      return es
    })
    return () => streams.forEach((es) => es.close())
  }, [tasksKey])

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

  // 已启用仓库的开放 issue 聚合（issue #64，独立慢轮询）
  const loadIssues = useCallback(async () => {
    try {
      const d = await api.get('/api/issues/overview')
      setRepoIssues(d.repos || [])
      setIssueErrors(d.errors || [])
      setIssueError('')
    } catch (e) {
      setIssueError(e.message)
    }
  }, [])

  useEffect(() => {
    loadIssues()
    const t = setInterval(loadIssues, ISSUE_POLL_MS)
    return () => clearInterval(t)
  }, [loadIssues])

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

      {/* issue #68：板块排序调整——开放 Issue 置于页面顶部，
          其后依次为运行中任务、CI/CD 流水线 */}
      <section className="issues-section">
        <h2>开放 Issue</h2>
        <p className="muted">已启用仓库的开放 issue，按仓库优先级排序（每 {ISSUE_POLL_MS / 1000} 秒自动刷新）</p>
        {issueError && (
          <div className="alert alert-error" onClick={() => setIssueError('')}>{issueError}</div>
        )}
        {issueErrors.length > 0 && (
          <div className="alert alert-error">
            {issueErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        {repoIssues.length === 0 || repoIssues.every((r) => !r.issues || r.issues.length === 0) ? (
          <p className="muted">暂无开放 issue</p>
        ) : (
          <div className="issues-list">
            {repoIssues.map((r) => (
              <div key={r.repo_id} className="card issue-repo-card">
                <div className="issue-repo-head">
                  <span className="issue-repo-name" title="仓库">📁 {r.repo_name || '（已删除）'}</span>
                  <span className="badge badge-muted" title="仓库优先级：数字越小越优先">
                    优先级 {r.priority ?? 100}
                  </span>
                  <span className="muted">{r.issues.length} 个开放 issue</span>
                </div>
                {(r.issues || []).length === 0 ? (
                  <p className="muted">该仓库暂无开放 issue</p>
                ) : (
                  /* issue #80：按 bot 终态标签分组（bot-failed / bot-done /
                     其他），只渲染非空组，组标题带计数 */
                  ISSUE_GROUPS.map((g) => {
                    const items = groupIssuesByBotLabel(r.issues)[g.key]
                    if (items.length === 0) return null
                    return (
                      <div key={g.key} className="issue-group">
                        <div className="issue-group-head">
                          <span className="issue-group-title" title={g.hint}>{g.title}</span>
                          <span className="issue-group-count"
                                title="组内 issue 数量">{items.length} 个</span>
                        </div>
                        <ul className="issue-list">
                          {items.map((i) => {
                            const bot = botStatusKey(i)
                            const statusMeta = bot ? BOT_STATUS_META[bot] : null
                            // issue #80：终态标签由状态徽章替代展示，其余标签保留胶囊
                            const otherLabels = (i.labels || []).filter(
                              (l) => l && !BOT_STATUS_NAMES.has(l.name))
                            return (
                              <li key={i.iid} className="issue-item">
                                {/* issue #71：参考 GitLab issue 列表页布局——左列编号+标题+
                                    标签/里程碑胶囊，右列 assignee 头像+更新时间+评论数
                                    issue #85：标题改为按钮——点击打开右边栏，不再直接
                                    跳转 GitLab（跳转统一走右边栏右上角按钮） */}
                                <div className="issue-main">
                                  <button type="button" className="issue-link"
                                          onClick={() => setSelectedIssue({
                                            issue: i, repoName: r.repo_name,
                                          })}
                                          title="查看 issue 详情">
                                    <span className="issue-iid">#{i.iid}</span>
                                    {statusMeta && (
                                      <span className={`issue-status ${statusMeta.cls}`}
                                            title={statusMeta.hint}>{statusMeta.label}</span>
                                    )}
                                    {i.title || '—'}
                                  </button>
                                  {(otherLabels.length > 0 || i.milestone) && (
                                    <div className="issue-meta">
                                      {otherLabels.map((l) => (
                                        <span key={l.name} className="label-pill"
                                              style={l.color
                                                ? { background: `#${l.color}`, color: `#${l.text_color}` }
                                                : undefined}
                                              title={`标签 ${l.name}`}>{l.name}</span>
                                      ))}
                                      {i.milestone && (
                                        <span className="milestone-chip" title={`里程碑 ${i.milestone}`}>
                                          🏷️ {i.milestone}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                                <div className="issue-side">
                                  {(i.assignees || []).map((a) => (
                                    a.avatar_url ? (
                                      <img key={a.username || a.name}
                                           className="assignee-avatar" src={a.avatar_url}
                                           alt={a.name || a.username || ''}
                                           title={`负责人 ${a.name || a.username || ''}`} />
                                    ) : (
                                      <span key={a.username || a.name}
                                            className="assignee-avatar avatar-fallback"
                                            title={`负责人 ${a.name || a.username || ''}`}>
                                        {(a.name || a.username || '?').slice(0, 1).toUpperCase()}
                                      </span>
                                    )
                                  ))}
                                  {i.updated_at && (
                                    <span className="issue-updated" title="最后更新时间">
                                      {fmtAgo(i.updated_at) || ''}
                                    </span>
                                  )}
                                  {typeof i.user_notes_count === 'number' && (
                                    <span className="issue-notes" title="评论数">
                                      💬 {i.user_notes_count}
                                    </span>
                                  )}
                                </div>
                              </li>
                            )
                          })}
                        </ul>
                      </div>
                    )
                  })
                )}
              </div>
            ))}
          </div>
        )}
      </section>

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
                      {/* 最近流水线对应提交的提交时间 + 距今多久（issue #43） */}
                      {p.commit_time && (
                        <span className="pipeline-commit-time">
                          {fmtTime(p.commit_time)}（{fmtAgo(p.commit_time) || '—'}）
                        </span>
                      )}
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

      {/* issue #85：issue 详情右边栏——点击列表项打开，显示具体信息与正文 */}
      {selectedIssue && (
        <IssueDrawer issue={selectedIssue.issue} repoName={selectedIssue.repoName}
                     onClose={() => setSelectedIssue(null)} />
      )}
    </div>
  )
}
