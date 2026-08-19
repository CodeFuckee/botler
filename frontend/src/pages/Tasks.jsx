import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling.js'
import { api, fmtTime, fmtDuration, shortSha, STATUS_META } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'
import { useI18n } from '../i18n.jsx'
import { focusElement, useShortcuts } from '../keymap.js'
import { UsageSummary } from '../components/UsageCard.jsx'

// 每页条数（与后端 limit 一致，issue #50 翻页）
const PAGE_SIZE = 50

// ---- 响应式列隐藏（issue #70）----
// 窄视口下按「不显示优先级」排序的可隐藏列。列宽与 styles.css 的
// .table.tasks-table th:nth-child(n) 规则保持一致（tasks-responsive-cols.test.mjs
// 从 styles.css 提取列宽断言两者同步，改列宽时两处都要改）。
export const HIDDEN_COL_PRIORITY = [
  { key: 'attempt', label: '尝试', width: 88 }, // 第 6 列
  { key: 'source', label: '来源', width: 76 }, // 第 7 列
  { key: 'created', label: '创建时间', width: 165 }, // 第 10 列
  { key: 'reason', label: '失败原因', width: 140 }, // 第 8 列
  { key: 'duration', label: '用时', width: 120 }, // 第 11 列
]

// 可选用量列（issue #235）：勾选「显示用量」后追加为第 6 列（状态之后），
// 不参与响应式隐藏（可选列勾选后恒显示，窄视口走 .table-wrap 横向滚动）；
// 宽度与 styles.css 的 .tasks-table-usage th:nth-child(6) 一致
export const USAGE_COL_WIDTH = 150

// 12 列宽度总和（styles.css .table.tasks-table min-width 同值）
export const TABLE_MIN_WIDTH = 1360

// 移动端卡片列表断点（issue #270）：复用设置页窄视口回落（issue #139）
// 的 860px 断点约定——视口 ≤860px 时任务表格整体切换为卡片式列表
// （触屏友好、关键操作按钮直接可见、无横向滚动；表格场景不再需要
// 横向滚动兜底）。与 styles.css 的 @media (max-width: 860px) 保持同步。
export const MOBILE_BREAKPOINT_PX = 860

// 是否窄视口（≤860px）：SSR/测试环境无 window 时调用方不触发，
// 组件内默认桌面布局（表格）；异常输入（NaN/≤0/null）按桌面处理
export function isMobileViewport(viewportWidth) {
  return Number.isFinite(viewportWidth) && viewportWidth > 0 && viewportWidth <= MOBILE_BREAKPOINT_PX
}

// .content 与 .card 左右 padding 之和（styles.css 布局常量，issue #53）
const CONTENT_CARD_PAD_X = 80

// 视口宽度 → 内容区 --content-width（与 styles.css :root / 媒体查询断点同步；
// issue #70 新增 1360/1280/1120/1000 四档，让列隐藏按优先级渐进生效；
// issue #96 宽屏档放宽至 1840/2480；
// issue #98 宽屏档改为动态跟随视口：≥1440 取 max(1440, 视口 − 100)，
// 与 CSS max(1440px, calc(100vw - 100px)) 一致，单边留白恒 50px）
export function contentWidthAt(viewportWidth) {
  if (viewportWidth >= 1440) return Math.max(1440, viewportWidth - 100)
  if (viewportWidth >= 1360) return 1360
  if (viewportWidth >= 1280) return 1280
  if (viewportWidth >= 1120) return 1120
  if (viewportWidth >= 1000) return 1000
  return 1100
}

// 视口宽度下需要隐藏的列 key 集合（issue #70）：
// 表格可用宽度 = min(--content-width, 视口) − 80px（.content/.card 左右 padding），
// 按 HIDDEN_COL_PRIORITY 优先级逐个隐藏，直到剩余列宽 ≤ 可用宽度；
// 全部隐藏后仍装不下时保留 .table-wrap 横向滚动兜底（issue #28）。
// 异常输入（NaN/≤0/undefined/null）按最窄处理（5 列全隐藏），不越界不报错。
export function hiddenColumnsForWidth(viewportWidth) {
  if (!Number.isFinite(viewportWidth) || viewportWidth <= 0) {
    return new Set(HIDDEN_COL_PRIORITY.map((c) => c.key))
  }
  const avail = Math.min(contentWidthAt(viewportWidth), viewportWidth) - CONTENT_CARD_PAD_X
  const hidden = new Set()
  let remain = TABLE_MIN_WIDTH
  for (const c of HIDDEN_COL_PRIORITY) {
    if (remain <= avail) break
    hidden.add(c.key)
    remain -= c.width
  }
  return hidden
}

