// 概览页「添加 Issue」弹窗（issue #92）：仓库卡片右上角按钮打开，
// 表单包含标题（必填）/ 描述（选填）/ 分配人（项目成员下拉，必填，
// 默认选中 agent）/ 标签（仓库已有标签多选，必填，不可新建）。
//
// 交互约定（与 IssueDrawer / RepoEditModal 一致）：
// - 打开时加载 /api/issues/form-meta/{repo_id}（项目成员 + 项目标签），
//   成员含 agent 时分配人默认选中 agent（用户确认的默认值）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键；
// - 提交成功回调 onCreated（关闭弹窗并立即刷新 issue 列表）。
import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function AddIssueModal({ repo, onClose, onCreated }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [members, setMembers] = useState([])
  const [labels, setLabels] = useState([])
  const [assigneeId, setAssigneeId] = useState('')
  const [selectedLabels, setSelectedLabels] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Esc 关闭弹窗（SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (e && e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 打开时加载成员与标签；成员含 agent 时默认选中（用户确认：必填、默认 agent）
  useEffect(() => {
    let alive = true
    api.get(`/api/issues/form-meta/${repo.repo_id}`).then((d) => {
      if (!alive) return
      setMembers(d.members || [])
      setLabels(d.labels || [])
      const agent = (d.members || []).find((m) => m.username === 'agent')
      if (agent && agent.id != null) setAssigneeId(String(agent.id))
      setLoading(false)
    }).catch((e) => {
      if (!alive) return
      setLoadError(e.message)
      setLoading(false)
    })
    return () => { alive = false }
  }, [repo.repo_id])

  const toggleLabel = (name) => {
    setSelectedLabels((prev) => (prev.includes(name)
      ? prev.filter((n) => n !== name)
      : [...prev, name]))
  }

  const submit = async () => {
    setError('')
    const trimmedTitle = title.trim()
    if (!trimmedTitle) {
      setError('标题不能为空')
      return
    }
    if (!assigneeId) {
      setError('请选择分配人')
      return
    }
    if (selectedLabels.length === 0) {
      setError('请至少选择一个标签')
      return
    }
    setBusy(true)
    try {
      await api.post('/api/issues', {
        repo_id: repo.repo_id,
        title: trimmedTitle,
        description: description.trim() || null,
        assignee_id: Number(assigneeId),
        labels: selectedLabels,
      })
      onCreated()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal add-issue" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>添加 Issue「{repo.repo_name}」</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭">×</button>
        </div>

        {loading ? (
          <p className="muted">加载仓库成员与标签中…</p>
        ) : loadError ? (
          <div className="alert alert-error">{loadError}</div>
        ) : (
          <>
            <label className="edit-field">
              Issue 标题
              <input
                className="input add-issue-title"
                placeholder="必填"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>

            <label className="edit-field">
              描述
              <textarea
                className="input add-issue-desc"
                rows={6}
                placeholder="选填"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>

            <label className="edit-field">
              分配人
              <select
                className="input add-issue-assignee"
                value={assigneeId}
                onChange={(e) => setAssigneeId(e.target.value)}
              >
                {assigneeId === '' && <option value="">请选择…</option>}
                {members.map((m) => (
                  <option key={m.id} value={String(m.id)}>
                    {m.name || m.username || m.id}
                  </option>
                ))}
              </select>
            </label>
            <div className="muted small">必填 · 默认选择 agent</div>

            <div className="edit-field">
              标签
              {labels.length === 0 ? (
                <p className="muted">该仓库暂无标签</p>
              ) : (
                <div className="label-picker">
                  {labels.map((l) => (
                    <label key={l.name} className="label-choice">
                      <input
                        type="checkbox"
                        checked={selectedLabels.includes(l.name)}
                        onChange={() => toggleLabel(l.name)}
                      />
                      <span
                        className="label-pill"
                        style={l.color
                          ? { background: `#${l.color}`, color: `#${l.text_color}` }
                          : undefined}
                      >
                        {l.name}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
            <div className="muted small">必填 · 仅可选仓库已有标签</div>

            {error && <div className="alert alert-error">{error}</div>}

            <div className="modal-footer">
              <button className="btn" onClick={onClose}>取消</button>
              <button className="btn btn-primary add-issue-submit"
                      disabled={busy} onClick={submit}>
                {busy ? '创建中…' : '创建 Issue'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
