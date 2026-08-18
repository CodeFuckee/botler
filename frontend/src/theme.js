// ============================================================
// 界面显示主题（issue #217）：三态——跟随系统 / 浅色 / 深色。
//
// 存储双通道：
//   1. localStorage（键 botler.theme）——浏览器本地偏好，index.html
//      首屏 inline 脚本在应用 JS 加载前读取，避免深色用户先看到浅色白屏；
//   2. 后端 config.yaml（ui.theme）——设置页「界面显示」卡片保存后写回，
//      跨设备权威配置（App 启动时拉取并覆盖本地偏好）。
//
// 取值与后端 ui.theme 完全一致：system / light / dark。
// 渲染机制：<html data-theme="light|dark"> + CSS 变量翻转；system 模式
// 下监听系统 prefers-color-scheme 变化自动适配。
// ============================================================

export const THEME_MODES = ['system', 'light', 'dark']

// 三态中文文案（设置页「界面显示」下拉选项）
export const THEME_MODE_LABELS = { system: '跟随系统', light: '浅色', dark: '深色' }

// 本地偏好存储键（issue #217）：index.html 首屏脚本与 App/设置页共用
export const THEME_STORAGE_KEY = 'botler.theme'

/** 读取本地主题偏好：非法值（手改/旧版本写入）一律回退 null（= 跟随系统）。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
 *  getItem 抛异常（隐私模式）时兜底 null，不影响页面使用。 */
export function loadThemePreference(storage) {
  try {
    if (!storage) return null
    const raw = storage.getItem(THEME_STORAGE_KEY)
    return THEME_MODES.includes(raw) ? raw : null
  } catch {
    return null
  }
}

/** 保存本地主题偏好：只接受 system / light / dark；
 *  存储不可用（SSR/隐私模式）时静默忽略，不抛错。 */
export function saveThemePreference(storage, mode) {
  try {
    if (!THEME_MODES.includes(mode)) return
    storage?.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    /* 无存储环境：静默忽略，不影响页面使用 */
  }
}

/** 系统当前是否偏好深色（prefers-color-scheme: dark）。
 *  无 matchMedia 环境（SSR/旧浏览器）按浅色处理。 */
export function systemPrefersDark() {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : false
  } catch {
    return false
  }
}

/** 解析实际生效主题：light / dark 直接采用；system（或缺省）跟随系统偏好。 */
export function resolveTheme(mode) {
  const m = THEME_MODES.includes(mode) ? mode : 'system'
  if (m === 'light') return 'light'
  if (m === 'dark') return 'dark'
  return systemPrefersDark() ? 'dark' : 'light'
}

/** 应用主题：设置 <html data-theme="light|dark"> 与 color-scheme（原生
 *  滚动条/表单控件同步），返回实际生效主题。
 *  doc：document 兼容对象（测试可注入）；无 DOM 环境（SSR）时静默跳过。 */
export function applyTheme(mode, doc) {
  const actual = resolveTheme(mode)
  const d = doc || (typeof document !== 'undefined' ? document : null)
  if (d?.documentElement) {
    d.documentElement.setAttribute('data-theme', actual)
    d.documentElement.style.colorScheme = actual
  }
  return actual
}

/** 监听系统深色偏好变化：仅当当前选择为 system 时回调 onChange（自动适配，
 *  手动 light/dark 不响应系统变化）。返回取消监听函数；无 matchMedia
 *  环境时返回空操作。 */
export function watchSystemTheme(modeFn, onChange) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return () => {}
  }
  let mq = null
  try {
    mq = window.matchMedia('(prefers-color-scheme: dark)')
  } catch {
    return () => {}
  }
  const handler = () => {
    try {
      if ((modeFn() || 'system') === 'system') onChange()
    } catch {
      /* 忽略单个变化事件的异常 */
    }
  }
  if (typeof mq.addEventListener === 'function') {
    mq.addEventListener('change', handler)
  }
  return () => {
    if (typeof mq?.removeEventListener === 'function') {
      mq.removeEventListener('change', handler)
    }
  }
}
