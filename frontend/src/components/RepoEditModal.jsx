import { useState } from 'react'
import { api } from '../api.js'

/**
 * 仓库设置编辑弹窗（issue #51）：编辑显示名称、启用状态与调度优先级。
 *
 * 优先级为整数 1~999，数字越小越优先——多个仓库同时有排队任务时，
 * 调度器优先派发优先级高（数字小）的仓库；相同优先级按任务提交时间
 * 排序。URL / 本地路径不在编辑范围（涉及 webhook 重注册与 project_id
 * 变更，风险较高，按用户确认的 Q1 方案排除）。
 */
export default function RepoEditModal({ repo, onClose, onSaved }) {
  const [name, setName] = useState(repo.name || '')
  const [enabled, setEnabled] = useState(!!repo.enabled)
  const [priority, setPriority] = useState(String(repo.priority ?? 100))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setError('')
    const trimmedName = name.trim()
    if (!trimmedName) {
      setError('名称不能为空')
      return
    }
    const num = Number(priority)
    if (!priority.trim() || !Number.isInteger(num) || num < 1 || num > 999) {
      setError('优先级需为 1~999 之间的整数')
      return
    }
    setBusy(true)
    try {
      await api.put(`/api/repos/${repo.id}`, {
        name: trimmedName,
        enabled,
        priority: num,
      })
      onSaved()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal repo-edit" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>编辑仓库「{repo.name}」</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭弹窗">×</button>
        </div>

        <label className="edit-field">
          显示名称
          <input
            className="input"
            placeholder="显示名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>

        <label className="edit-field">
          优先级
          <input
            className="input"
            placeholder="1~999"
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
          />
        </label>
        <div className="muted small">
          1~999 之间的整数，数字越小越优先；相同优先级按任务提交时间排序
        </div>

        <label className="edit-checkbox">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          启用该仓库（停用后不再接收新任务）
        </label>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="modal-footer">
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
