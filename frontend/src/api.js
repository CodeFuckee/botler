// 后端 API 封装

import { showToast } from './toast.js'

// SSO 是否启用（issue #27 第五轮）：由 App 从 /api/auth/status 探测后设置。
// 401 兜底仅在 SSO 启用时跳登录页——非 SSO 场景（或探测完成前）的 401 不应
// 跳转，否则与页面重载叠加会形成无限刷新循环。
let ssoEnabled = false

export function setSsoEnabled(v) {
  ssoEnabled = !!v
}

// 请求失败自动重试（issue #226）：仅 GET 生效，最多重试 1 次（间隔
// 500ms），只对网络错误（fetch 拒绝）与 HTTP 5xx 重试，4xx 等业务错误
// 不重试——内网/ZeroTier 网络抖动导致的轮询偶发假失败重试即可消除，
// 重试成功对用户无感知。
const RETRY_DELAY_MS = 500
const GET_MAX_ATTEMPTS = 2

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 读取 cookie（issue #263）：CSRF 双提交模式前端从 botler_csrf cookie
// 取值回填请求头；非浏览器环境（node 测试）返回 null，不抛错。
function getCookie(name) {
  if (typeof document === 'undefined' || !document.cookie) return null
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
  return m ? decodeURIComponent(m[1]) : null
}

// 写请求自动附带 CSRF token（issue #263）：与后端双提交 cookie 校验配套
// ——后端校验 X-CSRF-Token 头 == botler_csrf cookie == 派生期望值。
// 无 cookie（SSO 未启用 / 未登录）时不带头，行为与现状一致。
function csrfHeader() {
  const token = getCookie('botler_csrf')
  return token ? { 'X-CSRF-Token': token } : {}
}

// 构造 fetch 选项：JSON 序列化 / multipart 由浏览器自动带 boundary
function buildFetchOpts(method, body) {
  const opts = { method, headers: {} }
  // 非 GET/HEAD 写请求带 CSRF 头（读请求无副作用，无需校验）
  if (method !== 'GET' && method !== 'HEAD') {
    Object.assign(opts.headers, csrfHeader())
  }
  if (body !== undefined) {
    if (body instanceof FormData) {
      opts.body = body
    } else {
      opts.headers['Content-Type'] = 'application/json'
      opts.body = JSON.stringify(body)
    }
  }
  return opts
}

// 网络层失败统一文案（fetch 原生 TypeError 文案对用户无意义）
function networkErrorMessage() {
  return '网络请求失败，请检查网络连接'
}

// 统一请求入口（issue #226）：
//   opts = { silent } —— silent: true 用于轮询类接口：失败不弹 toast，
//   由页面保留上次数据并展示「刷新失败」错误文本，避免每几秒弹一次骚扰。
// 非 2xx 且非 silent 时自动 toast 错误信息；401 SSO 会话失效仍跳登录页
// （登录页自身端点除外，避免死循环，issue #27）。
async function request(method, path, body, opts = {}) {
  const silent = !!opts.silent
  const maxAttempts = method === 'GET' ? GET_MAX_ATTEMPTS : 1
  for (let attempt = 1; ; attempt++) {
    let resp
    try {
      resp = await fetch(path, buildFetchOpts(method, body))
    } catch (e) {
      // 网络层失败（断网/超时/连接重置）：GET 重试一次，最终失败 toast
      if (attempt < maxAttempts) {
        await sleep(RETRY_DELAY_MS)
        continue
      }
      if (!silent) showToast(networkErrorMessage(), { type: 'error' })
      throw e
    }
    let data = null
    try { data = await resp.json() } catch { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      // 会话失效兜底（issue #27）：SSO 启用时未登录访问受保护 API → 401，
      // 跳登录页（登录流程自身端点除外，避免死循环）
      if (resp.status === 401 && ssoEnabled && !path.startsWith('/api/auth/')) {
        window.location.href = '/login'
      }
      const msg = data?.error || data?.detail || `HTTP ${resp.status}`
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
      // 5xx（服务端瞬时故障）GET 重试一次；4xx 业务错误不重试
      if (attempt < maxAttempts && resp.status >= 500) {
        await sleep(RETRY_DELAY_MS)
        continue
      }
      if (!silent) showToast(err.message, { type: 'error' })
      throw err
    }
    return data
  }
}

