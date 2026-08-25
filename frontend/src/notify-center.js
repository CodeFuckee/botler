// 通知中心纯函数（issue #215）：
// - 需人工介入类通知（bot-failed / 平台告警）默认置顶高亮——task_failed
//   （任务失败需人工介入）与 alert_*（聚合告警/健康巡检告警）按
//   NOTIFICATION_CHANGED_EVENT 语义排序置顶；
// - 排序稳定、幂等，供通知中心页与测试复用。
//
// 通知已读操作后广播 NOTIFICATION_CHANGED_EVENT（window CustomEvent），
// App 导航栏未读徽标监听后触发一次通知轮询立即刷新计数（复用现有 10s
// 轮询，不额外增加请求频率）。

export const NEEDS_ATTENTION = 'task_failed'

/** 是否需人工介入类通知（置顶高亮判定）。 */
export function isNeedsAttention(type) {
  return type === NEEDS_ATTENTION ||
    (typeof type === 'string' && type.startsWith('alert_'))
}

/** 通知中心列表排序：需人工介入置顶（组内 id 降序），其余 id 降序。
 *  id 单调递增即时间先后（AUTOINCREMENT），无需解析 created_at。 */
export function sortNotificationCenter(notifications) {
  const list = Array.isArray(notifications) ? notifications : []
  return [...list].sort((a, b) => {
    const pa = isNeedsAttention(a?.type) ? 0 : 1
    const pb = isNeedsAttention(b?.type) ? 0 : 1
    if (pa !== pb) return pa - pb
    return (b?.id || 0) - (a?.id || 0)
  })
}

/** 通知已读操作完成后的广播事件名（App 徽标监听刷新）。 */
export const NOTIFICATION_CHANGED_EVENT = 'botler:notifications-changed'

/** 广播「通知已读状态变化」（标记单条/全部已读成功后调用）。 */
export function notifyNotificationsChanged() {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(NOTIFICATION_CHANGED_EVENT))
}
