// api.request() 统一错误处理与 GET 重试测试（issue #226）：
//   - GET 网络错误/5xx 自动重试 1 次（间隔 500ms），4xx 不重试；
//   - 非 2xx 自动 toast 错误信息，silent（轮询）接口静默；
//   - 401 SSO 会话失效跳转逻辑回归保护。
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { api, setSsoEnabled } from '../src/api.js'
import { currentToasts, clearToasts } from '../src/toast.js'

// ---- fetch mock 工具 ----
// resp 可为 Response 形态对象 {ok, status, json} 或抛错函数
function installFetch(responder) {
  const calls = []
  const orig = globalThis.fetch
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts })
    return responder(calls.length - 1, url, opts)
  }
  return {
    calls,
    restore() { globalThis.fetch = orig },
  }
}

function httpResp(status, data) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  }
}

const NETWORK_ERR = new TypeError('Failed to fetch')

beforeEach(() => {
  clearToasts()
  setSsoEnabled(false) // 默认非 SSO，401 不触发跳转
})

// ---- GET 自动重试：网络错误 / 5xx ----

test('GET 网络错误 → 重试 1 次成功（调用 2 次，返回数据，无 toast）', async () => {
  const m = installFetch((i) => {
    if (i === 0) throw NETWORK_ERR
    return httpResp(200, { ok: true })
  })
  try {
    const d = await api.get('/api/tasks')
    assert.deepEqual(d, { ok: true })
    assert.equal(m.calls.length, 2) // 首次失败 + 重试成功
    assert.equal(currentToasts().length, 0) // 重试成功无感知，不弹 toast
  } finally {
    m.restore()
  }
})

test('GET 5xx → 重试 1 次成功（调用 2 次，无 toast）', async () => {
  const m = installFetch((i) => {
    if (i === 0) return httpResp(500, { error: '服务器繁忙' })
    return httpResp(200, [1, 2, 3])
  })
  try {
    const d = await api.get('/api/issues')
    assert.deepEqual(d, [1, 2, 3])
    assert.equal(m.calls.length, 2)
    assert.equal(currentToasts().length, 0)
  } finally {
    m.restore()
  }
})

test('GET 重试间隔 500ms（两次调用时间差 ≥ 490ms）', async () => {
  const t0 = Date.now()
  const m = installFetch((i) => {
    if (i === 0) throw NETWORK_ERR
    return httpResp(200, {})
  })
  try {
    await api.get('/api/x')
    const elapsed = Date.now() - t0
    assert.ok(elapsed >= 490, `重试应间隔 500ms，实际 ${elapsed}ms`)
  } finally {
    m.restore()
  }
})

// ---- GET 不重试：4xx / 业务错误 ----

test('GET 4xx → 不重试（仅 1 次调用），抛错并弹 toast', async () => {
  const m = installFetch(() => httpResp(404, { error: '资源不存在' }))
  try {
    await assert.rejects(() => api.get('/api/nope'), /资源不存在/)
    assert.equal(m.calls.length, 1)
    const toasts = currentToasts()
    assert.equal(toasts.length, 1)
    assert.equal(toasts[0].type, 'error')
    assert.equal(toasts[0].message, '资源不存在')
  } finally {
    m.restore()
  }
})

test('GET 401 业务错误 → 不重试（SSO 未启用不跳转），toast 提示', async () => {
  const m = installFetch(() => httpResp(401, { error: '未登录' }))
  try {
    await assert.rejects(() => api.get('/api/tasks'), /未登录/)
    assert.equal(m.calls.length, 1)
    assert.equal(currentToasts().length, 1)
  } finally {
    m.restore()
  }
})

// ---- 最终失败（重试也用完）----

test('GET 网络错误重试仍失败 → 抛错并弹 toast（仅一次）', async () => {
  const m = installFetch(() => { throw NETWORK_ERR })
  try {
    await assert.rejects(() => api.get('/api/x'), /Failed to fetch/)
    assert.equal(m.calls.length, 2)
    const toasts = currentToasts()
    assert.equal(toasts.length, 1) // 只弹一次（最终失败），非每次失败
  } finally {
    m.restore()
  }
})

