// 操作审计日志卡片（issue #260）：设置页「审计日志」查看入口。
//
// 数据：GET /api/audit-logs（分页 + 按操作类型过滤），响应含 items /
// total / page / per_page / actions（全部操作类型下拉）/ admin（当前用户
// 是否管理员，前端据此显隐删除按钮）。
// 删除：DELETE /api/audit-logs/{id}（仅管理员，普通用户 403）——二次确认。
// 管理员名单：audit_logs.admin_usernames（SSO 用户名，逗号分隔），保存走
// PUT /api/settings 的 audit_logs 段；名单为空 = 所有登录用户均为管理员
// （SSO 未启用时本机用户恒为管理员）。
import { useEffect, useState } from 'react'
import { Icon } from '../Icon.jsx'
import { api } from '../../api.js'
import { confirmDialog } from '../../dialog.js'

const PER_PAGE = 20

// detail JSON 展示：截断超长值（diff 大对象时避免表格撑爆）
function fmtValue(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  const s = String(v)
  return s.length > 80 ? s.slice(0, 80) + '…' : s
}

// 变更摘要：diff 优先展示 {字段: [旧, 新]}，其次展示 detail 概要
function summaryText(detail) {
  if (!detail || typeof detail !== 'object') return ''
  const parts = []
  if (Array.isArray(detail.sections)) parts.push('配置段: ' + detail.sections.join(', '))
  const diff = detail.diff
  if (diff && typeof diff === 'object') {
    for (const [section, fields] of Object.entries(diff)) {
      if (!fields || typeof fields !== 'object') continue
      for (const [key, pair] of Object.entries(fields)) {
        if (!Array.isArray(pair) || pair.length !== 2) continue
        parts.push(`${section}.${key}: ${fmtValue(pair[0])} → ${fmtValue(pair[1])}`)
      }
    }
  }
  if (parts.length) return parts.join('；')
  const keys = Object.keys(detail)
  if (keys.length) return keys.map((k) => `${k}=${fmtValue(detail[k])}`).join('；')
  return ''
}

// 操作类型中文标签（未知类型直接展示原文）
const ACTION_LABELS = {
  'settings.update': '设置保存',
  'config.external_edit': '外部修改 config.yaml',
  'repo.add': '添加仓库',
  'repo.update': '修改仓库',
  'repo.delete': '删除仓库',
  'repo.template_update': '修改仓库提示词模版',
  'task.retry': '任务重试',
  'task.stop': '任务停止',
  'task.delete': '任务移出队列',
  'task.stop_all': '一键停止任务',
  'task.priority': '任务人工优先级',
  'task.reconcile_all': '全量对账',
  'plugin.install': '安装插件',
  'plugin.uninstall': '卸载插件',
  'plugin.reload': '插件重载',
  'plugin.settings_update': '插件设置变更',
  'backup.create': '执行备份',
  'backup.delete': '删除备份',
  'backup.restore': '恢复备份',
}

