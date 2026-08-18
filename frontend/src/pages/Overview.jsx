import { useCallback, useEffect, useRef, useState } from 'react'
import { api, STATUS_META, shortSha, fmtTime, fmtAgo, fmtSeconds, summarizeToolInput } from '../api.js'
import IssueDrawer, { ENGINE_META } from '../components/IssueDrawer.jsx'
import { useI18n } from '../i18n.jsx'
import { Icon } from '../components/Icon.jsx'
import { fmtTokens, fmtCost } from '../components/UsageCard.jsx'
import AddIssueModal from '../components/AddIssueModal.jsx'
import { useShortcuts } from '../keymap.js'

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

// Token 用量统计轮询间隔（issue #235）：数据来自本地 task_usage 表
// （GET /api/usage/stats），无 GitLab 请求压力，沿用 60 秒低频轮询
export const USAGE_STATS_POLL_MS = 60000

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

// ---- issue #230：开放 issue 过滤（标签多选 + 状态）----
// 概览页开放 issue 支持按标签多选（命中任一选中标签即展示）与状态
// （全部/开放/进行中）过滤，仅过滤条目、保留仓库分组结构；过滤偏好
// 持久化 localStorage（键 botler.overview.issueFilter），刷新后保持。
export const ISSUE_FILTER_STORAGE_KEY = 'botler.overview.issueFilter'

// 状态过滤选项：all=全部（不过滤）；open=开放（无运行中任务，含
// bot-failed/bot-done/其他分组）；running=进行中（有 running/retrying
// 任务，与置顶 running 分组同源判定）。hint 为按钮悬浮说明文案
export const ISSUE_STATUS_FILTERS = [
  { key: 'all', label: '全部', hint: '展示全部开放 issue' },
  { key: 'open', label: '开放', hint: '仅展示无运行中任务的开放 issue（bot-failed / bot-done / 其他）' },
  { key: 'running', label: '进行中', hint: '仅展示正在被 bot 执行的 issue' },
]

// 读取本地过滤偏好：非法 JSON / 未知状态 / 标签非数组或含非字符串
// 元素时逐项兜底回默认值（手改/旧版本写入），不抛错。
// storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
// getItem 抛异常（隐私模式）时返回默认 { status: 'all', labels: [] }
export function loadIssueFilter(storage) {
  const def = { status: 'all', labels: [] }
  try {
    if (!storage) return def
    const raw = storage.getItem(ISSUE_FILTER_STORAGE_KEY)
    if (!raw) return def
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return def
    const status = ISSUE_STATUS_FILTERS.some((s) => s.key === parsed.status)
      ? parsed.status : 'all'
    const labels = Array.isArray(parsed.labels)
      ? parsed.labels.filter((l) => typeof l === 'string')
      : []
    return { status, labels }
  } catch {
    return def
  }
}

// 保存本地过滤偏好：只写合法结构（未知状态回 all、非字符串标签剔除）；
// 存储不可用（SSR/隐私模式）或 setItem 抛异常时静默忽略，不影响页面
export function saveIssueFilter(storage, filter) {
  try {
    if (!storage || !filter || typeof filter !== 'object') return
    const status = ISSUE_STATUS_FILTERS.some((s) => s.key === filter.status)
      ? filter.status : 'all'
    const labels = Array.isArray(filter.labels)
      ? filter.labels.filter((l) => typeof l === 'string')
      : []
    storage.setItem(ISSUE_FILTER_STORAGE_KEY, JSON.stringify({ status, labels }))
  } catch {
    /* 无存储环境：静默忽略，不影响页面使用 */
  }
}

// ---- issue #285：概览页 issue 分组折叠/展开 ----
// 分组头部增加折叠开关（chevronRight/chevronDown），折叠后隐藏组内
// issue 列表、保留组标题与计数，方便用户折叠长列表；折叠偏好存
// localStorage（键 botler.overview.collapsedGroups），刷新后保持。
export const GROUP_COLLAPSE_STORAGE_KEY = 'botler.overview.collapsedGroups'

// 读取折叠偏好：localStorage 兼容对象（测试可注入）；无存储环境或
// getItem 抛异常（隐私模式）时返回空 Set（全展开）。值须为 JSON 数组
// 且元素为已知分组 key（running/failed/done/other），非法元素剔除
export function loadCollapsedGroups(storage) {
  const known = new Set(ISSUE_GROUPS.map((g) => g.key))
  const out = new Set()
  try {
    if (!storage) return out
    const raw = storage.getItem(GROUP_COLLAPSE_STORAGE_KEY)
    if (!raw) return out
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return out
    for (const k of parsed) {
      if (typeof k === 'string' && known.has(k)) out.add(k)
    }
  } catch {
    /* 无存储环境/损坏数据：静默回退全展开 */
  }
  return out
}

