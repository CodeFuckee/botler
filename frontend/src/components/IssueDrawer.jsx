// 概览页 issue 详情右边栏（issue #85）：点击开放 issue 列表项打开，
// 展示 issue 具体信息（状态 / 作者 / 时间 / 标签 / 里程碑 / 负责人 /
// 评论数）与正文（Markdown 渲染，复用 issue #27 的 Markdown 组件）。
//
// issue #94：「关闭 issue」按钮——二次确认后调用后端关闭 GitLab
// issue；成功后按钮消失、状态徽章变「已关闭」并通知父组件刷新
// 开放 issue 列表（该 issue 从列表消失）。
//
// issue #97：描述下方新增「评论」与「活动」两个区块——抽屉打开时
// 按需拉取 GET /api/issues/{project_id}/{iid}/detail，评论（用户
// 发言，Markdown 渲染）与活动（系统事件，纯文本）按 note 的
// system 标志分区展示；覆盖加载中/加载失败重试/空占位/旧数据缺
// project_id 等边界。
//
// issue #117：失败任务（带 bot-failed 标签且无 bot-done）右上角增加
// 「重试」按钮——二次确认后调用后端重新执行该 issue 的任务（复用最近
// 失败任务或新建任务入队），成功后通知父组件刷新开放 issue 列表（该
// issue 即将进入「运行中」组）。
//
// issue #108：标签行增加「编辑标记」功能——编辑态加载项目标记池
// （GET /api/issues/{project_id}/labels，checkbox 多选、当前标记
// 预勾选、池外当前标记仍可取消勾选移除），保存时 diff 出 add/
// remove 一次 PUT /api/issues/{project_id}/{iid}/labels 提交（remove
// 只含当前实际存在的标记，规避 GitLab remove_labels 对不存在标记
// 返回 404）；成功后本地标记即时更新（displayLabels 覆盖）并通知
// 父组件刷新列表（onLabelsUpdated）；失败保留编辑态可重试。
//
// issue #125：评论区块新增「添加评论」与「回复评论」——区块底部评论
// 输入区（POST /api/issues/{project_id}/{iid}/comments）与每条评论
// 的「回复」按钮（内联回复框，POST /api/issues/{project_id}/{iid}/
// comments/{note_id}/reply）；成功后本地即时追加新评论并叠加评论
// 计数（localNotesCount），无需重新拉取详情；失败保留输入内容可重试。
//
// issue #290：任务 id——detail 响应新增 task_id 字段（该 issue 最近
// 一条任务记录 id，从未执行为 null）：已执行过的 issue 在 KV 表
// 「任务」行展示对应任务 id（#id），从未执行/加载失败显示「—」。
//
// issue #300：完成耗时——detail 响应新增 task_duration_seconds 字段
// （该 issue 最近任务成功终态（succeeded）时的完成耗时秒数，
// finished_at - created_at，与 issue #180 完成耗时统计语义一致）：
// 任务已完成（succeeded）的 issue 在 KV 表「任务」行下方新增
// 「完成耗时」行展示人类可读耗时（fmtSeconds），未完成/从未执行/
// 加载失败/异常数据显示「—」。
//
// 交互约定：
// - 列表项本身不再直接跳转 GitLab，跳转统一走抽屉右上角
//   「在 GitLab 中打开」按钮（web_url 新窗口）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键。
import { useCallback, useEffect, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api, fmtTime, fmtSeconds } from '../api.js'
import { confirmDialog } from '../dialog.js'
import Markdown, { linkifyCommits } from './Markdown.jsx'
import TaskDetailDrawer from './TaskDetailDrawer.jsx'  // issue #167：任务执行详情第二层右边栏
import { loadTimelineEnabled, buildTimeline } from '../lib/notesTimeline.js'  // issue #342：评论/活动合并时间线开关与排序

// issue 状态 → 徽章映射（聚合只返回开放 issue，closed 为兜底映射）
export const ISSUE_STATE_META = {
  opened: { label: '开放', cls: 'status-running' },
  closed: { label: '已关闭', cls: 'status-interrupted' },
}

// 失败任务判定（issue #117）：issue 带 bot-failed 标签且无 bot-done。
// 与概览列表分组判定一致（botStatusKey：bot-done 优先级高于 bot-failed
// ——失败后重试成功两标签并存时视为成功，不再显示重试按钮）。labels
// 元素可能缺 name 或非对象（旧缓存/异常数据），逐一防御
export function isFailedTask(issue) {
  const labels = issue && issue.labels
  if (!Array.isArray(labels)) return false
  let hasFailed = false
  for (const l of labels) {
    const name = l && typeof l === 'object' ? l.name : null
    if (name === 'bot-done') return false
    if (name === 'bot-failed') hasFailed = true
  }
  return hasFailed
}

// ---- issue #118 + #120：任务执行引擎类型 ----
// 概览页弹出的 issue 右边栏展示任务执行引擎的类型：抽屉打开时随
// GET /api/issues/{project_id}/{iid}/detail 一起返回该 issue 最近
// 任务实际使用的引擎（issue #120 修复：不再读全局 worker.engine——
// 全局引擎切换后历史 issue 会全部误显新引擎；后端回退链：任务落库
// engine > 断点续跑会话字段推断 > 全局 worker.engine）。文案与设置页
// 「任务调度」卡片下拉选项一致。
export const ENGINE_META = {
  claude: { label: 'Claude Code CLI' },
  hermes: { label: 'hermes-agent SDK' },
  dsh: { label: 'deepseek-harness SDK' },
}

// 引擎 → 展示文本（纯函数导出，便于测试）：null/undefined 表示加载中；
// 空值/纯空白回退默认 claude；未知值（配置手改非法）原样展示兜底，
// 保证抽屉不因异常引擎值崩溃
export function engineDisplay(raw) {
  if (raw === null || raw === undefined) return '加载中…'
  const v = String(raw).trim().toLowerCase()
  if (!v) return ENGINE_META.claude.label
  return (ENGINE_META[v] || {}).label || v
}

// Esc 键判定（纯函数导出，便于测试）
export function isEscapeKey(e) {
  return !!e && e.key === 'Escape'
}

