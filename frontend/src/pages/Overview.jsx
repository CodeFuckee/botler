import { useCallback, useEffect, useState } from 'react'
import { api, STATUS_META, shortSha, fmtTime, fmtAgo, fmtSeconds, summarizeToolInput } from '../api.js'
import IssueDrawer, { ENGINE_META } from '../components/IssueDrawer.jsx'
import { Icon } from '../components/Icon.jsx'
import AddIssueModal from '../components/AddIssueModal.jsx'

// 概览页展示的活跃任务状态（issue #32）：执行中 + 重试中
export const LIVE_STATUSES = ['running', 'retrying']

// 每个任务信息块保留的实时输出行数（issue #114：任务信息随 issue 项
// 展示，超出丢弃最旧行，防止任务块无限增长）
export const MAX_CARD_LINES = 40

// 任务列表轮询间隔
export const OVERVIEW_POLL_MS = 3000

// 流水线状态轮询间隔（issue #39）：比任务轮询慢——流水线变化不频繁，
// 且后端有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const PIPELINE_POLL_MS = 15000

// 开放 issue 聚合轮询间隔（issue #64）：与流水线板块同频，后端同样
// 有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const ISSUE_POLL_MS = 15000

// 灵感板块轮询间隔（issue #131）：数据存 Botler 本地数据库，无 GitLab
// 请求压力，与开放 issue 板块同频（本地改动提交后手动刷新即时生效）
export const INSPIRATION_POLL_MS = 15000

// DeepSeek 账户余额轮询间隔（issue #138）：余额变化不频繁，60 秒低频
// 轮询 + 卡片内手动刷新按钮兜底（后端代调 deepseek user/balance）
export const DEEPSEEK_BALANCE_POLL_MS = 60000

// DeepSeek 开放平台充值页（issue #178）：余额卡片「去充值」链接按钮的跳转
// 目标，点击后在新标签页打开官方充值页，方便用户直接在 DeepSeek 页面充值
export const DEEPSEEK_TOPUP_URL = 'https://platform.deepseek.com/top_up'

// Issue 完成耗时统计轮询间隔（issue #180）：平均完成耗时与走势图数据
// 来自本地 tasks 表成功终态任务（GET /api/issues/completion-stats），
// 无 GitLab 请求压力，低频轮询即可（任务完成后再等下一轮刷新）
export const COMPLETION_STATS_POLL_MS = 60000

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
  done: { label: 'bot-done', icon: 'checkCircle', cls: 'issue-status-done',
          hint: 'bot 已完成开发，待人工确认关闭' },
  failed: { label: 'bot-failed', icon: 'xCircle', cls: 'issue-status-failed',
            hint: 'bot 处理失败，需人工介入' },
}

// 组显示顺序：运行中 → bot-failed → bot-done → 其他。运行中置顶
// （issue #101）；其后沿用用户指定 bot-failed → bot-done → 其他
// （issue #80 评论区）
export const ISSUE_GROUPS = [
  { key: 'running', title: '运行中', icon: 'settings', hint: '正在被 bot 执行的 issue，置顶展示' },
  { key: 'failed', title: 'bot-failed', icon: 'xCircle', hint: 'bot 处理失败，需人工介入' },
  { key: 'done', title: 'bot-done', icon: 'checkCircle', hint: 'bot 已完成开发，待人工确认关闭' },
  { key: 'other', title: '其他', icon: 'clipboard', hint: '尚未处理或处理中的 issue' },
]

// ---- issue #99：正在运行的 issue 高亮 ----
// 正在执行的任务（running/retrying）与开放 issue 列表按 repo_id+issue_iid
// 匹配，命中项高亮并显示「运行中」徽章。任务数据复用概览页已有轮询，
// 任务结束从列表消失后高亮自动消失。键统一字符串化，避免数字/字符串
// 类型差异导致匹配失败；Set 天然去重（同一 issue 的重复任务记录）
export function runningIssueKeys(tasks) {
  const keys = new Set()
  if (!Array.isArray(tasks)) return keys
  for (const t of tasks) {
    if (!t || !LIVE_STATUSES.includes(t.status)) continue
    if (t.repo_id == null || t.issue_iid == null) continue
    keys.add(`${t.repo_id}:${t.issue_iid}`)
  }
  return keys
}

// ---- issue #114：正在运行任务的信息整合进开放 issue 列表 ----
// 概览页独立任务板块删除后，任务信息（状态徽章 / 执行引擎 / 实时
// 输出）随对应 issue 项展示。匹配规则与 runningIssueKeys 一致：
// LIVE_STATUSES 状态 + repo_id:issue_iid 键字符串化，跨仓库同 iid
// 不误匹配。同一 issue 可能有多条任务记录（如重试产生的新任务），
// 按任务列表原始顺序全部返回，逐一渲染任务信息块
export function tasksForIssue(tasks, repoId, iid) {
  if (!Array.isArray(tasks)) return []
  const key = `${repoId}:${iid}`
  const out = []
  for (const t of tasks) {
    if (!t || !LIVE_STATUSES.includes(t.status)) continue
    if (t.repo_id == null || t.issue_iid == null) continue
    if (`${t.repo_id}:${t.issue_iid}` === key) out.push(t)
  }
  return out
}

// 任务执行引擎展示文案：空值/空白返回空串（旧任务未落库引擎时不
// 展示引擎信息）；未知值原样兜底。与 IssueDrawer.engineDisplay 同
// 文案源（ENGINE_META），但列表数据来自任务轮询、无加载态
export function engineLabel(raw) {
  const v = raw == null ? '' : String(raw).trim()
  if (!v) return ''
  const key = v.toLowerCase()
  return (ENGINE_META[key] || {}).label || v
}

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

