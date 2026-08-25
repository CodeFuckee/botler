// 通知中心页（issue #215）：已读/未读状态、全部已读、需人工介入置顶高亮。
// 数据来自 GET /api/notifications（最新优先 + unread_count），15s 轮询
// 刷新（与页面常规轮询周期一致）；标记单条/全部已读后本地即时更新并广播
// NOTIFICATION_CHANGED_EVENT，导航栏未读徽标监听后触发一次通知轮询立即
// 刷新计数（复用现有 10s 通知轮询，不额外增加请求频率）。
// 需人工介入类通知（task_failed / alert_*）默认置顶并高亮（.notify-attn），
// 提示 bot 失败等异常需人工介入，避免被大量成功通知淹没。
import { useCallback, useState } from 'react'
import { api, fmtTime } from '../api.js'
import { Icon } from '../components/Icon.jsx'
import { useI18n } from '../i18n.jsx'
import { usePolling } from '../hooks/usePolling.js'
import {
  isNeedsAttention, notifyNotificationsChanged, sortNotificationCenter,
} from '../notify-center.js'

// 通知中心列表轮询周期（issue #215）：与页面常规轮询一致
export const NOTIFICATIONS_POLL_MS = 15000

export default function Notifications() {
  const { t } = useI18n()
  const [items, setItems] = useState([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await api.get('/api/notifications', { silent: true })
      setItems(sortNotificationCenter(data.notifications || []))
      setUnreadCount(data.unread_count || 0)
    } catch {
      // 旧后端无该端点：保留现有数据，页面展示空态不报错
    } finally {
      setLoading(false)
    }
  }, [])
  usePolling(load, NOTIFICATIONS_POLL_MS, { enabled: true })

  const markRead = async (id) => {
    if (busy) return
    setBusy(true)
    try {
      await api.post(`/api/notifications/${id}/read`)
      setItems((prev) => prev.map((it) => (it.id === id ? { ...it, read: true } : it)))
      setUnreadCount((n) => Math.max(0, n - 1))
      notifyNotificationsChanged()
    } catch {
      // 标记失败静默（下次轮询自愈）
    } finally {
      setBusy(false)
    }
  }

  const markAllRead = async () => {
    if (busy || unreadCount === 0) return
    setBusy(true)
    try {
      await api.post('/api/notifications/read-all')
      setItems((prev) => prev.map((it) => ({ ...it, read: true })))
      setUnreadCount(0)
      notifyNotificationsChanged()
    } catch {
      // 全部已读失败静默（下次轮询自愈）
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page notifications-page">
      <div className="page-head">
        <h1 className="page-title">{t('notifyCenter.title')}</h1>
        <div className="page-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={markAllRead}
            disabled={busy || unreadCount === 0}
            title={t('notifyCenter.markAllReadTitle')}
          >
            <Icon name="check" aria-hidden="true" />
            {t('notifyCenter.markAllRead')}
          </button>
        </div>
      </div>
      {loading ? (
        <div className="page-loading" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <p className="muted">{t('common.loading')}</p>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-state">
          <span className="empty-icon" aria-hidden="true"><Icon name="bell" /></span>
          <p>{t('notifyCenter.empty')}</p>
        </div>
      ) : (
        <ul className="notify-list">
          {items.map((it) => {
            const attn = isNeedsAttention(it.type)
            const cls = 'notify-item' +
              (it.read ? ' read' : ' unread') +
              (attn ? ' notify-attn' : '')
            return (
              <li key={it.id} className={cls}>
                <button
                  type="button"
                  className="notify-item-main"
                  onClick={() => !it.read && markRead(it.id)}
                  disabled={it.read}
                  title={it.read ? t('notifyCenter.readTitle') : t('notifyCenter.markReadTitle')}
                >
                  <span className="notify-dot" aria-hidden="true" />
                  <span className="notify-body">
                    <span className="notify-title">
                      {attn && <Icon name="triangleAlert" aria-hidden="true" />}
                      {it.title}
                    </span>
                    {it.body && <span className="notify-text">{it.body}</span>}
                    <span className="notify-meta">
                      {it.repo_name && <span className="notify-repo">{it.repo_name}</span>}
                      {it.type && <span className="notify-type">{it.type}</span>}
                      <span className="notify-time">{fmtTime(it.created_at)}</span>
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
