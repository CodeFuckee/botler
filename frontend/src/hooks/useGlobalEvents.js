// 全局数据变更事件流 hook（issue #478）：订阅后端 /api/events，事件驱动
// 刷新数据模块，替代固定间隔轮询。
//   useGlobalEvents(onEvent, { onOpen, enabled })
//   - onEvent(ev)：收到 {type, ...} 事件时回调
//   - onOpen()：连接建立/断线重连成功时回调（全量刷新兜底）
//   - enabled：false 时不订阅（登录态未就绪等场景）
import { useEffect, useRef } from 'react'
import { subscribeGlobalEvents } from '../lib/eventStream.js'

export function useGlobalEvents(onEvent, options = {}) {
  const { onOpen, enabled = true } = options
  const onEventRef = useRef(onEvent)
  const onOpenRef = useRef(onOpen)
  useEffect(() => { onEventRef.current = onEvent }, [onEvent])
  useEffect(() => { onOpenRef.current = onOpen }, [onOpen])

  useEffect(() => {
    if (!enabled) return undefined
    return subscribeGlobalEvents(
      (ev) => onEventRef.current(ev),
      { onOpen: () => onOpenRef.current && onOpenRef.current() },
    )
  }, [enabled])
}
