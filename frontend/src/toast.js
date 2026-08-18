// 全局 toast 提示模块（issue #226）：request() 统一错误提示的展示层。
// 与 dialog.js 同架构：模块级队列 + ToastHost（App 根部）订阅渲染。
// toast 为轻量自动消失提示（默认 3.5s），区别于对话框（需用户交互），
// 用于「非 2xx 自动提示错误信息」等不打断页面操作的场景。
//
// 设计：
//   - 同一时刻可堆叠多条 toast（队列 FIFO 展示，最新在底部）；
//   - 自动消失由 setTimeout 驱动，duration=0 可关闭（测试/常驻场景）；
//   - 无宿主（单元测试只渲染页面组件时）不渲染也不阻塞，调用零成本。

export const TOAST_DURATION_MS = 3500

let listener = null // ToastHost 订阅的强制刷新回调
const toasts = [] // 当前可见 toast（队尾为最新）
let seq = 0

// ToastHost 挂载时订阅（返回取消订阅函数；卸载时清除，防泄漏）
export function subscribeToastHost(fn) {
  listener = fn
  return () => {
    if (listener === fn) listener = null
  }
}

// 按 id 关闭一条 toast（× 按钮 / 自动消失调用）
export function dismissToast(id) {
  const idx = toasts.findIndex((t) => t.id === id)
  if (idx < 0) return
  toasts.splice(idx, 1)
  listener?.()
}

// 当前全部 toast（ToastHost 渲染时读取；测试断言用）
export function currentToasts() {
  return toasts
}

// 清空全部 toast（测试清理 / 宿主卸载兜底）
export function clearToasts() {
  if (toasts.length === 0) return
  toasts.length = 0
  listener?.()
}

// 弹一条 toast：message 文案，opts = { type: 'error'|'info'|'success', duration }
// type 默认 error（统一错误提示主场景）；duration 毫秒后自动消失
// （0 = 不自动消失）。返回自增 id。浏览器 setTimeout 返回的 timer 在
// node 测试环境 unref，避免挂起的自动消失定时器阻塞测试进程退出。
export function showToast(message, opts = {}) {
  const type = opts.type || 'error'
  const duration = opts.duration === undefined ? TOAST_DURATION_MS : opts.duration
  const id = ++seq
  toasts.push({ id, message: String(message), type })
  listener?.()
  if (duration > 0) {
    const timer = setTimeout(() => dismissToast(id), duration)
    if (typeof timer.unref === 'function') timer.unref() // 仅 node 测试环境
  }
  return id
}
