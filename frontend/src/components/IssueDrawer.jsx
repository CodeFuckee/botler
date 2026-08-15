// 概览页 issue 详情右边栏（issue #85）：点击开放 issue 列表项打开，
// 展示 issue 具体信息（状态 / 作者 / 时间 / 标签 / 里程碑 / 负责人 /
// 评论数）与正文（Markdown 渲染，复用 issue #27 的 Markdown 组件）。
//
// issue #94：「关闭 issue」按钮——二次确认后调用后端关闭 GitLab
// issue；成功后按钮消失、状态徽章变「已关闭」并通知父组件刷新
// 开放 issue 列表（该 issue 从列表消失）。
//
// 交互约定：
// - 列表项本身不再直接跳转 GitLab，跳转统一走抽屉右上角
//   「在 GitLab 中打开」按钮（web_url 新窗口）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键。
import { useEffect, useState } from 'react'
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

export default function IssueDrawer({ issue, repoName, onClose, onIssueClosed }) {
  const [closing, setClosing] = useState(false) // 关闭请求进行中（按钮禁用）
  const [closed, setClosed] = useState(false)   // 本次会话关闭成功标记
  const [closeErr, setCloseErr] = useState('')  // 关闭失败的错误信息

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
      </div>
    </div>
  )
}
