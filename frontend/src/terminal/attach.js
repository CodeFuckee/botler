// 终端 WebSocket 直连适配层（issue #183）——等价 @xterm/addon-attach 语义
//
// terminado 服务端使用标准 JSON 协议（见 protocol.js），@xterm/addon-attach
// 只收发原始文本无法直接对接，且不具备 resize 发送能力；本类补齐：
//   - term.onData      → ws.send(encodeStdin(data))
//   - term.onResize    → ws.send(encodeResize(cols, rows))（AttachAddon 缺失）
//   - ws.onmessage     → decodeMessage → term.write（stdout）/ 状态回调
//   - ws.onopen/close  → 连接状态回调（连接中/已连接/已断开）
import { encodeResize, encodeStdin, decodeMessage } from './protocol.js'

// WebSocket.readyState 常量（node 测试环境无 WebSocket 全局，用数字）
const WS_OPEN = 1

export class TerminalAttach {
  /**
   * @param {WebSocket} socket 已构造（未必要已连接）的 WebSocket
   * @param {(status: string) => void} onStatus 状态回调：connecting/connected/closed
   */
  constructor(socket, onStatus) {
    this._socket = socket
    this._onStatus = onStatus || (() => {})
    this._disposables = []
  }

  activate(term) {
    this._term = term
    this._onOpen = () => this._onStatus('connected')
    this._onClose = () => this._onStatus('closed')
    this._onMessage = (ev) => {
      const msg = decodeMessage(ev.data)
      if (!msg) return
      if (msg.type === 'stdout') {
        this._term.write(msg.payload || '')
      } else if (msg.type === 'setup') {
        this._onStatus('connected')
      } else if (msg.type === 'disconnect') {
        this._onStatus('closed')
      }
    }
    this._socket.addEventListener('open', this._onOpen)
    this._socket.addEventListener('message', this._onMessage)
    this._socket.addEventListener('close', this._onClose)
    this._disposables.push(term.onData((data) => {
      if (this._socket && this._socket.readyState === WS_OPEN) {
        this._socket.send(encodeStdin(data))
      }
    }))
    this._disposables.push(term.onResize(({ cols, rows }) => {
      if (this._socket && this._socket.readyState === WS_OPEN) {
        this._socket.send(encodeResize(cols, rows))
      }
    }))
  }

  dispose() {
    for (const d of this._disposables) {
      try { d.dispose() } catch { /* 忽略 */ }
    }
    this._disposables = []
    if (this._socket) {
      this._socket.removeEventListener('open', this._onOpen)
      this._socket.removeEventListener('message', this._onMessage)
      this._socket.removeEventListener('close', this._onClose)
      try {
        if (this._socket.readyState === WS_OPEN || this._socket.readyState === 0) {
          this._socket.close()
        }
      } catch { /* 忽略 */ }
    }
  }
}