// 订阅任务事件流（SSE 实时输出）：后端逐事件推送执行过程
// （thinking/文本/工具调用/工具结果/结果），终态任务连接后先回放
// 历史事件再发 done 收尾；EventSource 断线自动重连（后端重新回放，
// 事件带递增 seq，消费方按 seq 去重即可无缝衔接）。
// 返回 EventSource 实例；kind=done 时自动关闭并回调 onDone。
export function openTaskEventStream(taskId, handlers = {}) {
  if (typeof EventSource === 'undefined') {
    // 非浏览器环境（node 测试）降级为空连接，避免组件渲染抛错
    return { close() {} }
  }
  const es = new EventSource(`/api/tasks/${taskId}/events`)
  es.onmessage = (msg) => {
    let ev = null
    try {
      ev = JSON.parse(msg.data)
    } catch {
      return // 非法 data 容错
    }
    if (!ev || typeof ev !== 'object') return
    if (ev.kind === 'done') {
      es.close()
      if (handlers.onDone) handlers.onDone()
      return
    }
    if (handlers.onEvent) handlers.onEvent(ev)
  }
  return es
}

export const api = {
  get: (path, opts) => request('GET', path, undefined, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
  put: (path, body, opts) => request('PUT', path, body, opts),
  del: (path, opts) => request('DELETE', path, undefined, opts),
  openTaskEventStream,
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
    const headers = { ...csrfHeader() }
    const resp = await fetch(path, { method: 'POST', headers, body: fd })
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
  canceled_by_user: { label: '已移出队列', cls: 'status-canceled' },
}

// 页面时间显示时区（IANA 名，null = 跟随浏览器本机时区）。由 App 启动时从
// /api/settings 的 ui.timezone 加载、设置页保存时更新（issue #14）。
let displayTz = null

export function setDisplayTz(tz) {
  displayTz = tz || null
}

export function fmtTime(ts, tz = displayTz) {
  if (!ts) return '—'
  // 数字时间戳（issue #271）：会话过期 exp 等 unix 秒级（<1e12）自动转
  // 毫秒；毫秒级原样使用。字符串按既有规则：后端 SQLite datetime('now')
  // 存 UTC 无时区后缀（如 '2026-08-12 01:25:54'），补 Z 解析为 UTC 时刻，
  // 再按配置时区（缺省 = 浏览器本机）格式化
  let date
  if (typeof ts === 'number' && Number.isFinite(ts)) {
    date = new Date(ts < 1e12 ? ts * 1000 : ts)
  } else {
    date = new Date(String(ts).replace(' ', 'T') + 'Z')
  }
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

// 秒数人类可读（issue #180）：平均完成耗时等以秒为单位的时长换算为
// 秒/分钟/小时/天（与 fmtDuration 输出格式一致）。非法输入（null /
// 非有限数 / 负数）返回 null（页面显示占位符）。
export function fmtSeconds(totalSec) {
  if (totalSec == null || !Number.isFinite(totalSec)) return null
  const sec = Math.floor(totalSec)
  if (sec < 0) return null
  if (sec < 60) return `${sec} 秒`
  const totalMin = Math.floor(sec / 60)
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

// 时长人类可读（issue #23）：start → end 的时长换算为 秒/分钟/小时/天。
// 任务「用时」（issue #49）以此动态计算完整处理周期——系统接收时间
// created_at → bot-done 打标时间 finished_at（不落库时长字段）。
// 与 fmtTime 同规则解析后端 UTC 时间串；缺字段、解析失败或结束早于开始
// （时钟异常）返回 null（页面显示占位符）；换算复用 fmtSeconds。
export function fmtDuration(startTs, endTs) {
  if (!startTs || !endTs) return null
  const start = new Date(String(startTs).replace(' ', 'T') + 'Z')
  const end = new Date(String(endTs).replace(' ', 'T') + 'Z')
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null
  return fmtSeconds((end - start) / 1000)
}

// commit sha 短显示（issue #19）：完整 sha 截断为前 8 位，空值返回占位符
export function shortSha(sha) {
  if (!sha || typeof sha !== 'string') return '—'
  return sha.length > 8 ? sha.slice(0, 8) : sha
}

// 相对时间人类可读（issue #43）：距今多久，供概览页流水线卡片展示
// 提交时间用。与 fmtTime 同规则解析后端 UTC 无后缀时间串；60 秒内与
// 未来时间（本地时钟偏差）统一按「刚刚」；空值/解析失败返回 null
// （页面不渲染）。now 参数可注入固定时刻供测试。
export function fmtAgo(ts, now = Date.now()) {
  if (!ts) return null
  const date = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (Number.isNaN(date.getTime())) return null
  const sec = Math.floor((now - date.getTime()) / 1000)
  if (sec < 60) return '刚刚'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min} 分钟前`
  const hours = Math.floor(min / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  if (days < 365) return `${Math.floor(days / 30)} 个月前`
  return `${Math.floor(days / 365)} 年前`
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
