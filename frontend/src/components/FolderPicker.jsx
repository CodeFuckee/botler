import { useEffect, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api } from '../api.js'

/**
 * 服务器目录选择对话框：浏览服务器文件系统，
 * 点击目录进入子级、返回上级、路径输入跳转，底部「选择此文件夹」确认。
 * 用于「本地文件夹方式添加仓库」时挑选服务器上的 git 仓库目录。
 * 打开时无初始路径则从服务端默认初始目录开始（browse.default_path，
 * 未配置时为服务器用户主目录 ~）。
 */
export default function FolderPicker({ open, initialPath, onSelect, onClose }) {
  const [currentPath, setCurrentPath] = useState('/')
  const [parent, setParent] = useState(null)
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [jump, setJump] = useState('')

  const load = async (path) => {
    setLoading(true)
    setError('')
    try {
      const res = await api.get(`/api/repos/browse?path=${encodeURIComponent(path)}`)
      setCurrentPath(res.path)
      setParent(res.parent)
      setEntries(res.subdirs)
      setJump(res.path)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  // 打开对话框时从当前表单路径开始浏览；无路径则交给后端默认初始目录
  useEffect(() => {
    if (open) load(initialPath || '')
  }, [open, initialPath])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const visible = entries.filter((d) => showHidden || !d.name.startsWith('.'))

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal folder-picker" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>选择服务器上的文件夹</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭弹窗"><Icon name="x" /></button>
        </div>

        <div className="folder-nav">
          <button className="btn" disabled={!parent} onClick={() => load(parent)} title="返回上级">
            <Icon name="arrowUp" /> 上级
          </button>
          <code className="folder-path" title={currentPath}>{currentPath}</code>
        </div>

        <div className="form-row">
          <input
            className="input grow"
            placeholder="输入路径后跳转（如 /home/user/projects）"
            value={jump}
            onChange={(e) => setJump(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') load(jump.trim() || '/') }}
          />
          <button className="btn" disabled={loading} onClick={() => load(jump.trim() || '/')}>
            跳转
          </button>
          <label className="folder-hidden-toggle">
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />
            显示隐藏
          </label>
        </div>

        {error && <div className="alert alert-error">{error}</div>}

        <div className="folder-list">
          {loading && (
            <div className="folder-empty loading-hint">
              <span className="spinner" aria-hidden="true" />
              <span>加载中…</span>
            </div>
          )}
          {!loading && visible.length === 0 && (
            <div className="folder-empty">
              <span className="empty-icon" aria-hidden="true"><Icon name="folder" /></span>
              <div className="muted">{showHidden ? '此文件夹没有子目录' : '此文件夹没有子目录（可勾选“显示隐藏”查看隐藏目录）'}</div>
            </div>
          )}
          {!loading && visible.map((d) => (
            <button
              key={d.path}
              className="folder-item"
              disabled={!d.readable}
              title={d.readable ? d.path : `${d.path}（无权限，无法进入）`}
              onClick={() => load(d.path)}
            >
              <span className="folder-icon"><Icon name="folder" /></span>
              <span className="folder-name">{d.name}</span>
              {d.is_git && <span className="badge badge-git">git</span>}
              {!d.readable && <span className="muted small">（无权限）</span>}
            </button>
          ))}
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={loading} onClick={() => onSelect(currentPath)}>
            选择此文件夹
          </button>
        </div>
      </div>
    </div>
  )
}
