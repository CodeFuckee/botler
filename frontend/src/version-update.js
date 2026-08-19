// 版本更新提示模块（issue #233）：页面加载后定时轮询 /version.json，
// 检测到版本号与页面加载时的基线不一致（新版部署完成）→ 触发 onUpdate
// 回调，由 App 渲染「检测到新版本，点击刷新」横幅。避免用户停留在旧
// 页面无法感知部署更新（排查「这个功能部署了吗」依赖版本可见）。
//
// 设计：
//   - 首次轮询成功只记录基线，不弹提示（避免历史版本误报）；
//   - 版本变化只提示一次（notified 置位），用户忽略后不再重复打扰，
//     直到刷新页面重新初始化；
//   - 轮询失败静默跳过（网络抖动/构建目录临时不可读不打断用户）；
//   - 数据源与 VersionBadge 同源（/version.json）——后端 /api/health
//     返回的版本号也来自同一构建产物，三方口径一致。

export const VERSION_CHECK_INTERVAL_MS = 60000

// 纯函数：前后两次版本信息是否发生变化（issue #233 验收「部署后版本
// 正确更新」）。缺失信息 / 版本号非字符串一律视为未变化（保守不打扰）。
export function detectVersionChange(previous, current) {
  if (!previous || !current) return false
  return Boolean(
    typeof previous.version === 'string' && previous.version &&
    typeof current.version === 'string' && current.version &&
    previous.version !== current.version,
  )
}

// 创建版本检查器：start() 启动轮询（立即检查一次 + 定时轮询），stop()
// 停止并清理定时器。getVersion 可注入（默认 fetch /version.json，返回
// 版本信息对象），onUpdate 在新版本出现时调用一次。
export function createVersionChecker({
  getVersion = () => fetch('/version.json').then((r) => (r.ok ? r.json() : null)),
  onUpdate = () => {},
  intervalMs = VERSION_CHECK_INTERVAL_MS,
} = {}) {
  let baseline = null // 页面加载时版本（首次轮询成功记录）
  let notified = false // 已提示过（只提示一次）
  let timer = null

  async function check() {
    let current = null
    try {
      current = await getVersion()
    } catch {
      return // 轮询失败静默跳过，下个周期重试
    }
    if (!current || typeof current.version !== 'string' || !current.version) return
    if (!baseline) {
      baseline = current // 首次成功记录基线，不弹提示
      return
    }
    if (!notified && detectVersionChange(baseline, current)) {
      notified = true
      onUpdate(current)
    }
  }

  // issue #200：页面可见性感知——页面隐藏（后台标签页）时暂停版本检查
  // 轮询（0 请求），恢复可见时立即检查一次再恢复定时器，检测后台部署的
  // 新版本不延迟到下一个周期
  function startTimer() {
    if (timer !== null) return
    timer = setInterval(check, intervalMs)
  }

  function stopTimer() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === 'hidden') {
      stopTimer() // 页面隐藏：暂停轮询
    } else {
      check() // 恢复可见：立即检查一次
      startTimer()
    }
  }

  // 页面当前可见（SSR/测试环境无 document 时视为可见，保持既有行为）
  function isDocumentVisible() {
    return typeof document === 'undefined' ||
      typeof document.visibilityState !== 'string' ||
      document.visibilityState !== 'hidden'
  }

  return {
    start() {
      if (timer !== null) return
      if (isDocumentVisible()) {
        check()
        startTimer()
      }
      if (typeof document !== 'undefined' &&
          typeof document.addEventListener === 'function') {
        document.addEventListener('visibilitychange', onVisibilityChange)
      }
    },
    stop() {
      stopTimer()
      if (typeof document !== 'undefined' &&
          typeof document.removeEventListener === 'function') {
        document.removeEventListener('visibilitychange', onVisibilityChange)
      }
    },
    // 测试辅助：手动触发一次检查（不依赖定时器）
    check,
    // 测试辅助：当前基线（未初始化时为 null）
    getBaseline: () => baseline,
  }
}
