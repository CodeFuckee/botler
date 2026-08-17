import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtTime, fmtDuration, shortSha, STATUS_META } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'

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

// 12 列宽度总和（styles.css .table.tasks-table min-width 同值）
export const TABLE_MIN_WIDTH = 1360

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

// 任务抽屉（issue #70）：窄视口下部分列被隐藏时，点操作列「⋯」按钮
// 弹出右侧抽屉显示该任务全部字段（含被隐藏列的数据）。
function TaskDrawer({ task, onClose }) {
  const meta = STATUS_META[task.status] || { label: task.status, cls: '' }
  const failedReason =
    (task.status === 'failed' || task.status === 'interrupted') && task.error_message
      ? task.error_message
      : ''
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>任务 #{task.id} 全部数据 — #{task.issue_iid} {task.issue_title}</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭抽屉"><Icon name="x" /></button>
        </div>
        <table className="table kv">
          <tbody>
            <tr><th>任务</th><td><Link to={`/tasks/${task.id}`}>#{task.id}</Link></td></tr>
            <tr><th>仓库</th><td>{task.repo_name || '—'}</td></tr>
            <tr><th>Issue</th><td><Link to={`/tasks/${task.id}`}>#{task.issue_iid}</Link></td></tr>
            <tr><th>标题</th><td className="pre-wrap">{task.issue_title || '—'}</td></tr>
            <tr><th>状态</th><td><span className={'badge ' + meta.cls}>{meta.label}</span></td></tr>
            <tr><th>尝试</th><td>
              {task.attempt_count}
              {task.resumed && (
                <span className="badge resume" title="从上次中断的 claude 会话恢复执行（断点续跑）">恢复</span>
              )}
            </td></tr>
            <tr><th>来源</th><td>{sourceLabel(task)}</td></tr>
            <tr><th>失败原因</th><td className="pre-wrap">{failedReason || '—'}</td></tr>
            <tr><th>提交</th><td>
              {task.commit_url ? (
                <a href={task.commit_url} target="_blank" rel="noreferrer"
                   title={`查看提交 ${task.commit_sha}`}>{shortSha(task.commit_sha)}</a>
              ) : (
                <span className="muted">—</span>
              )}
            </td></tr>
            <tr><th>创建时间</th><td>{fmtTime(task.created_at)}</td></tr>
            <tr><th>用时</th><td>{fmtDuration(task.created_at, task.finished_at) || '—'}</td></tr>
            <tr><th>操作</th><td>
              <Link to={`/tasks/${task.id}?live=1`} className="btn btn-mini"
                    title="实时查看 agent 执行进度与聊天记录（issue #20）">执行</Link>
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
  const [retryMsg, setRetryMsg] = useState('') // 手动重试成功提示（issue #36）
  const [retryId, setRetryId] = useState(null) // 正在重试的任务 id（请求中禁用）
  const [reconcileMsg, setReconcileMsg] = useState('') // 一键对账成功提示（issue #38）
  const [reconciling, setReconciling] = useState(false) // 对账请求进行中
  const [refreshing, setRefreshing] = useState(false) // 手动刷新请求进行中（issue #59）
  const [drawerTask, setDrawerTask] = useState(null) // ⋯ 按钮打开的右侧抽屉任务（issue #70）
  const [hiddenCols, setHiddenCols] = useState(() => new Set()) // 窄视口隐藏的列 key 集合（issue #70）
  const timer = useRef(null)

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      })
      if (status) q.set('status', status)
      if (search.trim()) q.set('search', search.trim())
      if (repoId) q.set('repo_id', repoId)
      const d = await api.get('/api/tasks?' + q)
      setData(d)
    } catch (e) {
      setError(e.message)
    }
  }, [status, search, repoId, page])

  // 总页数（issue #50）：total 为 0 时也保持 ≥1，组件渲染条件另由 totalPages > 1 控制
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))

  // 有活跃任务时每 5s 自动刷新
  useEffect(() => {
    load()
    const active = data.stats?.queued + data.stats?.running + data.stats?.retrying
    if (active > 0 && !timer.current) {
      timer.current = setInterval(load, 5000)
    } else if (active === 0 && timer.current) {
      clearInterval(timer.current)
      timer.current = null
    }
    return () => { if (timer.current) { clearInterval(timer.current); timer.current = null } }
  }, [load, data.stats?.queued, data.stats?.running, data.stats?.retrying])

  useEffect(() => {
    api.get('/api/repos').then((d) => setRepos(d.repos)).catch(() => {})
  }, [])

  // 窄视口列隐藏（issue #70）：挂载时按视口宽度计算需隐藏的列，窗口缩放时重算。
  // SSR 测试环境无 window 时保持默认全显示。
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return
    const update = () => setHiddenCols(hiddenColumnsForWidth(window.innerWidth))
    update()
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [])

  // 有隐藏列时操作列最右侧出现「⋯」按钮（issue #70）
  const hasHiddenCols = hiddenCols.size > 0
  // 表格 min-width 随隐藏列缩减为剩余列宽总和，窄屏下不出现横向滚动条
  const tableMinWidth = TABLE_MIN_WIDTH -
    HIDDEN_COL_PRIORITY.reduce((sum, c) => sum + (hiddenCols.has(c.key) ? c.width : 0), 0)
  // 隐藏列的 className（display:none 由 styles.css 提供，列保留 DOM 保持 nth-child 索引）
  const colCls = (key) => (hiddenCols.has(key) ? 'col-hidden' : undefined)

  // 活跃任务数（issue #35）：排队 + 执行 + 重试
  const activeCount =
    (data.stats?.queued || 0) + (data.stats?.running || 0) + (data.stats?.retrying || 0)

  // 一键停止所有任务（issue #35）：确认后调后端批量停止，刷新列表
  const stopAll = async () => {
    if (!(await confirmDialog({
      message: `确定停止所有正在执行的任务吗？当前 ${activeCount} 个活跃任务（排队/执行/重试）将被标记为已中断，执行中的 claude 进程会被强制终止。`,
      danger: true,
    }))) {
      return
    }
    setStopping(true)
    setStopMsg('')
    try {
      const r = await api.post('/api/tasks/stop-all')
      setStopMsg(`已停止 ${r.count} 个任务`)
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
      message: `确定重试任务 #${t.id}（issue #${t.issue_iid} ${t.issue_title || ''}）吗？任务将重新入队执行，并接续上次 claude 会话继续处理。`,
    }))) {
      return
    }
    setRetryId(t.id)
    setRetryMsg('')
    try {
      await api.post(`/api/tasks/${t.id}/retry`)
      setRetryMsg(`任务 #${t.id} 已重新入队，开始重试`)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setRetryId(null)
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
        setError(`对账完成：扫描 ${r.scanned} 个 issue，补入队 ${r.enqueued} 个任务；` +
          `${r.errors.length} 个仓库失败：${r.errors[0]}`)
      } else {
        setReconcileMsg(`对账完成：扫描 ${r.scanned} 个 issue，补入队 ${r.enqueued} 个任务`)
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
      <h1>任务列表</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {stopMsg && <div className="alert alert-ok" onClick={() => setStopMsg('')}>{stopMsg}</div>}
      {retryMsg && <div className="alert alert-ok" onClick={() => setRetryMsg('')}>{retryMsg}</div>}
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
            <option value="">全部状态</option>
            {Object.entries(STATUS_META).map(([k, v]) => (
              <option key={k} value={k}>{v.label}</option>
            ))}
          </select>
          <select className="input" value={repoId} onChange={(e) => { setRepoId(e.target.value); setPage(1) }}>
            <option value="">全部仓库</option>
            {repos.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <input
            className="input grow"
            placeholder="搜索 issue 标题或编号…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          />
          {/* 一键停止所有任务（issue #35）：高危操作，需确认；无活跃任务或请求中禁用 */}
          <button
            className="btn btn-danger"
            onClick={stopAll}
            disabled={activeCount === 0 || stopping}
            title={activeCount === 0 ? '当前没有正在执行的任务' : '停止所有排队中、执行中、重试中的任务'}
          >
            <><Icon name="square" /> 停止所有任务{activeCount > 0 ? `（${activeCount}）` : ''}</>
          </button>
          {/* 一键对账所有启用仓库（issue #38）：低危操作无需确认；请求中禁用 */}
          <button
            className="btn btn-gap-left"
            onClick={reconcileAll}
            disabled={reconciling}
            title="立即扫描所有启用仓库，把漏掉的 issue 补入任务队列"
          >
            <><Icon name="refresh" /> 对账所有仓库{reconciling ? '…' : ''}</>
          </button>
          {/* 手动刷新任务列表（issue #59）：无活跃任务时页面无自动轮询，
              点此重新拉取更新所有任务状态；请求中禁用防重复点击 */}
          <button
            className="btn btn-gap-left"
            onClick={refreshList}
            disabled={refreshing}
            title="重新加载任务列表，更新所有任务的显示状态"
          >
            <><Icon name="refresh" /> 刷新{refreshing ? '…' : ''}</>
          </button>
        </div>

        {/* 12 列表格最小宽度超容器，外包滚动容器防止窄视口下内容溢出卡片（issue #28） */}
        <div className="table-wrap">
          {/* table-layout: fixed 固定布局，表格宽度恒等于容器宽度，宽视口不出现水平滚动条（issue #28 第二轮）；
              窄视口隐藏列后 min-width 缩减为剩余列宽总和（issue #70） */}
          <table className="table tasks-table" style={{ minWidth: tableMinWidth }}>
            <thead>
              <tr>
                <th>#</th><th>仓库</th><th>Issue</th><th>标题</th>
                <th>状态</th>
                <th className={colCls('attempt')}>尝试</th>
                <th className={colCls('source')}>来源</th>
                <th className={colCls('reason')}>失败原因</th>
                <th>提交</th>
                <th className={colCls('created')}>创建时间</th>
                <th className={colCls('duration')}>用时</th>
                <th>操作</th>
              </tr>
            </thead>
          <tbody>
            {data.tasks.length === 0 && (
              <tr><td colSpan={12}>
                <div className="empty-state">
                  <span className="empty-icon" aria-hidden="true"><Icon name="folderOpen" /></span>
                  <p className="muted">暂无任务</p>
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
                  <td className={colCls('attempt')}>
                    {t.attempt_count}
                    {t.resumed && (
                      <span className="badge resume" title="从上次中断的 claude 会话恢复执行（断点续跑）">恢复</span>
                    )}
                  </td>
                  <td className={colCls('source')}>{sourceLabel(t)}</td>
                  <td className={colCls('reason') ? 'ellipsis col-hidden' : 'ellipsis'} title={failedReason}>
                    {failedReason || <span className="muted">—</span>}
                    {hasDetail && (
                      <button className="btn btn-mini btn-gap-left" onClick={() => setDetailTask(t)}>详情</button>
                    )}
                  </td>
                  <td className="ellipsis" title={t.commit_sha ? `查看提交 ${t.commit_sha}` : undefined}>
                    {t.commit_url ? (
                      <a href={t.commit_url} target="_blank" rel="noreferrer"
                         title={`查看提交 ${t.commit_sha}`}>{shortSha(t.commit_sha)}</a>
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
                          title="实时查看 agent 执行进度与聊天记录（issue #20）">执行</Link>
                    {/* 手动重试（issue #36）：仅失败/中断任务可重试，请求中禁用防重复点击 */}
                    {(t.status === 'failed' || t.status === 'interrupted') && (
                      <button
                        className="btn btn-mini btn-gap-left"
                        onClick={() => retryTask(t)}
                        disabled={retryId === t.id}
                        title="手动重试：重新入队执行该任务（接续上次 claude 会话）"
                      >
                        {retryId === t.id ? '重试中…' : '重试'}
                      </button>
                    )}
                    {/* ⋯ 按钮（issue #70）：有列被隐藏时出现在操作列最右侧，
                        点击弹出右侧抽屉显示该任务全部数据 */}
                    {hasHiddenCols && (
                      <button
                        className="btn btn-mini btn-gap-left"
                        onClick={() => setDrawerTask(t)}
                        title="查看全部字段（窄屏下部分列已隐藏）"
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
        <p className="muted small">共 {data.total} 条</p>

        {/* 翻页组件（issue #50）：多页时显示；上一页/页码/下一页 + 当前页信息 */}
        {totalPages > 1 && (
          <div className="pagination">
            <button
              className="btn btn-sm"
              disabled={page === 1}
              onClick={() => setPage(page - 1)}
              title="上一页"
            >
              ‹ 上一页
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
                  title={`第 ${n} 页`}
                >
                  {String(n)}
                </button>
              ),
            )}
            <button
              className="btn btn-sm"
              disabled={page === totalPages}
              onClick={() => setPage(page + 1)}
              title="下一页"
            >
              下一页 ›
            </button>
            <span className="muted small">{`第 ${page} / ${totalPages} 页`}</span>
          </div>
        )}
      </div>

      {/* 任务抽屉（issue #70）：⋯ 按钮打开，显示全部字段（含窄屏下被隐藏的列） */}
      {drawerTask && <TaskDrawer task={drawerTask} onClose={() => setDrawerTask(null)} />}

      {detailTask && (
        <div className="modal-overlay" onClick={() => setDetailTask(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong>失败详细原因 — #{detailTask.issue_iid} {detailTask.issue_title}</strong>
              <button className="btn modal-close" onClick={() => setDetailTask(null)}
                      title="关闭" aria-label="关闭弹窗"><Icon name="x" /></button>
            </div>
            {detailTask.error_message && (
              <div className="error-summary">
                <strong>摘要：</strong>
                <span className="pre-wrap">{detailTask.error_message}</span>
              </div>
            )}
            <div className="error-detail-body">
              {(detailTask.error_detail?.attempts || []).map((a) => (
                <div key={a.attempt} className="error-attempt">
                  <div className="error-attempt-head">
                    <span>第 {a.attempt} 次尝试</span>
                    <code>退出码: {a.exit_code ?? '—'}</code>
                  </div>
                  <pre className="error-attempt-trace">{a.error || '（无输出）'}</pre>
                </div>
              ))}
              {!detailTask.error_detail?.attempts?.length && (
                <p className="muted">该任务没有更详细的失败记录</p>
              )}
            </div>
            <div className="modal-footer">
              {detailTask.log_path && <code className="muted small">日志文件: {detailTask.log_path}</code>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
