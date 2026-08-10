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
