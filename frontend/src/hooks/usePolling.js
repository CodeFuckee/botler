// 页面可见性感知的轮询 hook（issue #200）：统一管理各页手写 setInterval
// 的轮询定时器。页面切到后台标签页（document.visibilityState === 'hidden'）
// 时立即暂停全部定时器（后台 0 请求），恢复可见时立即拉取一次最新数据再
// 恢复定时器，避免后台标签页空耗资源/流量、放大服务器压力。
//
// 用法：
//   usePolling(fn, intervalMs, { enabled, immediate })
//   - fn: 轮询函数，必须保持引用稳定（建议 useCallback 包裹）；fn 变化会
//     触发立即拉取一次并重启定时器（过滤条件变化即时生效，与原手写
//     useEffect 依赖行为一致）
//   - intervalMs: 轮询间隔（毫秒）；null / undefined / <= 0 时不启动定时器
//   - enabled: 是否启用轮询（默认 true）；false 时既不启动定时器也不监听
//     可见性（用于登录态未就绪、无活跃任务等条件轮询场景）
//   - immediate: 每次启动（挂载 / enabled 或 interval 变化 / fn 变化）时
//     是否立即执行一次（默认跟随 enabled——启用才立即拉取；需要「停用时
//     也先拉一次」的场景显式传 true，如任务列表页挂载即加载）
//
// 生命周期：
//   - 启动（enabled && interval 有效）：页面可见时立即执行一次 fn，再按
//     interval 启动定时器；页面初始即隐藏时不启动（后台 0 请求）
//   - 页面隐藏：清除定时器（暂停轮询，后台标签页 0 请求）
//   - 页面恢复可见：立即执行一次 fn（刷新最新数据），再恢复定时器
//   - 卸载：清除定时器并移除 visibilitychange 监听
import { useEffect, useRef } from 'react'

// 页面当前是否可见（SSR / 测试环境无 document 时视为可见，保持既有行为）
export function isDocumentVisible() {
  if (typeof document === 'undefined') return true
  return typeof document.visibilityState !== 'string' ||
    document.visibilityState !== 'hidden'
}

export function usePolling(fn, interval, options = {}) {
  const { enabled = true, immediate = enabled } = options
  // fn 经 ref 读取最新引用：定时器回调不因 fn 变化而重建
  const fnRef = useRef(fn)
  useEffect(() => { fnRef.current = fn }, [fn])

  useEffect(() => {
    // 启动时立即执行一次（页面可见才发请求，后台打开不空耗）
    if (immediate && isDocumentVisible()) {
      fnRef.current()
    }
    // 未启用或间隔无效：不启动定时器、不监听可见性
    if (!enabled || interval == null || interval <= 0) return undefined

    let timer = null
    const stop = () => {
      if (timer != null) {
        clearInterval(timer)
        timer = null
      }
    }
    const start = () => {
      if (timer == null && isDocumentVisible()) {
        timer = setInterval(() => fnRef.current(), interval)
      }
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        stop() // 页面隐藏：暂停轮询（后台标签页 0 请求）
      } else {
        fnRef.current() // 恢复可见：立即拉取一次最新数据
        start()         // 再恢复定时器
      }
    }

    if (isDocumentVisible()) start()
    if (typeof document !== 'undefined' &&
        typeof document.addEventListener === 'function') {
      document.addEventListener('visibilitychange', onVisibilityChange)
    }
    return () => {
      stop()
      if (typeof document !== 'undefined' &&
          typeof document.removeEventListener === 'function') {
        document.removeEventListener('visibilitychange', onVisibilityChange)
      }
    }
  }, [fn, enabled, interval, immediate])
}
