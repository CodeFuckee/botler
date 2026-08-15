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
// issue #108：标签行增加「编辑标记」功能——编辑态加载项目标记池
// （GET /api/issues/{project_id}/labels，checkbox 多选、当前标记
// 预勾选、池外当前标记仍可取消勾选移除），保存时 diff 出 add/
// remove 一次 PUT /api/issues/{project_id}/{iid}/labels 提交（remove
// 只含当前实际存在的标记，规避 GitLab remove_labels 对不存在标记
// 返回 404）；成功后本地标记即时更新（displayLabels 覆盖）并通知
// 父组件刷新列表（onLabelsUpdated）；失败保留编辑态可重试。
//
// 交互约定：
// - 列表项本身不再直接跳转 GitLab，跳转统一走抽屉右上角
//   「在 GitLab 中打开」按钮（web_url 新窗口）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键。
import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'
import { confirmDialog } from '../dialog.js'
import Markdown from './Markdown.jsx'

// issue 状态 → 徽章映射（聚合只返回开放 issue，closed 为兜底映射）
export const ISSUE_STATE_META = {
  opened: { label: '开放', cls: 'status-running' },
  closed: { label: '已关闭', cls: 'status-interrupted' },
}

// Esc 键判定（纯函数导出，便于测试）
export function isEscapeKey(e) {
  return !!e && e.key === 'Escape'
}

// note 作者名：name 优先回退 username，全无显示「—」（issue #97，
// 与列表头像/作者展示的兜底逻辑一致）
export function noteAuthorName(note) {
  const a = note && typeof note === 'object' ? note.author : null
  if (!a || typeof a !== 'object') return '—'
  return a.name || a.username || '—'
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
                                      onLabelsUpdated }) {
  const [closing, setClosing] = useState(false) // 关闭请求进行中（按钮禁用）
  const [closed, setClosed] = useState(false)   // 本次会话关闭成功标记
  const [closeErr, setCloseErr] = useState('')  // 关闭失败的错误信息
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

  // Esc 关闭抽屉（SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (isEscapeKey(e)) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const i = issue || {}
  // 有效状态：本次点击关闭成功后本地立即标记 closed（后端已确认），
  // 状态徽章与按钮即时反映，无需等待下一轮轮询
  const effectiveState = closed ? 'closed' : i.state
  const stateMeta = ISSUE_STATE_META[effectiveState]
    || { label: effectiveState || '—', cls: '' }
  // 可关闭条件：开放状态 + 带 project_id（关闭接口按 project_id
  // 定位仓库，旧缓存数据缺失时隐藏按钮）
  const canClose = !closed && i.state === 'opened'
    && typeof i.project_id === 'number'
  // 作者显示：name 优先，缺失回退 username，全无显示「—」
  const author = i.author && typeof i.author === 'object'
    ? (i.author.name || i.author.username || '—')
    : '—'
  // 负责人列表：name 优先回退 username（与列表头像的兜底逻辑一致）
  const assigneeNames = (i.assignees || []).map(
    (a) => (a && typeof a === 'object' ? (a.name || a.username || '—') : '—'))

  // 评论/活动详情数据来源（issue #97）：project_id 与 iid 均为数字时
  // 拉取 detail；旧缓存数据缺 project_id 时不发请求（无法定位仓库）
  const hasDetail = typeof i.project_id === 'number' && typeof i.iid === 'number'
  const loadNotes = useCallback(async () => {
    if (typeof i.project_id !== 'number' || typeof i.iid !== 'number') return
    setNotes(null)
    setDetailErr('')
    try {
      const d = await api.get(`/api/issues/${i.project_id}/${i.iid}/detail`)
      setNotes(Array.isArray(d && d.notes) ? d.notes : [])
    } catch (e) {
      setDetailErr(e.message || '加载失败')
    }
  }, [i.project_id, i.iid])

  // 抽屉打开/切换 issue 时拉取详情（依赖 project_id/iid，切换即重拉）
  useEffect(() => {
    loadNotes()
  }, [loadNotes])

  // 分区：system=true 为系统活动事件，false 为用户评论（issue #97）
  const comments = (notes || []).filter((n) => n && !n.system)
  const activities = (notes || []).filter((n) => n && n.system)

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

  // ---- issue #108：标记编辑 ----

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

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer issue-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong className="issue-drawer-title">
            #{i.iid} {i.title || '—'}
          </strong>
          <span className="issue-drawer-actions">
            {canClose && (
              <button className="btn btn-danger" onClick={handleCloseIssue}
                      disabled={closing} title="关闭 GitLab 中的 issue">
                {closing ? '关闭中…' : '关闭 issue'}
              </button>
            )}
            <a className="btn" href={i.web_url} target="_blank" rel="noreferrer"
               title="在 GitLab 中打开 issue">在 GitLab 中打开</a>
            <button className="btn modal-close" onClick={onClose} title="关闭"
                    aria-label="关闭右边栏">×</button>
          </span>
        </div>
        {closeErr && <div className="issue-drawer-error" role="alert">{closeErr}</div>}
        <table className="table kv">
          <tbody>
            <tr><th>仓库</th><td>{repoName || '—'}</td></tr>
            <tr><th>状态</th>
              <td><span className={'badge ' + stateMeta.cls}>{stateMeta.label}</span></td></tr>
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
            <tr><th>负责人</th><td>{assigneeNames.length > 0 ? assigneeNames.join('、') : '—'}</td></tr>
            <tr><th>评论</th>
              <td>{typeof i.user_notes_count === 'number' ? i.user_notes_count : '—'}</td></tr>
          </tbody>
        </table>
        <div className="issue-drawer-desc">
          <h3>描述</h3>
          {i.description && String(i.description).trim() ? (
            <Markdown content={i.description} />
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
          <div className="issue-notes-block">
            <h3>评论</h3>
            {renderNotesBody(comments, '暂无评论', (n) => (
              <li key={n.id} className="comment-item">
                <div className="comment-head">
                  <NoteAvatar note={n} />
                  <span className="comment-author">{noteAuthorName(n)}</span>
                  <span className="comment-time">{fmtTime(n.created_at)}</span>
                </div>
                <div className="comment-body">
                  {n.body && String(n.body).trim() ? (
                    <Markdown content={n.body} />
                  ) : (
                    <p className="muted">（无内容）</p>
                  )}
                </div>
              </li>
            ))}
          </div>
          <div className="issue-notes-block">
            <h3>活动</h3>
            {renderNotesBody(activities, '暂无活动', (n) => (
              <li key={n.id} className="activity-item">
                <span className="activity-dot" title="系统活动">•</span>
                <span className="activity-text">{n.body || '（无内容）'}</span>
                {n.created_at && (
                  <span className="activity-time">{fmtTime(n.created_at)}</span>
                )}
              </li>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
