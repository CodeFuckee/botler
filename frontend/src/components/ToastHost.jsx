// 全局 toast 宿主（issue #226）：渲染 toast.js 队列中的全部 toast。
// 挂在 App 根部（与 DialogHost 并列），右上角固定浮层堆叠展示；
// 自动消失（TOAST_DURATION_MS）或点 × 手动关闭。用于 request()
// 统一错误提示等轻量通知，不打断页面操作。
import { useEffect, useState } from 'react'
import { Icon } from './Icon.jsx'
import { subscribeToastHost, currentToasts, dismissToast } from '../toast.js'

export default function ToastHost() {
  const [items, setItems] = useState([])

  // 订阅队列变化（新 toast 入队 / 关闭出队 / 自动消失）触发重渲染
  useEffect(() => {
    const sync = () => setItems([...currentToasts()])
    sync()
    return subscribeToastHost(sync)
  }, [])

  if (items.length === 0) return null
  return (
    <div className="toast-host" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={'toast toast-' + t.type}>
          <span className="toast-message">{t.message}</span>
          <button
            className="btn toast-close"
            onClick={() => dismissToast(t.id)}
            title="关闭提示"
            aria-label="关闭提示"
          >
            <Icon name="x" />
          </button>
        </div>
      ))}
    </div>
  )
}