export default function AuditLogsCard() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [action, setAction] = useState('')
  const [actions, setActions] = useState([])
  const [admin, setAdmin] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [deleting, setDeleting] = useState(null)
  const [adminsInput, setAdminsInput] = useState('')
  const [adminsSaved, setAdminsSaved] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = async (pg, act) => {
    setLoading(true); setError('')
    try {
      const params = new URLSearchParams({ page: String(pg), per_page: String(PER_PAGE) })
      if (act) params.set('action', act)
      const data = await api.get(`/api/audit-logs?${params}`)
      setItems(data.items || [])
      setTotal(data.total || 0)
      setPage(data.page || pg)
      setActions(data.actions || [])
      setAdmin(!!data.admin)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(1, '') }, [])

  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE))

  const changeAction = (act) => { setAction(act); load(1, act) }

  const changePage = (pg) => { if (pg >= 1 && pg <= totalPages) load(pg, action) }

  const del = async (row) => {
    if (!(await confirmDialog({
      message: `确定删除审计日志 #${row.id}（${ACTION_LABELS[row.action] || row.action}）？删除不可恢复。`,
      danger: true,
    }))) return
    setDeleting(row.id); setError('')
    try {
      await api.del(`/api/audit-logs/${row.id}`)
      setNote('审计日志已删除')
      // 删除后刷新当前页（若当前页删空则回退一页）
      const reloadPage = items.length === 1 && page > 1 ? page - 1 : page
      await load(reloadPage, action)
    } catch (e) {
      setError(e.message)
      // 403（权限被收紧）等场景刷新 admin 标记
      await load(page, action)
    } finally {
      setDeleting(null)
    }
  }

  const saveAdmins = async () => {
    setBusy(true); setError(''); setNote(''); setAdminsSaved(false)
    try {
      // 逗号/顿号/空白分隔 + 去重保序（与后端 _validate_audit_logs 归一一致）
      const list = [...new Set(
        adminsInput.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean))]
      await api.put('/api/settings', { audit_logs: { admin_usernames: list } })
      setAdminsSaved(true)
      setNote('管理员名单已保存（写回 config.yaml）')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const loadAdmins = async () => {
    try {
      const settings = await api.get('/api/settings')
      setAdminsInput((settings.audit_logs?.admin_usernames || []).join(', '))
    } catch { /* 列表加载失败不阻塞主列表 */ }
  }
  useEffect(() => { loadAdmins() }, [])

  if (loading && !items.length) {
    return (
      <div className="card">
        <h2>审计日志</h2>
        {error
          ? <div className="alert alert-error" onClick={() => load(page, action)}>{error}（点击重试）</div>
          : <p className="muted">加载中…</p>}
      </div>
    )
  }

  return (
    <div className="card">
      <h2>审计日志</h2>
      <p className="muted small">
        关键操作留痕（谁 / 什么时间 / 改了什么）：设置保存、仓库增删改、任务重试/停止/删除、
        插件安装卸载、备份执行、webhook 轮换、直接编辑 config.yaml 等。删除仅限管理员。
      </p>

      {error && <div className="alert alert-error" onClick={() => load(page, action)}>{error}（点击重试）</div>}
      {note && <div className="alert alert-ok"><Icon name="check" /> {note}</div>}

      <div className="form-row">
        <label className="muted small">操作类型</label>
        <select className="input" value={action} onChange={(e) => changeAction(e.target.value)}>
          <option value="">全部</option>
          {actions.map((a) => (
            <option key={a} value={a}>{ACTION_LABELS[a] || a}（{a}）</option>
          ))}
        </select>
        <button className="btn" onClick={() => load(page, action)} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </button>
        <span className="muted small">共 {total} 条，第 {page}/{totalPages} 页</span>
        <button className="btn" onClick={() => changePage(page - 1)} disabled={page <= 1 || loading}>上一页</button>
        <button className="btn" onClick={() => changePage(page + 1)} disabled={page >= totalPages || loading}>下一页</button>
      </div>

      {items.length === 0 ? (
        <p className="muted">暂无审计记录。</p>
      ) : (
        // issue #459 CI：审计日志表格 7 列（时间/操作者/操作类型/目标/变更摘要/
        // IP/操作）min-content 宽约 565px，手机（≤640px）与平板竖屏（641~860px）
        // 下直接溢出页面产生横向滚动（e2e responsive-mobile 设置页用例失败）。
        // 套用项目既有 .table-wrap 横向滚动容器（issue #28），表格在卡片内滚动、
        // 页面不再横向溢出；桌面宽视口行为不变。
        <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>时间 (UTC)</th>
              <th>操作者</th>
              <th>操作类型</th>
              <th>目标</th>
              <th>变更摘要</th>
              <th>IP</th>
              {admin && <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id}>
                <td className="nowrap">{row.created_at}</td>
                <td>{row.actor || '—'}</td>
                <td>{ACTION_LABELS[row.action] || row.action}</td>
                <td>
                  {row.target_type
                    ? <code>{row.target_type}{row.target_id != null ? `#${row.target_id}` : ''}</code>
                    : '—'}
                </td>
                <td className="muted small">{summaryText(row.detail) || '—'}</td>
                <td className="nowrap">{row.ip || '—'}</td>
                {admin && (
                  <td>
                    <button
                      className="btn btn-danger btn-sm"
                      disabled={deleting === row.id}
                      onClick={() => del(row)}
                    >
                      {deleting === row.id ? '删除中…' : '删除'}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}

      <hr className="muted" />
      <h3 className="muted">管理员名单</h3>
      <p className="muted small">
        SSO 用户名（逗号分隔）。名单为空 = 所有登录用户均可查看/删除审计日志；
        配置后仅名单内用户可访问（普通用户不可删除）。SSO 未启用时本机用户恒为管理员。
      </p>
      <div className="form-row">
        <input
          className="input grow"
          placeholder="如 zhangsan, lisi"
          value={adminsInput}
          onChange={(e) => { setAdminsInput(e.target.value); setAdminsSaved(false) }}
        />
        <button className="btn" onClick={saveAdmins} disabled={busy}>
          {busy ? '保存中…' : '保存管理员名单'}
        </button>
        {adminsSaved && <span className="saved-hint"><Icon name="check" /> 已保存</span>}
      </div>
    </div>
  )
}
