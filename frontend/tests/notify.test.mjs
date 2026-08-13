// 网页通知模块测试（issue #21）：事件过滤按设置开关、未知类型、
// 轮询游标推进与首次不弹语义。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  NOTIFY_TYPE_MAP,
  filterNotifyEvents,
  createNotifyPoller,
  notifyFailureReason,
  sendTestNotification,
} from '../src/notify.js'

const DEFAULT_SETTINGS = {
  notifications: {
    enabled: true,
    task_needs_interaction: true,
    issue_completed: true,
    queue_empty: true,
    queue_no_work: true,
  },
}

const EVENTS = [
  { id: 1, type: 'task_failed', title: 'a', body: 'b' },
  { id: 2, type: 'task_succeeded', title: 'c', body: 'd' },
  { id: 3, type: 'queue_empty', title: 'e', body: 'f' },
  { id: 4, type: 'queue_no_work', title: 'g', body: 'h' },
]

test('NOTIFY_TYPE_MAP 覆盖全部四种事件类型', () => {
  assert.deepEqual(NOTIFY_TYPE_MAP, {
    task_succeeded: 'issue_completed',
    task_failed: 'task_needs_interaction',
    queue_empty: 'queue_empty',
    queue_no_work: 'queue_no_work',
  })
})

test('filterNotifyEvents：总开关关闭 → 全部过滤', () => {
  const settings = { notifications: { ...DEFAULT_SETTINGS.notifications, enabled: false } }
  assert.deepEqual(filterNotifyEvents(EVENTS, settings), [])
})

test('filterNotifyEvents：无 notifications 配置 → 全部过滤', () => {
  assert.deepEqual(filterNotifyEvents(EVENTS, {}), [])
  assert.deepEqual(filterNotifyEvents(EVENTS, null), [])
})

test('filterNotifyEvents：全开 → 全部保留（顺序不变）', () => {
  assert.deepEqual(filterNotifyEvents(EVENTS, DEFAULT_SETTINGS), EVENTS)
})

test('filterNotifyEvents：单个时机开关关闭 → 只过滤该类型', () => {
  const settings = {
    notifications: { ...DEFAULT_SETTINGS.notifications, issue_completed: false },
  }
  const kept = filterNotifyEvents(EVENTS, settings)
  assert.deepEqual(kept.map((e) => e.type), ['task_failed', 'queue_empty', 'queue_no_work'])
})

test('filterNotifyEvents：未知事件类型 → 过滤（不弹）', () => {
  const events = [...EVENTS, { id: 99, type: 'unknown_type', title: 'x' }]
  const kept = filterNotifyEvents(events, DEFAULT_SETTINGS)
  assert.deepEqual(kept.map((e) => e.type), [
    'task_failed', 'task_succeeded', 'queue_empty', 'queue_no_work',
  ])
})

test('filterNotifyEvents：开关缺省视为开启', () => {
  const settings = { notifications: { enabled: true } }
  assert.deepEqual(filterNotifyEvents(EVENTS, settings), EVENTS)
})

test('filterNotifyEvents：空事件列表 → 空', () => {
  assert.deepEqual(filterNotifyEvents([], DEFAULT_SETTINGS), [])
})

// ---- 轮询器游标语义 ----

function makePoller(eventsByAfter, settings = DEFAULT_SETTINGS) {
  const shown = []
  const poller = createNotifyPoller({
    getEvents: (after) => Promise.resolve({
      events: eventsByAfter[after] || [],
      latest_id: (eventsByAfter[after] || []).length
        ? Math.max(...eventsByAfter[after].map((e) => e.id)) : after,
    }),
    getSettings: () => Promise.resolve(settings),
    onError: (e) => { throw e },
    show: (ev) => shown.push(ev),
  })
  const wrapped = async () => {
    await poller.poll()
    return shown
  }
  return { poller, wrapped, shown }
}

test('createNotifyPoller：首次拉取只记游标不弹', async () => {
  const { wrapped, shown } = makePoller({ 0: EVENTS })
  await wrapped()
  await wrapped()
  assert.deepEqual(shown, [])
})

test('createNotifyPoller：第二次轮询弹新增事件', async () => {
  const eventsByAfter = { 0: [EVENTS[0]], 1: [EVENTS[1]] }
  const { wrapped, shown } = makePoller(eventsByAfter)
  await wrapped()
  await wrapped()
  assert.deepEqual(shown.map((e) => e.id), [2])
})

