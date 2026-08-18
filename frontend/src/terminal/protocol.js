// terminado 标准 WebSocket JSON 协议编解码 + WebSocket 地址构造（issue #183）
//
// 终端服务进程（backend/terminal_service.py，Tornado + terminado）采用
// terminado 标准协议：
//   客户端 → 服务端：["stdin", 文本] / ["set_size", rows, cols]
//   服务端 → 客户端：["setup", {}] / ["stdout", 文本] / ["disconnect", 码]
// 前端经 Botler 主后端反向代理（/api/terminal/ws/<name>）同源直连。
//
// 说明：@xterm/addon-attach 只收发原始文本、无法表达 resize，且与
// terminado 的 JSON 协议不兼容；本模块 + attach.js 提供等价 AttachAddon
// 的直连语义（onData → send、onmessage → write），并额外编码 resize。

/** 编码 stdin 消息：term.onData 的原始输入 → terminado 协议 */
export function encodeStdin(data) {
  return JSON.stringify(['stdin', data])
}

/** 编码 resize 消息：term.onResize 的 {cols, rows} → terminado 协议 */
export function encodeResize(cols, rows) {
  return JSON.stringify(['set_size', rows, cols])
}

/** 解析服务端消息（文本或二进制）→ {type, payload}；非法消息返回 null */
export function decodeMessage(raw) {
  if (typeof raw === 'string') return parseArray(raw)
  if (raw instanceof ArrayBuffer) return parseArray(new TextDecoder().decode(raw))
  if (ArrayBuffer.isView(raw)) return parseArray(new TextDecoder().decode(raw))
  return null
}

function parseArray(text) {
  try {
    const arr = JSON.parse(text)
    return Array.isArray(arr) && arr.length ? { type: arr[0], payload: arr[1] } : null
  } catch {
    return null // 非法 JSON（心跳/干扰数据）容错
  }
}

/** 构造终端 WebSocket 地址：与主后端同源，经 /api/terminal/ws/<name> 反向代理 */
export function buildWsUrl(location, name, token) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const base = `${proto}//${location.host}/api/terminal/ws/${encodeURIComponent(name)}`
  return token ? `${base}?token=${encodeURIComponent(token)}` : base
}
