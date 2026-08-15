// 自定义对话框模块（issue #105）：替代浏览器原生 alert/confirm 弹窗。
// 用户反馈「不要使用 alert 来弹出通知，自定义一个对话框」——对话框在
// 页面内由 DialogHost 组件渲染，风格与现有 Modal 一致，不再弹浏览器原生框。
//
// 架构：模块级队列 + 宿主订阅。confirmDialog / alertDialog 返回 Promise
// （原生 confirm 是同步阻塞的，自定义 DOM 对话框必然是异步应答），
// DialogHost（挂载在 App 根部，全局唯一）渲染队头对话框，用户点确定 /
// 取消 / × / 遮罩 / Esc 时结算 Promise 并出队。同一时刻只显示一个
// 对话框，后续调用排队依次弹出。
//
// 测试注入：installAutoAnswer(fn) —— 单元测试只渲染单个页面组件时
// DialogHost 未挂载，此时调用直接由 fn 应答（fn 收到对话框配置对象，
// 可记录消息文案并返回应答值），避免 Promise 悬挂。无宿主且无注入时
// 保守按「取消」应答（confirm 返回 false），安全优先、调用不卡死。

let listener = null // DialogHost 订阅的强制刷新回调
let autoAnswer = null // 测试注入：无宿主挂载时的自动应答函数
const queue = [] // 待显示对话框队列（队头为当前显示项）
let seq = 0

// DialogHost 挂载时订阅（返回取消订阅函数；卸载时清除，防泄漏）
export function subscribeDialogHost(fn) {
  listener = fn
  return () => {
    if (listener === fn) listener = null
  }
}

// 测试注入：无宿主时由 fn 直接应答；传 null 清除注入
export function installAutoAnswer(fn) {
  autoAnswer = fn
}

// 清空队列并全部按「取消」结算（测试清理 / 宿主卸载兜底）
export function resetDialogs() {
  queue.splice(0).forEach((d) => d.resolve(d.kind === 'confirm' ? false : undefined))
}

// 队头对话框（无则 null）；DialogHost 渲染时读取
export function currentDialog() {
  return queue[0] || null
}

// 结算指定对话框并出队（点确定/取消/×/遮罩/Esc 时由 DialogHost 调用）
export function settleDialog(id, result) {
  const idx = queue.findIndex((d) => d.id === id)
  if (idx < 0) return
  const [d] = queue.splice(idx, 1)
  d.resolve(result)
  listener?.() // 通知宿主重渲染：显示下一个排队对话框或清空
}

function enqueue(item) {
  const d = { ...item, id: ++seq }
  const promise = new Promise((resolve) => { d.resolve = resolve })
  queue.push(d)
  if (listener) {
    listener() // 宿主已挂载：正常显示流程，等待用户交互结算
  } else if (autoAnswer) {
    settleDialog(d.id, autoAnswer(d)) // 测试注入：直接应答
  } else {
    // 无宿主且无注入（生产环境不会发生，App 根挂载了 DialogHost）：
    // 保守按「取消」应答，避免调用方 await 悬挂
    settleDialog(d.id, d.kind === 'confirm' ? false : undefined)
  }
  return promise
}

// 弹确认对话框：opts = { title?, message, confirmText?, cancelText?, danger? }
// resolve(true) = 用户点「确定」；resolve(false) = 取消/×/遮罩/Esc。
export function confirmDialog(opts = {}) {
  return enqueue({ kind: 'confirm', ...opts })
}

// 弹提示对话框：opts = { title?, message, confirmText? }
// 单「确定」按钮，任何关闭方式均 resolve(undefined)（提示无二义结果）。
export function alertDialog(opts = {}) {
  return enqueue({ kind: 'alert', ...opts })
}