test('createNotifyPoller：总开关关闭时游标仍推进', async () => {
  const settings = { notifications: { enabled: false } }
  const eventsByAfter = { 0: [EVENTS[0]], 1: [EVENTS[1]] }
  const { poller, wrapped, shown } = makePoller(eventsByAfter, settings)
  await wrapped()
  await wrapped()
  // 事件被过滤（不弹），但游标照常推进到最新 id 2，避免下次重复拉取
  assert.deepEqual(shown, [])
  assert.equal(poller.getCursor(), 2)
})

test('createNotifyPoller：拉取失败不中断（onError 收到错误，游标不变）', async () => {
  let calls = 0
  const poller = createNotifyPoller({
    getEvents: async () => {
      calls += 1
      if (calls === 1) throw new Error('网络错误')
      return { events: [EVENTS[0]], latest_id: 1 }
    },
    getSettings: () => Promise.resolve(DEFAULT_SETTINGS),
    onError: () => {},
  })
  await poller.poll()
  assert.equal(poller.getCursor(), 0)
  await poller.poll()
  assert.equal(poller.getCursor(), 1)
})

// ---- 测试通知（设置页「弹出测试通知」按钮，issue #21 增量）----

// 构造可测环境：mock window/Notification 全局，返回构造过的通知参数。
// useSupport=false 模拟浏览器不支持 Notification（window 存在但无 Notification）。
// isSecureContext=false 模拟非安全上下文（http/证书不受信任，issue #21 第三轮）。
function mockNotifyEnv({
  permission = 'granted', requestResult = 'granted', useSupport = true, isSecureContext = true,
} = {}) {
  const constructed = []
  const prev = {
    window: globalThis.window, Notification: globalThis.Notification,
    isSecureContext: globalThis.isSecureContext,
  }
  if (useSupport) {
    globalThis.window = globalThis
    globalThis.Notification = class {
      static permission = permission
      static requestPermission = async () => requestResult
      constructor(title, opts) { constructed.push({ title, ...opts }) }
    }
  } else {
    delete globalThis.Notification
    globalThis.window = globalThis
  }
  if (isSecureContext === false) globalThis.isSecureContext = false
  return {
    constructed,
    restore() {
      if (prev.window === undefined) delete globalThis.window
      else globalThis.window = prev.window
      if (prev.Notification === undefined) delete globalThis.Notification
      else globalThis.Notification = prev.Notification
      if (prev.isSecureContext === undefined) delete globalThis.isSecureContext
      else globalThis.isSecureContext = prev.isSecureContext
    },
  }
}

test('sendTestNotification：已授权 → 弹一条测试通知并返回 ok', async () => {
  const env = mockNotifyEnv({ permission: 'granted' })
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: true })
    assert.equal(env.constructed.length, 1)
    assert.equal(env.constructed[0].title, 'Botler 测试通知')
    assert.match(env.constructed[0].body, /测试通知/)
  } finally { env.restore() }
})

test('sendTestNotification：权限未决（default）→ 先请求授权，同意后弹通知', async () => {
  const env = mockNotifyEnv({ permission: 'default', requestResult: 'granted' })
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: true })
    assert.equal(env.constructed.length, 1)
  } finally { env.restore() }
})

test('sendTestNotification：请求授权被拒 → 不弹通知返回 denied', async () => {
  const env = mockNotifyEnv({ permission: 'default', requestResult: 'denied' })
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: false, reason: 'denied' })
    assert.equal(env.constructed.length, 0)
  } finally { env.restore() }
})

test('sendTestNotification：已拒绝（denied）→ 不弹通知返回 denied', async () => {
  const env = mockNotifyEnv({ permission: 'denied' })
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: false, reason: 'denied' })
    assert.equal(env.constructed.length, 0)
  } finally { env.restore() }
})

test('sendTestNotification：浏览器不支持通知 → browser-unsupported', async () => {
  const env = mockNotifyEnv({ useSupport: false })
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: false, reason: 'browser-unsupported' })
  } finally { env.restore() }
})

test('sendTestNotification：构造通知抛异常 → 返回 error 不崩溃', async () => {
  const env = mockNotifyEnv({ permission: 'granted' })
  const Base = globalThis.Notification
  globalThis.Notification = class extends Base {
    constructor() { throw new Error('boom') }
  }
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: false, reason: 'error' })
  } finally { env.restore() }
})