// issue #181：issue web_url → 项目 web 基地址（纯函数导出，便于测试）。
// GitLab issue web_url 形如 https://host/ns/repo/-/work_items/181（工作
// 项）或 .../-/issues/181（传统 issue），去掉 /-/xxx/<iid> 后缀即项目
// 基地址，提交链接在此基础上拼 /-/commit/<sha>；无法识别返回 ''（提交
// 链接不渲染，纯文本兜底）。
// note 作者名：name 优先回退 username，全无显示「—」（issue #97，
// 与列表头像/作者展示的兜底逻辑一致）
export function noteAuthorName(note) {
  const a = note && typeof note === 'object' ? note.author : null
  if (!a || typeof a !== 'object') return '—'
  return a.name || a.username || '—'
}

/** issue web_url → 项目 web 基地址（issue #181）：去掉 /-/work_items/<iid>
 *  或 /-/issues/<iid> 后缀；无法识别时返回 ''（调用方不渲染提交链接）。 */
export function projectUrlFromIssueWebUrl(webUrl) {
  if (!webUrl || typeof webUrl !== 'string') return ''
  const m = webUrl.match(/^(.+)\/-\/(?:work_items|issues)\/\d+$/)
  return m ? m[1] : ''
}

// note 作者头像：avatar_url 渲染 img，缺失回退首字母兜底块
// （复用列表 assignee 头像的 avatar-fallback 样式）
export function NoteAvatar({ note }) {
  const a = note && typeof note === 'object' && typeof note.author === 'object'
    ? note.author : null
  const name = noteAuthorName(note)
  if (a && a.avatar_url) {
    return <img className="comment-avatar" src={a.avatar_url} alt={name}
                title={`评论者 ${name}`} />
  }
  return (
    <span className="comment-avatar avatar-fallback" title={`评论者 ${name}`}>
      {(name !== '—' ? name : '?').slice(0, 1).toUpperCase()}
    </span>
  )
}