// 来源列文案（表格与抽屉共用，issue #70）
export function sourceLabel(t) {
  return t.triggered_by === 'reconcile' ? '对账' : t.triggered_by === 'manual' ? '手动' : 'webhook'
}

// 来源列 i18n 键后缀（issue #268）：表格与抽屉经 tr('tasks.source' + 后缀) 渲染
export function sourceLabelKey(t) {
  return t.triggered_by === 'reconcile' ? 'Reconcile' : t.triggered_by === 'manual' ? 'Manual' : 'Webhook'
}

// 任务抽屉（issue #70）：窄视口下部分列被隐藏时，点操作列「⋯」按钮
// 弹出右侧抽屉显示该任务全部字段（含被隐藏列的数据）。
function TaskDrawer({ task, onClose }) {
  const { tr } = useI18n()
  const meta = STATUS_META[task.status] || { label: task.status, cls: '' }
  const failedReason =
    (task.status === 'failed' || task.status === 'interrupted') && task.error_message
      ? task.error_message
      : ''
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>{tr('tasks.drawerTitle', { id: task.id, iid: task.issue_iid, title: task.issue_title })}</strong>
          <button className="btn modal-close" onClick={onClose} title={tr('common.close')}
                  aria-label={tr('tasks.closeDrawer')}><Icon name="x" /></button>
        </div>
        <table className="table kv">
          <tbody>
            <tr><th>{tr('tasks.task')}</th><td><Link to={`/tasks/${task.id}`}>#{task.id}</Link></td></tr>
            <tr><th>{tr('tasks.repo')}</th><td>{task.repo_name || '—'}</td></tr>
            <tr><th>{tr('tasks.issue')}</th><td><Link to={`/tasks/${task.id}`}>#{task.issue_iid}</Link></td></tr>
            <tr><th>{tr('tasks.title')}</th><td className="pre-wrap">{task.issue_title || '—'}</td></tr>
            <tr><th>{tr('tasks.status')}</th><td><span className={'badge ' + meta.cls}>{meta.label}</span></td></tr>
            <tr><th>{tr('tasks.attempt')}</th><td>
              {task.attempt_count}
              {task.resumed && (
                <span className="badge resume" title={tr('tasks.resumedTitle')}>{tr('tasks.resumed')}</span>
              )}
            </td></tr>
            <tr><th>{tr('tasks.source')}</th><td>{tr('tasks.source' + sourceLabelKey(task))}</td></tr>
            <tr><th>{tr('tasks.reason')}</th><td className="pre-wrap">{failedReason || '—'}</td></tr>
            <tr><th>{tr('tasks.commit')}</th><td>
              {task.commit_url ? (
                <a href={task.commit_url} target="_blank" rel="noreferrer"
                   title={tr('tasks.viewCommit', { sha: task.commit_sha })}>{shortSha(task.commit_sha)}</a>
              ) : (
                <span className="muted">—</span>
              )}
            </td></tr>
            <tr><th>{tr('tasks.createdAt')}</th><td>{fmtTime(task.created_at)}</td></tr>
            <tr><th>{tr('tasks.duration')}</th><td>{fmtDuration(task.created_at, task.finished_at) || '—'}</td></tr>
            <tr><th>{tr('tasks.actions')}</th><td>
              <Link to={`/tasks/${task.id}?live=1`} className="btn btn-mini"
                    title={tr('tasks.runTitle')}>{tr('tasks.run')}</Link>
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}

