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
// 交互约定：
// - 列表项本身不再直接跳转 GitLab，跳转统一走抽屉右上角
//   「在 GitLab 中打开」按钮（web_url 新窗口）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键。
import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'
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

export default function IssueDrawer({ issue, repoName, onClose, onIssueClosed }) {
  const [closing, setClosing] = useState(false) // 关闭请求进行中（按钮禁用）
  const [closed, setClosed] = useState(false)   // 本次会话关闭成功标记
  const [closeErr, setCloseErr] = useState('')  // 关闭失败的错误信息
  // issue #97：评论与活动（notes 为 null 表示加载中；detailErr
  // 非空表示加载失败，两个区块共用错误横幅 + 重试按钮）
  const [notes, setNotes] = useState(null)
  const [detailErr, setDetailErr] = useState('')

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
  // SSR 测试环境无 window 时默认确认通过（测试用 mock 控制取消路径）
  async function handleCloseIssue() {
    const confirmText = '确定要关闭该 issue 吗？关闭后可在 GitLab 中重新打开。'
    const confirmed = typeof window !== 'undefined'
      ? window.confirm(confirmText) : true
    if (!confirmed) return
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
            <button className="btn modal-close" onClick={onClose} title="关闭">×</button>
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
                {(i.labels || []).length > 0 ? (
                  i.labels.map((l) => (
                    <span key={l.name} className="label-pill"
                          style={l.color
                            ? { background: `#${l.color}`, color: `#${l.text_color}` }
                            : undefined}
                          title={`标签 ${l.name}`}>{l.name}</span>
                  ))
                ) : '—'}
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
