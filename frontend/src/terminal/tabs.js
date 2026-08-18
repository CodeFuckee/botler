// 终端标签页纯逻辑（issue #183）：编号/命名/快捷键/状态文案，便于单元测试
export const MAX_TABS = 8

/** 状态 → 中文文案 */
export function statusLabel(status) {
  return { connecting: '连接中', connected: '已连接', closed: '已断开' }[status] || status || ''
}

/** 下一个标签显示名：终端 1、终端 2 …（编号单调递增，关闭后不回收避免歧义） */
export function nextTabName(tabs) {
  let max = 0
  for (const t of tabs) {
    const m = /^终端 (\d+)$/.exec(t.name)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return `终端 ${max + 1}`
}

/**
 * 终端页快捷键解析（issue #183）：返回 'new' / 'close' / null。
 * 刻意避开浏览器保留快捷键（Ctrl+Shift+T 重开标签、Ctrl+W 关标签等），
 * 采用 Alt 组合：Alt+T 新建、Alt+W 关闭当前标签。
 */
export function handleTerminalKey(e, hasTabs) {
  if (!e.altKey || e.ctrlKey || e.shiftKey || e.metaKey) return null
  const key = e.key
  if (key === 't' || key === 'T') return 'new'
  if ((key === 'w' || key === 'W') && hasTabs) return 'close'
  return null
}
