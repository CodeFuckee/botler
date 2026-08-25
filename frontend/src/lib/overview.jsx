// 概览页纯函数与常量（issue #201 拆分）：从 Overview.jsx 抽出
// 数据无关的排序 / 过滤 / 分组 / 工具函数与轮询常量，页面组件与
// 测试均可直接引用；Overview.jsx 再导出保持旧导入路径兼容。
import { summarizeToolInput } from '../api.js'
import { fmtRateWindow } from '../balanceRate.js'
import { ENGINE_META } from '../components/IssueDrawer.jsx'
import { Icon } from '../components/Icon.jsx'

export const LIVE_STATUSES = ['running', 'retrying']
// 可重复执行保护使用的完整活跃状态：queued 虽未开始运行，也必须隐藏
// Issue 详情的「执行」按钮，防止同一 issue 重复入队（issue #431）。
export const ACTIVE_TASK_STATUSES = ['queued', ...LIVE_STATUSES]

// 每个任务信息块保留的实时输出行数（issue #114：任务信息随 issue 项
// 展示，超出丢弃最旧行，防止任务块无限增长）
export const MAX_CARD_LINES = 40

// 任务列表轮询间隔（issue #478 起不再使用：改为 SSE 事件驱动刷新，
// 常量保留供文案/测试引用）
export const OVERVIEW_POLL_MS = 3000

// 流水线状态低频兜底轮询间隔（issue #478）：流水线状态变化主要来自
// 任务执行（提交代码触发 CI），由 task 事件联动刷新；GitLab 侧独立变化
// （手动触发流水线等）后端无法感知，保留 60s 低频兜底轮询（后端另有
// 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API）
export const PIPELINE_POLL_MS = 60000

// 开放 issue 聚合轮询间隔（issue #64）：与流水线板块同频，后端同样
// 有 10 秒 TTL 缓存兜底，避免高频轮询打爆 GitLab API
export const ISSUE_POLL_MS = 15000

// 灵感板块轮询间隔（issue #131）：数据存 Botler 本地数据库，无 GitLab
// 请求压力，与开放 issue 板块同频（本地改动提交后手动刷新即时生效）
export const INSPIRATION_POLL_MS = 15000

// DeepSeek 账户余额轮询间隔（issue #138）：余额变化不频繁，60 秒低频
// 轮询 + 卡片内手动刷新按钮兜底（后端代调 deepseek user/balance）
export const DEEPSEEK_BALANCE_POLL_MS = 60000

// DeepSeek 开放平台充值页（issue #178）：余额卡片「充值」链接按钮的跳转
// 目标，点击后在新标签页打开官方充值页，方便用户直接在 DeepSeek 页面充值
export const DEEPSEEK_TOPUP_URL = 'https://platform.deepseek.com/top_up'

// 流水线整体状态 → 徽章映射（issue #39，#317 起随详情抽屉组件维护，
// 概览页卡片复用；样式类复用任务状态徽章 status-*）
export { PIPELINE_STATUS_META, stageClass } from '../components/PipelineDrawer.jsx'

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