// 按 bot 终态标签分组：{ running, failed, done, other }。issue #101：
// 正在运行的 issue（任务 running/retrying 命中 runningKeys）独立成
// running 组置顶展示，优先于终态标签分组；任务结束键消失后自动回落
// 原分组。组内保持原始相对顺序（后端已按 updated_at 降序），前端不重排
export function groupIssuesByBotLabel(issues, runningKeys, repoId) {
  const groups = { running: [], failed: [], done: [], other: [] }
  for (const i of Array.isArray(issues) ? issues : []) {
    // 运行中判定优先于终态标签（重试中的 bot-failed 等一并置顶）
    if (runningKeys && typeof runningKeys.has === 'function'
        && runningKeys.has(`${repoId}:${i.iid}`)) {
      groups.running.push(i)
      continue
    }
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
  if (e.kind === 'thinking') return <><Icon name="brain" /> {e.text || ''}</>
  if (e.kind === 'tool') return <><Icon name="wrench" /> {e.tool} {summarizeToolInput(e.input, e.tool)}</>
  if (e.kind === 'tool_result') return e.text || '（无输出）'
  if (e.kind === 'result') return <><Icon name="flag" /> {e.result || ''}</>
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
  // issue #132：owner token 是否已配置——概览页 issue 编辑必须使用 owner
  // token（未配置时后端直接 400 拦截，绝不回退 code01 身份发布）。null=
  // 检测中，false=未配置（显示醒目提示），true=已配置/检测失败
  const [ownerTokenOk, setOwnerTokenOk] = useState(null)
  // 详情右边栏选中的 issue（issue #85）：{issue, repoName}，null 表示关闭
  const [selectedIssue, setSelectedIssue] = useState(null)
  // 添加 issue 弹窗（issue #92）：打开的仓库卡片数据，null 表示关闭
  const [addIssueRepo, setAddIssueRepo] = useState(null)
  // 对账结果: repoId -> {loading/scanned/enqueued/note/error}（issue #134）
  const [reconcileResults, setReconcileResults] = useState({})
  // 灵感（issue #131）：概览页「灵感」板块——按仓库随手记录新功能灵感，
  // 仅保存在 Botler 本地数据库，不提交到 GitLab issue
  const [inspirationRepos, setInspirationRepos] = useState([])
  const [inspirationError, setInspirationError] = useState('')
  // 各仓库卡片「新灵感」输入草稿（repo_id -> 内容）
  const [newInspirationDrafts, setNewInspirationDrafts] = useState({})
  // 正在编辑的灵感：{id, repo_id, content}，null 表示未在编辑
  const [editingInspiration, setEditingInspiration] = useState(null)
  // 编辑框草稿内容
  const [editInspirationDraft, setEditInspirationDraft] = useState('')
  // 一键提交为 GitLab issue 的灵感 id 集合（issue #143）：请求中按钮
  // 禁用防重复提交
  const [addingIssueInspIds, setAddingIssueInspIds] = useState({})
  // 创建成功的 issue 对象（issue #143）：非空时显示成功提示与新 issue 链接
  const [inspirationCreatedIssue, setInspirationCreatedIssue] = useState(null)
  // 灵感 AI 对话（issue #166）：与 AI agent 探讨灵感——当前对话的灵感
  // 对象（null=面板关闭）、消息列表、输入草稿、发送中/加载中/错误状态
  const [chatInspiration, setChatInspiration] = useState(null)
  const [chatMessages, setChatMessages] = useState([])
  const [chatLoading, setChatLoading] = useState(false)
  const [chatDraft, setChatDraft] = useState('')
  const [chatSending, setChatSending] = useState(false)
  const [chatError, setChatError] = useState('')
  // DeepSeek 账户余额（issue #138）：null=未加载/未配置；
  // {configured, balance, error} 为 /api/settings/deepseek-balance 返回
  const [dsBalance, setDsBalance] = useState(null)
  // 余额接口请求失败（网络/后端异常）时的错误文案
  const [dsBalanceError, setDsBalanceError] = useState('')
  // Issue 完成耗时统计（issue #180）：null=加载中；{completed_count,
  // avg_seconds, trend} 为 /api/issues/completion-stats 返回
  const [completionStats, setCompletionStats] = useState(null)
  const [completionStatsError, setCompletionStatsError] = useState('')
  // 任务集合签名：任务增删 / 状态变化时重建事件流连接
  const tasksKey = tasks.map((t) => `${t.id}:${t.status}`).sort().join('|')

  // issue #99：正在运行的 issue 匹配键集合（repo_id:iid），任务每 3 秒
  // 轮询刷新，任务结束（消失）后对应 issue 高亮自动消失
  const runningKeys = runningIssueKeys(tasks)

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

  // issue #132：owner token 配置状态（启动时检测一次；配置变化由设置页
  // 保存后手动刷新页面生效）
  useEffect(() => {
    api.get('/api/settings')
      .then((d) => setOwnerTokenOk(!!(d.gitlab && d.gitlab.owner_token_masked)))
      .catch(() => setOwnerTokenOk(true)) // 读取失败不打扰编辑（后端自会拦截）
  }, [])

  // 灵感聚合（issue #131）：本地数据库数据，独立慢轮询；本地增删改
  // 提交成功后手动刷新，轮询兜底多标签页并发场景
  const loadInspirations = useCallback(async () => {
    try {
      const d = await api.get('/api/inspirations/overview')
      setInspirationRepos(d.repos || [])
      setInspirationError('')
    } catch (e) {
      setInspirationError(e.message)
    }
  }, [])

  useEffect(() => {
    loadInspirations()
    const t = setInterval(loadInspirations, INSPIRATION_POLL_MS)
    return () => clearInterval(t)
  }, [loadInspirations])

  // DeepSeek 账户余额（issue #138）：后端代调 deepseek user/balance，
  // API Key 明文不流转到前端（与 ai_providers 掩码同安全策略）；
  // 未配置时后端返回 configured=false，前端不渲染余额卡片
  const loadDeepSeekBalance = useCallback(async () => {
    try {
      const d = await api.get('/api/settings/deepseek-balance')
      setDsBalance(d || { configured: false, balance: null, error: null })
      setDsBalanceError('')
    } catch (e) {
      // 余额展示尽力而为：接口失败不打扰页面，保留上次数据（未加载
      // 成功过则 dsBalance 保持 null，卡片不渲染）
      setDsBalanceError(e.message)
    }
  }, [])

  useEffect(() => {
    loadDeepSeekBalance()
    const t = setInterval(loadDeepSeekBalance, DEEPSEEK_BALANCE_POLL_MS)
    return () => clearInterval(t)
  }, [loadDeepSeekBalance])

  // 已完成 issue 平均耗时与逐日走势（issue #180，独立低频轮询）：数据
  // 来自本地 tasks 表成功终态任务，无 GitLab 请求压力；接口失败保留
  // 上次数据并展示错误提示，不影响页面其他板块
  const loadCompletionStats = useCallback(async () => {
    try {
      const d = await api.get('/api/issues/completion-stats')
      setCompletionStats(d || { completed_count: 0, avg_seconds: null, trend: [] })
      setCompletionStatsError('')
    } catch (e) {
      setCompletionStatsError(e.message)
    }
  }, [])

  useEffect(() => {
    loadCompletionStats()
    const t = setInterval(loadCompletionStats, COMPLETION_STATS_POLL_MS)
    return () => clearInterval(t)
  }, [loadCompletionStats])

  // ---- 灵感增删改（issue #131）：仅写 Botler 本地数据库 ----

  // 记录新灵感：内容去首尾空白，空内容不发请求；成功后清草稿并刷新列表
  const submitNewInspiration = useCallback(async (repoId) => {
    const content = (newInspirationDrafts[repoId] || '').trim()
    if (!content) return
    try {
      await api.post('/api/inspirations', { repo_id: repoId, content })
      setNewInspirationDrafts((prev) => ({ ...prev, [repoId]: '' }))
      await loadInspirations()
    } catch (e) {
      setInspirationError(e.message)
    }
  }, [newInspirationDrafts, loadInspirations])

  // 保存编辑：PUT 更新内容，成功后退出编辑态并刷新列表
  const saveInspiration = useCallback(async (insp) => {
    const content = editInspirationDraft.trim()
    if (!content) return
    try {
      await api.put('/api/inspirations/' + insp.id, { content })
      setEditingInspiration(null)
      setEditInspirationDraft('')
      await loadInspirations()
    } catch (e) {
      setInspirationError(e.message)
    }
  }, [editInspirationDraft, loadInspirations])

  // 删除灵感：DELETE 后刷新列表
  const deleteInspiration = useCallback(async (insp) => {
    try {
      await api.del('/api/inspirations/' + insp.id)
      await loadInspirations()
    } catch (e) {
      setInspirationError(e.message)
    }
  }, [loadInspirations])

  // 将灵感一键提交为 GitLab issue（issue #143）：灵感内容作为 issue
  // 的标题与描述，通过 GitLab API 创建，默认标签 feature + ui；成功后
  // 刷新开放 issue 列表并展示新 issue 链接
  // issue #162：创建成功后端已删除该灵感，同时刷新灵感列表移除条目
  // （不等 15 秒轮询，避免条目残留误导重复提交）
  const addIssueFromInspiration = useCallback(async (ins) => {
    if (addingIssueInspIds[ins.id]) return // 请求中禁止重复提交
    setAddingIssueInspIds((prev) => ({ ...prev, [ins.id]: true }))
    setInspirationCreatedIssue(null)
    try {
      const created = await api.post(`/api/inspirations/${ins.id}/add-issue`)
      setInspirationCreatedIssue(created)
      setInspirationError('')
      await loadIssues()
      await loadInspirations()
    } catch (e) {
      // 失败（GitLab 故障 / 未配置 owner token 等）：灵感未被删除，
      // 不刷新灵感列表——条目保留可重试，避免界面闪烁
      setInspirationError(e.message)
    } finally {
      setAddingIssueInspIds((prev) => ({ ...prev, [ins.id]: false }))
    }
  }, [addingIssueInspIds, loadIssues, loadInspirations])

  // ---- 灵感 AI 对话（issue #166）：与 AI agent 探讨灵感 ----

  // 打开对话面板：加载该灵感的历史消息（GET messages），加载中显示提示
  const openInspirationChat = useCallback(async (ins) => {
    setChatInspiration(ins)
    setChatMessages([])
    setChatLoading(true)
    setChatError('')
    try {
      const d = await api.get(`/api/inspirations/${ins.id}/messages`)
      setChatMessages(d.messages || [])
    } catch (e) {
      setChatError(e.message)
    } finally {
      setChatLoading(false)
    }
  }, [])

  // 关闭对话面板：清空全部对话状态（再次打开重新拉取历史）
  const closeInspirationChat = useCallback(() => {
    setChatInspiration(null)
    setChatMessages([])
    setChatDraft('')
    setChatError('')
  }, [])

  // 发送消息：POST 后端保存用户消息并调 AI 回复，成功后 append
  // 用户消息 + AI 回复到消息列表并清空输入；失败保留输入可重试
  const sendInspirationChat = useCallback(async () => {
    if (chatSending || !chatInspiration) return
    const content = chatDraft.trim()
    if (!content) return
    setChatSending(true)
    setChatError('')
    try {
      const d = await api.post(`/api/inspirations/${chatInspiration.id}/messages`, { content })
      setChatMessages((prev) => prev.concat(d.messages || []))
      setChatDraft('')
    } catch (e) {
      setChatError(e.message)
    } finally {
      setChatSending(false)
    }
  }, [chatSending, chatInspiration, chatDraft])

  // 对话面板 Esc 关闭（SSR 测试环境无 document 时跳过，与 AddIssueModal
  // 一致）；面板关闭后移除监听，避免误关其他弹窗
  useEffect(() => {
    if (typeof document === 'undefined' || !chatInspiration) return
    const onKey = (e) => {
      if (e && e.key === 'Escape') closeInspirationChat()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [chatInspiration, closeInspirationChat])

  // 对账（issue #134）：立即扫描该仓库，把「assignee 是 bot 但任务表无
  // 活跃记录」的 open issues 补入队（复用仓库页对账接口，issue #17）。
  // 与仓库页对账按钮一致为低危操作无需确认；请求中禁用防重复点击。
  const reconcileRepo = useCallback(async (repo) => {
    setReconcileResults((prev) => ({ ...prev, [repo.repo_id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.repo_id}/reconcile`)
      setReconcileResults((prev) => ({ ...prev, [repo.repo_id]: res }))
    } catch (e) {
      setReconcileResults((prev) => ({ ...prev, [repo.repo_id]: { error: e.message } }))
    }
  }, [])

  // 各任务信息块实时输出自动滚动到底部（issue #114：任务板块删除后
  // 任务块迁入开放 issue 列表项内；SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    document.querySelectorAll('.issue-task-log').forEach((el) => {
      el.scrollTop = el.scrollHeight
    })
  }, [liveLines])

  return (
    <div>
      <h1>概览</h1>

      {/* issue #138：DeepSeek 账户余额——设置里配置了 deepseek api 时
          在概览页展示（未配置时整卡不渲染，页面保持简洁）。数据由后端
          代调 https://api.deepseek.com/user/balance 获取，Key 不外发 */}
      {dsBalance && dsBalance.configured && (
        <section className="deepseek-balance-section">
          <h2>DeepSeek 账户余额</h2>
          <p className="muted">
            来自 DeepSeek user/balance 接口（每 {DEEPSEEK_BALANCE_POLL_MS / 1000} 秒自动刷新）
          </p>
          {(dsBalance.error || dsBalanceError) && (
            <div className="alert alert-error" role="alert">
              {dsBalance.error || dsBalanceError}
            </div>
          )}
          {dsBalance.balance && (
            <div className="deepseek-balance-body">
              <div className="deepseek-balance-head">
                {dsBalance.balance.is_available ? (
                  <span className="ok-text"><Icon name="check" /> 账户可用</span>
                ) : (
                  <span className="muted">账户不可用</span>
                )}
                {dsBalance.balance.fetched_at && (
                  <span className="muted small" title="查询时间">
                    更新于 {fmtTime(dsBalance.balance.fetched_at)}
                  </span>
                )}
              </div>
              {(dsBalance.balance.balance_infos || []).length === 0 ? (
                <div className="empty-state small">
                  <span className="empty-icon" aria-hidden="true"><Icon name="wallet" /></span>
                  <p className="muted">暂无余额信息</p>
                </div>
              ) : (
                <ul className="deepseek-balance-list">
                  {(dsBalance.balance.balance_infos || []).map((info, i) => (
                    <li key={i} className="deepseek-balance-item">
                      <span className="deepseek-balance-currency" title="币种">
                        {info.currency || '—'}
                      </span>
                      <span className="deepseek-balance-total" title="总余额">
                        {info.total_balance != null ? `${info.total_balance}` : '—'}
                      </span>
                      <span className="muted small" title="赠送余额">
                        赠送 {info.granted_balance ?? '—'}
                      </span>
                      <span className="muted small" title="充值余额">
                        充值 {info.topped_up_balance ?? '—'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <div className="form-row">
            <button type="button" className="btn btn-small"
                    onClick={loadDeepSeekBalance}><Icon name="refresh" /> 刷新</button>
            <a className="btn btn-small deepseek-topup-link"
               href={DEEPSEEK_TOPUP_URL} target="_blank" rel="noreferrer"
               title="前往 DeepSeek 开放平台充值"><Icon name="externalLink" /> 去充值</a>
          </div>
        </section>
      )}

      {/* issue #68：板块排序调整——开放 Issue 置于页面顶部，
          其后为 CI/CD 流水线。
          issue #114：独立任务板块删除，正在运行任务的信息（状态徽章
          / 引擎 / 实时输出）整合进本板块 running 组的 issue 项内，
          任务轮询错误一并在此展示 */}
      <section className="issues-section">
        <h2>开放 Issue</h2>
        <p className="muted">已启用仓库的开放 issue，按仓库优先级排序，正在运行的 issue 置顶展示任务执行详情（每 {ISSUE_POLL_MS / 1000} 秒自动刷新）</p>
        {ownerTokenOk === false && (
          <div className="alert alert-warning" role="alert">
            <Icon name="warning" /> <strong>Owner GitLab Token 未配置</strong>：概览页的 issue 编辑
            （关闭 issue / 编辑标签 / 添加评论 / 回复评论 / 添加 issue）必须使用
            owner token，未配置时操作会被拦截（不会以 code01 身份发布）。
            请先在「设置」页配置 <code>gitlab.owner_token</code> 后再操作。
          </div>
        )}
        {issueError && (
          <div className="alert alert-error" onClick={() => setIssueError('')}>{issueError}</div>
        )}
        {error && (
          <div className="alert alert-error" onClick={() => setError('')}>{error}</div>
        )}
        {issueErrors.length > 0 && (
          <div className="alert alert-error">
            {issueErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        {repoIssues.length === 0 || repoIssues.every((r) => !r.issues || r.issues.length === 0) ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
            <p className="muted">暂无开放 issue</p>
          </div>
        ) : (
          <div className="issues-list">
            {repoIssues.map((r) => (
              <div key={r.repo_id} className="card issue-repo-card">
                <div className="issue-repo-head">
                  <span className="issue-repo-name" title="仓库"><Icon name="folder" /> {r.repo_name || '（已删除）'}</span>
                  <span className="badge badge-muted" title="仓库优先级：数字越小越优先">
                    优先级 {r.priority ?? 100}
                  </span>
                  <span className="muted">{r.issues.length} 个开放 issue</span>
                  {/* issue #134：卡片右上角操作组——「对账」按钮 + issue #92
                      「添加 Issue」按钮，整体推到卡片头最右侧 */}
                  <div className="issue-repo-actions">
                    <button type="button" className="btn btn-small reconcile-btn"
                            onClick={() => reconcileRepo(r)}
                            disabled={reconcileResults[r.repo_id]?.loading}
                            title="立即扫描该仓库开放 issue，把分配给了 bot 但还没有任务的 issue 补入任务队列">
                      {reconcileResults[r.repo_id]?.loading ? <><Icon name="refresh" /> 对账中…</> : <><Icon name="refresh" /> 对账</>}
                    </button>
                    {/* issue #92：卡片右上角「添加 Issue」按钮——打开弹窗，
                        提交后调用 GitLab API 在对应仓库创建 issue */}
                    <button type="button" className="btn btn-small add-issue-btn"
                            onClick={() => setAddIssueRepo(r)}
                            title="在该仓库创建新 issue"><Icon name="plus" /> 添加 Issue</button>
                  </div>
                </div>
                {/* issue #134：对账结果——与仓库页对账结果一致，小字展示
                    扫描/补入队结果，请求失败显示错误 */}
                {reconcileResults[r.repo_id] && <ReconcileResult result={reconcileResults[r.repo_id]} />}
                {(r.issues || []).length === 0 ? (
                  <div className="empty-state small">
                    <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                    <p className="muted">该仓库暂无开放 issue</p>
                  </div>
                ) : (
                  /* issue #80：按 bot 终态标签分组（bot-failed / bot-done /
                     其他），只渲染非空组，组标题带计数
                     issue #101：正在运行的 issue 独立成 running 组置顶，
                     任务结束键消失后自动回落原分组 */
                  ISSUE_GROUPS.map((g) => {
                    const items = groupIssuesByBotLabel(r.issues, runningKeys, r.repo_id)[g.key]
                    if (items.length === 0) return null
                    return (
                      <div key={g.key} className="issue-group">
                        <div className="issue-group-head">
                          <span className="issue-group-title" title={g.hint}><Icon name={g.icon} /> {g.title}</span>
                          <span className="issue-group-count"
                                title="组内 issue 数量">{items.length} 个</span>
                        </div>
                        <ul className="issue-list">
                          {items.map((i) => {
                            const bot = botStatusKey(i)
                            const statusMeta = bot ? BOT_STATUS_META[bot] : null
                            // issue #99：任务（running/retrying）命中则该 issue 高亮
                            const running = runningKeys.has(`${r.repo_id}:${i.iid}`)
                            // issue #80：终态标签由状态徽章替代展示，其余标签保留胶囊
                            const otherLabels = (i.labels || []).filter(
                              (l) => l && !BOT_STATUS_NAMES.has(l.name))
                            return (
                              <li key={i.iid}
                                  className={running ? 'issue-item issue-item-running' : 'issue-item'}>
                                {/* issue #71：参考 GitLab issue 列表页布局——左列编号+标题+
                                    标签/里程碑胶囊，右列 assignee 头像+更新时间+评论数
                                    issue #85：标题改为按钮——点击打开右边栏，不再直接
                                    跳转 GitLab（跳转统一走右边栏右上角按钮）
                                    issue #114：issue 行（issue-row）与任务信息块
                                    纵向排布——任务板块删除后任务详情随项展示 */}
                                <div className="issue-row">
                                <div className="issue-main">
                                  <button type="button" className="issue-link"
                                          onClick={() => setSelectedIssue({
                                            issue: i, repoName: r.repo_name,
                                            running,
                                          })}
                                          title="查看 issue 详情">
                                    <span className="issue-iid">#{i.iid}</span>
                                    {statusMeta && (
                                      <span className={`issue-status ${statusMeta.cls}`}
                                            title={statusMeta.hint}><Icon name={statusMeta.icon} /> {statusMeta.label}</span>
                                    )}
                                    {/* issue #99：正在运行的 issue 显示「运行中」徽章
                                        （任务结束后随任务列表轮询自动消失） */}
                                    {running && (
                                      <span className="issue-status issue-status-running"
                                            title="该 issue 正在被 bot 执行中"><Icon name="settings" /> 运行中</span>
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
                                          <Icon name="tag" /> {i.milestone}
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
                                      <Icon name="message" /> {i.user_notes_count}
                                    </span>
                                  )}
                                </div>
                                </div>
                                {/* issue #114：正在运行任务的信息块——任务板块已删除，
                                    任务状态徽章 / 执行引擎 / 实时输出随对应 issue 项
                                    展示（同一 issue 的多条任务记录逐一渲染） */}
                                {running && tasksForIssue(tasks, r.repo_id, i.iid).map((t) => {
                                  const meta = STATUS_META[t.status]
                                    || { label: t.status || '—', cls: '' }
                                  const lines = liveLines[t.id] || []
                                  const eng = engineLabel(t.engine)
                                  return (
                                    <div key={t.id} className="issue-task">
                                      <div className="issue-task-head">
                                        <span className={'badge ' + meta.cls}>{meta.label}</span>
                                        {eng && (
                                          <span className="issue-task-engine"
                                                title="任务执行引擎">{eng}</span>
                                        )}
                                        {t.issue_url ? (
                                          <a className="issue-task-link" href={t.issue_url}
                                             target="_blank" rel="noreferrer"
                                             title="在 GitLab 中打开 issue">在 GitLab 中打开</a>
                                        ) : null}
                                      </div>
                                      <pre className="log-view issue-task-log">
                                        {lines.length > 0
                                          ? lines.map((line, i) => <span key={i}>{line}{'\n'}</span>)
                                          : '（暂无输出）'}
                                      </pre>
                                    </div>
                                  )
                                })}
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

      {/* issue #131：灵感板块——位于开放 Issue 下方、CI/CD 流水线上方。
          按仓库随手记录关于对应仓库的新功能灵感，仅保存在 Botler
          本地数据库，不提交到 GitLab issue。每个仓库一张卡：灵感列表
          （编辑/删除）+ 底部随手记录表单 */}
      <section className="inspirations-section">
        <h2><Icon name="lightbulb" /> 灵感</h2>
        <p className="muted">按仓库随手记录新功能灵感，仅保存在本地数据库；可一键将灵感提交为 GitLab issue（默认标签 feature、ui；每 {INSPIRATION_POLL_MS / 1000} 秒自动刷新）</p>
        {inspirationError && (
          <div className="alert alert-error" onClick={() => setInspirationError('')}>{inspirationError}</div>
        )}
        {inspirationCreatedIssue && (
          <div className="alert alert-ok" onClick={() => setInspirationCreatedIssue(null)}
               title="点击关闭">
            <Icon name="checkCircle" /> 已创建{' '}
            <a href={inspirationCreatedIssue.web_url || '#'} target="_blank" rel="noreferrer"
               onClick={(e) => e.stopPropagation()}>
              {'issue #' + inspirationCreatedIssue.iid}
            </a>
            （默认标签 feature、ui）
          </div>
        )}
        {inspirationRepos.length === 0 ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
            <p className="muted">暂无灵感（未配置仓库）</p>
          </div>
        ) : (
          <div className="inspirations-list">
            {inspirationRepos.map((r) => (
              <div key={r.repo_id} className="card inspiration-repo-card">
                <div className="inspiration-repo-head">
                  <span className="inspiration-repo-name" title="仓库"><Icon name="folder" /> {r.repo_name || '（已删除）'}</span>
                  {r.enabled === false && (
                    <span className="badge badge-muted" title="该仓库在 Botler 中未启用">未启用</span>
                  )}
                  <span className="muted">{(r.inspirations || []).length} 条灵感</span>
                </div>
                {(r.inspirations || []).length === 0 ? (
                  <div className="empty-state small">
                    <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                    <p className="muted">暂无灵感，记一条吧</p>
                  </div>
                ) : (
                  <ul className="inspiration-list">
                    {r.inspirations.map((ins) => (
                      <li key={ins.id} className="inspiration-item">
                        {editingInspiration && editingInspiration.id === ins.id ? (
                          <div className="inspiration-edit">
                            <textarea className="input inspiration-textarea"
                                      value={editInspirationDraft}
                                      onChange={(e) => setEditInspirationDraft(e.target.value)}
                                      rows={3} />
                            <div className="inspiration-actions">
                              <button type="button" className="btn btn-small inspiration-save-btn"
                                      onClick={() => saveInspiration(ins)}
                                      disabled={!editInspirationDraft.trim()}>保存</button>
                              <button type="button" className="btn btn-small"
                                      onClick={() => {
                                        setEditingInspiration(null)
                                        setEditInspirationDraft('')
                                      }}>取消</button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <p className="inspiration-content">{ins.content}</p>
                            <div className="inspiration-meta">
                              <span className="inspiration-time" title="最后更新时间">
                                {fmtAgo(ins.updated_at) || '—'}
                              </span>
                              <span className="inspiration-actions">
                                <button type="button" className="inspiration-action-btn inspiration-add-issue-btn"
                                        title="将灵感内容作为标题与描述，通过 GitLab API 创建 issue（默认标签 feature、ui；分配人为该仓库 remote url 用户，可在仓库设置页查看/重新读取）"
                                        onClick={() => addIssueFromInspiration(ins)}
                                        disabled={!!addingIssueInspIds[ins.id]}>
                                  {addingIssueInspIds[ins.id] ? <><Icon name="hourglass" /> 提交中…</> : <><Icon name="pin" /> 添加 Issue</>}
                                </button>
                                <button type="button" className="inspiration-action-btn inspiration-chat-btn"
                                        title="与 AI agent 探讨该灵感（复用设置页「AI API 供应商」配置的对话模型）"
                                        onClick={() => openInspirationChat(ins)}><Icon name="message" /> 对话</button>
                                <button type="button" className="inspiration-action-btn"
                                        title="编辑该灵感"
                                        onClick={() => {
                                          setEditingInspiration(ins)
                                          setEditInspirationDraft(ins.content)
                                        }}><Icon name="pencil" /> 编辑</button>
                                <button type="button" className="inspiration-action-btn inspiration-delete-btn"
                                        title="删除该灵感"
                                        onClick={() => deleteInspiration(ins)}><Icon name="trash" /> 删除</button>
                              </span>
                            </div>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {/* 随手记录表单：内容去首尾空白非空才允许提交 */}
                <form className="inspiration-add-form"
                      onSubmit={(e) => { e.preventDefault(); submitNewInspiration(r.repo_id) }}>
                  <textarea className="input inspiration-textarea"
                            placeholder="记一条关于该仓库的新功能灵感…"
                            value={newInspirationDrafts[r.repo_id] || ''}
                            onChange={(e) => setNewInspirationDrafts((prev) => ({ ...prev, [r.repo_id]: e.target.value }))}
                            rows={2} />
                  <button type="submit" className="btn btn-small inspiration-add-btn"
                          disabled={!(newInspirationDrafts[r.repo_id] || '').trim()}><Icon name="plus" /> 记录</button>
                </form>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* issue #166：灵感 AI 对话面板——与 AI agent 探讨当前灵感。
          复用 .modal 体系（遮罩点击 / × / Esc 关闭，与 AddIssueModal
          一致）：顶部灵感摘要，中部消息列表（用户右 / AI 左），底部
          输入框 + 发送按钮（Enter 发送 / Shift+Enter 换行）；发送中
          按钮禁用，AI 回复实时 append */}
      {chatInspiration && (
        <div className="modal-overlay" onClick={closeInspirationChat}>
          <div className="modal chat-modal" role="dialog" aria-modal="true"
               onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong><Icon name="message" /> 与 AI 探讨灵感</strong>
              <button type="button" className="btn modal-close"
                      onClick={closeInspirationChat} title="关闭" aria-label="关闭"><Icon name="x" /></button>
            </div>
            <div className="chat-subject" title={chatInspiration.content}>
              <span className="muted">仓库：{chatInspiration.repo_name || '—'}</span>
              <p className="chat-subject-content">{chatInspiration.content}</p>
            </div>
            <div className="chat-body">
              {chatLoading ? (
                <div className="chat-empty muted">加载对话历史…</div>
              ) : chatMessages.length === 0 ? (
                <div className="chat-empty muted">还没有对话，向 AI 说说你对这条灵感的想法吧</div>
              ) : (
                chatMessages.map((m) => (
                  <div key={m.id}
                       className={'chat-msg ' + (m.role === 'user' ? 'chat-msg-user' : 'chat-msg-ai')}>
                    <div className="chat-msg-bubble">{m.content}</div>
                    <div className="chat-msg-meta">
                      {m.role === 'user' ? '我' : 'AI'} · {fmtAgo(m.created_at) || '—'}
                    </div>
                  </div>
                ))
              )}
              {chatSending && <div className="chat-empty muted"><Icon name="bot" /> AI 思考中…</div>}
              {chatError && (
                <div className="alert alert-error chat-error"
                     onClick={() => setChatError('')}>{chatError}</div>
              )}
            </div>
            <form className="chat-input-row"
                  onSubmit={(e) => { e.preventDefault(); sendInspirationChat() }}>
              <textarea className="input chat-input" rows={2}
                        placeholder="向 AI 提出你的想法 / 疑问…"
                        value={chatDraft}
                        onChange={(e) => setChatDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey && !chatSending) {
                            e.preventDefault()
                            sendInspirationChat()
                          }
                        }}
                        disabled={chatSending} />
              <button type="submit" className="btn btn-small chat-send-btn"
                      disabled={chatSending || !chatDraft.trim()}>
                {chatSending ? '发送中…' : '发送'}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* issue #114：独立任务板块已删除——正在运行任务的信息（状态徽章
          / 执行引擎 / 实时输出）整合进上方开放 Issue 板块 running 组的
          issue 项内，任务轮询与 SSE 数据流保持不变 */}
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
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="rocket" /></span>
            <p className="muted">暂无流水线</p>
          </div>
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
                    <span className="pipeline-repo" title="仓库"><Icon name="folder" /> {p.repo_name || '（已删除）'}</span>
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

      {/* issue #180：Issue 完成耗时——平均每个 issue 完成所需的时间
          （成功任务的处理用时：系统接收时间 → bot-done 打标时间，与任务
          详情「处理用时」issue #49 语义一致）与逐日平均走势图，置于
          概览页最下方。数据来自本地 tasks 表成功终态任务
          （GET /api/issues/completion-stats），无 GitLab 请求压力 */}
      <section className="completion-stats-section">
        <h2>Issue 完成耗时</h2>
        <p className="muted">平均每个 issue 完成所需时间（处理用时：系统接收 → bot-done 打标）与逐日走势（每 {COMPLETION_STATS_POLL_MS / 1000} 秒自动刷新）</p>
        {completionStatsError && (
          <div className="alert alert-error" onClick={() => setCompletionStatsError('')}>{completionStatsError}</div>
        )}
        {completionStats && completionStats.completed_count === 0 ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="hourglass" /></span>
            <p className="muted">暂无已完成 issue</p>
          </div>
        ) : completionStats ? (
          <>
            <div className="completion-stats-summary">
              <span className="completion-stats-value"
                    title="全部已完成 issue 的平均完成耗时">
                {fmtSeconds(completionStats.avg_seconds) || <span className="muted">—</span>}
              </span>
              <span className="muted">平均完成耗时（{completionStats.completed_count} 个已完成 issue）</span>
            </div>
            <CompletionTrendChart trend={completionStats.trend} />
          </>
        ) : null}
      </section>

      {/* issue #85：issue 详情右边栏——点击列表项打开，显示具体信息与正文。
          issue #94：关闭 issue 成功后刷新列表（后端已清缓存，该 issue
          从开放列表消失）；抽屉保持打开，状态徽章由抽屉内部更新。
          issue #108：标记编辑成功后同样刷新列表（后端已清缓存，
          列表卡片标记即时同步） */}
      {selectedIssue && (
        <IssueDrawer issue={selectedIssue.issue} repoName={selectedIssue.repoName}
                     running={selectedIssue.running}
                     onClose={() => setSelectedIssue(null)}
                     onIssueClosed={() => loadIssues()}
                     onLabelsUpdated={() => loadIssues()}
                     onRetried={() => loadIssues()} />
      )}

      {/* issue #92：添加 issue 弹窗——创建成功后关闭并立即刷新列表 */}
      {addIssueRepo && (
        <AddIssueModal repo={addIssueRepo}
                       onClose={() => setAddIssueRepo(null)}
                       onCreated={() => {
                         setAddIssueRepo(null)
                         loadIssues()
                       }} />
      )}
    </div>
  )
}

// 对账结果（issue #134）：与仓库页对账结果（issue #17）一致——
// 入队 N 个 = 发现待处理；0 个 = 无需处理；仓库停用时后端返回 note
function ReconcileResult({ result }) {
  if (result.error) return <div className="alert alert-error small reconcile-result">{result.error}</div>
  if (result.note) return <div className="small muted reconcile-result">{result.note}</div>
  return (
    <div className="small reconcile-result">
      {result.enqueued > 0
        ? <span className="test-chip ok"><Icon name="check" /> {result.enqueued} 个待处理 issue 已入队</span>
        : <span className="test-chip ok"><Icon name="check" /> 无需处理</span>}
      {result.scanned > 0 && <span className="muted">扫描 {result.scanned} 个 issue</span>}
    </div>
  )
}

// 走势图（issue #180）：轻量 SVG 折线图，无第三方图表库依赖——
// 横轴为完成日（数据本身是逐日序列，等距排布即可），纵轴为当日平均
// 完成耗时（秒），范围 0 → 最大值留 10% 余量；折线 + 数据点，每个点
// 带 <title> 悬浮提示（日期 / 平均耗时 / 当日完成数）。trend 非数组
// 或为空时返回 null（不渲染）。
export function CompletionTrendChart({ trend }) {
  if (!Array.isArray(trend) || trend.length === 0) return null
  const W = 640
  const H = 180
  const PAD_L = 8
  const PAD_R = 8
  const PAD_T = 14
  const PAD_B = 24
  const n = trend.length
  const maxSec = Math.max(...trend.map((t) => Number(t.avg_seconds) || 0))
  const yMax = maxSec > 0 ? maxSec * 1.1 : 1
  const innerW = W - PAD_L - PAD_R
  const innerH = H - PAD_T - PAD_B
  const px = (i) => (n === 1 ? PAD_L + innerW / 2 : PAD_L + (innerW * i) / (n - 1))
  const py = (v) => H - PAD_B - (innerH * (Number(v) || 0)) / yMax
  const points = trend
    .map((t, i) => `${px(i).toFixed(2)},${py(t.avg_seconds).toFixed(2)}`)
    .join(' ')
  const first = trend[0]
  const last = trend[n - 1]
  return (
    <svg className="completion-trend-chart" viewBox={`0 0 ${W} ${H}`}
         role="img" aria-label="平均完成耗时走势图">
      <line className="completion-trend-axis" x1={PAD_L} y1={H - PAD_B}
            x2={W - PAD_R} y2={H - PAD_B} />
      <polyline className="completion-trend-line" points={points} fill="none" />
      {trend.map((t, i) => (
        <circle key={t.date || i} className="completion-trend-dot"
                cx={px(i).toFixed(2)} cy={py(t.avg_seconds).toFixed(2)} r="3">
          <title>{`${t.date}：平均 ${fmtSeconds(t.avg_seconds) || '—'}（${t.count} 个 issue）`}</title>
        </circle>
      ))}
      <text className="completion-trend-label" x={PAD_L} y={H - PAD_B + 16}>{first.date}</text>
      <text className="completion-trend-label" x={W - PAD_R} y={H - PAD_B + 16}
            textAnchor="end">{last.date}</text>
      <text className="completion-trend-label" x={PAD_L} y={PAD_T - 4}>{fmtSeconds(yMax) || ''}</text>
    </svg>
  )
}
