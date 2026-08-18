// Web 终端页面（issue #183）：浏览器内多标签终端，无需再打开系统终端
//
// - 多标签：每个标签一个独立 terminado PTY 会话（name 隔离），可增删切换；
// - 快捷键：Alt+T 新建标签 / Alt+W 关闭当前标签（避开浏览器保留快捷键）；
// - 复制粘贴：xterm.js 原生支持 Ctrl+Shift+C 复制选区 / Ctrl+Shift+V 粘贴；
// - 认证：POST /api/terminal/token 获取短时效 token（SSO 会话保护），
//   WebSocket 经 /api/terminal/ws/<name> 反向代理到独立终端服务进程。
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Icon } from '../components/Icon.jsx'
import TerminalView from '../terminal/TerminalView.jsx'
import { handleTerminalKey, MAX_TABS, nextTabName, statusLabel } from '../terminal/tabs.js'

export default function Terminal() {
  const [tabs, setTabs] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const seqRef = useRef(0)

  // 获取短时效终端 token（仅获取一次，后续标签复用；过期由服务端拒绝）
  const ensureToken = useCallback(async () => {
    if (token) return token
    setBusy(true)
    try {
      const data = await api.post('/api/terminal/token')
      setToken(data.token)
      return data.token
    } catch (e) {
      setError(`无法获取终端访问凭证：${e.message}`)
      return null
    } finally {
      setBusy(false)
    }
  }, [token])

  // 新建标签页：先拿 token，再追加标签并激活
  const addTab = useCallback(async () => {
    const tok = await ensureToken()
    if (!tok) return
    seqRef.current += 1
    const id = seqRef.current
    setTabs((prev) => {
      if (prev.length >= MAX_TABS) return prev
      return [...prev, { id, name: nextTabName(prev), status: 'connecting' }]
    })
    setActiveId(id)
  }, [ensureToken])

  // 关闭标签页；关闭当前标签后由下方 effect 自动选中相邻标签
  const closeTab = useCallback((id) => {
    setTabs((prev) => prev.filter((t) => t.id !== id))
    setActiveId((prev) => (prev === id ? null : prev))
  }, [])

  // 终端连接状态回写（TerminalView 经 onStatus(id, status) 回调）
  const handleStatus = useCallback((id, status) => {
    setTabs((prev) => prev.map((t) => (t.id === id ? { ...t, status } : t)))
  }, [])

  // 关闭当前标签后若仍有标签，自动选中最后一个
  useEffect(() => {
    if (activeId == null && tabs.length > 0) {
      setActiveId(tabs[tabs.length - 1].id)
    }
  }, [activeId, tabs])

  // 页面级快捷键（Alt+T / Alt+W）
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onKey = (e) => {
      const action = handleTerminalKey(e, tabs.length > 0)
      if (action === 'new') {
        e.preventDefault()
        addTab()
      } else if (action === 'close') {
        e.preventDefault()
        if (activeId != null) closeTab(activeId)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [addTab, closeTab, activeId, tabs.length])

  return (
    <div className="page terminal-page">
      <div className="page-head">
        <h1><Icon name="terminal" /> Web 终端</h1>
        <p className="muted">浏览器内直接使用终端，无需再打开系统终端；多标签 + 快捷键</p>
      </div>

      <div className="terminal-toolbar">
        <div className="terminal-tabs" role="tablist" aria-label="终端标签页">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={tab.id === activeId}
              className={'terminal-tab' + (tab.id === activeId ? ' active' : '')}
              onClick={() => setActiveId(tab.id)}
            >
              <span className="terminal-tab-name">{tab.name}</span>
              <span className={'terminal-status terminal-status-' + tab.status}>
                {statusLabel(tab.status)}
              </span>
              <span
                className="terminal-tab-close"
                role="button"
                aria-label={`关闭 ${tab.name}`}
                onClick={(e) => { e.stopPropagation(); closeTab(tab.id) }}
              >
                <Icon name="x" />
              </span>
            </button>
          ))}
          <button
            type="button"
            className="terminal-add"
            onClick={addTab}
            disabled={tabs.length >= MAX_TABS || busy}
            aria-label="新建终端标签页"
          >
            <Icon name="plus" /> 新建
          </button>
        </div>
      </div>

      <div className="terminal-hint muted">
        快捷键：<kbd>Alt+T</kbd> 新建标签 · <kbd>Alt+W</kbd> 关闭当前标签 ·
        <kbd>Ctrl+Shift+C</kbd> 复制 · <kbd>Ctrl+Shift+V</kbd> 粘贴
      </div>

      {error && <div className="terminal-error"><Icon name="warning" /> {error}</div>}

      <div className="terminal-body">
        {tabs.length === 0 ? (
          <div className="terminal-empty muted">点击「新建」打开第一个终端</div>
        ) : (
          tabs.map((tab) => (
            <div
              key={tab.id}
              className={'terminal-pane' + (tab.id === activeId ? ' active' : '')}
              style={tab.id === activeId ? undefined : { display: 'none' }}
            >
              <TerminalView tab={tab} token={token} onStatus={handleStatus} />
            </div>
          ))
        )}
      </div>
    </div>
  )
}
