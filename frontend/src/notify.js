// 网页通知模块（issue #21）：轮询后端通知事件，用浏览器 Notification
// API 在用户电脑上弹系统通知。时机与设置页「通知」卡片开关一一对应：
//   task_failed   → task_needs_interaction（任务需要交互）
//   task_succeeded → issue_completed（issue 完成）
//   queue_empty    → queue_empty（issue 列表为空）
//   queue_no_work  → queue_no_work（无 issue 可处理）
// 过滤在前端做：改设置页开关立即生效，无需重启后端。
//
// 游标语义：首次拉取只记录游标不弹（避免历史事件轰炸）；之后每次轮询
// 把新事件按开关过滤后弹通知，游标推进到 latest_id（不丢事件）。

export const POLL_INTERVAL_MS = 10000

// 事件类型 → 设置页开关键
export const NOTIFY_TYPE_MAP = {
  task_succeeded: 'issue_completed',
  task_failed: 'task_needs_interaction',
  queue_empty: 'queue_empty',
  queue_no_work: 'queue_no_work',
  // 聚合告警（issue #229）：无对应设置开关键（缺省视为开启），弹窗与否
  // 由「网页通知」总开关控制——告警生成开关在「聚合告警」卡片独立配置
  alert_failure_rate: 'alert_failure_rate',
  alert_queue_backlog: 'alert_queue_backlog',
  alert_token_invalid: 'alert_token_invalid',
  alert_disk_low: 'alert_disk_low',
  // 仓库健康巡检告警（issue #265）：仓库健康状态异常聚合通知，缺省视为
  // 开启（生成开关在设置页「仓库健康巡检」卡片与 alerts 节流共用）
  alert_repo_health: 'alert_repo_health',
}

// 按设置过滤可弹通知的事件（纯函数，可测）
export function filterNotifyEvents(events, settings) {
  const notify = settings?.notifications
  if (!notify || !notify.enabled) return []
  return (events || []).filter((e) => {
    const key = NOTIFY_TYPE_MAP[e.type]
    if (!key) return false // 未知类型不弹
    return notify[key] !== false // 缺省视为开启
  })
}

export function canNotify() {
  return typeof window !== 'undefined' && 'Notification' in window &&
    window.isSecureContext !== false // 非安全上下文（http/证书不受信任）下 Notification 受限不可用
}

// 通知不可用的原因（纯函数，可测）：
//   browser-unsupported 浏览器不支持 Notification
//   insecure-context     页面非安全上下文（http/证书不受信任）——此时 permission 恒为
//                        'denied' 且 requestPermission() 永不弹框，只能换 https 访问
//   denied               用户已拒绝授权（可在地址栏改回）
//   可通知时返回 null
export function notifyFailureReason() {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'browser-unsupported'
  if (window.isSecureContext === false) return 'insecure-context'
  if (Notification.permission === 'denied') return 'denied'
  return null
}

// 弹一条系统通知；未授权/浏览器不支持时静默跳过（不影响游标推进）
export function showNotification(event) {
  if (!canNotify() || Notification.permission !== 'granted') return false
  try {
    new Notification(event.title || 'Botler', {
      body: event.body || '',
      tag: `botler-${event.type}-${event.id}`, // 同事件不重复堆积
    })
    return true
  } catch {
    return false
  }
}

// 主动发送一条测试通知（设置页「弹出测试通知」按钮，issue #21 增量）：
// 绕过事件过滤——用户主动点击就是要验证通知能力本身。权限未决（default）
// 时先请求授权；返回 {ok, reason} 供 UI 提示结果
// （reason: browser-unsupported / insecure-context / denied / error）。
// tag 每次唯一（issue #21 第四轮）：浏览器对相同 tag 的通知做「替换」而非
// 「新弹」，固定 tag 会导致连续点击只有第一次真正弹出。
let testNotifySeq = 0
export async function sendTestNotification() {
  const reason = notifyFailureReason()
  if (reason) return { ok: false, reason }
  if (Notification.permission === 'default') {
    if ((await Notification.requestPermission()) !== 'granted') {
      return { ok: false, reason: 'denied' }
    }
  }
  try {
    new Notification('Botler 测试通知', {
      body: '这是一条测试通知，网页通知功能正常',
      tag: `botler-test-${++testNotifySeq}`, // 唯一 tag：每次点击独立弹出
    })
    return { ok: true }
  } catch {
    return { ok: false, reason: 'error' }
  }
}

// 创建轮询器：opts = { getEvents(after), getSettings(), onError?, onEvents?, onData? }
// show 默认用 showNotification（测试可注入收集函数）。
// 返回 { poll, getCursor }；poll 一次 = 拉取 → 过滤 → 弹通知 → 推进游标。
// onData：每次轮询成功都回调（含无新事件），供导航栏未读徽标读取
// data.unread_count（issue #215）——已读操作后 App 触发一次 poll 即可
// 立即刷新计数。
export function createNotifyPoller(opts) {
  const { getEvents, getSettings, onError, onEvents, onData } = opts
  const show = opts.show || showNotification
  let cursor = 0
  let first = true
  async function poll() {
    try {
      const [data, settings] = await Promise.all([getEvents(cursor), getSettings()])
      const events = data.events || []
      // 全量数据回调（issue #215）：每次轮询都触发，导航栏未读徽标数据源
      onData?.(data)
      // 新事件回调（issue #257）：任务状态变化等新事件到达时触发一次
      // 附加刷新（如导航栏水位徽章即时更新）——仅在确实有新事件时回调，
      // 不增加轮询请求频率
      if (data.latest_id > cursor) {
        onEvents?.(events, data)
      }
      if (!first) {
        for (const ev of filterNotifyEvents(events, settings)) {
          show(ev)
        }
      }
      first = false
      if (data.latest_id > cursor) cursor = data.latest_id
    } catch (e) {
      onError?.(e)
    }
  }
  return { poll, getCursor: () => cursor }
}
