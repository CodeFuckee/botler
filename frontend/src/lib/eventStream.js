// 全局数据变更事件流（issue #478）：单一 SSE 长连接订阅后端 /api/events，
// 以「事件通知 + 事件驱动刷新」替代各页面的固定间隔轮询，避免过多 HTTP
// 请求。
//
// 设计：
// - 模块级单例连接：多个组件/页面共享一个 EventSource，避免每页一个
//   长连接（后端事件只携带 type 等轻量通知，不携带数据——收到事件后由
//   订阅方按需调用对应数据接口刷新，断线重连后全量兜底刷新即可）
// - 断线自动重连（EventSource 原生）；每次连接建立（onopen，含首次连接
//   与断线重连）通知订阅者「全量刷新兜底」——断线期间可能错过的事件由
//   全量刷新补齐（事件不携带数据，无历史回放需求）
// - 无订阅者时关闭连接（组件卸载后不残留长连接）
// - 非浏览器环境（node 测试）降级为空连接，避免渲染抛错

let es = null
// 是否已建立过连接：首次连接由各组件挂载 effect 自行初始加载，不重复
// 广播 onOpen；断线重连（everConnected 已为 true）才广播全量刷新兜底
let everConnected = false
const listeners = new Set()
const openListeners = new Set()

function ensureConnected() {
  if (es || typeof EventSource === 'undefined') return
  es = new EventSource('/api/events')
  es.onopen = () => {
    if (!everConnected) {
      // 首次连接：组件已自行初始加载（挂载 effect），不重复刷新
      everConnected = true
      return
    }
    // 断线重连成功：通知订阅者全量刷新（兜底断线期间错过的事件）
    openListeners.forEach((fn) => {
      try { fn() } catch { /* 订阅方异常不影响其他订阅者 */ }
    })
  }
  es.onmessage = (msg) => {
    let ev = null
    try { ev = JSON.parse(msg.data) } catch { return /* 非法 data 容错 */ }
    if (!ev || typeof ev !== 'object') return
    listeners.forEach((fn) => {
      try { fn(ev) } catch { /* 订阅方异常不影响其他订阅者 */ }
    })
  }
  // 不设置 onerror：EventSource 断线自动重连，重连成功走 onopen；
  // 连接失败期间事件丢失由重连后的全量刷新兜底
}

function closeIfIdle() {
  if (listeners.size === 0 && es) {
    es.close()
    es = null
    everConnected = false
  }
}

// 订阅全局事件流。
//   onEvent(ev)：收到 {type, ...} 事件时回调（事件驱动刷新）
//   onOpen()：连接建立/重连成功时回调（全量刷新兜底）
// 返回退订函数。
export function subscribeGlobalEvents(onEvent, { onOpen } = {}) {
  listeners.add(onEvent)
  if (onOpen) openListeners.add(onOpen)
  const wasConnected = !!es
  ensureConnected()
  // 连接已由其他订阅者建立（本订阅者加入时不会收到 onopen）：立即触发
  // 一次 onOpen，等同「连接正常 + 全量刷新」语义（断线重连场景由
  // es.onopen 兜底；组件的初始加载由组件自身挂载 effect 负责，避免重复）
  if (onOpen && wasConnected) {
    queueMicrotask(() => {
      // 订阅期间（microtask 前）已退订则不触发，避免卸载后 setState
      if (listeners.has(onEvent)) {
        try { onOpen() } catch { /* 订阅方异常不影响其他订阅者 */ }
      }
    })
  }
  return () => {
    listeners.delete(onEvent)
    if (onOpen) openListeners.delete(onOpen)
    closeIfIdle()
  }
}

// 测试辅助：强制断开单例连接（不影响订阅关系，下次订阅自动重连）。
// 供 node 测试验证「重连后 onOpen 全量刷新」语义。
export function closeGlobalEventsForTest() {
  if (es) {
    es.close()
    es = null
  }
}
