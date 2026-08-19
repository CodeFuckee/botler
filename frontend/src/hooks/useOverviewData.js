// 概览页数据加载 / 状态管理 / 轮询 hook（issue #201 拆分）：
// 把 Overview.jsx 内全部数据加载、轮询、增删改处理器、键盘快捷键与
// 派生数据收敛到本 hook，页面组件只接数据（数据加载与轮询抽
// usePolling hook，组件只接数据）。
import { useCallback, useEffect, useRef, useState } from 'react'
import { usePolling } from './usePolling.js'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import { useShortcuts } from '../keymap.js'
import {
  MAX_CARD_LINES,
  OVERVIEW_POLL_MS,
  PIPELINE_POLL_MS,
  ISSUE_POLL_MS,
  INSPIRATION_POLL_MS,
  DEEPSEEK_BALANCE_POLL_MS,
  runningIssueKeys,
  DEFAULT_ISSUE_PRIORITY,
  loadIssueFilter,
  saveIssueFilter,
  loadCollapsedGroups,
  saveCollapsedGroups,
  loadIssueSort,
  saveIssueSort,
  sortIssuesByMethod,
  filterIssuesByFilter,
  collectLabelOptions,
  MANUAL_ORDER_LOCAL_TTL_MS,
  moveItem,
  pinIssueToTop,
  trimLogTail,
  eventToLine,
} from '../lib/overview.jsx'
import {
  appendBalanceSample,
  computeBalanceRate,
  loadBalanceHistory,
  saveBalanceHistory,
} from '../balanceRate.js'

