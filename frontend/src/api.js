// 后端 API 封装

// SSO 是否启用（issue #27 第五轮）：由 App 从 /api/auth/status 探测后设置。
// 401 兜底仅在 SSO 启用时跳登录页——非 SSO 场景（或探测完成前）的 401 不应
// 跳转，否则与页面重载叠加会形成无限刷新循环。
let ssoEnabled = false

export function setSsoEnabled(v) {
  ssoEnabled = !!v
}

async function request(method, path, body) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const resp = await fetch(path, opts)
  let data = null
  try { data = await resp.json() } catch { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    // 会话失效兜底（issue #27）：SSO 启用时未登录访问受保护 API → 401，
    // 跳登录页（登录流程自身端点除外，避免死循环）
    if (resp.status === 401 && ssoEnabled && !path.startsWith('/api/auth/')) {
      window.location.href = '/login'
    }
    const msg = data?.error || data?.detail || `HTTP ${resp.status}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
  // 下载备份文件（blob，不走 JSON 解析）
  download: async (path, filename) => {
    const resp = await fetch(path)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  // 上传备份文件（multipart/form-data）
  upload: async (path, file) => {
    const fd = new FormData()
    fd.append('file', file)
    const resp = await fetch(path, { method: 'POST', body: fd })
    let data = null
    try { data = await resp.json() } catch { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      const msg = data?.error || data?.detail || `HTTP ${resp.status}`
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
    return data
  },
}

// 文件大小人类可读
export function fmtSize(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

// 状态徽标样式
export const STATUS_META = {
  queued: { label: '排队中', cls: 'status-queued' },
  running: { label: '执行中', cls: 'status-running' },
  retrying: { label: '重试中', cls: 'status-retrying' },
  succeeded: { label: '成功', cls: 'status-succeeded' },
  failed: { label: '失败', cls: 'status-failed' },
  interrupted: { label: '已中断', cls: 'status-interrupted' },
}

// 页面时间显示时区（IANA 名，null = 跟随浏览器本机时区）。由 App 启动时从
// /api/settings 的 ui.timezone 加载、设置页保存时更新（issue #14）。
let displayTz = null

export function setDisplayTz(tz) {
  displayTz = tz || null
}

export function fmtTime(ts, tz = displayTz) {
  if (!ts) return '—'
  // 后端 SQLite datetime('now') 存 UTC 无时区后缀（如 '2026-08-12 01:25:54'），
  // 补 Z 解析为 UTC 时刻，再按配置时区（缺省 = 浏览器本机）格式化
  const date = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (Number.isNaN(date.getTime())) return String(ts) // 非标准格式原样兜底
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: tz || undefined,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]))
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
}

// 任务执行时长人类可读（issue #23）：start → end 的时长换算为 秒/分钟/小时/天。
// 与 fmtTime 同规则解析后端 UTC 时间串；缺字段、解析失败或结束早于开始
// （时钟异常）返回 null（页面显示占位符）。
export function fmtDuration(startTs, endTs) {
  if (!startTs || !endTs) return null
  const start = new Date(String(startTs).replace(' ', 'T') + 'Z')
  const end = new Date(String(endTs).replace(' ', 'T') + 'Z')
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null
  const totalSec = Math.floor((end - start) / 1000)
  if (totalSec < 0) return null
  if (totalSec < 60) return `${totalSec} 秒`
  const totalMin = Math.floor(totalSec / 60)
  if (totalMin < 60) return `${totalMin} 分钟`
  const hours = Math.floor(totalMin / 60)
  if (hours < 24) {
    const mins = totalMin % 60
    return mins ? `${hours} 小时 ${mins} 分钟` : `${hours} 小时`
  }
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours ? `${days} 天 ${restHours} 小时` : `${days} 天`
}

// commit sha 短显示（issue #19）：完整 sha 截断为前 8 位，空值返回占位符
export function shortSha(sha) {
  if (!sha || typeof sha !== 'string') return '—'
  return sha.length > 8 ? sha.slice(0, 8) : sha
}

// 实时执行面板文本截断（issue #20）：超长文本截断到 max 字符并加省略号
export function truncateText(text, max = 120) {
  if (!text) return ''
  const s = typeof text === 'string' ? text : JSON.stringify(text)
  return s.length > max ? s.slice(0, max) + '…' : s
}

// 工具调用输入一行式摘要（issue #20）：Bash 命令显示 `$ cmd`，
// 其余对象/数组序列化为单行 JSON；空值返回占位符
export function summarizeToolInput(input, tool) {
  if (input == null || input === '') return '—'
  if (typeof input === 'object') {
    if (tool === 'Bash' && typeof input.command === 'string' && input.command) {
      return truncateText('$ ' + input.command, 120)
    }
    return truncateText(input, 120)
  }
  return truncateText(String(input), 120)
}
