// 单个终端标签页视图（issue #183）：xterm.js + FitAddon + 协议直连适配层
//
// - 测试环境（node --test 无 DOM）跳过真实终端初始化，仅渲染容器；
// - onStatus 经 ref 透传，避免父组件每次渲染导致终端重建；
// - WebSocket 经主后端反向代理同源连接（buildWsUrl），token 由父页面获取。
import { useEffect, useRef } from 'react'
import { buildWsUrl } from './protocol.js'
import { TerminalAttach } from './attach.js'

export default function TerminalView({ tab, token, onStatus }) {
  const hostRef = useRef(null)
  // onStatus 以 ref 保存：父组件渲染不影响本组件 effect 依赖，终端不重建
  const onStatusRef = useRef(onStatus)
  onStatusRef.current = onStatus

  useEffect(() => {
    // 非浏览器环境（单元测试）不初始化真实终端
    if (typeof window === 'undefined' || !hostRef.current) return undefined
    let disposed = false
    let cleanup = null
    let socket = null
    onStatusRef.current?.(tab.id, 'connecting')
    ;(async () => {
      if (disposed) return
      try {
        // @xterm/* 为 CJS 产物：Vite 预构建后可具名导入，这里做双通道兼容
        const termMod = await import('@xterm/xterm')
        const fitMod = await import('@xterm/addon-fit')
        const Terminal = termMod.Terminal || termMod.default?.Terminal
        const FitAddon = fitMod.FitAddon || fitMod.default?.FitAddon
        if (disposed || !hostRef.current) return
        const term = new Terminal({
          cursorBlink: true,
          fontSize: 13,
          fontFamily: '"Cascadia Code", "JetBrains Mono", Menlo, Consolas, monospace',
          theme: { background: '#0f1115', foreground: '#e6edf3' },
          scrollback: 5000,
        })
        const fit = new FitAddon()
        term.loadAddon(fit)
        term.open(hostRef.current)
        try { fit.fit() } catch { /* 容器尺寸不可用时不阻塞 */ }
        // 直连 WebSocket（同源反向代理），terminado 协议由适配层编解码
        socket = new WebSocket(buildWsUrl(window.location, tab.name, token))
        const attach = new TerminalAttach(socket, (status) => {
          if (!disposed) onStatusRef.current?.(tab.id, status)
        })
        attach.activate(term)
        const onResize = () => { try { fit.fit() } catch { /* 忽略 */ } }
        window.addEventListener('resize', onResize)
        cleanup = () => {
          attach.dispose()
          window.removeEventListener('resize', onResize)
          term.dispose()
        }
      } catch {
        if (!disposed) onStatusRef.current?.(tab.id, 'closed')
      }
    })()
    return () => {
      disposed = true
      if (cleanup) cleanup()
      if (socket) { try { socket.close() } catch { /* 忽略 */ } }
    }
  }, [tab.name, token])

  return <div className="terminal-host" ref={hostRef} data-tab={tab.name} />
}
