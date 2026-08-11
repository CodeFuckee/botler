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

export function fmtTime(ts) {
  if (!ts) return '—'
  // SQLite datetime('now') 是 UTC，本地展示
  return ts.replace('T', ' ').slice(0, 19) + ' UTC'
}
