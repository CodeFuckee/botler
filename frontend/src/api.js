// 后端 API 封装

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

// commit sha 短显示（issue #19）：完整 sha 截断为前 8 位，空值返回占位符
export function shortSha(sha) {
  if (!sha || typeof sha !== 'string') return '—'
  return sha.length > 8 ? sha.slice(0, 8) : sha
}
