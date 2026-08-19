// api.js 自动附带 CSRF token（issue #263）测试：
//   - 非 GET 请求（POST/PUT/DELETE）自动带 X-CSRF-Token 头（值取自
//     botler_csrf cookie，双提交 cookie 模式后端校验头==cookie）；
//   - GET / HEAD 不带头（只读请求无 CSRF 风险面）；
//   - cookie 缺失（SSO 未启用 / 未登录）时不带该头，行为与现状一致；
//   - upload（multipart POST）同样带头，终端 token 等场景不受影响。
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../src/api.js'

// ---- cookie mock 工具 ----
const origDocument = globalThis.document

function mockDocument(cookieStr) {
  globalThis.document = { cookie: cookieStr }
}

beforeEach(() => {
  globalThis.document = origDocument
})

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

// ---- 非 GET 自动带 CSRF 头 ----

test('POST 自动带 X-CSRF-Token（cookie 存在时）', async () => {
  mockDocument('botler_session=abc; botler_csrf=tok-123; other=1')
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(200, { ok: true })
  })
  try {
    await api.post('/api/settings', { a: 1 })
    assert.equal(seenOpts.headers['X-CSRF-Token'], 'tok-123')
    assert.equal(seenOpts.method, 'POST')
  } finally {
    m.restore()
  }
})

test('PUT / DELETE 同样自动带 CSRF 头', async () => {
  mockDocument('botler_csrf=tok-456')
  const seen = []
  const m = installFetch((i, url, opts) => {
    seen.push(opts)
    return httpResp(200, {})
  })
  try {
    await api.put('/api/settings', {})
    await api.del('/api/repos/1')
    assert.equal(seen[0].headers['X-CSRF-Token'], 'tok-456')
    assert.equal(seen[1].headers['X-CSRF-Token'], 'tok-456')
  } finally {
    m.restore()
  }
})

test('GET 不带头（只读请求无 CSRF 风险面）', async () => {
  mockDocument('botler_csrf=tok-789')
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(200, {})
  })
  try {
    await api.get('/api/settings')
    assert.equal(seenOpts.headers['X-CSRF-Token'], undefined)
  } finally {
    m.restore()
  }
})

test('cookie 缺失时 POST 不带头（SSO 未启用 / 未登录场景）', async () => {
  mockDocument('botler_session=abc; other=1') // 无 botler_csrf
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(200, {})
  })
  try {
    await api.post('/api/settings', {})
    assert.equal(seenOpts.headers['X-CSRF-Token'], undefined)
    assert.equal(seenOpts.headers['Content-Type'], 'application/json') // 原有行为保持
  } finally {
    m.restore()
  }
})

test('upload（multipart POST）自动带 CSRF 头', async () => {
  mockDocument('botler_csrf=tok-upload')
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(200, { ok: true })
  })
  try {
    await api.upload('/api/backups/restore/upload', new File(['x'], 'a.bin'))
    assert.equal(seenOpts.headers['X-CSRF-Token'], 'tok-upload')
    assert.equal(seenOpts.method, 'POST')
  } finally {
    m.restore()
  }
})

test('非浏览器环境（无 document）POST 不带头不抛错', async () => {
  globalThis.document = undefined
  let seenOpts = null
  const m = installFetch((i, url, opts) => {
    seenOpts = opts
    return httpResp(200, {})
  })
  try {
    await api.post('/api/settings', {})
    assert.equal(seenOpts.headers['X-CSRF-Token'], undefined)
  } finally {
    m.restore()
  }
})
