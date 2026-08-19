// 测试辅助：模拟 document.visibilityState 与 visibilitychange 监听
// （node --test 环境无 document），供 usePolling / version-update 的
// 「页面隐藏暂停轮询、恢复可见立即刷新」行为测试使用。
export function installFakeDocument(initialState = 'visible') {
  const listeners = new Map()
  const doc = {
    visibilityState: initialState,
    addEventListener(type, cb) {
      listeners.set(type, cb)
    },
    removeEventListener(type, cb) {
      if (listeners.get(type) === cb) listeners.delete(type)
    },
  }
  const hadDoc = 'document' in globalThis
  const orig = globalThis.document
  globalThis.document = doc
  return {
    // 切换可见性并触发 visibilitychange（无监听时静默跳过）
    setVisibility(state) {
      doc.visibilityState = state
      const cb = listeners.get('visibilitychange')
      if (cb) cb()
    },
    // 当前注册的监听器数量（断言挂载/卸载清理用）
    listenerCount() {
      return listeners.size
    },
    restore() {
      if (hadDoc) globalThis.document = orig
      else delete globalThis.document
    },
  }
}