export function useOverviewData() {
  // 界面国际化（issue #268）：静态 UI 文案经 t() 翻译（默认中文）
  const { tr } = useI18n()
  const [tasks, setTasks] = useState([])
  const [liveLines, setLiveLines] = useState({}) // taskId -> 实时输出行数组
  const [error, setError] = useState('')
  // 流水线状态（issue #39）：所有配置仓库（含未启用，第二轮）的最新 CI/CD 流水线
  const [pipelines, setPipelines] = useState([])
  // issue #317：当前选中查看详情的流水线条目（点击流水线卡片打开右边栏）
  const [selectedPipeline, setSelectedPipeline] = useState(null)
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
  // 自省结果（issue #187）: repoId -> {loading/created/error}——点击
  // 「自省」按钮调用 agent 审查仓库并把建议写入该仓库 issue 的结果
  const [introspectResults, setIntrospectResults] = useState({})
  // 发掘结果（issue #189）: repoId -> {loading/created/error}——点击
  // 「发掘」按钮让 agent 根据仓库实现的功能去 GitHub 搜索类似仓库、翻找
  // 用户需求，整理成需求写入该仓库 issue 的结果
  const [discoverResults, setDiscoverResults] = useState({})
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
  // 每小时余额变化速率（issue #304）：{currency: {ratePerHour, windowMs}}，
  // 由 balanceRate 模块基于 localStorage 历史观测样本计算（纯前端）
  const [dsRate, setDsRate] = useState({})
  // 任务集合签名：任务增删 / 状态变化时重建事件流连接
  const tasksKey = tasks.map((t) => `${t.id}:${t.status}`).sort().join('|')

  // issue #99：正在运行的 issue 匹配键集合（repo_id:iid），任务每 3 秒
  // 轮询刷新，任务结束（消失）后对应 issue 高亮自动消失
  const runningKeys = runningIssueKeys(tasks)

  // issue #230：开放 issue 过滤偏好——标签多选 + 状态（全部/开放/进行
  // 中）。初值从 localStorage 恢复（无存储环境/解析失败回默认），变更
  // 时持久化，刷新后保持
  const [issueFilter, setIssueFilter] = useState(() =>
    loadIssueFilter(typeof localStorage !== 'undefined' ? localStorage : null))

  // 过滤偏好持久化（issue #230）：localStorage 不可用（SSR/隐私模式）
  // 时静默忽略，不影响页面使用
  useEffect(() => {
    saveIssueFilter(typeof localStorage !== 'undefined' ? localStorage : null, issueFilter)
  }, [issueFilter])

  // issue #285：分组折叠偏好——初值从 localStorage 恢复（无存储环境/
  // 解析失败回全展开），变更时持久化，刷新后保持
  const [collapsedGroups, setCollapsedGroups] = useState(() =>
    loadCollapsedGroups(typeof localStorage !== 'undefined' ? localStorage : null))

  useEffect(() => {
    saveCollapsedGroups(typeof localStorage !== 'undefined' ? localStorage : null,
                        collapsedGroups)
  }, [collapsedGroups])

  // issue #286：开放 issue 排序偏好——初值从 localStorage 恢复（无存储
  // 环境/损坏数据回默认「调度器执行顺序」），变更时持久化，刷新后保持
  const [issueSort, setIssueSort] = useState(() =>
    loadIssueSort(typeof localStorage !== 'undefined' ? localStorage : null))

  useEffect(() => {
    saveIssueSort(typeof localStorage !== 'undefined' ? localStorage : null,
                  issueSort)
  }, [issueSort])

  // issue #287：「其他」分组手动调度顺序——repo_id → iid 数组（数据源为
  // /api/issues/overview 的 manual_order 字段，拖动保存后本地乐观更新）。
  // manualSaving 记录保存中的仓库（轮询覆盖时保留乐观顺序，避免旧缓存
  // 回弹）；manualErrors 记录保存失败提示（点击可关闭）。仅「调度器执行
  // 顺序」排序 + 「其他」分组 + 无过滤时启用拖动（过滤子集拖动会误清
  // 未显示条目的顺序，故过滤时禁用）
  const [manualOrders, setManualOrders] = useState({})
  const [manualSaving, setManualSaving] = useState(() => new Set())
  const [manualErrors, setManualErrors] = useState({})
  // 同步镜像 ref：loadIssues 的 useCallback 依赖保持稳定（[]），轮询
  // 回调读取 ref 而非 state，避免保存过程中轮询 effect 重建导致的
  // 请求风暴与旧数据回弹
  const manualSavingRef = useRef(new Set())
  // 保存成功后「本地优先」窗口到期时间戳（repo_id → Date.now()+TTL）
  const manualLocalUntilRef = useRef({})
  const setManualSavingBoth = (repoId, add) => {
    if (add) manualSavingRef.current.add(repoId)
    else manualSavingRef.current.delete(repoId)
    setManualSaving((prev) => {
      const next = new Set(prev)
      if (add) next.add(repoId)
      else next.delete(repoId)
      return next
    })
  }
  // 拖拽过程状态：dragFrom = { repoId, from }（拖起位置），
  // dragOverIndex = 当前悬停目标索引（落点高亮）
  const [dragFrom, setDragFrom] = useState(null)
  const [dragOverIndex, setDragOverIndex] = useState(null)

  // 保存手动调度顺序：乐观更新本地顺序，PUT 失败回滚到保存前顺序并提示
  const saveManualOrder = async (repo, iids, prevIids) => {
    setManualSavingBoth(repo.repo_id, true)
    setManualErrors((prev) => {
      if (prev[repo.repo_id] == null) return prev
      const next = { ...prev }
      delete next[repo.repo_id]
      return next
    })
    try {
      const d = await api.put(
        `/api/issues/${repo.project_id}/manual-orders`, { iids })
      setManualOrders((prev) => ({
        ...prev,
        [repo.repo_id]: Array.isArray(d && d.iids) ? d.iids : iids,
      }))
      // 保存成功：登记本地优先窗口，防止轮询携带旧缓存回弹
      manualLocalUntilRef.current[repo.repo_id] =
        Date.now() + MANUAL_ORDER_LOCAL_TTL_MS
    } catch (e) {
      setManualOrders((prev) => ({ ...prev, [repo.repo_id]: prevIids }))
      setManualErrors((prev) => ({
        ...prev,
        [repo.repo_id]: tr('overview.manualOrderError', { msg: e.message }),
      }))
    } finally {
      setManualSavingBoth(repo.repo_id, false)
    }
  }

  // 拖拽落点提交：把拖动项插入目标位置，整组顺序全量保存
  const commitManualReorder = async (repo, ordered, toIndex) => {
    const from = dragFrom && dragFrom.repoId === repo.repo_id
      ? dragFrom.from : null
    setDragFrom(null)
    setDragOverIndex(null)
    if (from == null || from === toIndex) return
    const reordered = moveItem(ordered, from, toIndex)
    const iids = reordered
      .map((it) => (it && Number.isInteger(it.iid) ? it.iid : null))
      .filter((v) => v != null)
    const prevIids = manualOrders[repo.repo_id] || []
    setManualOrders((prev) => ({ ...prev, [repo.repo_id]: iids }))
    await saveManualOrder(repo, iids, prevIids)
  }

  // issue #308：「其他」分组置顶按钮——把 issue 移到手动调度顺序最前并
  // 保存（复用 #287 的手动顺序机制与 PUT 接口，调度器优先按手动顺序
  // 派发，置顶即第一个处理）。已置顶（手动顺序首位）时直接返回，避免
  // 无意义的重复保存；保存失败由 saveManualOrder 统一回滚并提示
  const pinIssue = async (repo, issue) => {
    if (issue == null || !Number.isInteger(issue.iid)) return
    const prevIids = manualOrders[repo.repo_id] || []
    if (prevIids[0] === issue.iid) return
    const nextIids = pinIssueToTop(prevIids, issue.iid)
    setManualOrders((prev) => ({ ...prev, [repo.repo_id]: nextIids }))
    await saveManualOrder(repo, nextIids, prevIids)
  }

  // 调度器 issue 标签优先级（issue #286）：从 /api/settings 读取
  // worker.issue_priority（与调度器动态读取同一配置源），用于「调度器
  // 执行顺序」排序；接口失败/未配置回退内置默认（DEFAULT_ISSUE_PRIORITY）
  const [issuePriority, setIssuePriority] = useState(DEFAULT_ISSUE_PRIORITY)

  // 过滤后的仓库 issue（issue #230）：保留仓库分组结构、仅过滤条目。
  // 未过滤时全部仓库卡片照常渲染（含零 issue 仓库的仓库级空状态）；
  // 过滤激活时无匹配条目的仓库整卡隐藏（避免空卡噪音）。标签候选
  // 来自未过滤全量数据，过滤后候选标签不因筛选消失
  const issueFilterActive = issueFilter.status !== 'all' || issueFilter.labels.length > 0
  const issueLabelOptions = collectLabelOptions(repoIssues)
  const hasAnyIssue = repoIssues.some((r) => (r.issues || []).length > 0)
  // issue #286：按所选排序方法重排仓库内 issue（排序在前、过滤在后——
  // 过滤只做子集不重排，最终展示顺序即排序结果）。仓库卡片外层顺序仍
  // 由后端按仓库优先级升序保证（与调度器仓库优先级派发一致）
  const sortedRepoIssues = repoIssues
    .map((r) => ({ ...r, issues: sortIssuesByMethod(r.issues, issueSort, issuePriority) }))
  const filteredRepoIssues = sortedRepoIssues
    .map((r) => ({
      ...r,
      issues: filterIssuesByFilter(r.issues, issueFilter, runningKeys, r.repo_id),
    }))
    .filter((r) => !issueFilterActive || (r.issues || []).length > 0)

  // 拉取全部正在执行的任务（running+retrying 多值过滤，issue #32）
  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ status: 'running,retrying', limit: '200' })
      const d = await api.get('/api/tasks?' + q, { silent: true })
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
    // tasksKey 是 tasks 的稳定签名（id:status 拼接）：轮询返回相同内容时
    // tasks 引用变化但签名不变，依赖 tasksKey 避免无谓重建 SSE 连接
    // （依赖 tasks 会每次轮询断开重连，产生可见闪烁/抖动）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasksKey])

  // 所有配置仓库的最新流水线状态（issue #39，独立慢轮询）
  const loadPipelines = useCallback(async () => {
    try {
      const d = await api.get('/api/pipelines/overview', { silent: true })
      setPipelines(d.pipelines || [])
      setPipeErrors(d.errors || [])
      setPipeError('')
    } catch (e) {
      setPipeError(e.message)
    }
  }, [])

  // 任务列表轮询（issue #200：页面隐藏时暂停、恢复可见立即拉一次，
  // 由 usePolling 统一处理可见性）
  usePolling(load, OVERVIEW_POLL_MS)

  usePolling(loadPipelines, PIPELINE_POLL_MS)

  // 已启用仓库的开放 issue 聚合（issue #64，独立慢轮询）
  const loadIssues = useCallback(async () => {
    try {
      const d = await api.get('/api/issues/overview', { silent: true })
      setRepoIssues(d.repos || [])
      setIssueErrors(d.errors || [])
      setIssueError('')
      // issue #287：同步各仓库手动调度顺序（manual_order 字段，旧数据
      // 缺失时按空列表处理）；保存中的仓库（manualSavingRef）与保存成功
      // 后 20 秒本地优先窗口内（manualLocalUntilRef）保留本地顺序，防止
      // 轮询请求先于 PUT 发出的旧缓存回弹（PUT 已清 overview 缓存，窗口
      // 过后服务端数据必为新顺序，恢复以服务端为准）
      setManualOrders((prev) => {
        const next = {}
        for (const r of d.repos || []) {
          next[r.repo_id] = Array.isArray(r.manual_order) ? r.manual_order : []
        }
        const now = Date.now()
        for (const rid of manualSavingRef.current) {
          if (next[rid] !== undefined) next[rid] = prev[rid]
        }
        for (const [rid, until] of Object.entries(manualLocalUntilRef.current)) {
          if (until > now && next[rid] !== undefined) next[rid] = prev[rid]
        }
        return next
      })
    } catch (e) {
      setIssueError(e.message)
    }
  }, [])

  usePolling(loadIssues, ISSUE_POLL_MS)

  // issue #132：owner token 配置状态（启动时检测一次；配置变化由设置页
  // 保存后手动刷新页面生效）
  useEffect(() => {
    api.get('/api/settings')
      .then((d) => {
        setOwnerTokenOk(!!(d.gitlab && d.gitlab.owner_token_masked))
        // issue #286：同步调度器 issue 标签优先级（设置页可自定义），
        // 排序「调度器执行顺序」据此计算；缺失/非法回退内置默认
        const prio = d && d.worker && d.worker.issue_priority
        if (Array.isArray(prio) && prio.length > 0) setIssuePriority(prio)
      })
      .catch(() => setOwnerTokenOk(true)) // 读取失败不打扰编辑（后端自会拦截）
  }, [])

  // 灵感聚合（issue #131）：本地数据库数据，独立慢轮询；本地增删改
  // 提交成功后手动刷新，轮询兜底多标签页并发场景
  const loadInspirations = useCallback(async () => {
    try {
      const d = await api.get('/api/inspirations/overview', { silent: true })
      setInspirationRepos(d.repos || [])
      setInspirationError('')
    } catch (e) {
      setInspirationError(e.message)
    }
  }, [])

  usePolling(loadInspirations, INSPIRATION_POLL_MS)

  // DeepSeek 账户余额（issue #138）：后端代调 deepseek user/balance，
  // API Key 明文不流转到前端（与 ai_providers 掩码同安全策略）；
  // 未配置时后端返回 configured=false，前端不渲染余额卡片
  const loadDeepSeekBalance = useCallback(async () => {
    try {
      const d = await api.get('/api/settings/deepseek-balance', { silent: true })
      setDsBalance(d || { configured: false, balance: null, error: null })
      setDsBalanceError('')
      // issue #304：余额变化速率（按小时）——每次成功获取余额后把观测
      // 样本追加到 localStorage 历史（键 botler.overview.dsBalanceHistory），
      // 按最早/最近观测窗口计算每小时平均变化速率；纯前端计算，后端
      // 无改动；样本不足/窗口过短时速率对象为空，界面展示「暂无速率
      // 数据」，不影响余额卡片其他内容
      if (d && d.configured && d.balance
          && Array.isArray(d.balance.balance_infos)) {
        const storage = typeof localStorage !== 'undefined' ? localStorage : null
        const now = Date.now()
        const history = appendBalanceSample(
          loadBalanceHistory(storage), now, d.balance.balance_infos)
        saveBalanceHistory(storage, history)
        setDsRate(computeBalanceRate(history))
      }
    } catch (e) {
      // 余额展示尽力而为：接口失败不打扰页面，保留上次数据（未加载
      // 成功过则 dsBalance 保持 null，卡片不渲染）
      setDsBalanceError(e.message)
    }
  }, [])

  usePolling(loadDeepSeekBalance, DEEPSEEK_BALANCE_POLL_MS)

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

  // 自省（issue #187）：调用后端同步审查接口——AI agent 审查该仓库的
  // 功能与实现情况，把改进建议写入该仓库的 issue（分配人 = 仓库 owner）。
  // 审查 + 建 issue 为同步请求（最长约 2 分钟），请求中禁用按钮防重复
  // 点击；成功后刷新开放 issue 列表（新 issue 立即出现在列表顶部）。
  const introspectRepo = useCallback(async (repo) => {
    setIntrospectResults((prev) => ({ ...prev, [repo.repo_id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.repo_id}/introspect`)
      setIntrospectResults((prev) => ({ ...prev, [repo.repo_id]: { created: res.issue } }))
      await loadIssues()
    } catch (e) {
      setIntrospectResults((prev) => ({ ...prev, [repo.repo_id]: { error: e.message } }))
    }
  }, [loadIssues])

  // 发掘（issue #189）：调用后端同步发掘接口——AI agent 根据该仓库实现的
  // 功能去 GitHub 搜索类似仓库、翻找用户需求 issue，整理成若干条需求写入
  // 该仓库的 issue（分配人 = 仓库 owner，一条需求一个 issue）。同步请求
  // 耗时较长（AI 两轮 + GitHub 采集 + 建 issue），请求中禁用按钮防重复
  // 点击；成功后刷新开放 issue 列表（新 issue 立即出现在列表顶部）。
  const discoverRepo = useCallback(async (repo) => {
    setDiscoverResults((prev) => ({ ...prev, [repo.repo_id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.repo_id}/discover`)
      // issue #301：响应同时携带 similar_repos 与 count，一并存入结果
      // （无论是否找到用户需求 issue，相似仓库列表都随响应返回）
      setDiscoverResults((prev) => ({ ...prev, [repo.repo_id]: {
        created: res.issues, count: res.count, similar_repos: res.similar_repos } }))
      await loadIssues()
    } catch (e) {
      setDiscoverResults((prev) => ({ ...prev, [repo.repo_id]: { error: e.message } }))
    }
  }, [loadIssues])

  // 键盘快捷键（issue #269）：页面级绑定——n = 打开首个仓库的
  // 「添加 Issue」弹窗（与点击仓库卡片右上角按钮等效），r = 手动
  // 刷新当前页数据（开放 issue / 活跃任务 / 流水线 / 灵感，均走
  // 已有加载函数，低危操作无需确认）。输入框聚焦自动不触发、开关
  // 关闭全部失效（keymap.js 统一处理）；弹窗已打开时 n 不再重复
  // 打开（避免覆盖用户正在填写的表单）
  useShortcuts({
    'new-issue': () => {
      if (repoIssues.length > 0 && !addIssueRepo) setAddIssueRepo(repoIssues[0])
    },
    'refresh': () => {
      loadIssues()
      load()
      loadPipelines()
      loadInspirations()
    },
  }, { storage: typeof localStorage !== 'undefined' ? localStorage : null })

  // 各任务信息块实时输出自动滚动到底部（issue #114：任务板块删除后
  // 任务块迁入开放 issue 列表项内；SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    document.querySelectorAll('.issue-task-log').forEach((el) => {
      el.scrollTop = el.scrollHeight
    })
  }, [liveLines])


  // ---- 页面组件消费的数据与处理器（issue #201）----
  return {
    tasks, liveLines, error,
    pipelines, selectedPipeline, setSelectedPipeline, pipeErrors, pipeError,
    repoIssues, issueErrors, issueError, ownerTokenOk,
    selectedIssue, setSelectedIssue,
    addIssueRepo, setAddIssueRepo,
    reconcileResults, introspectResults, discoverResults,
    inspirationRepos, inspirationError,
    newInspirationDrafts, setNewInspirationDrafts,
    editingInspiration, setEditingInspiration,
    editInspirationDraft, setEditInspirationDraft,
    addingIssueInspIds, inspirationCreatedIssue, setInspirationCreatedIssue,
    chatInspiration, chatMessages, chatLoading,
    chatDraft, setChatDraft, chatSending, chatError,
    dsBalance, dsBalanceError, dsRate,
    issueFilter, setIssueFilter,
    collapsedGroups, setCollapsedGroups,
    issueSort, setIssueSort,
    manualOrders, manualSaving, manualErrors, setManualErrors,
    dragFrom, setDragFrom, dragOverIndex, setDragOverIndex,
    issuePriority,
    runningKeys, issueFilterActive, issueLabelOptions, hasAnyIssue,
    sortedRepoIssues, filteredRepoIssues,
    saveManualOrder, commitManualReorder, pinIssue,
    load, loadPipelines, loadIssues, loadInspirations, loadDeepSeekBalance,
    submitNewInspiration, saveInspiration, deleteInspiration,
    addIssueFromInspiration,
    openInspirationChat, closeInspirationChat, sendInspirationChat,
    reconcileRepo, introspectRepo, discoverRepo,
  }
}