// 返回所有活跃（queued/running/retrying）任务对应的 issue 键。展示上的
// 「运行中」仍沿用 runningIssueKeys，仅执行按钮的去重判断使用本函数。
export function activeIssueKeys(tasks) {
  const keys = new Set()
  if (!Array.isArray(tasks)) return keys
  for (const t of tasks) {
    if (!t || !ACTIVE_TASK_STATUSES.includes(t.status)) continue
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
// issue #304：余额变化速率观测窗口人话化——按 fmtRateWindow 结果选
// i18n 单位文案（≥1 小时显示「X 小时」，否则「X 分钟」），tr 由组件传入
export function fmtRateWindowText(tr, ms) {
  const w = fmtRateWindow(ms)
  return tr(w.kind === 'hour' ? 'overview.rateWindowHour' : 'overview.rateWindowMinute',
            { value: w.value })
}

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

// ---- issue #485：单列分组布局「状态 → 仓库」两级分组 ----
// 单列分组布局改为先按 issue 状态分组、组内再按仓库分组。状态分组
// 顺序为需求指定：进行中（running）第一 → 完成任务（done）第二 →
// 失败任务（failed）第三 → 其他（other）第四（与卡片布局
// running → failed → done → other 不同，完成任务排在失败任务之前）。
// title/icon/hint 与 ISSUE_GROUPS 同源字段结构，渲染文案走 i18n
// overview.group.<key> / overview.groupHint.<key>
export const COLUMN_ISSUE_GROUPS = [
  { key: 'running', title: '运行中', icon: 'settings',
    hint: '正在被 bot 执行的 issue，置顶展示' },
  { key: 'done', title: 'bot-done', icon: 'checkCircle',
    hint: 'bot 已完成开发，待人工确认关闭' },
  { key: 'failed', title: 'bot-failed', icon: 'xCircle',
    hint: 'bot 处理失败，需人工介入' },
  { key: 'other', title: '其他', icon: 'clipboard',
    hint: '尚未处理或处理中的 issue' },
]

// 将「仓库 → issue」扁平结构重组为「状态 → 仓库」两级分组（issue #485）：
// 返回 { running: [{...repo, issues: [...]}], done: [...], failed: [...],
// other: [...] }。每个状态键只收录该状态非空的仓库分组（零 issue 仓库
// 不出现，避免空组噪音）；仓库分组保持输入仓库的相对顺序，组内 issue
// 保持原始相对顺序（后端已按 updated_at 降序，前端不重排）。状态判定
// 复用 groupIssuesByBotLabel（running 优先于终态标签：重试中的
// bot-failed / bot-done 一并归入进行中）
export function groupIssuesByStatusThenRepo(repos, runningKeys) {
  const out = {}
  for (const g of COLUMN_ISSUE_GROUPS) out[g.key] = []
  for (const r of Array.isArray(repos) ? repos : []) {
    if (!r || typeof r !== 'object' || r.repo_id == null) continue
    const byLabel = groupIssuesByBotLabel(r.issues, runningKeys, r.repo_id)
    for (const g of COLUMN_ISSUE_GROUPS) {
      const items = byLabel[g.key]
      if (items.length > 0) out[g.key].push({ ...r, issues: items })
    }
  }
  return out
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

// issue #308：置顶按钮——把 issue 移到手动调度顺序最前（纯函数，返回
// 新数组不改动入参）：iid 已在首位时返回原序副本；不在列表中时直接
// 插到最前；在列表中时移到最前并保序去重（其余元素相对顺序不变）。
// 与 applyManualOrder 的防御风格一致：非数组入参按空列表处理、非整数
// iid 原样返回副本（调用方在保存时还会再做整数过滤）
export function pinIssueToTop(iids, iid) {
  const arr = Array.isArray(iids) ? iids : []
  if (!Number.isInteger(iid)) return [...arr]
  return [iid, ...arr.filter((x) => x !== iid)]
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


// ---- issue #471：开放 issue 布局切换 ----
// 概览页「开放 Issue」板块支持切换展示布局：默认「仓库卡片」（每个仓库
// 一张卡片，卡片内按 bot 状态分组，宽屏多列网格）；新增「单列分组」——
// 所有仓库 issue 在同一列展示，同一个仓库的 issue 归为一个分组，分组
// 可折叠/展开。布局偏好存 localStorage（键 botler.overview.layout），
// 刷新后保持。
export const ISSUE_LAYOUT_STORAGE_KEY = 'botler.overview.layout'

// 布局选项：cards=仓库卡片（默认）；column=单列分组。label 为 UI 展示
// 文案（i18n key 见 overview.layoutBy.<key>），hint 为悬浮说明
// （overview.layoutHint.<key>）
export const ISSUE_LAYOUTS = [
  { key: 'cards', label: '仓库卡片',
    hint: '每个仓库一张卡片，卡片内按 bot 状态分组展示（默认布局）' },
  { key: 'column', label: '单列分组',
    hint: '所有仓库 issue 同一列展示，先按 issue 状态分组（进行中/完成/失败/其他），组内再按仓库分组，仓库分组可折叠展开' },
]

// 读取布局偏好：localStorage 兼容对象（测试可注入）；无存储环境或
// getItem 抛异常（隐私模式）时回默认「仓库卡片」。未知布局键（手改/
// 旧版本写入）同样回默认，不抛错
export function loadIssueLayout(storage) {
  try {
    if (!storage) return 'cards'
    const raw = storage.getItem(ISSUE_LAYOUT_STORAGE_KEY)
    if (!raw) return 'cards'
    return ISSUE_LAYOUTS.some((l) => l.key === raw) ? raw : 'cards'
  } catch {
    return 'cards'
  }
}

// 保存布局偏好：只接受 ISSUE_LAYOUTS 已知布局键；存储不可用或 setItem
// 抛异常时静默忽略，不影响页面使用
export function saveIssueLayout(storage, layout) {
  try {
    if (!storage || !layout) return
    if (!ISSUE_LAYOUTS.some((l) => l.key === layout)) return
    storage.setItem(ISSUE_LAYOUT_STORAGE_KEY, layout)
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

// ---- issue #471：单列分组布局的仓库分组折叠 ----
// 单列分组布局下每个仓库是一个分组，分组头带折叠开关（chevronRight/
// chevronDown），折叠后隐藏组内 issue 列表、保留组头（仓库名/优先级/
// 计数/操作按钮）；折叠偏好存 localStorage（键
// botler.overview.collapsedRepos），刷新后保持。键统一用仓库 id 的
// 字符串形式（数字/字符串类型差异防误匹配，与 runningIssueKeys 同源
// 防御）。toggle 复用 toggleGroupCollapsed（纯 Set 切换语义一致）。
export const REPO_COLLAPSE_STORAGE_KEY = 'botler.overview.collapsedRepos'

// 读取仓库折叠偏好：localStorage 兼容对象（测试可注入）；无存储环境或
// getItem 抛异常（隐私模式）时返回空 Set（全展开）。值须为 JSON 数组
// 且元素为字符串（仓库 id 以字符串存储，数字/布尔/null 等异常元素剔除；
// 仓库 id 集合来自后端动态数据，不做已知集合校验）
export function loadCollapsedRepos(storage) {
  const out = new Set()
  try {
    if (!storage) return out
    const raw = storage.getItem(REPO_COLLAPSE_STORAGE_KEY)
    if (!raw) return out
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return out
    for (const k of parsed) {
      if (typeof k === 'string' && k) out.add(k)
    }
  } catch {
    /* 无存储环境/损坏数据：静默回退全展开 */
  }
  return out
}

// 保存仓库折叠偏好：只写字符串元素数组；存储不可用或 setItem 抛异常时
// 静默忽略，不影响页面使用
export function saveCollapsedRepos(storage, collapsed) {
  try {
    if (!storage || !collapsed) return
    const keys = Array.from(collapsed).filter((k) => typeof k === 'string' && k)
    storage.setItem(REPO_COLLAPSE_STORAGE_KEY, JSON.stringify(keys))
  } catch {
    /* 无存储环境：静默忽略 */
  }
}
