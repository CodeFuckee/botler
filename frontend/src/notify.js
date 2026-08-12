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
  return typeof window !== 'undefined' && 'Notification' in window
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
// （reason: browser-unsupported / denied / error）。
export async function sendTestNotification() {
  if (!canNotify()) return { ok: false, reason: 'browser-unsupported' }
  if (Notification.permission === 'denied') return { ok: false, reason: 'denied' }
  if (Notification.permission === 'default') {
    if ((await Notification.requestPermission()) !== 'granted') {
      return { ok: false, reason: 'denied' }
    }
  }
  try {
    new Notification('Botler 测试通知', {
      body: '这是一条测试通知，网页通知功能正常 ✅',
      tag: 'botler-test', // 固定 tag：连续点击不重复堆积
    })
    return { ok: true }
  } catch {
    return { ok: false, reason: 'error' }
  }
}

// 创建轮询器：opts = { getEvents(after), getSettings(), onError?, show? }
// show 默认用 showNotification（测试可注入收集函数）。
// 返回 { poll, getCursor }；poll 一次 = 拉取 → 过滤 → 弹通知 → 推进游标。
export function createNotifyPoller(opts) {
  const { getEvents, getSettings, onError } = opts
  const show = opts.show || showNotification
  let cursor = 0
  let first = true
  async function poll() {
    try {
      const [data, settings] = await Promise.all([getEvents(cursor), getSettings()])
      const events = data.events || []
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