// 翻页组件页码窗口（issue #50）：页数少（≤7）时全量显示；
// 页数多时显示首尾页 + 当前页 ±1，中间省略号。返回数组元素为数字或 '…'。
export function pageNumbers(totalPages, current) {
  if (totalPages <= 0) return []
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1)
  const set = new Set([1, totalPages, current - 1, current, current + 1])
  const nums = [...set].filter((n) => n >= 1 && n <= totalPages).sort((a, b) => a - b)
  const out = []
  let prev = 0
  for (const n of nums) {
    if (n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
}

export default function Tasks() {
  // 界面国际化（issue #268）：静态 UI 文案经 t() 翻译（默认中文）
  const { tr } = useI18n()
  const [data, setData] = useState({ tasks: [], total: 0, stats: {} })
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [repos, setRepos] = useState([])
  const [repoId, setRepoId] = useState('')
  const [page, setPage] = useState(1) // 当前页（issue #50 翻页组件）
  const [error, setError] = useState('')
  const [detailTask, setDetailTask] = useState(null) // 正在查看详细失败原因的任务
  const [stopMsg, setStopMsg] = useState('') // 一键停止成功提示（issue #35）
  const [stopping, setStopping] = useState(false) // 停止请求进行中
  const [stopId, setStopId] = useState(null) // 正在单任务停止的任务 id（issue #214，请求中禁用）
  const [stopTaskMsg, setStopTaskMsg] = useState('') // 单任务停止成功提示（issue #214）
  const [retryMsg, setRetryMsg] = useState('') // 手动重试成功提示（issue #36）
  const [retryId, setRetryId] = useState(null) // 正在重试的任务 id（请求中禁用）
  const [reconcileMsg, setReconcileMsg] = useState('') // 一键对账成功提示（issue #38）
  const [reconciling, setReconciling] = useState(false) // 对账请求进行中
  const [refreshing, setRefreshing] = useState(false) // 手动刷新请求进行中（issue #59）
  const [drawerTask, setDrawerTask] = useState(null) // ⋯ 按钮打开的右侧抽屉任务（issue #70）
  // 窄视口卡片式列表（issue #270）：≤860px 时表格切换为卡片列表（触屏友好）
  const [isMobile, setIsMobile] = useState(false) // 默认桌面布局（SSR/无 window 环境）
  const [hiddenCols, setHiddenCols] = useState(() => new Set()) // 窄视口隐藏的列 key 集合（issue #70）
  // issue #235：任务列表可选展示 token 用量列（默认关闭，勾选后带
  // include_usage=1 重新拉取，后端批量返回避免逐任务 N+1 查询）
  const [showUsage, setShowUsage] = useState(false)
  // 搜索框 ref（issue #269）：/ 快捷键聚焦搜索框用
  const searchRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      })
      if (status) q.set('status', status)
      if (search.trim()) q.set('search', search.trim())
      if (repoId) q.set('repo_id', repoId)
      if (showUsage) q.set('include_usage', '1')
      const d = await api.get('/api/tasks?' + q, { silent: true })
      setData(d)
    } catch (e) {
      setError(e.message)
    }
  }, [status, search, repoId, page, showUsage])

  // 总页数（issue #50）：total 为 0 时也保持 ≥1，组件渲染条件另由 totalPages > 1 控制
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))

  useEffect(() => {
    api.get('/api/repos').then((d) => setRepos(d.repos)).catch(() => {})
  }, [])

  // 键盘快捷键（issue #269）：页面级绑定——r = 手动刷新任务列表
  // （与「刷新」按钮等效，低危操作无需确认），/ = 聚焦搜索框
  // （防误触：搜索框已聚焦时按 / 不重复聚焦/输入斜杠）。输入框
  // 聚焦自动不触发、开关关闭全部失效（keymap.js 统一处理）
  useShortcuts({
    'refresh': () => refreshList(),
    'focus-search': () => focusElement(searchRef.current),
  }, { storage: typeof localStorage !== 'undefined' ? localStorage : null })

  // 窄视口列隐藏（issue #70）：挂载时按视口宽度计算需隐藏的列，窗口缩放时重算。
  // SSR 测试环境无 window 时保持默认全显示。
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return
    const update = () => {
      setHiddenCols(hiddenColumnsForWidth(window.innerWidth))
      // 窄视口卡片列表（issue #270）：与列隐藏同源监听 resize，
      // 跨过 860px 断点时在表格/卡片两种形态间切换
      setIsMobile(isMobileViewport(window.innerWidth))
    }
    update()
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [])

  // 有隐藏列时操作列最右侧出现「⋯」按钮（issue #70）
  const hasHiddenCols = hiddenCols.size > 0
  // 表格 min-width 随隐藏列缩减为剩余列宽总和，窄屏下不出现横向滚动条；
  // issue #235：勾选「显示用量」时追加用量列宽（默认不勾选，布局不变）
  const tableMinWidth = TABLE_MIN_WIDTH + (showUsage ? USAGE_COL_WIDTH : 0) -
    HIDDEN_COL_PRIORITY.reduce((sum, c) => sum + (hiddenCols.has(c.key) ? c.width : 0), 0)
  // 隐藏列的 className（display:none 由 styles.css 提供，列保留 DOM 保持 nth-child 索引）
  const colCls = (key) => (hiddenCols.has(key) ? 'col-hidden' : undefined)

  // 活跃任务数（issue #35）：排队 + 执行 + 重试
  const activeCount =
    (data.stats?.queued || 0) + (data.stats?.running || 0) + (data.stats?.retrying || 0)

  // 有活跃任务时每 5s 自动刷新（issue #59，issue #200 起经 usePolling 统一
  // 管理可见性）：页面隐藏时暂停轮询（后台标签页 0 请求），恢复可见立即
  // 拉一次再恢复；immediate=true 保证挂载 / 过滤条件变化时即使无活跃任务
  // 也会先拉一次列表（与原 useEffect 行为一致）
  usePolling(load, 5000, { enabled: activeCount > 0, immediate: true })

  // 一键停止所有任务（issue #35）：确认后调后端批量停止，刷新列表
  const stopAll = async () => {
    if (!(await confirmDialog({
      message: tr('tasks.confirmStopAll', { n: activeCount }),
      danger: true,
    }))) {
      return
    }
    setStopping(true)
    setStopMsg('')
    try {
      const r = await api.post('/api/tasks/stop-all')
      setStopMsg(tr('tasks.stopped', { n: r.count }))
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setStopping(false)
    }
  }

  // 手动重试任务（issue #36）：确认后重新入队执行（接续上次 claude 会话），刷新列表
  const retryTask = async (t) => {
    if (!(await confirmDialog({
      message: tr('tasks.confirmRetry', { id: t.id, iid: t.issue_iid, title: t.issue_title || '' }),
    }))) {
      return
    }
    setRetryId(t.id)
    setRetryMsg('')
    try {
      await api.post(`/api/tasks/${t.id}/retry`)
      setRetryMsg(tr('tasks.retried', { id: t.id }))
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setRetryId(null)
    }
  }

  // 单任务停止（issue #214）：仅执行中（running）任务可停止；确认后调
  // 后端 POST /api/tasks/{id}/stop——状态落库 interrupted 并强制终止执行
  // 中的引擎进程（停止不可逆），随后刷新列表。请求中禁用防重复点击。
  const stopTask = async (t) => {
    if (!(await confirmDialog({
      message: tr('tasks.confirmStop', { id: t.id, iid: t.issue_iid, title: t.issue_title || '' }),
      danger: true,
    }))) {
      return
    }
    setStopId(t.id)
    setStopTaskMsg('')
    try {
      await api.post(`/api/tasks/${t.id}/stop`)
      setStopTaskMsg(tr('tasks.stoppedOne', { id: t.id }))
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setStopId(null)
    }
  }

  // 一键对账所有启用仓库（issue #38）：同步扫描全部启用仓库把漏单补入队列。
  // 与仓库页对账按钮一致为低危操作（无需确认）；部分仓库失败时展示失败明细。
  const reconcileAll = async () => {
    setReconciling(true)
    setReconcileMsg('')
    try {
      const r = await api.post('/api/tasks/reconcile-all')
      if (r.errors && r.errors.length > 0) {
        setError(tr('tasks.reconcileDone', { scanned: r.scanned, enqueued: r.enqueued }) + '；' +
          tr('tasks.reconcileErrors', { n: r.errors.length, msg: r.errors[0] }))
      } else {
        setReconcileMsg(tr('tasks.reconcileDone', { scanned: r.scanned, enqueued: r.enqueued }))
      }
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setReconciling(false)
    }
  }

  // 手动刷新任务列表（issue #59）：重新拉取 /api/tasks 更新所有任务的显示状态。
  // 页面仅在存在活跃任务时每 5s 自动轮询，全部结束后轮询停止，状态可能陈旧；
  // 刷新为低危操作无需确认；load 内部已捕获错误并置 error 提示，请求中禁用防重复点击。
  const refreshList = async () => {
    setRefreshing(true)
    await load()
    setRefreshing(false)
  }

  return (
    <div>
      <h1>{tr('tasks.listTitle')}</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {stopMsg && <div className="alert alert-ok" onClick={() => setStopMsg('')}>{stopMsg}</div>}
      {retryMsg && <div className="alert alert-ok" onClick={() => setRetryMsg('')}>{retryMsg}</div>}
      {stopTaskMsg && <div className="alert alert-ok" onClick={() => setStopTaskMsg('')}>{stopTaskMsg}</div>}
      {reconcileMsg && <div className="alert alert-ok" onClick={() => setReconcileMsg('')}>{reconcileMsg}</div>}

      <div className="stats-row">
        {Object.entries(data.stats || {}).map(([k, v]) => (
          <div key={k} className="stat-chip">
            <span className={'status-dot ' + (STATUS_META[k]?.cls || '')} />
            {STATUS_META[k]?.label || k}: <b>{v}</b>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="form-row wrap">
          {/* 筛选变化重置回第 1 页（issue #50），避免停留在越界页码 */}
          <select className="input" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
            <option value="">{tr('tasks.allStatus')}</option>
            {Object.entries(STATUS_META).map(([k, v]) => (
              <option key={k} value={k}>{v.label}</option>
            ))}
          </select>
          <select className="input" value={repoId} onChange={(e) => { setRepoId(e.target.value); setPage(1) }}>
            <option value="">{tr('tasks.allRepos')}</option>
            {repos.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <input
            ref={searchRef}
            className="input grow"
            placeholder={tr('tasks.searchPlaceholder')}
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          />
          {/* 一键停止所有任务（issue #35）：高危操作，需确认；无活跃任务或请求中禁用 */}
          <button
            className="btn btn-danger"
            onClick={stopAll}
            disabled={activeCount === 0 || stopping}
            title={activeCount === 0 ? tr('tasks.noActiveTasks') : tr('tasks.stopAllTitle')}
          >
            <><Icon name="square" /> {activeCount > 0 ? tr('tasks.stopAllWithCount', { n: activeCount }) : tr('tasks.stopAll')}</>
          </button>
          {/* 一键对账所有启用仓库（issue #38）：低危操作无需确认；请求中禁用 */}
          <button
            className="btn btn-gap-left"
            onClick={reconcileAll}
            disabled={reconciling}
            title={tr('tasks.reconcileAllTitle')}
          >
            <><Icon name="refresh" /> {tr('tasks.reconcileAll')}{reconciling ? '…' : ''}</>
          </button>
          {/* 手动刷新任务列表（issue #59）：无活跃任务时页面无自动轮询，
              点此重新拉取更新所有任务状态；请求中禁用防重复点击 */}
          <button
            className="btn btn-gap-left"
            onClick={refreshList}
            disabled={refreshing}
            title={tr('tasks.refreshTitle')}
          >
            <><Icon name="refresh" /> {tr('common.refresh')}{refreshing ? '…' : ''}</>
          </button>
          {/* issue #235：任务列表可选展示 token 用量列——默认关闭（列表
              不查询用量，无额外开销）；勾选后重新拉取展示 */}
          <label className="checkbox-label tasks-usage-toggle" title={tr('tasks.showUsageTitle')}>
            <input type="checkbox" checked={showUsage}
                   onChange={(e) => setShowUsage(e.target.checked)} />
            {tr('tasks.showUsage')}
          </label>
        </div>

        {isMobile ? (
          <div className="tasks-card-list">
            {data.tasks.length === 0 ? (
              <div className="card">
                <div className="empty-state">
                  <span className="empty-icon" aria-hidden="true"><Icon name="folderOpen" /></span>
                  <p className="muted">{tr('tasks.noTasks')}</p>
                </div>
              </div>
            ) : data.tasks.map((t) => {
              const meta = STATUS_META[t.status] || { label: t.status, cls: '' }
              // 仅失败/中断的任务展示失败原因
              const failedReason =
                (t.status === 'failed' || t.status === 'interrupted') && t.error_message
                  ? t.error_message
                  : ''
              // 详细失败原因（每次尝试 + trace）不直接展示，通过按钮弹窗查看
              const hasDetail = Array.isArray(t.error_detail?.attempts) && t.error_detail.attempts.length > 0
              return (
                <div key={t.id} className="card tasks-card">
                  <div className="tasks-card-head">
                    <Link to={`/tasks/${t.id}`} className="tasks-card-title"
                          title={t.issue_title || ''}>
                      #{t.id} {t.issue_title || '—'}
                    </Link>
                    <span className={'badge ' + meta.cls}>{meta.label}</span>
                  </div>
                  <dl className="tasks-card-meta">
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.repo')}</dt><dd>{t.repo_name || '—'}</dd></div>
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.issue')}</dt><dd><Link to={`/tasks/${t.id}`}>#{t.issue_iid}</Link></dd></div>
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.source')}</dt><dd>{tr('tasks.source' + sourceLabelKey(t))}</dd></div>
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.attempt')}</dt><dd>
                      {t.attempt_count}
                      {t.resumed && (
                        <span className="badge resume" title={tr('tasks.resumedTitle')}>{tr('tasks.resumed')}</span>
                      )}
                    </dd></div>
                    {showUsage && (
                      <div className="tasks-card-meta-row"><dt>{tr('tasks.usage')}</dt><dd>
                        {t.usage ? <UsageSummary usage={t.usage} /> : <span className="muted">—</span>}
                      </dd></div>
                    )}
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.createdAt')}</dt><dd>{fmtTime(t.created_at)}</dd></div>
                    <div className="tasks-card-meta-row"><dt>{tr('tasks.duration')}</dt><dd>{fmtDuration(t.created_at, t.finished_at) || <span className="muted">—</span>}</dd></div>
                    {t.commit_url && (
                      <div className="tasks-card-meta-row"><dt>{tr('tasks.commit')}</dt><dd>
                        <a href={t.commit_url} target="_blank" rel="noreferrer"
                           title={tr('tasks.viewCommit', { sha: t.commit_sha })}>{shortSha(t.commit_sha)}</a>
                      </dd></div>
                    )}
                  </dl>
                  {failedReason && (
                    <p className="tasks-card-reason">
                      <strong>{tr('tasks.reason')}：</strong>{failedReason}
                    </p>
                  )}
                  <div className="tasks-card-actions">
                    <Link to={`/tasks/${t.id}?live=1`} className="btn btn-mini"
                          title={tr('tasks.runTitle')}>{tr('tasks.run')}</Link>
                    {t.status === 'running' && (
                      <button
                        className="btn btn-mini btn-danger"
                        onClick={() => stopTask(t)}
                        disabled={stopId === t.id}
                        title={tr('tasks.stopTitle')}
                      >
                        {stopId === t.id ? tr('tasks.stopping') : tr('tasks.stop')}
                      </button>
                    )}
                    {(t.status === 'failed' || t.status === 'interrupted') && (
                      <button
                        className="btn btn-mini"
                        onClick={() => retryTask(t)}
                        disabled={retryId === t.id}
                        title={tr('tasks.retryTitle')}
                      >
                        {retryId === t.id ? tr('tasks.retrying') : tr('tasks.retry')}
                      </button>
                    )}
                    {hasDetail && (
                      <button className="btn btn-mini" onClick={() => setDetailTask(t)}>{tr('tasks.detail')}</button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="table-wrap">
          {/* table-layout: fixed 固定布局，表格宽度恒等于容器宽度，宽视口不出现水平滚动条（issue #28 第二轮）；
              窄视口隐藏列后 min-width 缩减为剩余列宽总和（issue #70） */}
          <table className={'table tasks-table' + (showUsage ? ' tasks-table-usage' : '')}
                 style={{ minWidth: tableMinWidth }}>
            <thead>
              <tr>
                <th>#</th><th>{tr('tasks.repo')}</th><th>{tr('tasks.issue')}</th><th>{tr('tasks.title')}</th>
                <th>{tr('tasks.status')}</th>
                {showUsage && <th className="usage-col">{tr('tasks.usage')}</th>}
                <th className={colCls('attempt')}>{tr('tasks.attempt')}</th>
                <th className={colCls('source')}>{tr('tasks.source')}</th>
                <th className={colCls('reason')}>{tr('tasks.reason')}</th>
                <th>{tr('tasks.commit')}</th>
                <th className={colCls('created')}>{tr('tasks.createdAt')}</th>
                <th className={colCls('duration')}>{tr('tasks.duration')}</th>
                <th>{tr('tasks.actions')}</th>
              </tr>
            </thead>
          <tbody>
            {data.tasks.length === 0 && (
              <tr><td colSpan={showUsage ? 13 : 12}>
                <div className="empty-state">
                  <span className="empty-icon" aria-hidden="true"><Icon name="folderOpen" /></span>
                  <p className="muted">{tr('tasks.noTasks')}</p>
                </div>
              </td></tr>
            )}
            {data.tasks.map((t) => {
              const meta = STATUS_META[t.status] || { label: t.status, cls: '' }
              // 仅失败/中断的任务展示失败原因
              const failedReason =
                (t.status === 'failed' || t.status === 'interrupted') && t.error_message
                  ? t.error_message
                  : ''
              // 详细失败原因（每次尝试 + trace）不直接展示，通过按钮弹窗查看
              const hasDetail = Array.isArray(t.error_detail?.attempts) && t.error_detail.attempts.length > 0
              return (
                <tr key={t.id}>
                  <td><Link to={`/tasks/${t.id}`}>#{t.id}</Link></td>
                  <td className="ellipsis" title={t.repo_name}>{t.repo_name || '—'}</td>
                  <td><Link to={`/tasks/${t.id}`}>#{t.issue_iid}</Link></td>
                  <td className="ellipsis" title={t.issue_title}>{t.issue_title || '—'}</td>
                  <td><span className={'badge ' + meta.cls}>{meta.label}</span></td>
                  {showUsage && (
                    <td className="usage-col">
                      {t.usage ? <UsageSummary usage={t.usage} /> : <span className="muted">—</span>}
                    </td>
                  )}
                  <td className={colCls('attempt')}>
                    {t.attempt_count}
                    {t.resumed && (
                      <span className="badge resume" title={tr('tasks.resumedTitle')}>{tr('tasks.resumed')}</span>
                    )}
                  </td>
                  <td className={colCls('source')}>{tr('tasks.source' + sourceLabelKey(t))}</td>
                  <td className={colCls('reason') ? 'ellipsis col-hidden' : 'ellipsis'} title={failedReason}>
                    {failedReason || <span className="muted">—</span>}
                    {hasDetail && (
                      <button className="btn btn-mini btn-gap-left" onClick={() => setDetailTask(t)}>{tr('tasks.detail')}</button>
                    )}
                  </td>
                  <td className="ellipsis" title={t.commit_sha ? tr('tasks.viewCommit', { sha: t.commit_sha }) : undefined}>
                    {t.commit_url ? (
                      <a href={t.commit_url} target="_blank" rel="noreferrer"
                         title={tr('tasks.viewCommit', { sha: t.commit_sha })}>{shortSha(t.commit_sha)}</a>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className={colCls('created')}>{fmtTime(t.created_at)}</td>
                  {/* 用时（issue #49）：系统接收时间 created_at → bot-done 打标时间
                      finished_at 的动态计算，不再用执行开始时间 started_at 作起点 */}
                  <td className={colCls('duration')}>{fmtDuration(t.created_at, t.finished_at) || <span className="muted">—</span>}</td>
                  <td>
                    <Link to={`/tasks/${t.id}?live=1`} className="btn btn-mini"
                          title={tr('tasks.runTitle')}>{tr('tasks.run')}</Link>
                    {/* 单任务停止（issue #214）：仅执行中（running）任务可停止，
                        确认后标记 interrupted 并强制终止引擎进程（不可逆），
                        请求中禁用防重复点击 */}
                    {t.status === 'running' && (
                      <button
                        className="btn btn-mini btn-danger btn-gap-left"
                        onClick={() => stopTask(t)}
                        disabled={stopId === t.id}
                        title={tr('tasks.stopTitle')}
                      >
                        {stopId === t.id ? tr('tasks.stopping') : tr('tasks.stop')}
                      </button>
                    )}
                    {/* 手动重试（issue #36）：仅失败/中断任务可重试，请求中禁用防重复点击 */}
                    {(t.status === 'failed' || t.status === 'interrupted') && (
                      <button
                        className="btn btn-mini btn-gap-left"
                        onClick={() => retryTask(t)}
                        disabled={retryId === t.id}
                        title={tr('tasks.retryTitle')}
                      >
                        {retryId === t.id ? tr('tasks.retrying') : tr('tasks.retry')}
                      </button>
                    )}
                    {/* ⋯ 按钮（issue #70）：有列被隐藏时出现在操作列最右侧，
                        点击弹出右侧抽屉显示该任务全部数据 */}
                    {hasHiddenCols && (
                      <button
                        className="btn btn-mini btn-gap-left"
                        onClick={() => setDrawerTask(t)}
                        title={tr('tasks.viewAllFields')}
                      >
                        ⋯
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          </table>
        </div>
        )}
        <p className="muted small">{tr('tasks.total', { n: data.total })}</p>

        {/* 翻页组件（issue #50）：多页时显示；上一页/页码/下一页 + 当前页信息 */}
        {totalPages > 1 && (
          <div className="pagination">
            <button
              className="btn btn-sm"
              disabled={page === 1}
              onClick={() => setPage(page - 1)}
              title={tr('tasks.prevTitle')}
            >
              {tr('tasks.prev')}
            </button>
            {pageNumbers(totalPages, page).map((n, i) =>
              n === '…' ? (
                <span key={`gap-${i}`} className="muted">…</span>
              ) : (
                <button
                  key={n}
                  className={'btn btn-sm' + (n === page ? ' btn-primary' : '')}
                  disabled={n === page}
                  onClick={() => setPage(n)}
                  title={tr('tasks.pageBtnTitle', { n })}
                >
                  {String(n)}
                </button>
              ),
            )}
            <button
              className="btn btn-sm"
              disabled={page === totalPages}
              onClick={() => setPage(page + 1)}
              title={tr('tasks.nextTitle')}
            >
              {tr('tasks.next')}
            </button>
            <span className="muted small">{tr('tasks.pageInfo', { page, total: totalPages })}</span>
          </div>
        )}
      </div>

      {/* 任务抽屉（issue #70）：⋯ 按钮打开，显示全部字段（含窄屏下被隐藏的列） */}
      {drawerTask && <TaskDrawer task={drawerTask} onClose={() => setDrawerTask(null)} />}

      {detailTask && (
        <div className="modal-overlay" onClick={() => setDetailTask(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong>{tr('tasks.detailTitle', { iid: detailTask.issue_iid, title: detailTask.issue_title })}</strong>
              <button className="btn modal-close" onClick={() => setDetailTask(null)}
                      title={tr('common.close')} aria-label={tr('tasks.closeModal')}><Icon name="x" /></button>
            </div>
            {detailTask.error_message && (
              <div className="error-summary">
                <strong>{tr('tasks.summary')}</strong>
                <span className="pre-wrap">{detailTask.error_message}</span>
              </div>
            )}
            <div className="error-detail-body">
              {(detailTask.error_detail?.attempts || []).map((a) => (
                <div key={a.attempt} className="error-attempt">
                  <div className="error-attempt-head">
                    <span>{tr('tasks.attemptN', { n: a.attempt })}</span>
                    <code>{tr('tasks.exitCode', { code: a.exit_code ?? '—' })}</code>
                  </div>
                  <pre className="error-attempt-trace">{a.error || tr('common.noOutput')}</pre>
                </div>
              ))}
              {!detailTask.error_detail?.attempts?.length && (
                <p className="muted">{tr('tasks.noDetail')}</p>
              )}
            </div>
            <div className="modal-footer">
              {detailTask.log_path && <code className="muted small">{tr('tasks.logFile', { path: detailTask.log_path })}</code>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