// 保存折叠偏好：只写合法分组 key 数组；存储不可用或 setItem 抛异常时
// 静默忽略，不影响页面使用
export function saveCollapsedGroups(storage, collapsed) {
  try {
    if (!storage || !collapsed) return
    const known = new Set(ISSUE_GROUPS.map((g) => g.key))
    const keys = Array.from(collapsed)
      .filter((k) => typeof k === 'string' && known.has(k))
    storage.setItem(GROUP_COLLAPSE_STORAGE_KEY, JSON.stringify(keys))
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

// 折叠状态切换（纯函数）：已折叠的分组展开、未折叠的分组折叠，返回
// 新 Set（不改动入参），供组件 setState 使用
export function toggleGroupCollapsed(collapsed, key) {
  const next = new Set(collapsed || [])
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}

// ---- issue #286：概览页开放 issue 排序方法切换 ----
// 开放 issue 板块支持切换排序方法，默认按「调度器执行顺序」排序——
// 与任务调度器派发语义一致（仓库优先级升序 → issue 标签优先级 →
// issue 创建时间升序，创建早的先处理），方便用户预判调度器接下来会
// 按什么顺序处理各分组（尤其是「其他」分组的未处理 issue，issue
// #287 在此基础上做拖动改调度顺序）。排序偏好存 localStorage（键
// botler.overview.issueSort），刷新后保持。
export const ISSUE_SORT_STORAGE_KEY = 'botler.overview.issueSort'

// 调度器默认 issue 标签优先级（issue #76）：设置页 worker.issue_priority
// 未配置/读取失败时前端兜底（后端 settings 接口恒有默认值，此处仅防御，
// 与 backend/config.py 默认一致）
export const DEFAULT_ISSUE_PRIORITY = ['bug', 'test', 'feature']

// 排序方法选项：scheduler=调度器执行顺序（默认）；updated=最近更新降序；
// created=创建时间降序。label 为 UI 展示文案（i18n key 见
// overview.sortBy.<key>），hint 为悬浮说明（overview.sortHint.<key>）
export const ISSUE_SORTS = [
  { key: 'scheduler', label: '调度器执行顺序',
    hint: '按调度器派发顺序：仓库优先级 → issue 标签优先级（默认 bug > test > feature）→ 创建时间升序' },
  { key: 'updated', label: '最近更新',
    hint: '按 issue 最后更新时间降序，最新更新在前' },
  { key: 'created', label: '创建时间',
    hint: '按 issue 创建时间降序，最新创建在前' },
]

// 读取排序偏好：localStorage 兼容对象（测试可注入）；无存储环境或
// getItem 抛异常（隐私模式）时回默认「调度器执行顺序」。未知排序键
// （手改/旧版本写入）同样回默认，不抛错
export function loadIssueSort(storage) {
  try {
    if (!storage) return 'scheduler'
    const raw = storage.getItem(ISSUE_SORT_STORAGE_KEY)
    if (!raw) return 'scheduler'
    return ISSUE_SORTS.some((s) => s.key === raw) ? raw : 'scheduler'
  } catch {
    return 'scheduler'
  }
}

// 保存排序偏好：只接受 ISSUE_SORTS 已知排序键；存储不可用或 setItem
// 抛异常时静默忽略，不影响页面使用
export function saveIssueSort(storage, sortKey) {
  try {
    if (!storage || !sortKey) return
    if (!ISSUE_SORTS.some((s) => s.key === sortKey)) return
    storage.setItem(ISSUE_SORT_STORAGE_KEY, sortKey)
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

// issue 标签在调度器优先级列表中的权重（issue #286，语义对齐
// scheduler._task_sort_key）：首个命中标签的索引即权重（越靠前越先
// 处理），未命中任何配置标签（或无标签）排最后（权重 = len(priority)）。
// priority 非数组/为空时用内置默认（与后端 issue_priority_labels 兜底一致）
export function issueLabelWeight(issue, priority) {
  const list = Array.isArray(priority) && priority.length > 0
    ? priority : DEFAULT_ISSUE_PRIORITY
  const names = new Set(issueLabelNames(issue))
  for (let i = 0; i < list.length; i++) {
    if (names.has(list[i])) return i
  }
  return list.length
}

// 调度器执行顺序排序键（issue #286）：(标签权重, 创建时间)。
// 同权重按 issue 创建时间升序（创建早的先处理，issue #234）；创建
// 时间缺失按更新时间兜底（与 scheduler._task_sort_key 语义一致），
// UTC 无后缀串可直接字典序比较
export function schedulerOrderKey(issue, priority) {
  const created = (issue && (issue.created_at || issue.updated_at)) || ''
  return [issueLabelWeight(issue, priority), created]
}

// 按排序方法重排 issue 列表（纯函数，返回新数组不改动入参）：
// scheduler=调度器执行顺序（标签权重升序 → 创建时间升序）；updated=
// 最近更新降序（与后端 order_by=updated_at 一致）；created=创建时间
// 降序。时间字段缺失的 issue 按空串兜底排最后，排序稳定确定（比较
// 相等时保持原相对顺序，组内分组逻辑不受影响）
export function sortIssuesByMethod(issues, method, priority) {
  const list = Array.isArray(issues) ? issues : []
  if (method === 'scheduler') {
    return [...list].sort((a, b) => {
      const [wa, ca] = schedulerOrderKey(a, priority)
      const [wb, cb] = schedulerOrderKey(b, priority)
      if (wa !== wb) return wa - wb
      return ca < cb ? -1 : ca > cb ? 1 : 0
    })
  }
  if (method === 'created') {
    return [...list].sort((a, b) => {
      const ca = (a && a.created_at) || ''
      const cb = (b && b.created_at) || ''
      return cb < ca ? -1 : cb > ca ? 1 : 0
    })
  }
  // 默认（updated）：最近更新降序，保持后端 order_by=updated_at 语义
  return [...list].sort((a, b) => {
    const ua = (a && a.updated_at) || ''
    const ub = (b && b.updated_at) || ''
    return ub < ua ? -1 : ub > ua ? 1 : 0
  })
}

// issue #287：手动调度顺序保存成功后「本地优先」窗口时长——overview
// 聚合接口带 10 秒缓存，保存（PUT）会清缓存，但轮询请求可能先于 PUT
// 发出并携带旧缓存返回（晚于 PUT 响应到达时会把已保存的顺序覆盖回弹）。
// 保存成功后 20 秒内轮询合并保留本地顺序，之后恢复以服务端为准
// （服务端缓存此时必已重建为新顺序；多标签页并发改动也可在窗口后同步）
export const MANUAL_ORDER_LOCAL_TTL_MS = 20000

// ---- issue #287：概览页「其他」分组手动调度顺序 ----
// 在「调度器执行顺序」排序下，「其他」分组（尚未处理/处理中的 issue）
// 支持拖动 issue 上下移动来手动改变调度顺序：拖动后的整组顺序全量保存
// 到后端（PUT /api/issues/{project_id}/manual-orders，issue_manual_orders
// 表），调度器派发时优先按该顺序（语义对齐 scheduler._task_sort_key 的
// 手动标记/位置），刷新后保持。仅「其他」分组、仅「调度器执行顺序」
// 排序、无过滤时启用拖动。

// 按手动顺序重排 issue 列表（纯函数，返回新数组不改动入参）：手动列表
// 中的 issue 按用户拖动后的顺序排在前面（列表里已不存在的 iid 自动
// 跳过，如已关闭/已进入其他分组的 issue），其余 issue（新开放/未拖动过
// 的）保持原顺序排在后面。手动列表为空或非数组时原样返回；入参非数组
// 回空数组（与 sortIssuesByMethod 的防御风格一致）
export function applyManualOrder(items, manualIids) {
  const list = Array.isArray(items) ? items : []
  const order = Array.isArray(manualIids) ? manualIids : []
  if (order.length === 0) return list
  const placed = new Set()
  const out = []
  for (const iid of order) {
    const item = list.find((it) => it && it.iid === iid)
    if (item && !placed.has(iid)) {
      out.push(item)
      placed.add(iid)
    }
  }
  for (const it of list) {
    if (it && it.iid != null && placed.has(it.iid)) continue
    out.push(it)
  }
  return out
}

// 列表移动元素（纯函数，返回新数组）：把 from 位置的元素移到 to 位置，
// 其余元素相对顺序不变。越界/相同位置/非数组入参返回安全副本（不改动
// 入参，供拖拽落点与测试使用）
export function moveItem(list, from, to) {
  const arr = Array.isArray(list) ? [...list] : []
  if (from < 0 || from >= arr.length || to < 0 || to >= arr.length) return arr
  if (from === to) return arr
  const [item] = arr.splice(from, 1)
  arr.splice(to, 0, item)
  return arr
}

// 提取 issue 的标签名数组：labels 元素可能是 {name} 对象（后端标准）、
// 纯字符串或 null/缺 name（旧缓存/异常数据），逐一防御兼容
export function issueLabelNames(issue) {
  const labels = issue && issue.labels
  if (!Array.isArray(labels)) return []
  const names = []
  for (const l of labels) {
    if (l == null) continue
    const name = typeof l === 'string' ? l : l.name
    if (typeof name === 'string' && name) names.push(name)
  }
  return names
}

// 汇总全部仓库开放 issue 的标签池：去重 + 字典序排序（稳定可预期），
// 供过滤条候选标签渲染。数据来自未过滤全量，过滤后标签选项不消失
export function collectLabelOptions(repos) {
  const names = new Set()
  for (const r of Array.isArray(repos) ? repos : []) {
    for (const i of Array.isArray(r && r.issues) ? r.issues : []) {
      for (const n of issueLabelNames(i)) names.add(n)
    }
  }
  return Array.from(names).sort()
}

// 单个 issue 是否命中状态过滤：all=不过滤；open=无运行中任务；
// running=有运行中任务（与 groupIssuesByBotLabel 的 running 判定同源）
export function matchesIssueStatus(issue, status, runningKeys, repoId) {
  if (status === 'all') return true
  const running = !!(runningKeys && typeof runningKeys.has === 'function'
    && runningKeys.has(`${repoId}:${issue && issue.iid}`))
  if (status === 'running') return running
  if (status === 'open') return !running
  return true
}

// 单个 issue 是否命中标签过滤：未选中标签 = 不过滤；否则命中任一
// 选中标签（多选 OR 语义，标签胶囊逐个点选直观好理解）
export function matchesIssueLabels(issue, labels) {
  if (!Array.isArray(labels) || labels.length === 0) return true
  const names = issueLabelNames(issue)
  return labels.some((l) => names.includes(l))
}

// 按过滤条件过滤仓库内 issue：保留仓库分组结构、仅过滤条目，组内
// 保持原始相对顺序（后端已按 updated_at 降序，前端不重排）
export function filterIssuesByFilter(issues, filter, runningKeys, repoId) {
  const f = filter || {}
  const status = f.status || 'all'
  const labels = Array.isArray(f.labels) ? f.labels : []
  return (Array.isArray(issues) ? issues : []).filter((i) =>
    matchesIssueStatus(i, status, runningKeys, repoId)
    && matchesIssueLabels(i, labels))
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
  // 界面国际化（issue #268）：静态 UI 文案经 t() 翻译（默认中文）
  const { tr } = useI18n()
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
  // Issue 完成耗时统计（issue #180）：null=加载中；{completed_count,
  // avg_seconds, trend} 为 /api/issues/completion-stats 返回
  const [completionStats, setCompletionStats] = useState(null)
  const [completionStatsError, setCompletionStatsError] = useState('')
  // Token 用量统计（issue #235）：null=加载中；{summary, by_repo,
  // by_engine, by_date, currency} 为 /api/usage/stats 返回；过滤器
  // usageRepoId（''=全部仓库）/ usageEngine（''=全部引擎）/
  // usageRange（'7'|'30'|'0'=最近 7/30 天或全部）
  const [usageStats, setUsageStats] = useState(null)
  const [usageStatsError, setUsageStatsError] = useState('')
  const [usageRepoId, setUsageRepoId] = useState('')
  const [usageEngine, setUsageEngine] = useState('')
  const [usageRange, setUsageRange] = useState('7')
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

  useEffect(() => {
    loadIssues()
    const t = setInterval(loadIssues, ISSUE_POLL_MS)
    return () => clearInterval(t)
  }, [loadIssues])

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

  // Token 用量统计（issue #235）：按仓库/引擎/时间段聚合，数据来自本地
  // task_usage 表（GET /api/usage/stats），无 GitLab 请求压力，沿用 60 秒
  // 低频轮询；过滤器变化时立即重拉（不清空旧数据避免闪烁）
  const loadUsageStats = useCallback(async () => {
    const q = new URLSearchParams()
    if (usageRepoId) q.set('repo_id', usageRepoId)
    if (usageEngine) q.set('engine', usageEngine)
    if (usageRange && usageRange !== '0') {
      q.set('since', new Date(Date.now() - Number(usageRange) * 86400000)
        .toISOString().slice(0, 10))
    }
    try {
      const d = await api.get('/api/usage/stats?' + q)
      setUsageStats(d || { summary: {}, by_repo: [], by_engine: [], by_date: [] })
      setUsageStatsError('')
    } catch (e) {
      setUsageStatsError(e.message)
    }
  }, [usageRepoId, usageEngine, usageRange])

  useEffect(() => {
    loadUsageStats()
    const t = setInterval(loadUsageStats, USAGE_STATS_POLL_MS)
    return () => clearInterval(t)
  }, [loadUsageStats])

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
      setDiscoverResults((prev) => ({ ...prev, [repo.repo_id]: { created: res.issues } }))
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

  return (
    <div>
      <h1>{tr('overview.title')}</h1>

      {/* issue #293：灵感组件保持原始位置——位于「开放 Issue」板块下方、
          CI/CD 流水线上方（灵感组件还是和原来一样，放在开放 issue 组件
          的下方）；AI 对话面板以右侧抽屉打开（见下方 issue #166 区块） */}

          {/* issue #138：DeepSeek 账户余额——设置里配置了 deepseek api 时
              在概览页展示（未配置时整卡不渲染，页面保持简洁）。数据由后端
              代调 https://api.deepseek.com/user/balance 获取，Key 不外发 */}
          {dsBalance && dsBalance.configured && (
            <section className="deepseek-balance-section">
              <h2>{tr('overview.balanceTitle')}</h2>
              <p className="muted">
                {tr('overview.balanceDesc', { seconds: DEEPSEEK_BALANCE_POLL_MS / 1000 })}
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
                      <span className="ok-text"><Icon name="check" /> {tr('overview.balanceAvailable')}</span>
                    ) : (
                      <span className="muted">{tr('overview.balanceUnavailable')}</span>
                    )}
                    {dsBalance.balance.fetched_at && (
                      <span className="muted small" title={tr('overview.queryTime')}>
                        {tr('overview.updatedAt', { time: fmtTime(dsBalance.balance.fetched_at) })}
                      </span>
                    )}
                  </div>
                  {(dsBalance.balance.balance_infos || []).length === 0 ? (
                    <div className="empty-state small">
                      <span className="empty-icon" aria-hidden="true"><Icon name="wallet" /></span>
                      <p className="muted">{tr('overview.noBalance')}</p>
                    </div>
                  ) : (
                    <ul className="deepseek-balance-list">
                      {(dsBalance.balance.balance_infos || []).map((info, i) => (
                        <li key={i} className="deepseek-balance-item">
                          <span className="deepseek-balance-currency" title={tr('overview.currency')}>
                            {info.currency || '—'}
                          </span>
                          <span className="deepseek-balance-total" title={tr('overview.totalBalance')}>
                            {info.total_balance != null ? `${info.total_balance}` : '—'}
                          </span>
                          <span className="muted small" title="赠送余额">
                            {tr('overview.grantedBalance', { amount: info.granted_balance ?? '—' })}
                          </span>
                          <span className="muted small" title="充值余额">
                            {tr('overview.rechargeBalance', { amount: info.topped_up_balance ?? '—' })}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              <div className="form-row">
                <button type="button" className="btn btn-small"
                        onClick={loadDeepSeekBalance}><Icon name="refresh" /> {tr('common.refresh')}</button>
                <a className="btn btn-small deepseek-topup-link"
                   href={DEEPSEEK_TOPUP_URL} target="_blank" rel="noreferrer"
                   title={tr('overview.rechargeTitle')}><Icon name="externalLink" /> {tr('overview.recharge')}</a>
              </div>
            </section>
          )}

          {/* issue #68：板块排序调整——开放 Issue 置于页面顶部，
              其后为灵感板块（issue #293：灵感组件放在开放 issue 组件的
              下方）、再后为 CI/CD 流水线。
              issue #114：独立任务板块删除，正在运行任务的信息（状态徽章
              / 引擎 / 实时输出）整合进本板块 running 组的 issue 项内，
              任务轮询错误一并在此展示 */}
          <section className="issues-section">
            <h2>{tr('overview.issuesTitle')}</h2>
            <p className="muted">{tr('overview.issuesDesc', { seconds: ISSUE_POLL_MS / 1000 })}</p>

            {/* issue #230：过滤条——按状态（全部/开放/进行中）+ 标签多选
                过滤，仅过滤条目、保留仓库分组结构；偏好存 localStorage
                刷新后保持。状态「开放」= 无运行中任务（含 bot-failed /
                bot-done/其他分组），「进行中」= 有 running/retrying 任务
                （与置顶 running 组同源判定） */}
            {hasAnyIssue && (
              <div className="issue-filter-bar">
                {/* issue #286：排序方法切换——默认「调度器执行顺序」，与
                    任务调度器派发语义一致（仓库优先级 → issue 标签优先级
                    → 创建时间升序），方便预判各分组 issue 的处理顺序；可
                    切「最近更新」（原默认展示顺序）/「创建时间」；偏好存
                    localStorage 刷新后保持 */}
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.sortTitle')}>{tr('overview.sort')}</span>
                  <div className="issue-filter-sorts" role="group" aria-label={tr('overview.sortAria')}>
                    {ISSUE_SORTS.map((s) => (
                      <button key={s.key} type="button"
                              className={'issue-sort-option' + (issueSort === s.key ? ' active' : '')}
                              title={tr(`overview.sortHint.${s.key}`)} aria-pressed={issueSort === s.key}
                              onClick={() => setIssueSort(s.key)}>
                        {tr(`overview.sortBy.${s.key}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.filterByStatusTitle')}>{tr('overview.status')}</span>
                  <div className="issue-filter-statuses" role="group" aria-label={tr('overview.filterStatusAria')}>
                    {ISSUE_STATUS_FILTERS.map((s) => (
                      <button key={s.key} type="button"
                              className={'issue-filter-status' + (issueFilter.status === s.key ? ' active' : '')}
                              title={tr(`overview.filterStatusHint.${s.key}`)} aria-pressed={issueFilter.status === s.key}
                              onClick={() => setIssueFilter((prev) => ({ ...prev, status: s.key }))}>
                        {tr(`overview.filterStatus.${s.key}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.labelsTitle')}>{tr('overview.labels')}</span>
                  <div className="issue-filter-labels">
                    {issueLabelOptions.length === 0 ? (
                      <span className="muted small">{tr('overview.noLabels')}</span>
                    ) : (
                      issueLabelOptions.map((name) => {
                        const active = issueFilter.labels.includes(name)
                        return (
                          <button key={name} type="button"
                                  className={'issue-filter-label-chip' + (active ? ' active' : '')}
                                  title={active ? tr('overview.cancelFilter', { name }) : tr('overview.showOnlyWithLabel', { name })}
                                  aria-pressed={active}
                                  onClick={() => setIssueFilter((prev) => ({
                                    ...prev,
                                    labels: active
                                      ? prev.labels.filter((l) => l !== name)
                                      : prev.labels.concat(name),
                                  }))}>
                            {active && <Icon name="check" />}
                            {name}
                          </button>
                        )
                      })
                    )}
                  </div>
                </div>
                {issueFilterActive && (
                  <button type="button" className="btn btn-small issue-filter-reset"
                          onClick={() => setIssueFilter({ status: 'all', labels: [] })}
                          title={tr('overview.clearFilterTitle')}>
                    {tr('overview.clearFilter')}
                  </button>
                )}
              </div>
            )}
            {ownerTokenOk === false && (
              <div className="alert alert-warning" role="alert">
                <Icon name="warning" /> <strong>{tr('overview.ownerTokenWarning')}</strong>
                {tr('overview.ownerTokenBefore')}<code>gitlab.owner_token</code>{tr('overview.ownerTokenAfter')}
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
            {!hasAnyIssue ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                <p className="muted">{tr('overview.noOpenIssues')}</p>
              </div>
            ) : filteredRepoIssues.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="search" /></span>
                <p className="muted">{tr('overview.noMatchingIssues')}</p>
                <button type="button" className="btn btn-small"
                        onClick={() => setIssueFilter({ status: 'all', labels: [] })}>
                  清除过滤
                </button>
              </div>
            ) : (
              <div className="issues-list">
                {filteredRepoIssues.map((r) => (
                  <div key={r.repo_id} className="card issue-repo-card">
                    <div className="issue-repo-head">
                      <span className="issue-repo-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                      <span className="badge badge-muted" title={tr('overview.repoPriorityTitle')}>
                        优先级 {r.priority ?? 100}
                      </span>
                      <span className="muted" title={issueFilterActive
                        ? '当前过滤条件下匹配的开放 issue 数量'
                        : '该仓库开放 issue 总数'}>
                        {issueFilterActive ? `匹配 ${r.issues.length} 个` : `${r.issues.length} 个开放 issue`}
                      </span>
                      {/* issue #134：卡片右上角操作组——「对账」按钮 + issue #92
                          「添加 Issue」按钮，整体推到卡片头最右侧 */}
                      <div className="issue-repo-actions">
                        {/* issue #187：卡片右上角「自省」按钮——调用 AI
                            agent 审查仓库功能与实现，把改进建议写入该仓库
                            issue（分配人 = 仓库 owner）。请求中禁用防重复
                            点击，与「对账」按钮同风格 */}
                        <button type="button" className="btn btn-small introspect-btn"
                                onClick={() => introspectRepo(r)}
                                disabled={introspectResults[r.repo_id]?.loading}
                                title={tr('overview.introspectTitle')}>
                          {introspectResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.introspecting')}</> : <><Icon name="search" /> {tr('overview.introspect')}</>}
                        </button>
                        {/* issue #189：卡片右上角「发掘」按钮——让 agent
                            根据该仓库实现的功能去 GitHub 搜索类似仓库、翻找
                            用户需求 issue，整理成需求写入该仓库 issue（分配人
                            = 仓库 owner，一条需求一个 issue）。请求中禁用防
                            重复点击，与「自省」按钮同风格 */}
                        <button type="button" className="btn btn-small discover-btn"
                                onClick={() => discoverRepo(r)}
                                disabled={discoverResults[r.repo_id]?.loading}
                                title={tr('overview.discoverTitle')}>
                          {discoverResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.discovering')}</> : <><Icon name="compass" /> {tr('overview.discover')}</>}
                        </button>
                        <button type="button" className="btn btn-small reconcile-btn"
                                onClick={() => reconcileRepo(r)}
                                disabled={reconcileResults[r.repo_id]?.loading}
                                title={tr('overview.reconcileTitle')}>
                          {reconcileResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.reconciling')}</> : <><Icon name="refresh" /> {tr('overview.reconcile')}</>}
                        </button>
                        {/* issue #92：卡片右上角「添加 Issue」按钮——打开弹窗，
                            提交后调用 GitLab API 在对应仓库创建 issue */}
                        <button type="button" className="btn btn-small add-issue-btn"
                                onClick={() => setAddIssueRepo(r)}
                                title={tr('overview.addIssueTitle')}><Icon name="plus" /> {tr('overview.addIssue')}</button>
                      </div>
                    </div>
                    {/* issue #134：对账结果——与仓库页对账结果一致，小字展示
                        扫描/补入队结果，请求失败显示错误 */}
                    {reconcileResults[r.repo_id] && <ReconcileResult result={reconcileResults[r.repo_id]} />}
                    {/* issue #187：自省结果——AI 审查进行中 / 已创建自省
                        issue（带跳转链接）/ 失败原因 */}
                    {introspectResults[r.repo_id] && <IntrospectResult result={introspectResults[r.repo_id]} />}
                    {/* issue #189：发掘结果——AI 发掘进行中 / 已创建发掘
                        issue 链接列表 / 失败原因 */}
                    {discoverResults[r.repo_id] && <DiscoverResult result={discoverResults[r.repo_id]} />}
                    {/* issue #287：手动调度顺序保存失败提示——点击可关闭，
                        与概览页其他 alert 交互一致 */}
                    {manualErrors[r.repo_id] && (
                      <div className="alert alert-error issue-manual-error"
                           title={tr('overview.manualOrderTitle')}
                           onClick={() => setManualErrors((prev) => {
                             const next = { ...prev }
                             delete next[r.repo_id]
                             return next
                           })}>
                        <Icon name="warning" /> {manualErrors[r.repo_id]}
                      </div>
                    )}
                    {(r.issues || []).length === 0 ? (
                      <div className="empty-state small">
                        <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                        <p className="muted">{tr('overview.repoNoOpenIssues')}</p>
                      </div>
                    ) : (
                      /* issue #80：按 bot 终态标签分组（bot-failed / bot-done /
                         其他），只渲染非空组，组标题带计数
                         issue #101：正在运行的 issue 独立成 running 组置顶，
                         任务结束键消失后自动回落原分组 */
                      ISSUE_GROUPS.map((g) => {
                        const items = groupIssuesByBotLabel(r.issues, runningKeys, r.repo_id)[g.key]
                        if (items.length === 0) return null
                        // issue #285：分组折叠开关——折叠态隐藏组内 issue
                        // 列表、保留组标题与计数（chevron 方向指示状态）
                        const collapsed = collapsedGroups.has(g.key)
                        // issue #287：手动调度顺序——仅「调度器执行顺序」
                        // 排序下应用（其余排序视图按时间重排，手动顺序仍
                        // 影响实际调度）；「其他」分组 + 无过滤 + 保存中
                        // 除外时才允许拖动（过滤子集拖动会误清未显示条目
                        // 的顺序；保存中禁用防并发覆盖）
                        const manualIids = manualOrders[r.repo_id] || []
                        const ordered = issueSort === 'scheduler'
                          ? applyManualOrder(items, manualIids) : items
                        const repoProjectId = r.project_id != null
                          ? r.project_id
                          : (r.issues[0] && r.issues[0].project_id)
                        const dragEnabled = issueSort === 'scheduler'
                          && g.key === 'other' && !issueFilterActive
                          && ordered.length > 1 && repoProjectId != null
                          && !manualSaving.has(r.repo_id)
                        const dragging = dragFrom && dragFrom.repoId === r.repo_id
                        return (
                          <div key={g.key} className={'issue-group' + (collapsed ? ' issue-group-collapsed' : '')}>
                            <div className="issue-group-head">
                              <button type="button"
                                      className="issue-group-toggle"
                                      onClick={() => setCollapsedGroups((prev) => toggleGroupCollapsed(prev, g.key))}
                                      aria-expanded={!collapsed}
                                      aria-label={tr(collapsed ? 'overview.expandGroup' : 'overview.collapseGroup')}
                                      title={tr(collapsed ? 'overview.expandGroupHint' : 'overview.collapseGroupHint')}>
                                <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} />
                              </button>
                              <span className="issue-group-title" title={tr(`overview.groupHint.${g.key}`)}><Icon name={g.icon} /> {tr(`overview.group.${g.key}`)}</span>
                              <span className="issue-group-count"
                                    title={tr('overview.groupCountTitle')}>{tr('overview.groupCount', { n: items.length })}</span>
                              {/* issue #287：「其他」分组拖动排序提示——仅
                                  调度器执行顺序 + 无过滤 + 多条目时显示 */}
                              {dragEnabled && (
                                <span className="issue-drag-note"
                                      title={tr('overview.manualOrderTitle')}>
                                  <Icon name="gripVertical" /> {tr('overview.manualOrderHint')}
                                </span>
                              )}
                            </div>
                            {!collapsed && (
                            <ul className="issue-list">
                              {ordered.map((i, idx) => {
                                const bot = botStatusKey(i)
                                const statusMeta = bot ? BOT_STATUS_META[bot] : null
                                // issue #99：任务（running/retrying）命中则该 issue 高亮
                                const running = runningKeys.has(`${r.repo_id}:${i.iid}`)
                                // issue #80：终态标签由状态徽章替代展示，其余标签保留胶囊
                                const otherLabels = (i.labels || []).filter(
                                  (l) => l && !BOT_STATUS_NAMES.has(l.name))
                                // issue #287：拖拽状态类——拖起项半透明、
                                // 悬停目标高亮落点
                                const isDragging = dragging && dragFrom.from === idx
                                const isDragOver = dragging && dragOverIndex === idx
                                const itemCls = (running
                                  ? 'issue-item issue-item-running'
                                  : 'issue-item')
                                  + (isDragging ? ' issue-item-dragging' : '')
                                  + (isDragOver ? ' issue-item-drag-over' : '')
                                return (
                                  <li key={i.iid}
                                      draggable={dragEnabled}
                                      onDragStart={dragEnabled ? (e) => {
                                        // HTML5 拖放：标记移动语义 + 记录拖起
                                        // 位置（按当前渲染索引），拖拽结束后清除
                                        e.dataTransfer.effectAllowed = 'move'
                                        e.dataTransfer.setData('text/plain', String(i.iid))
                                        setDragFrom({ repoId: r.repo_id, from: idx })
                                      } : undefined}
                                      onDragOver={dragEnabled ? (e) => {
                                        // 必须 preventDefault 才允许落点（drop）
                                        e.preventDefault()
                                        e.dataTransfer.dropEffect = 'move'
                                        if (dragOverIndex !== idx) setDragOverIndex(idx)
                                      } : undefined}
                                      onDrop={dragEnabled ? (e) => {
                                        e.preventDefault()
                                        commitManualReorder(r, ordered, idx)
                                      } : undefined}
                                      onDragEnd={dragEnabled ? () => {
                                        setDragFrom(null)
                                        setDragOverIndex(null)
                                      } : undefined}
                                      className={itemCls}>
                                    {/* issue #71：参考 GitLab issue 列表页布局——左列编号+标题+
                                        标签/里程碑胶囊，右列 assignee 头像+更新时间+评论数
                                        issue #85：标题改为按钮——点击打开右边栏，不再直接
                                        跳转 GitLab（跳转统一走右边栏右上角按钮）
                                        issue #114：issue 行（issue-row）与任务信息块
                                        纵向排布——任务板块删除后任务详情随项展示 */}
                                    <div className="issue-row">
                                    {/* issue #287：「其他」分组拖动排序手柄——
                                        装饰性图标（gripVertical），li 整体可拖，
                                        图标只是视觉提示与抓取点 */}
                                    {dragEnabled && (
                                      <span className="issue-drag-handle"
                                            title={tr('overview.manualOrderTitle')}
                                            aria-hidden="true">
                                        <Icon name="gripVertical" />
                                      </span>
                                    )}
                                    <div className="issue-main">
                                      <button type="button" className="issue-link"
                                              onClick={() => setSelectedIssue({
                                                issue: i, repoName: r.repo_name,
                                                running,
                                              })}
                                              title={tr('overview.viewIssueDetail')}>
                                        <span className="issue-iid">#{i.iid}</span>
                                        {statusMeta && (
                                          <span className={`issue-status ${statusMeta.cls}`}
                                                title={tr(`overview.botStatusHint.${bot}`)}><Icon name={statusMeta.icon} /> {statusMeta.label}</span>
                                        )}
                                        {/* issue #99：正在运行的 issue 显示「运行中」徽章
                                            （任务结束后随任务列表轮询自动消失） */}
                                        {running && (
                                          <span className="issue-status issue-status-running"
                                                title={tr('overview.runningBadgeTitle')}><Icon name="settings" /> {tr('overview.runningBadge')}</span>
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
                                                  title={tr('overview.labelPillTitle', { name: l.name })}>{l.name}</span>
                                          ))}
                                          {i.milestone && (
                                            <span className="milestone-chip" title={tr('overview.milestoneTitle', { name: i.milestone })}>
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
                                               title={tr('overview.assigneeTitle', { name: a.name || a.username || '' })} />
                                        ) : (
                                          <span key={a.username || a.name}
                                                className="assignee-avatar avatar-fallback"
                                                title={tr('overview.assigneeTitle', { name: a.name || a.username || '' })}>
                                            {(a.name || a.username || '?').slice(0, 1).toUpperCase()}
                                          </span>
                                        )
                                      ))}
                                      {i.updated_at && (
                                        <span className="issue-updated" title={tr('overview.lastUpdated')}>
                                          {fmtAgo(i.updated_at) || ''}
                                        </span>
                                      )}
                                      {typeof i.user_notes_count === 'number' && (
                                        <span className="issue-notes" title={tr('overview.notesCount')}>
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
                                                    title={tr('overview.taskEngineTitle')}>{eng}</span>
                                            )}
                                            {t.issue_url ? (
                                              <a className="issue-task-link" href={t.issue_url}
                                                 target="_blank" rel="noreferrer"
                                                 title={tr('overview.openIssueInGitlab')}>{tr('overview.openInGitlab')}</a>
                                            ) : null}
                                          </div>
                                          <pre className="log-view issue-task-log">
                                            {lines.length > 0
                                              ? lines.map((line, i) => <span key={i}>{line}{'\n'}</span>)
                                              : tr('overview.noTaskOutput')}
                                          </pre>
                                        </div>
                                      )
                                    })}
                                  </li>
                                )
                              })}
                            </ul>
                            )}
                          </div>
                        )
                      })
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* issue #131/#293：灵感板块——位于开放 Issue 下方、CI/CD
              流水线上方（issue #293：灵感组件还是和原来一样，放在开放
              issue 组件的下方）：按仓库随手记录关于对应仓库的新功能灵感，
              仅保存在 Botler 本地数据库，不提交到 GitLab issue。每个仓库
              一张卡：灵感列表（编辑/删除/对话）+ 底部随手记录表单 */}
          <section className="inspirations-section">
            <h2><Icon name="lightbulb" /> {tr('overview.inspirationsTitle')}</h2>
            <p className="muted">{tr('overview.inspirationsDesc', { seconds: INSPIRATION_POLL_MS / 1000 })}</p>
            {inspirationError && (
              <div className="alert alert-error" onClick={() => setInspirationError('')}>{inspirationError}</div>
            )}
            {inspirationCreatedIssue && (
              <div className="alert alert-ok" onClick={() => setInspirationCreatedIssue(null)}
                   title="点击关闭">
                <Icon name="checkCircle" /> {tr('overview.inspirationCreated')}{' '}
                <a href={inspirationCreatedIssue.web_url || '#'} target="_blank" rel="noreferrer"
                   onClick={(e) => e.stopPropagation()}>
                  {'issue #' + inspirationCreatedIssue.iid}
                </a>
                {tr('overview.defaultLabels')}
              </div>
            )}
            {inspirationRepos.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                <p className="muted">{tr('overview.noInspirations')}</p>
              </div>
            ) : (
              <div className="inspirations-list">
                {inspirationRepos.map((r) => (
                  <div key={r.repo_id} className="card inspiration-repo-card">
                    <div className="inspiration-repo-head">
                      <span className="inspiration-repo-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                      {r.enabled === false && (
                        <span className="badge badge-muted" title={tr('overview.repoDisabledTitle')}>{tr('common.disabled')}</span>
                      )}
                      <span className="muted">{tr('overview.inspirationCount', { n: (r.inspirations || []).length })}</span>
                    </div>
                    {(r.inspirations || []).length === 0 ? (
                      <div className="empty-state small">
                        <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                        <p className="muted">{tr('overview.noInspirationPlaceholder')}</p>
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
                                          disabled={!editInspirationDraft.trim()}>{tr('common.save')}</button>
                                  <button type="button" className="btn btn-small"
                                          onClick={() => {
                                            setEditingInspiration(null)
                                            setEditInspirationDraft('')
                                          }}>{tr('common.cancel')}</button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <p className="inspiration-content">{ins.content}</p>
                                <div className="inspiration-meta">
                                  <span className="inspiration-time" title={tr('overview.lastUpdated')}>
                                    {fmtAgo(ins.updated_at) || '—'}
                                  </span>
                                  <span className="inspiration-actions">
                                    <button type="button" className="inspiration-action-btn inspiration-add-issue-btn"
                                            title={tr('overview.inspirationAddIssueTitle')}
                                            onClick={() => addIssueFromInspiration(ins)}
                                            disabled={!!addingIssueInspIds[ins.id]}>
                                      {addingIssueInspIds[ins.id] ? <><Icon name="hourglass" /> {tr('overview.submitting')}</> : <><Icon name="pin" /> {tr('overview.addIssue')}</>}
                                    </button>
                                    <button type="button" className="inspiration-action-btn inspiration-chat-btn"
                                            title={tr('overview.inspirationChatTitle')}
                                            onClick={() => openInspirationChat(ins)}><Icon name="message" /> {tr('overview.chat')}</button>
                                    <button type="button" className="inspiration-action-btn"
                                            title={tr('overview.editInspirationTitle')}
                                            onClick={() => {
                                              setEditingInspiration(ins)
                                              setEditInspirationDraft(ins.content)
                                            }}><Icon name="pencil" /> {tr('common.edit')}</button>
                                    <button type="button" className="inspiration-action-btn inspiration-delete-btn"
                                            title={tr('overview.deleteInspirationTitle')}
                                            onClick={() => deleteInspiration(ins)}><Icon name="trash" /> {tr('common.delete')}</button>
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
                                placeholder={tr('overview.inspirationPlaceholder')}
                                value={newInspirationDrafts[r.repo_id] || ''}
                                onChange={(e) => setNewInspirationDrafts((prev) => ({ ...prev, [r.repo_id]: e.target.value }))}
                                rows={2} />
                      <button type="submit" className="btn btn-small inspiration-add-btn"
                              disabled={!(newInspirationDrafts[r.repo_id] || '').trim()}><Icon name="plus" /> {tr('overview.record')}</button>
                    </form>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* issue #114：独立任务板块已删除——正在运行任务的信息（状态徽章
              / 执行引擎 / 实时输出）整合进上方开放 Issue 板块 running 组的
              issue 项内，任务轮询与 SSE 数据流保持不变 */}
          <section className="pipelines-section">
            <h2>{tr('overview.pipelinesTitle')}</h2>
            <p className="muted">{tr('overview.pipelinesDesc', { seconds: PIPELINE_POLL_MS / 1000 })}</p>
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
                <p className="muted">{tr('overview.noPipelines')}</p>
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
                        <span className="pipeline-repo" title={tr('overview.repoTitle')}><Icon name="folder" /> {p.repo_name || tr('common.deleted')}</span>
                        {p.enabled === false && (
                          <span className="badge badge-muted" title={tr('overview.repoDisabledTitle')}>{tr('common.disabled')}</span>
                        )}
                        {meta ? (
                          <span className={'badge ' + meta.cls}>{meta.label}</span>
                        ) : (
                          <span className="muted">{tr('overview.noPipelines')}</span>
                        )}
                      </div>
                      {pl && (
                        <a className="pipeline-link" href={pl.web_url} target="_blank"
                           rel="noreferrer" title={tr('overview.openPipelineInGitlab')}>
                          <span className="pipeline-ref" title={tr('overview.pipelineRefTitle', { ref: pl.ref, sha: pl.sha })}>
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
            <h2>{tr('overview.completionTitle')}</h2>
            <p className="muted">{tr('overview.completionDesc', { seconds: COMPLETION_STATS_POLL_MS / 1000 })}</p>
            {completionStatsError && (
              <div className="alert alert-error" onClick={() => setCompletionStatsError('')}>{completionStatsError}</div>
            )}
            {completionStats && completionStats.completed_count === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="hourglass" /></span>
                <p className="muted">{tr('overview.noCompletedIssues')}</p>
              </div>
            ) : completionStats ? (
              <>
                <div className="completion-stats-summary">
                  <span className="completion-stats-value"
                        title={tr('overview.avgCompletionTitle')}>
                    {fmtSeconds(completionStats.avg_seconds) || <span className="muted">—</span>}
                  </span>
                  <span className="muted">{tr('overview.avgCompletion', { n: completionStats.completed_count })}</span>
                </div>
                <CompletionTrendChart trend={completionStats.trend} />
                {/* issue #288：每个开启仓库的平均耗时与走势拆分——接口
                    repos 数组（仅已启用仓库，按配置优先级升序，无已完成
                    任务仓库 avg_seconds=null/trend=[]），逐仓库渲染平均
                    耗时 + 紧凑迷你走势图 */}
                {Array.isArray(completionStats.repos) && completionStats.repos.length > 0 && (
                  <div className="completion-repo-list">
                    <h3 className="completion-repo-title">{tr('overview.completionPerRepoTitle')}</h3>
                    {completionStats.repos.map((r) => (
                      <div className="completion-repo-row" key={r.repo_id}>
                        <div className="completion-repo-info">
                          <span className="completion-repo-name"
                                title={r.repo_name}>{r.repo_name}</span>
                          <span className="completion-repo-value"
                                title={r.completed_count > 0 ? tr('overview.avgCompletionTitle') : undefined}>
                            {r.completed_count > 0
                              ? (fmtSeconds(r.avg_seconds) || <span className="muted">—</span>)
                              : <span className="muted">{tr('overview.repoNoData')}</span>}
                          </span>
                          <span className="muted">{tr('overview.avgCompletion', { n: r.completed_count })}</span>
                        </div>
                        {r.completed_count > 0 ? (
                          <div className="completion-repo-chart">
                            <CompletionTrendChart trend={r.trend} compact />
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : null}
          </section>

          {/* issue #235：Token 用量统计——按仓库/引擎/时间段聚合（本地
              task_usage 表，GET /api/usage/stats），无 GitLab 请求压力；
              展示合计 token 数与估算费用（未配置单价只展示 token 数）；
              过滤器变化立即重拉，沿用 60 秒低频轮询 */}
          <section className="usage-stats-section">
            <h2>{tr('overview.usageTitle')}</h2>
            <p className="muted">{tr('overview.usageDesc', { seconds: USAGE_STATS_POLL_MS / 1000 })}</p>
            <div className="form-row wrap">
              <select className="input usage-stats-filter" value={usageRepoId}
                      onChange={(e) => setUsageRepoId(e.target.value)}
                      title={tr('overview.filterByRepo')}>
                <option value="">{tr('overview.allRepos')}</option>
                {(repoIssues || []).map((r) => (
                  <option key={r.repo_id} value={r.repo_id}>{r.repo_name}</option>
                ))}
              </select>
              <select className="input usage-stats-filter" value={usageEngine}
                      onChange={(e) => setUsageEngine(e.target.value)}
                      title={tr('overview.filterByEngine')}>
                <option value="">{tr('overview.allEngines')}</option>
                <option value="claude">claude</option>
                <option value="hermes">hermes</option>
                <option value="dsh">dsh</option>
              </select>
              <select className="input usage-stats-filter" value={usageRange}
                      onChange={(e) => setUsageRange(e.target.value)}
                      title={tr('overview.filterByRange')}>
                <option value="7">{tr('overview.last7Days')}</option>
                <option value="30">{tr('overview.last30Days')}</option>
                <option value="0">{tr('overview.all')}</option>
              </select>
            </div>
            {usageStatsError && (
              <div className="alert alert-error" onClick={() => setUsageStatsError('')}>{usageStatsError}</div>
            )}
            {usageStats && (usageStats.summary?.task_count || 0) === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="coins" /></span>
                <p className="muted">{tr('overview.noUsage')}</p>
              </div>
            ) : usageStats ? (
              <>
                <div className="usage-stats-summary">
                  <span className="usage-stats-value">
                    {fmtTokens(usageStats.summary?.total_tokens || 0)} tokens
                  </span>
                  <span className="muted">
                    {tr('overview.usageTaskCount', { n: usageStats.summary?.task_count || 0 })}{' '}
                    {fmtCost(usageStats.summary?.estimated_cost, usageStats.currency)
                      ? <>{tr('overview.estimatedCost')} <b>{fmtCost(usageStats.summary?.estimated_cost, usageStats.currency)}</b></>
                      : tr('overview.noUnitPrice')}
                  </span>
                </div>
                <div className="usage-stats-grid">
                  <table className="table usage-stats-table">
                    <thead>
                      <tr><th>{tr('overview.engine')}</th><th>{tr('overview.taskCount')}</th><th>{tr('overview.totalTokens')}</th><th>{tr('overview.estimatedCost')}</th></tr>
                    </thead>
                    <tbody>
                      {(usageStats.by_engine || []).map((e) => (
                        <tr key={e.engine}>
                          <td>{e.engine || '—'}</td>
                          <td>{e.task_count}</td>
                          <td>{fmtTokens(e.total_tokens)}</td>
                          <td>{fmtCost(e.estimated_cost, usageStats.currency) || <span className="muted">—</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <table className="table usage-stats-table">
                    <thead>
                      <tr><th>{tr('overview.repo')}</th><th>{tr('overview.taskCount')}</th><th>{tr('overview.totalTokens')}</th><th>{tr('overview.estimatedCost')}</th></tr>
                    </thead>
                    <tbody>
                      {(usageStats.by_repo || []).map((r) => (
                        <tr key={r.repo_id}>
                          <td className="ellipsis" title={r.repo_name}>{r.repo_name || tr('common.deleted')}</td>
                          <td>{r.task_count}</td>
                          <td>{fmtTokens(r.total_tokens)}</td>
                          <td>{fmtCost(r.estimated_cost, usageStats.currency) || <span className="muted">—</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : null}
          </section>

      {/* issue #166/#184：灵感 AI 对话——与 AI agent 探讨当前灵感。
          右侧边栏抽屉形式（issue #184），复用 .drawer 右侧抽屉体系
          （与 issue 详情右边栏 issue #85 一致）：遮罩点击 / × / Esc
          关闭，从右侧滑入；顶部灵感摘要，中部消息列表（用户右 / AI
          左），底部输入框 + 发送按钮（Enter 发送 / Shift+Enter
          换行）；发送中按钮禁用，AI 回复实时 append */}
      {chatInspiration && (
        <div className="drawer-overlay" onClick={closeInspirationChat}>
          <div className="drawer chat-drawer" role="dialog" aria-modal="true"
               onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong><Icon name="message" /> {tr('overview.chatTitle')}</strong>
              <button type="button" className="btn modal-close"
                      onClick={closeInspirationChat} title={tr('common.close')} aria-label={tr('common.close')}><Icon name="x" /></button>
            </div>
            <div className="chat-subject" title={chatInspiration.content}>
              <span className="muted">{tr('overview.chatRepo', { name: chatInspiration.repo_name || '—' })}</span>
              <p className="chat-subject-content">{chatInspiration.content}</p>
            </div>
            <div className="chat-body">
              {chatLoading ? (
                <div className="chat-empty muted">{tr('overview.chatLoading')}</div>
              ) : chatMessages.length === 0 ? (
                <div className="chat-empty muted">{tr('overview.chatEmpty')}</div>
              ) : (
                chatMessages.map((m) => (
                  <div key={m.id}
                       className={'chat-msg ' + (m.role === 'user' ? 'chat-msg-user' : 'chat-msg-ai')}>
                    <div className="chat-msg-bubble">{m.content}</div>
                    <div className="chat-msg-meta">
                      {m.role === 'user' ? tr('overview.me') : 'AI'} · {fmtAgo(m.created_at) || '—'}
                    </div>
                  </div>
                ))
              )}
              {chatSending && <div className="chat-empty muted"><Icon name="bot" /> {tr('overview.aiThinking')}</div>}
              {chatError && (
                <div className="alert alert-error chat-error"
                     onClick={() => setChatError('')}>{chatError}</div>
              )}
            </div>
            <form className="chat-input-row"
                  onSubmit={(e) => { e.preventDefault(); sendInspirationChat() }}>
              <textarea className="input chat-input" rows={2}
                        placeholder={tr('overview.chatPlaceholder')}
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
                {chatSending ? tr('overview.sending') : tr('overview.send')}
              </button>
            </form>
          </div>
        </div>
      )}
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
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small reconcile-result">{result.error}</div>
  if (result.note) return <div className="small muted reconcile-result">{result.note}</div>
  return (
    <div className="small reconcile-result">
      {result.enqueued > 0
        ? <span className="test-chip ok"><Icon name="check" /> {tr('overview.reconcileEnqueued', { n: result.enqueued })}</span>
        : <span className="test-chip ok"><Icon name="check" /> {tr('overview.reconcileNoop')}</span>}
      {result.scanned > 0 && <span className="muted">{tr('overview.reconcileScanned', { n: result.scanned })}</span>}
    </div>
  )
}

// 自省结果（issue #187）：AI 审查进行中显示加载提示；成功显示已创建的
// 自省 issue 链接（点击跳转 GitLab）；失败显示后端错误信息
function IntrospectResult({ result }) {
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small introspect-result">{result.error}</div>
  if (result.loading) return <div className="small muted introspect-result"><Icon name="refresh" /> {tr('overview.introspectLoading')}</div>
  if (result.created?.web_url) {
    return (
      <div className="small introspect-result">
        <span className="test-chip ok"><Icon name="check" /> {tr('overview.introspectCreated')}</span>{' '}
        <a href={result.created.web_url} target="_blank" rel="noreferrer"
           title={tr('overview.openIntrospectInGitlab')}>
          #{result.created.iid} <Icon name="externalLink" />
        </a>
      </div>
    )
  }
  return null
}

// 发掘结果（issue #189）：AI 发掘进行中显示加载提示；成功显示已创建的
// 发掘 issue 链接列表（点击跳转 GitLab）；失败显示后端错误信息
function DiscoverResult({ result }) {
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small discover-result">{result.error}</div>
  if (result.loading) return <div className="small muted discover-result"><Icon name="refresh" /> {tr('overview.discoverLoading')}</div>
  if (result.created?.length) {
    return (
      <div className="small discover-result">
        <span className="test-chip ok"><Icon name="check" /> {tr('overview.discoverCreated', { n: result.created.length })}</span>{' '}
        {result.created.map((issue, i) => (
          <a key={issue.web_url || i} href={issue.web_url} target="_blank" rel="noreferrer"
             title={tr('overview.openDiscoverInGitlab')}>
            #{issue.iid}{i < result.created.length - 1 ? '、' : ''} <Icon name="externalLink" />
          </a>
        ))}
      </div>
    )
  }
  return null
}

// 走势图（issue #180）：轻量 SVG 折线图，无第三方图表库依赖——
// 横轴为完成日（数据本身是逐日序列，等距排布即可），纵轴为当日平均
// 完成耗时（秒），范围 0 → 最大值留 10% 余量；折线 + 数据点，每个点
// 带 <title> 悬浮提示（日期 / 平均耗时 / 当日完成数）。trend 非数组
// 或为空时返回 null（不渲染）。
// issue #288：compact 紧凑模式——各仓库明细行的迷你走势图（更小画布、
// 更小数据点，隐藏日期/刻度文字避免缩小后不可读，仍保留 <title> 提示）。
export function CompletionTrendChart({ trend, compact = false }) {
  const { tr } = useI18n()
  if (!Array.isArray(trend) || trend.length === 0) return null
  const W = compact ? 240 : 640
  const H = compact ? 48 : 180
  const PAD_L = compact ? 2 : 8
  const PAD_R = compact ? 2 : 8
  const PAD_T = compact ? 4 : 14
  const PAD_B = compact ? 4 : 24
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
    <svg className={compact ? 'completion-trend-chart compact' : 'completion-trend-chart'}
         viewBox={`0 0 ${W} ${H}`}
         role="img" aria-label={compact
           ? tr('overview.repoTrendAria')
           : tr('overview.trendAria')}>
      <line className="completion-trend-axis" x1={PAD_L} y1={H - PAD_B}
            x2={W - PAD_R} y2={H - PAD_B} />
      <polyline className="completion-trend-line" points={points} fill="none" />
      {trend.map((t, i) => (
        <circle key={t.date || i} className="completion-trend-dot"
                cx={px(i).toFixed(2)} cy={py(t.avg_seconds).toFixed(2)}
                r={compact ? '2' : '3'}>
          <title>{tr('overview.trendPoint', { date: t.date, avg: fmtSeconds(t.avg_seconds) || '—', n: t.count })}</title>
        </circle>
      ))}
      {!compact && (
        <>
          <text className="completion-trend-label" x={PAD_L} y={H - PAD_B + 16}>{first.date}</text>
          <text className="completion-trend-label" x={W - PAD_R} y={H - PAD_B + 16}
                textAnchor="end">{last.date}</text>
          <text className="completion-trend-label" x={PAD_L} y={PAD_T - 4}>{fmtSeconds(yMax) || ''}</text>
        </>
      )}
    </svg>
  )
}