export default function IssueDrawer({ issue, repoName, onClose, onIssueClosed,
                                      onLabelsUpdated, running = false, onRetried,
                                      onAssigneeUpdated, onPrioritized }) {
  const [closing, setClosing] = useState(false) // 关闭请求进行中（按钮禁用）
  const [closed, setClosed] = useState(false)   // 本次会话关闭成功标记
  const [closeErr, setCloseErr] = useState('')  // 关闭失败的错误信息
  // issue #117：重试状态——retrying 请求中（按钮禁用）、retried 本次会话
  // 重试成功标记（按钮消失防重复点击）、retryErr 失败错误、retryMsg 成功提示
  const [retrying, setRetrying] = useState(false)
  const [retried, setRetried] = useState(false)
  const [retryErr, setRetryErr] = useState('')
  const [retryMsg, setRetryMsg] = useState('')
  // issue #97：评论与活动（notes 为 null 表示加载中；detailErr
  // 非空表示加载失败，两个区块共用错误横幅 + 重试按钮）
  const [notes, setNotes] = useState(null)
  const [detailErr, setDetailErr] = useState('')
  // issue #108：标记编辑状态——editingLabels 是否处于编辑态；
  // labelPool null=标记池加载中；labelPoolErr 非空=加载失败；
  // selectedLabels 编辑态勾选集合；savingLabels 保存请求进行中；
  // labelErr 保存失败信息；displayLabels 保存成功后的本地标记覆盖
  // （props issue 未刷新，标签行展示以此为准，null 表示用 issue.labels）
  const [editingLabels, setEditingLabels] = useState(false)
  const [labelPool, setLabelPool] = useState(null)
  const [labelPoolErr, setLabelPoolErr] = useState('')
  const [selectedLabels, setSelectedLabels] = useState([])
  const [savingLabels, setSavingLabels] = useState(false)
  const [labelErr, setLabelErr] = useState('')
  const [displayLabels, setDisplayLabels] = useState(null)
  // issue #303：负责人编辑状态——editingAssignee 是否处于编辑态；
  // memberPool null=成员加载中；memberPoolErr 非空=加载失败；
  // selectedAssigneeId 编辑态选中的用户 id（null=不指定）；
  // savingAssignee 保存请求进行中；assigneeErr 保存失败信息；
  // displayAssignees 保存成功后的本地负责人覆盖（props issue 未
  // 刷新，负责人行展示以此为准，null 表示用 issue.assignees）
  const [editingAssignee, setEditingAssignee] = useState(false)
  const [memberPool, setMemberPool] = useState(null)
  const [memberPoolErr, setMemberPoolErr] = useState('')
  const [selectedAssigneeId, setSelectedAssigneeId] = useState(null)
  const [savingAssignee, setSavingAssignee] = useState(false)
  const [assigneeErr, setAssigneeErr] = useState('')
  const [displayAssignees, setDisplayAssignees] = useState(null)
  // issue #118/#120：任务执行引擎类型——engine null=加载中；engineErr
  // 非空=加载失败（执行引擎行显示「—」）；成功值经 engineDisplay 归一
  // 展示。引擎来自该 issue 最近任务的 detail 响应（后端按任务落库），
  // 随抽屉打开一起拉取，不单独请求 /api/settings
  const [engine, setEngine] = useState(null)
  const [engineErr, setEngineErr] = useState('')
  // issue #125：添加评论与回复评论状态——
  // commentText 新评论输入内容；posting 提交中（按钮禁用防重复）；
  // postErr 评论失败信息；replyingTo 正在回复的评论 id（null=未回复）；
  // replyText 回复输入内容；replying 回复提交中；replyErr 回复失败
  // 信息；localNotesCount 本次会话新增评论数（评论行计数 = 快照 + 新增）
  const [commentText, setCommentText] = useState('')
  const [posting, setPosting] = useState(false)
  const [postErr, setPostErr] = useState('')
  const [replyingTo, setReplyingTo] = useState(null)
  const [replyText, setReplyText] = useState('')
  const [replying, setReplying] = useState(false)
  const [replyErr, setReplyErr] = useState('')
  const [localNotesCount, setLocalNotesCount] = useState(null)
  // issue #167：任务执行详情第二层右边栏——detailOpen 打开时本层
  // 抽屉不响应 Esc（由第二层自己处理关闭，避免两层同时被 Esc 关闭）
  const [detailOpen, setDetailOpen] = useState(false)
  // issue #290：任务 id——detail 响应返回该 issue 最近任务 id（已执行
  // 过才有值），KV 表「任务」行据此展示 #id；null=加载中/从未执行
  // （加载完成后无任务显示「—」）
  const [taskId, setTaskId] = useState(null)
  // issue #300：完成耗时秒数——detail 响应返回该 issue 最近任务成功终态
  // 的完成耗时（finished_at - created_at，后端计算，未完成/无记录为
  // null），KV 表「完成耗时」行据此 fmtSeconds 展示；null=加载中/未完成
  // （加载完成后未完成任务显示「—」）
  const [taskDuration, setTaskDuration] = useState(null)
  // issue #242：最近任务状态——detail 响应返回该 issue 最近任务的状态
  // （queued/running/终态；null=加载中/从未执行）。「优先处理」按钮仅对
  // queued 任务展示（已 running 任务不受影响）
  const [taskStatus, setTaskStatus] = useState(null)
  // issue #242：优先处理状态——prioritizing 请求中（按钮禁用）、
  // prioritizeMsg 成功提示、prioritizeErr 失败信息
  const [prioritizing, setPrioritizing] = useState(false)
  const [prioritizeMsg, setPrioritizeMsg] = useState('')
  const [prioritizeErr, setPrioritizeErr] = useState('')

  // Esc 关闭抽屉（SSR 测试环境无 document 时跳过）。issue #167：任务
  // 执行详情第二层右边栏打开时不响应 Esc（由第二层自己关闭），避免
  // 两层抽屉同时被一次 Esc 关闭
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (detailOpen) return
      if (isEscapeKey(e)) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose, detailOpen])

  const i = issue || {}
  // issue #181：项目 web 基地址（由 issue web_url 推导），提交 SHA
  // 链接在此基础上拼 /-/commit/<sha>；识别失败为空串时评论/活动
  // 中的提交引用保持纯文本不渲染链接
  const projectUrl = projectUrlFromIssueWebUrl(i.web_url)
  // 有效状态：本次点击关闭成功后本地立即标记 closed（后端已确认），
  // 状态徽章与按钮即时反映，无需等待下一轮轮询
  const effectiveState = closed ? 'closed' : i.state
  const stateMeta = ISSUE_STATE_META[effectiveState]
    || { label: effectiveState || '—', cls: '' }
  // 可关闭条件：开放状态 + 带 project_id（关闭接口按 project_id
  // 定位仓库，旧缓存数据缺失时隐藏按钮）
  const canClose = !closed && i.state === 'opened'
    && typeof i.project_id === 'number'
  // 可查看执行详情条件（issue #167）：带 project_id + iid（任务接口
  // 按 project_id 定位仓库，旧缓存数据缺失时隐藏按钮，与关闭/编辑
  // 标记按钮同约定）
  const canViewDetail = typeof i.project_id === 'number'
    && typeof i.iid === 'number'
  // 作者显示：name 优先，缺失回退 username，全无显示「—」
  const author = i.author && typeof i.author === 'object'
    ? (i.author.name || i.author.username || '—')
    : '—'
  // 负责人列表：name 优先回退 username（与列表头像的兜底逻辑一致）。
  // issue #303：保存成功后的本地覆盖优先（displayAssignees），
  // props issue 是点击时的轮询快照，编辑成功前不刷新
  const currentAssignees = displayAssignees ?? (i.assignees || [])
  const assigneeNames = currentAssignees.map(
    (a) => (a && typeof a === 'object' ? (a.name || a.username || '—') : '—'))

  // 评论/活动详情数据来源（issue #97）：project_id 与 iid 均为数字时
  // 拉取 detail；旧缓存数据缺 project_id 时不发请求（无法定位仓库）
  const hasDetail = typeof i.project_id === 'number' && typeof i.iid === 'number'
  const loadNotes = useCallback(async () => {
    if (typeof i.project_id !== 'number' || typeof i.iid !== 'number') {
      // 旧缓存数据缺 project_id 无法定位仓库 → 执行引擎也无法获取
      setNotes([])
      setEngineErr('缺少项目信息，无法获取执行引擎')
      return
    }
    setNotes(null)
    setDetailErr('')
    setEngine(null)
    setEngineErr('')
    setTaskId(null)
    setTaskDuration(null)
    setTaskStatus(null)
    try {
      const d = await api.get(`/api/issues/${i.project_id}/${i.iid}/detail`)
      setNotes(Array.isArray(d && d.notes) ? d.notes : [])
      // issue #120：执行引擎来自该 issue 最近任务的实际记录（后端已
      // 做无任务回退），不再读取全局 worker.engine
      setEngine((typeof d.engine === 'string' && d.engine.trim()) ? d.engine : '')
      // issue #290：任务 id——后端无任务返回 null；异常值（0/字符串/
      // 负数）按无任务兜底显示「—」，不因坏数据崩溃
      setTaskId((typeof d.task_id === 'number' && Number.isInteger(d.task_id)
                 && d.task_id > 0) ? d.task_id : null)
      // issue #300：完成耗时——后端仅成功终态任务返回秒数（未完成/无
      // 记录为 null）；异常值（非有限数/负数/字符串）按未完成兜底显示
      // 「—」，不因坏数据崩溃
      setTaskDuration((typeof d.task_duration_seconds === 'number'
                       && Number.isFinite(d.task_duration_seconds)
                       && d.task_duration_seconds >= 0)
                      ? d.task_duration_seconds : null)
      // issue #242：最近任务状态——后端无任务返回 null；异常值兜底
      // null（不展示「优先处理」按钮，不因坏数据崩溃）
      setTaskStatus((typeof d.task_status === 'string' && d.task_status.trim())
                    ? d.task_status : null)
    } catch (e) {
      setDetailErr(e.message || '加载失败')
      setEngineErr(e.message || '加载失败')
    }
  }, [i.project_id, i.iid])

  // 抽屉打开/切换 issue 时拉取详情（依赖 project_id/iid，切换即重拉）
  useEffect(() => {
    loadNotes()
  }, [loadNotes])

  // issue #342：合并时间线开关——设置页「界面显示」卡片切换（localStorage
  // 键 botler.timeline，默认关闭=分开显示，保持 issue #97 现状）。概览页与
  // 设置页为不同路由，抽屉挂载时读取一次即拿到最新偏好，刷新后保持
  const [timeline] = useState(() =>
    loadTimelineEnabled(typeof localStorage !== 'undefined' ? localStorage : null))

  // 分区：system=true 为系统活动事件，false 为用户评论（issue #97）
  const comments = (notes || []).filter((n) => n && !n.system)
  const activities = (notes || []).filter((n) => n && n.system)
  // issue #342：合并时间线——评论与活动按 created_at 升序交错（同一时刻
  // 按 note id），由 renderTimelineBody 消费；分开显示模式不使用
  const timelineItems = timeline ? buildTimeline(notes) : []

  // 点击「关闭 issue」：二次确认 → 调用后端关闭 → 成功标记关闭状态
  // 并通知父组件刷新；失败展示错误信息（按钮保留可重试）。
  // 确认走自定义对话框（issue #105，替代原生 confirm）
  async function handleCloseIssue() {
    const confirmText = '确定要关闭该 issue 吗？关闭后可在 GitLab 中重新打开。'
    if (!(await confirmDialog({ message: confirmText, danger: true }))) return
    setClosing(true)
    setCloseErr('')
    try {
      await api.post(`/api/issues/${i.project_id}/${i.iid}/close`)
      setClosed(true)
      onIssueClosed?.()
    } catch (e) {
      setCloseErr(e.message || '关闭失败')
    } finally {
      setClosing(false)
    }
  }

  // 重试失败任务（issue #117）：二次确认后调用后端重新执行该 issue 的
  // 任务（复用最近失败任务或新建任务入队）。成功后本地标记 retried
  // （按钮消失防重复点击）、显示成功提示并通知父组件刷新开放列表
  // （该 issue 即将进入「运行中」组）；失败保留按钮可重试。
  async function handleRetry() {
    const confirmText = '确定要重新执行该 issue 的任务吗？任务将重新入队执行。'
    if (!(await confirmDialog({ message: confirmText }))) return
    setRetrying(true)
    setRetryErr('')
    setRetryMsg('')
    try {
      const d = await api.post(`/api/issues/${i.project_id}/${i.iid}/retry`)
      setRetried(true)
      setRetryMsg(`任务 #${d.task_id} 已重新入队，开始重试`)
      onRetried?.()
    } catch (e) {
      setRetryErr(e.message || '重试失败')
    } finally {
      setRetrying(false)
    }
  }

  // ---- issue #108：标记编辑 ----

  // issue #242：排队任务优先处理——该 issue 最近任务处于 queued 时展示
  // 「优先处理」按钮：调 POST /api/issues/{project_id}/{iid}/prioritize 把
  // 该任务人工优先级置顶（调度器优先于仓库/标签规则派发）。成功后本地
  // 提示并通知父组件刷新列表；失败展示错误信息（按钮保留可重试）。
  const handlePrioritize = async () => {
    setPrioritizing(true)
    setPrioritizeErr('')
    try {
      const r = await api.post(`/api/issues/${i.project_id}/${i.iid}/prioritize`)
      setPrioritizeMsg(
        `任务 #${r.task_id} 已置顶（人工优先级 ${r.manual_priority}），将优先执行`)
      if (typeof onPrioritized === 'function') onPrioritized()
    } catch (e) {
      setPrioritizeErr(e.message || '优先处理失败')
    } finally {
      setPrioritizing(false)
    }
  }

  // 当前生效的标记列表：保存成功后的本地覆盖优先（后端返回的更新后
  // 标记；props issue 是点击时的轮询快照，编辑成功前不刷新）
  const currentLabels = displayLabels ?? (i.labels || [])
  // 可编辑条件：带 project_id（标记接口按 project_id 定位仓库，
  // 旧缓存数据缺失时隐藏按钮，与关闭按钮同约定）
  const canEditLabels = typeof i.project_id === 'number'

  // 进入编辑态：预勾选当前标记，加载项目标记池
  async function startEditLabels() {
    setEditingLabels(true)
    setLabelErr('')
    setSelectedLabels(currentLabels.map((l) => l.name))
    setLabelPool(null)
    setLabelPoolErr('')
    try {
      const d = await api.get(`/api/issues/${i.project_id}/labels`)
      setLabelPool(Array.isArray(d && d.labels) ? d.labels : [])
    } catch (e) {
      setLabelPoolErr(e.message || '加载失败')
    }
  }

  // 勾选/取消勾选一个标记（编辑态内）
  function toggleLabel(name) {
    setSelectedLabels((prev) => (prev.includes(name)
      ? prev.filter((n) => n !== name)
      : [...prev, name]))
  }

  // 取消编辑：不调接口，丢弃本地勾选状态，标记展示保持原状
  function cancelEditLabels() {
    setEditingLabels(false)
    setLabelErr('')
  }

  // 保存：diff 出 add/remove 一次 PUT 提交。无变更直接退出编辑态
  // （不调接口）；remove 只含当前实际存在的标记，规避 GitLab
  // remove_labels 对不存在标记返回 404 的行为。成功后本地标记即时
  // 更新并通知父组件刷新列表；失败保留编辑态可重试。
  async function saveLabels() {
    const current = currentLabels.map((l) => l.name)
    const add = selectedLabels.filter((n) => !current.includes(n))
    const remove = current.filter((n) => !selectedLabels.includes(n))
    if (add.length === 0 && remove.length === 0) {
      setEditingLabels(false)
      return
    }
    setSavingLabels(true)
    setLabelErr('')
    try {
      const d = await api.put(`/api/issues/${i.project_id}/${i.iid}/labels`,
                              { add, remove })
      setDisplayLabels(Array.isArray(d && d.labels) ? d.labels : [])
      setEditingLabels(false)
      onLabelsUpdated?.()
    } catch (e) {
      setLabelErr(e.message || '保存失败')
    } finally {
      setSavingLabels(false)
    }
  }

  // ---- issue #303：负责人编辑 ----

  // 可编辑条件：带 project_id（负责人接口按 project_id 定位仓库，
  // 旧缓存数据缺失时隐藏按钮，与关闭/标记按钮同约定）
  const canEditAssignee = typeof i.project_id === 'number'

  // 进入编辑态：加载项目成员（负责人下拉数据源，GitLab API 读取），
  // 预选当前负责人——按 username 在成员池匹配（负责人精简对象无 id，
  // 成员条目 username 唯一）；匹配不到（负责人已不是项目成员）或暂无
  // 负责人时回退「不指定」
  async function startEditAssignee() {
    setEditingAssignee(true)
    setAssigneeErr('')
    setSelectedAssigneeId(null)
    setMemberPool(null)
    setMemberPoolErr('')
    try {
      const d = await api.get(`/api/issues/${i.project_id}/members`)
      const members = Array.isArray(d && d.members) ? d.members : []
      setMemberPool(members)
      const first = currentAssignees[0]
      if (first && typeof first.username === 'string') {
        const hit = members.find(
          (m) => m && typeof m.username === 'string' && m.username === first.username)
        if (hit && typeof hit.id === 'number') setSelectedAssigneeId(hit.id)
      }
    } catch (e) {
      setMemberPoolErr(e.message || '加载失败')
    }
  }

  // 取消编辑：不调接口，丢弃本地选择，负责人显示保持原状
  function cancelEditAssignee() {
    setEditingAssignee(false)
    setAssigneeErr('')
  }

  // 保存：PUT 同步到 GitLab（assignee_id 为项目成员的用户 id，null
  // 清除负责人）。成功后本地负责人即时更新（displayAssignees 覆盖）
  // 并通知父组件刷新列表；失败保留编辑态可重试。
  async function saveAssignee() {
    setSavingAssignee(true)
    setAssigneeErr('')
    try {
      const d = await api.put(`/api/issues/${i.project_id}/${i.iid}/assignee`,
                              { assignee_id: selectedAssigneeId })
      setDisplayAssignees(Array.isArray(d && d.assignees) ? d.assignees : [])
      setEditingAssignee(false)
      onAssigneeUpdated?.()
    } catch (e) {
      setAssigneeErr(e.message || '保存失败')
    } finally {
      setSavingAssignee(false)
    }
  }

  // 编辑态渲染：成员加载失败（错误横幅 + 重试）/ 加载中 / 空池提示 /
  // 下拉选择（「不指定」+ 项目成员）。保存/取消按钮与标记编辑同布局
  function renderAssigneeEdit() {
    if (memberPoolErr) {
      return (
        <div className="issue-drawer-error" role="alert">
          {memberPoolErr}
          <button type="button" className="btn btn-small labels-retry"
                  onClick={startEditAssignee} title="重新加载项目成员">重试</button>
        </div>
      )
    }
    if (memberPool === null) return <p className="muted">加载成员中…</p>
    return (
      <div className="assignee-edit">
        {memberPool.length === 0 ? (
          <p className="muted">该仓库暂无成员</p>
        ) : (
          <select className="input assignee-select"
                  value={selectedAssigneeId == null ? '' : selectedAssigneeId}
                  onChange={(e) => setSelectedAssigneeId(
                    e.target.value === '' ? null : Number(e.target.value))}
                  title="选择该 issue 的负责人">
            <option value="">不指定</option>
            {memberPool.map((m) => (
              <option key={typeof m.id === 'number' ? m.id : m.username}
                      value={typeof m.id === 'number' ? m.id : ''}>
                {m.name || m.username || m.id}
              </option>
            ))}
          </select>
        )}
        {assigneeErr && <div className="issue-drawer-error" role="alert">{assigneeErr}</div>}
        <div className="labels-edit-actions">
          <button type="button" className="btn btn-small"
                  onClick={cancelEditAssignee}>取消</button>
          <button type="button" className="btn btn-small btn-primary"
                  disabled={savingAssignee} onClick={saveAssignee}>
            {savingAssignee ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    )
  }

  // 编辑态渲染：标记池加载失败（错误横幅 + 重试）/ 加载中 / 空池
  // 提示 / checkbox 多选（当前标记预勾选）。池外当前标记（组标签
  // 或已从标记库删除的标记）单独渲染为已勾选 checkbox——仍可取消
  // 勾选移除，不可再添加（池外标记无法从池内重新勾选）
  function renderLabelsEdit() {
    if (labelPoolErr) {
      return (
        <div className="issue-drawer-error" role="alert">
          {labelPoolErr}
          <button type="button" className="btn btn-small labels-retry"
                  onClick={startEditLabels} title="重新加载标记池">重试</button>
        </div>
      )
    }
    if (labelPool === null) return <p className="muted">加载标记中…</p>
    const poolNames = new Set((labelPool || []).map((l) => l.name))
    const outside = currentLabels.filter((l) => !poolNames.has(l.name))
    // 标记胶囊内联 span（与 AddIssueModal 标签多选结构一致）
    const choice = (l) => (
      <label key={l.name} className="label-choice">
        <input type="checkbox"
               checked={selectedLabels.includes(l.name)}
               onChange={() => toggleLabel(l.name)} />
        <span className="label-pill"
              style={l.color
                ? { background: `#${l.color}`, color: `#${l.text_color}` }
                : undefined}
              title={`标签 ${l.name}`}>{l.name}</span>
      </label>
    )
    return (
      <div className="labels-edit">
        {labelPool.length === 0 ? (
          <p className="muted">该仓库暂无标记</p>
        ) : (
          <div className="label-picker">{labelPool.map(choice)}</div>
        )}
        {outside.length > 0 && (
          <div className="label-picker">{outside.map(choice)}</div>
        )}
        {labelErr && <div className="issue-drawer-error" role="alert">{labelErr}</div>}
        <div className="labels-edit-actions">
          <button type="button" className="btn btn-small"
                  onClick={cancelEditLabels}>取消</button>
          <button type="button" className="btn btn-small btn-primary"
                  disabled={savingLabels} onClick={saveLabels}>
            {savingLabels ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    )
  }

  // 评论/活动区块的四态渲染：缺 project_id 旧数据 / 加载中 /
  // 加载失败（错误横幅在区块上方统一展示）/ 空列表与内容列表
  function renderNotesBody(list, emptyText, renderItem) {
    if (!hasDetail) return <p className="muted">无法加载（缺少仓库信息）</p>
    if (detailErr) return <p className="muted">加载失败</p>
    if (notes === null) return <p className="muted">加载中…</p>
    if (list.length === 0) return <p className="muted">{emptyText}</p>
    return <ul className={list === comments ? 'comment-list' : 'activity-list'}>
      {list.map(renderItem)}
    </ul>
  }

  // 合并时间线四态渲染（issue #342，与 renderNotesBody 同款：缺
  // project_id / 加载中 / 加载失败 / 空列表与内容列表）；内容按
  // created_at 升序交错，评论与活动条目复用各自渲染函数（时间线类名）
  function renderTimelineBody() {
    if (!hasDetail) return <p className="muted">无法加载（缺少仓库信息）</p>
    if (detailErr) return <p className="muted">加载失败</p>
    if (notes === null) return <p className="muted">加载中…</p>
    if (timelineItems.length === 0) return <p className="muted">暂无评论与活动</p>
    return <ul className="timeline-list">
      {timelineItems.map((n) => (n.system
        ? renderActivityItem(n, 'timeline-item timeline-activity', false)
        : renderCommentItem(n, 'timeline-item timeline-comment')))}
    </ul>
  }

  // ---- issue #125：添加评论与回复评论 ----

  // 评论行计数：轮询快照（issue 对象）+ 本次会话新增。新评论已在
  // 后端落库，但快照要等下一轮轮询才更新，本地先行叠加展示更准确
  const notesCount = localNotesCount ?? i.user_notes_count

  function bumpNotesCount() {
    setLocalNotesCount((prev) => (typeof prev === 'number' ? prev
      : (typeof i.user_notes_count === 'number' ? i.user_notes_count : 0)) + 1)
  }

  // 可评论条件：带 project_id/iid 且详情已加载成功（加载中/失败时
  // 隐藏输入区，避免对不可知目标发言）
  const canComment = hasDetail && !detailErr && notes !== null

  // 添加评论：POST /comments 提交正文（去空白非空才可提交）；成功后
  // 本地追加返回的评论并清空输入框（无需重拉详情）；失败保留输入
  // 内容可重试
  async function handlePostComment() {
    const text = commentText.trim()
    if (!text || posting) return
    setPosting(true)
    setPostErr('')
    try {
      const d = await api.post(`/api/issues/${i.project_id}/${i.iid}/comments`,
                               { body: text })
      if (d && d.note) {
        setNotes((prev) => [...(prev || []), d.note])
        setCommentText('')
        bumpNotesCount()
      }
    } catch (e) {
      setPostErr(e.message || '评论失败')
    } finally {
      setPosting(false)
    }
  }

  // 展开/收起某条评论的回复框（同一时刻只展开一个）
  function startReply(noteId) {
    setReplyingTo(noteId)
    setReplyText('')
    setReplyErr('')
  }

  function cancelReply() {
    setReplyingTo(null)
    setReplyText('')
    setReplyErr('')
  }

  // 发送回复：POST /comments/{note_id}/reply；成功后本地追加回复并
  // 收起回复框；失败保留输入内容可重试
  async function handleSendReply(noteId) {
    const text = replyText.trim()
    if (!text || replying) return
    setReplying(true)
    setReplyErr('')
    try {
      const d = await api.post(
        `/api/issues/${i.project_id}/${i.iid}/comments/${noteId}/reply`,
        { body: text })
      if (d && d.note) {
        setNotes((prev) => [...(prev || []), d.note])
        cancelReply()
        bumpNotesCount()
      }
    } catch (e) {
      setReplyErr(e.message || '回复失败')
    } finally {
      setReplying(false)
    }
  }

  // ---- issue #342：评论/活动条目渲染（分开显示与合并时间线共用，
  // 仅条目类名不同） ----

  // 评论条目：作者头像/名字/时间 + Markdown 正文 + 「回复」按钮与内联
  // 回复框（issue #125）；cls 为条目类名（分开='comment-item'，合并
  // 时间线='timeline-item timeline-comment'）
  function renderCommentItem(n, cls) {
    return (
      <li key={n.id} className={cls}>
        <div className="comment-head">
          <NoteAvatar note={n} />
          <span className="comment-author">{noteAuthorName(n)}</span>
          <span className="comment-time">{fmtTime(n.created_at)}</span>
        </div>
        <div className="comment-body">
          {n.body && String(n.body).trim() ? (
            <Markdown content={n.body} projectUrl={projectUrl} />
          ) : (
            <p className="muted">（无内容）</p>
          )}
        </div>
        {/* issue #125：回复评论——「回复」按钮展开内联回复框；
            发送成功后本地追加回复并收起（同评论列表结构） */}
        <div className="comment-actions">
          <button type="button"
                  className="btn btn-small comment-reply-btn"
                  onClick={() => startReply(n.id)}
                  title={`回复 ${noteAuthorName(n)}`}>回复</button>
        </div>
        {replyingTo === n.id && (
          <div className="comment-reply-box">
            <textarea className="comment-input" rows="2"
                      placeholder={`回复 @${noteAuthorName(n)}…`}
                      value={replyText}
                      onChange={(e) => setReplyText(e.target.value)} />
            {replyErr && (
              <div className="issue-drawer-error" role="alert">
                {replyErr}
              </div>
            )}
            <div className="comment-reply-actions">
              <button type="button" className="btn btn-small"
                      onClick={cancelReply} disabled={replying}>取消</button>
              <button type="button" className="btn btn-small btn-primary"
                      disabled={replying || !replyText.trim()}
                      onClick={() => handleSendReply(n.id)}>
                {replying ? '回复中…' : '发送回复'}
              </button>
            </div>
          </div>
        )}
      </li>
    )
  }

  // 活动条目：节点圆点 + 系统事件文本（提交引用渲染链接）+ 时间；
  // cls 为条目类名（分开='activity-item'，合并时间线='timeline-item
  // timeline-activity'）；合并时间线模式的节点圆点由 CSS 绘制（showDot
  // 传 false），避免与「•」重复
  function renderActivityItem(n, cls, showDot = true) {
    return (
      <li key={n.id} className={cls}>
        {showDot && <span className="activity-dot" title="系统活动">•</span>}
        <span className="activity-text">
          {n.body ? linkifyCommits(n.body, projectUrl, `act${n.id}-`)
                  : '（无内容）'}
        </span>
        {n.created_at && (
          <span className="activity-time">{fmtTime(n.created_at)}</span>
        )}
      </li>
    )
  }

  // 添加评论输入区（issue #125）：分开显示置于评论区底部、合并时间线
  // 置于时间线底部；可评论条件不满足时返回 null（不渲染）
  function renderCommentComposer() {
    if (!canComment) return null
    return (
      <div className="comment-composer">
        <textarea className="comment-input" rows="2"
                  placeholder="写下你的评论…"
                  value={commentText}
                  onChange={(e) => setCommentText(e.target.value)} />
        {postErr && (
          <div className="issue-drawer-error" role="alert">{postErr}</div>
        )}
        <div className="comment-composer-actions">
          <button type="button" className="btn btn-small btn-primary"
                  disabled={posting || !commentText.trim()}
                  onClick={handlePostComment}>
            {posting ? '发表中…' : '发表评论'}
          </button>
        </div>
      </div>
    )
  }

  // 抽屉操作按钮（issue #270）：同一组按钮在桌面端置于头部右侧
  // （.issue-drawer-actions）、移动端（≤860px）下沉到抽屉底部操作栏
  // （.drawer-bottom-actions，sticky 常驻 thumb 可及）——两个容器引用
  // 同一段 JSX，仅渲染一份按钮逻辑（测试按按钮语义断言时两处同时命中）
  const drawerActions = (
    <>
      {canClose && (
        <button className="btn btn-danger" onClick={handleCloseIssue}
                disabled={closing} title="关闭 GitLab 中的 issue">
          {closing ? '关闭中…' : '关闭 issue'}
        </button>
      )}
      {/* issue #117：失败任务（bot-failed 且无 bot-done）显示重试按钮；
          重试成功后本地标记 retried 隐藏；任务正在运行（running）时
          不显示（重试中该 issue 已进入「运行中」组） */}
      {isFailedTask(i) && !running && !retried && (
        <button className="btn btn-primary" onClick={handleRetry}
                disabled={retrying}
                title="重新执行该 issue 的任务">
          {retrying ? '重试中…' : '重试'}
        </button>
      )}
      {/* issue #242：排队任务优先处理——该 issue 最近任务处于排队中
          （queued）时展示「优先处理」按钮：把任务人工优先级置顶，
          调度器优先派发（已 running 任务不受影响，不展示） */}
      {taskStatus === 'queued' && (
        <button className="btn btn-primary" onClick={handlePrioritize}
                disabled={prioritizing || taskId == null}
                title="把该 issue 的排队任务置顶，调度器优先执行">
          {prioritizing ? '优先处理中…' : '优先处理'}
        </button>
      )}
      {/* issue #167：查看执行的详情——点击弹出第二层右边栏，展示
           该 issue 的任务执行详情（任务记录列表 + 事件流/聊天/
           日志）；无任务记录时第二层显示空态引导 */}
      {canViewDetail && (
        <button className="btn" onClick={() => setDetailOpen(true)}
                title="查看该 issue 任务的执行详情"
                disabled={detailOpen}>查看执行的详情</button>
      )}
      <a className="btn" href={i.web_url} target="_blank" rel="noreferrer"
         title="在 GitLab 中打开 issue">在 GitLab 中打开</a>
      {/* issue #330：× 关闭按钮并入 drawerActions——桌面端渲染在头部
          右侧（与原单独按钮同位置），移动端（≤860px）头部操作区被
          styles.css 整体隐藏后，底部操作栏仍保留关闭入口（此前底部
          操作栏只有「关闭 issue/重试/查看执行详情/在 GitLab 中打开」，
          窄视口无任何可见关闭按钮，只能 Esc/点遮罩；流水线右边栏
          详情页头部 × 不受隐藏规则影响、始终可见，本修复与其对齐） */}
      <button type="button" className="btn modal-close" onClick={onClose}
              title="关闭" aria-label="关闭右边栏"><Icon name="x" /></button>
    </>
  )

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer issue-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong className="issue-drawer-title">
            #{i.iid} {i.title || '—'}
          </strong>
          <span className="issue-drawer-actions">
            {drawerActions}
          </span>
        </div>
        {closeErr && <div className="issue-drawer-error" role="alert">{closeErr}</div>}
        {retryErr && <div className="issue-drawer-error" role="alert">{retryErr}</div>}
        {retryMsg && (
          <div className="alert alert-ok" role="status"
               onClick={() => setRetryMsg('')}>{retryMsg}</div>
        )}
        {prioritizeErr && <div className="issue-drawer-error" role="alert">{prioritizeErr}</div>}
        {prioritizeMsg && (
          <div className="alert alert-ok" role="status"
               onClick={() => setPrioritizeMsg('')}>{prioritizeMsg}</div>
        )}
        <table className="table kv">
          <tbody>
            <tr><th>仓库</th><td>{repoName || '—'}</td></tr>
            <tr><th>状态</th>
              <td><span className={'badge ' + stateMeta.cls}>{stateMeta.label}</span></td></tr>
            {/* issue #118/#120：任务执行引擎类型——展示该 issue 最近
                任务实际使用的引擎（claude / hermes / dsh，无任务回退
                全局配置）；detail 加载失败显示「—」，其余信息不受影响 */}
            <tr><th>执行引擎</th>
              <td>
                {engineErr ? (
                  <span title={`加载失败：${engineErr}`}>—</span>
                ) : (
                  <span title={`任务执行引擎 ${engine || '加载中'}`}>
                    {engineDisplay(engine)}
                  </span>
                )}
              </td></tr>
            {/* issue #290：任务 id——已执行过（有任务记录）显示对应
                任务 id；从未执行/加载失败显示「—」；详情加载中显示
                「加载中…」 */}
            <tr><th>任务</th>
              <td>
                {detailErr ? (
                  <span title={`加载失败：${detailErr}`}>—</span>
                ) : notes === null ? (
                  <span className="muted">加载中…</span>
                ) : typeof taskId === 'number' ? (
                  <span title={`该 issue 最近一次任务 #${taskId}`}>#{taskId}</span>
                ) : '—'}
              </td></tr>
            {/* issue #300：完成耗时——任务已完成（最近任务 succeeded）
                显示人类可读完成耗时（finished_at - created_at）；未
                完成/从未执行/加载失败/异常数据显示「—」；详情加载中
                显示「加载中…」 */}
            <tr><th>完成耗时</th>
              <td>
                {detailErr ? (
                  <span title={`加载失败：${detailErr}`}>—</span>
                ) : notes === null ? (
                  <span className="muted">加载中…</span>
                ) : typeof taskDuration === 'number' ? (
                  <span title={`该 issue 最近任务完成耗时 ${fmtSeconds(taskDuration)}`}>
                    {fmtSeconds(taskDuration)}
                  </span>
                ) : '—'}
              </td></tr>
            <tr><th>作者</th><td>{author}</td></tr>
            <tr><th>创建时间</th><td>{fmtTime(i.created_at)}</td></tr>
            <tr><th>更新时间</th><td>{fmtTime(i.updated_at)}</td></tr>
            <tr><th>标签</th>
              <td>
                {editingLabels ? (
                  renderLabelsEdit()
                ) : (
                  <>
                    {currentLabels.length > 0 ? (
                      currentLabels.map((l) => (
                        <span key={l.name} className="label-pill"
                              style={l.color
                                ? { background: `#${l.color}`, color: `#${l.text_color}` }
                                : undefined}
                              title={`标签 ${l.name}`}>{l.name}</span>
                      ))
                    ) : '—'}
                    {canEditLabels && (
                      <button type="button" className="btn btn-small labels-edit-btn"
                              onClick={startEditLabels} title="编辑标记">
                        编辑标记
                      </button>
                    )}
                  </>
                )}
              </td></tr>
            <tr><th>里程碑</th><td>{i.milestone || '—'}</td></tr>
            <tr><th>负责人</th>
              <td>
                {editingAssignee ? (
                  renderAssigneeEdit()
                ) : (
                  <>
                    {assigneeNames.length > 0 ? assigneeNames.join('、') : '—'}
                    {canEditAssignee && (
                      <button type="button" className="btn btn-small labels-edit-btn"
                              onClick={startEditAssignee}
                              title="修改该 issue 的负责人">编辑</button>
                    )}
                  </>
                )}
              </td></tr>
            <tr><th>评论</th>
              {/* issue #125：计数 = 轮询快照 + 本次会话新增（新评论
                  已落库，快照要等下一轮轮询才更新） */}
              <td>{typeof notesCount === 'number' ? notesCount : '—'}</td></tr>
          </tbody>
        </table>
        <div className="issue-drawer-desc">
          <h3>描述</h3>
          {i.description && String(i.description).trim() ? (
            <Markdown content={i.description} projectUrl={projectUrl} />
          ) : (
            <p className="muted">暂无描述</p>
          )}
        </div>
        {/* issue #97：评论与活动区块——评论（用户发言，Markdown 渲染）与
            活动（系统事件，纯文本）按 note.system 标志分区展示；加载失败
            时错误横幅 + 重试按钮置于两区块上方共用 */}
        {hasDetail && detailErr && (
          <div className="issue-drawer-error" role="alert">
            {detailErr}
            <button type="button" className="btn btn-small notes-retry"
                    onClick={loadNotes} title="重新加载评论与活动">重试</button>
          </div>
        )}
        <div className="issue-notes">
          {timeline ? (
            /* issue #342：合并时间线——评论（用户发言）与活动（系统事件）
               按时间交错为一条时间线（类似 GitLab issue 时间线），设置页
               「界面显示」开关 botler.timeline 切换；加载失败错误横幅 +
               重试按钮仍在两区块上方共用，添加评论输入区保留在时间线底部 */
            <div className="issue-notes-block timeline-block">
              <h3>评论与活动（时间线）</h3>
              {renderTimelineBody()}
              {renderCommentComposer()}
            </div>
          ) : (
            <>
              <div className="issue-notes-block">
                <h3>评论</h3>
                {renderNotesBody(comments, '暂无评论',
                  (n) => renderCommentItem(n, 'comment-item'))}
                {renderCommentComposer()}
              </div>
              <div className="issue-notes-block">
                <h3>活动</h3>
                {renderNotesBody(activities, '暂无活动',
                  (n) => renderActivityItem(n, 'activity-item'))}
              </div>
            </>
          )}
        </div>
        {/* 移动端底部操作栏（issue #270）：与头部同一组 drawerActions，
            仅 ≤860px 视口显示（styles.css 控制），sticky 常驻抽屉底部，
            thumb 可及——关闭 issue/重试/查看执行详情/在 GitLab 中打开。
            issue #326：该栏必须是 .drawer 的「最后一个子项」而非
            .drawer-overlay 的兄弟节点——overlay 是 flex 行布局
            （justify-content: flex-end），兄弟节点会与抽屉横向排布，
            竖屏窄视口下把抽屉挤出屏幕左侧（实测 375px 视口抽屉左移
            131.5px，内容截断、遮罩右侧露出主页面）；移入抽屉后随
            margin-top:auto + sticky bottom 推底并常驻，不再参与
            overlay 的横向布局 */}
        <div className="drawer-bottom-actions">
          {drawerActions}
        </div>
      </div>
      {/* issue #167：任务执行详情第二层右边栏——叠加在本层抽屉之上，
          遮罩点击/×/Esc 关闭；project_id/iid 与 repoName 传给第二层
          用于拉取该 issue 的任务执行记录 */}
      {detailOpen && (
        <TaskDetailDrawer projectId={i.project_id} issueIid={i.iid}
                          issueTitle={i.title} repoName={repoName}
                          onClose={() => setDetailOpen(false)} />
      )}
    </div>
  )
}