// ---- 连续点击独立弹出（issue #21 第四轮）----
// 用户报告：点击多次测试通知，只有第一次在系统上弹出，后续点击没有反应。
// 根因：固定 tag 'botler-test' —— 浏览器通知中心对相同 tag 的通知做「替换」
// 而非「新弹」，第一条还在屏幕上时后续同 tag 通知只更新旧条目、不触发新弹出。

// 模拟真实浏览器通知中心行为：相同 tag 的新通知替换已有通知（不触发新弹出），
// 不同 tag 的通知各自独立弹出（popupCount 只统计真正的"弹出"次数）。
function mockNotificationCenter() {
  const center = new Map() // tag -> 通知
  const popupCount = { n: 0 }
  const constructed = []
  const prev = {
    window: globalThis.window, Notification: globalThis.Notification,
    isSecureContext: globalThis.isSecureContext,
  }
  globalThis.window = globalThis
  globalThis.isSecureContext = true
  globalThis.Notification = class {
    static permission = 'granted'
    static requestPermission = async () => 'granted'
    constructor(title, opts) {
      constructed.push({ title, ...opts })
      if (!center.has(opts.tag)) popupCount.n += 1 // 同 tag 替换不触发新弹出
      center.set(opts.tag, { title })
    }
  }
  return {
    constructed, popupCount,
    restore() {
      if (prev.window === undefined) delete globalThis.window
      else globalThis.window = prev.window
      if (prev.Notification === undefined) delete globalThis.Notification
      else globalThis.Notification = prev.Notification
      if (prev.isSecureContext === undefined) delete globalThis.isSecureContext
      else globalThis.isSecureContext = prev.isSecureContext
    },
  }
}

test('sendTestNotification：连续多次点击每次都独立弹出（tag 唯一，不被浏览器合并）', async () => {
  const env = mockNotificationCenter()
  try {
    for (let i = 0; i < 3; i++) await sendTestNotification()
    assert.equal(env.popupCount.n, 3, '三次点击应触发三次系统弹出')
    assert.equal(env.constructed.length, 3)
    // 三次构造的 tag 必须互不相同，浏览器才视为独立通知
    const tags = env.constructed.map((n) => n.tag)
    assert.equal(new Set(tags).size, 3)
  } finally { env.restore() }
})

// ---- 非安全上下文判定（issue #21 第三轮）----
// 根因：http/自签名证书不受信任时 Notification.permission 恒为 'denied' 且
// requestPermission() 永不弹框——必须用 isSecureContext 区分，提示用户换 https。

test('notifyFailureReason：非安全上下文 → insecure-context（即使 permission=denied）', () => {
  const env = mockNotifyEnv({ permission: 'denied', isSecureContext: false })
  try {
    assert.equal(notifyFailureReason(), 'insecure-context')
  } finally { env.restore() }
})

test('notifyFailureReason：安全上下文 + 已拒绝 → denied', () => {
  const env = mockNotifyEnv({ permission: 'denied', isSecureContext: true })
  try {
    assert.equal(notifyFailureReason(), 'denied')
  } finally { env.restore() }
})

test('notifyFailureReason：浏览器不支持 → browser-unsupported', () => {
  const env = mockNotifyEnv({ useSupport: false })
  try {
    assert.equal(notifyFailureReason(), 'browser-unsupported')
  } finally { env.restore() }
})

test('notifyFailureReason：可正常通知（granted/default）→ null', () => {
  const env = mockNotifyEnv({ permission: 'granted' })
  try {
    assert.equal(notifyFailureReason(), null)
  } finally { env.restore() }
  const env2 = mockNotifyEnv({ permission: 'default' })
  try {
    assert.equal(notifyFailureReason(), null)
  } finally { env2.restore() }
})

test('sendTestNotification：非安全上下文 → 不请求授权、不弹通知，返回 insecure-context', async () => {
  let requested = false
  const env = mockNotifyEnv({ permission: 'default', isSecureContext: false })
  const Base = globalThis.Notification
  globalThis.Notification = class extends Base {
    static requestPermission = async () => { requested = true; return 'granted' }
  }
  try {
    const res = await sendTestNotification()
    assert.deepEqual(res, { ok: false, reason: 'insecure-context' })
    assert.equal(env.constructed.length, 0)
    assert.equal(requested, false) // 非安全上下文下不调 requestPermission（弹了也不会有对话框）
  } finally { env.restore() }
})
