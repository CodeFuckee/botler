import { useEffect, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api } from '../api.js'

/**
 * 仓库设置编辑弹窗（issue #51）：编辑显示名称、启用状态与调度优先级。
 *
 * 优先级为整数 1~999，数字越小越优先——多个仓库同时有排队任务时，
 * 调度器优先派发优先级高（数字小）的仓库；相同优先级按任务提交时间
 * 排序。URL / 本地路径不在编辑范围（涉及 webhook 重注册与 project_id
 * 变更，风险较高，按用户确认的 Q1 方案排除）。
 *
 * issue #153：新增「仓库用户」展示——从该仓库 remote URL 读取的用户名
 * （如 https://user:token@host/... 的 user），提供「重新读取 remote URL」
 * 按钮调 POST /api/repos/{id}/remote-user 重新读取并落库；该用户作为
 * 灵感组件「添加 Issue」时的默认分配人（后端在提交时按用户名解析）。
 * 仓库用户只读展示（来源是 remote url，不做手填，防止填错账号）。
 *
 * issue #237：新增「任务参数」区——最大重试次数 / 执行引擎两个可选字段，
 * 保存时提交 null 清空；提示文案展示全局默认值（来自 /api/settings）。
 */
export default function RepoEditModal({ repo, onClose, onSaved }) {
  const [name, setName] = useState(repo.name || '')
  const [enabled, setEnabled] = useState(!!repo.enabled)
  const [priority, setPriority] = useState(String(repo.priority ?? 100))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // issue #153：仓库用户（remote url 用户名）与「重新读取 remote」状态
  const [remoteUsername, setRemoteUsername] = useState(repo.remote_username || '')
  const [readingRemote, setReadingRemote] = useState(false)
  // issue #237：仓库级任务参数覆盖——空串 = 继承全局（保存提交 null 清空）
  const [maxRetries, setMaxRetries] = useState(
    repo.max_retries != null ? String(repo.max_retries) : '')
  const [engine, setEngine] = useState(repo.engine || '')
  // 全局默认值（提示「留空继承全局」时展示，来自设置页 worker 段）
  const [globalWorker, setGlobalWorker] = useState(null)

  // issue #237：读取全局 worker 配置用于提示（失败静默，仅提示缺失）
  useEffect(() => {
    let alive = true
    api.get('/api/settings').then((data) => {
      if (alive && data?.worker) setGlobalWorker(data.worker)
    }).catch(() => {})
    return () => { alive = false }
  }, [])

  // issue #153：调后端重新读取仓库 remote url 的用户名并落库
  const readRemoteUser = async () => {
    if (readingRemote) return
    setReadingRemote(true)
    setError('')
    try {
      const res = await api.post(`/api/repos/${repo.id}/remote-user`)
      setRemoteUsername(res.remote_username || '')
    } catch (e) {
      setError(e.message)
    } finally {
      setReadingRemote(false)
    }
  }

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
    // issue #237：任务参数校验——留空继承全局；非空时校验取值范围与引擎白名单
    let retries = null
    if (maxRetries.trim() !== '') {
      const r = Number(maxRetries)
      if (!Number.isInteger(r) || r < 0 || r > 20) {
        setError('最大重试次数需为 0~20 之间的整数')
        return
      }
      retries = r
    }
    let eng = null
    if (engine.trim() !== '') {
      eng = engine.trim().toLowerCase()
      if (!['claude', 'hermes', 'dsh'].includes(eng)) {
        setError('执行引擎需为 claude / hermes / dsh 之一')
        return
      }
    }
    setBusy(true)
    try {
      await api.put(`/api/repos/${repo.id}`, {
        name: trimmedName,
        enabled,
        priority: num,
        max_retries: retries,
        engine: eng,
      })
      onSaved()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const globalRetries = globalWorker?.max_retries
  const globalEngine = globalWorker?.engine || 'claude'

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal repo-edit" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>编辑仓库「{repo.name}」</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭弹窗"><Icon name="x" /></button>
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

        <label className="edit-field">
          仓库用户
          <div className="remote-user-row">
            <code className={remoteUsername ? '' : 'remote-user-empty'}>
              {remoteUsername || '未读取到（remote URL 无用户名）'}
            </code>
            <button type="button" className="btn" disabled={readingRemote} onClick={readRemoteUser}>
              {readingRemote ? '读取中…' : '重新读取 remote URL'}
            </button>
          </div>
          <span className="muted small">
            从该仓库 remote URL 的用户名（如 https://user:token@host/... 的 user）读取；
            灵感组件「添加 Issue」时将该用户设为默认分配人
          </span>
        </label>

        {/* issue #237：仓库级任务参数覆盖——留空 = 继承全局（设置页「任务调度」卡片） */}
        <div className="edit-field-group">
          <div className="muted small edit-group-title">任务参数（留空 = 继承全局）</div>

          <label className="edit-field">
            最大重试次数
            <input
              className="input"
              placeholder={globalRetries != null ? `留空继承全局（${globalRetries} 次）` : '留空继承全局'}
              value={maxRetries}
              onChange={(e) => setMaxRetries(e.target.value)}
            />
          </label>
          <div className="muted small">
            0~20 之间的整数（0 = 不重试）；留空继承全局（当前 {globalRetries ?? '—'} 次）
          </div>

          <label className="edit-field">
            执行引擎
            <input
              className="input"
              placeholder={globalEngine ? `留空继承全局（${globalEngine}）` : '留空继承全局'}
              value={engine}
              onChange={(e) => setEngine(e.target.value)}
            />
          </label>
          <div className="muted small">
            claude / hermes / dsh 之一；留空继承全局（当前 {globalEngine || '—'}）
          </div>
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
