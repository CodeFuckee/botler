// 网页通知模块测试（issue #21）：事件过滤按设置开关、未知类型、
// 轮询游标推进与首次不弹语义。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  NOTIFY_TYPE_MAP,
  filterNotifyEvents,
  createNotifyPoller,
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