test('GET 5xx 重试仍失败 → 抛错并弹 toast', async () => {
  const m = installFetch(() => httpResp(502, { error: 'Bad Gateway' }))
  try {
    await assert.rejects(() => api.get('/api/x'), /Bad Gateway/)
    assert.equal(m.calls.length, 2)
    assert.equal(currentToasts().length, 1)
    assert.equal(currentToasts()[0].message, 'Bad Gateway')
  } finally {
    m.restore()
  }
})

// ---- silent（轮询接口）：失败静默，不弹 toast ----

test('GET silent 4xx → 不弹 toast（页面自行展示错误态）', async () => {
  const m = installFetch(() => httpResp(500, { error: '内部错误' }))
  try {
    await assert.rejects(() => api.get('/api/overview', { silent: true }), /内部错误/)
    assert.equal(m.calls.length, 2) // 5xx 仍重试
    assert.equal(currentToasts().length, 0)
  } finally {
    m.restore()
  }
})

test('GET silent 网络错误 → 重试仍失败不弹 toast', async () => {
  const m = installFetch(() => { throw NETWORK_ERR })
  try {
    await assert.rejects(() => api.get('/api/notify', { silent: true }), /Failed to fetch/)
    assert.equal(m.calls.length, 2)
    assert.equal(currentToasts().length, 0)
  } finally {
    m.restore()
  }
})

// ---- 非 GET 方法不重试 ----

test('POST 失败 → 不重试（1 次调用），弹 toast', async () => {
  const m = installFetch(() => httpResp(400, { detail: '参数错误' }))
  try {
    await assert.rejects(() => api.post('/api/tasks', { a: 1 }), /参数错误/)
    assert.equal(m.calls.length, 1)
    assert.equal(currentToasts().length, 1)
  } finally {
    m.restore()
  }
})

test('POST 网络错误 → 不重试，弹 toast', async () => {
  const m = installFetch(() => { throw NETWORK_ERR })
  try {
    await assert.rejects(() => api.post('/api/tasks', {}), /Failed to fetch/)
    assert.equal(m.calls.length, 1)
    assert.equal(currentToasts().length, 1)
  } finally {
    m.restore()
  }
})

test('DELETE 失败 → 不重试', async () => {
  const m = installFetch(() => httpResp(500, { error: '删除失败' }))
  try {
    await assert.rejects(() => api.del('/api/x'), /删除失败/)
    assert.equal(m.calls.length, 1)
  } finally {
    m.restore()
  }
})

// ---- 回归：请求体与响应解析 ----

test('POST body JSON 序列化 + Content-Type 保持（回归）', async () => {
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(201, { id: 7 })
  })
  try {
    const d = await api.post('/api/labels', { name: 'bug' })
    assert.deepEqual(d, { id: 7 })
    assert.equal(seenOpts.method, 'POST')
    assert.equal(seenOpts.headers['Content-Type'], 'application/json')
    assert.equal(seenOpts.body, JSON.stringify({ name: 'bug' }))
  } finally {
    m.restore()
  }
})

test('非 JSON 响应体容错：200 空 body 返回 null（回归）', async () => {
  const m = installFetch(() => ({
    ok: true,
    status: 200,
    json: async () => { throw new SyntaxError('Unexpected end of JSON input') },
  }))
  try {
    const d = await api.get('/api/empty')
    assert.equal(d, null)
  } finally {
    m.restore()
  }
})

// ---- 401 SSO 跳转回归保护（issue #27）----

test('SSO 启用 + 401（非 auth 端点）→ 跳登录页', async () => {
  setSsoEnabled(true)
  const m = installFetch(() => httpResp(401, { error: '未登录' }))
  const origLocation = globalThis.window
  const redirects = []
  globalThis.window = { location: { set href(v) { redirects.push(v) }, get href() { return redirects[redirects.length - 1] || '' } } }
  try {
    await assert.rejects(() => api.get('/api/tasks'), /未登录/)
    assert.equal(redirects[0], '/login')
  } finally {
    m.restore()
    globalThis.window = origLocation
  }
})
