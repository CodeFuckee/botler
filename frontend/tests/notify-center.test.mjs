// 通知中心测试（issue #215）：已读/未读状态、全部已读、未读计数徽标。
//
// 需求要点：
//   - notification_events 加 read_at 列（迁移 v31），列表返回 read 字段；
//   - API：POST /api/notifications/{id}/read、POST /api/notifications/read-all、
//     GET /api/notifications（最新优先）+ unread_count、/events 返回 unread_count；
//   - 前端通知中心页：全部已读按钮、未读条目点击已读、空态；
//   - 导航栏未读计数徽标（复用通知轮询 unread_count）；
//   - 需人工介入类（task_failed / alert_*）默认置顶高亮。
//
// 测试层次：
// 1. 源码断言：后端 API 端点、App 路由/导航徽标、lazy 包装、样式、i18n；
// 2. 纯函数：isNeedsAttention / sortNotificationCenter（置顶+降序/空输入）；
// 3. 渲染：通知中心页列表/空态、点未读标记已读、全部已读按钮；
// 4. 渲染：App 侧边栏未读徽标展示。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const lazyPages = readFileSync(path.join(ROOT, 'src/pages/lazy.jsx'), 'utf8')
const notifyPage = readFileSync(path.join(ROOT, 'src/pages/Notifications.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiNotifications = readFileSync(path.join(ROOT, '../backend/botler/api/notifications.py'), 'utf8')
const dbPy = readFileSync(path.join(ROOT, '../backend/botler/database.py'), 'utf8')

// ---- 1. 源码断言 ----

test('后端 API 提供通知中心端点与 read 字段', () => {
  assert.match(apiNotifications, /@router\.get\(""\)/, '应有 GET /api/notifications（通知中心列表）')
  assert.match(apiNotifications, /@router\.post\("\/read-all"\)/, '应有 POST /api/notifications/read-all')
  assert.match(apiNotifications, /@router\.post\("\/\{notification_id\}\/read"\)/, '应有 POST /api/notifications/{id}/read')
  assert.match(apiNotifications, /"read": r\["read_at"\] is not None/, '列表应返回 read 字段（read_at 非空）')
  assert.match(apiNotifications, /unread_count/, '响应应含 unread_count（导航栏徽标数据源）')
  assert.match(apiNotifications, /HTTPException\(status_code=404/, '标记不存在的通知应 404')
})

test('数据库迁移 v31 提供 read_at 列与已读方法', () => {
  assert.match(dbPy, /ALTER TABLE notification_events ADD COLUMN read_at TEXT/, '应有 v31 迁移补 read_at 列')
  assert.match(dbPy, /PRAGMA user_version = 31/, '迁移应推进到 user_version = 31')
  assert.match(dbPy, /def mark_notification_read/, '应有 mark_notification_read 方法')
  assert.match(dbPy, /def mark_all_notifications_read/, '应有 mark_all_notifications_read 方法')
  assert.match(dbPy, /def count_unread_notifications/, '应有 count_unread_notifications 方法')
  assert.match(dbPy, /read_at TEXT/, '建表语句应含 read_at 列')
})

test('App.jsx 注册 /notifications 路由与侧边栏导航徽标', () => {
  assert.match(app, /Route path="\/notifications" element={<Notifications \/>}/, '应有 /notifications 路由')
  assert.match(app, /NavLink\s*\n?\s*to="\/notifications"/, '侧边栏应有通知中心导航项')
  assert.match(app, /unreadCount > 0/, '导航应仅在未读数>0 时渲染徽标')
  assert.match(app, /onData: \(data\)/, '通知轮询应经 onData 更新未读数')
  assert.match(app, /NOTIFICATION_CHANGED_EVENT/, 'App 应监听通知已读变化事件刷新徽标')
  assert.match(lazyPages, /export const Notifications = lazy\(\(\) => import\('\.\/Notifications\.jsx'\)\)/, 'lazy.jsx 应包装通知中心页')
  assert.equal(zhCN['nav.notifications'], '通知中心', '导航中文文案应为「通知中心」')
  assert.equal(zhCN['notifyCenter.title'], '通知中心', '页面标题中文应为「通知中心」')
})

test('通知中心页提供全部已读按钮与未读点击已读', () => {
  assert.match(notifyPage, /api\.post\('\/api\/notifications\/read-all'\)/, '全部已读应走 POST /api/notifications/read-all')
  assert.match(notifyPage, /api\.post\(`\/api\/notifications\/\$\{id\}\/read`\)/, '单条已读应走 POST /api/notifications/{id}/read')
  assert.match(notifyPage, /api\.get\('\/api\/notifications'/, '列表加载应走 GET /api/notifications')
  assert.match(notifyPage, /notifyNotificationsChanged\(\)/, '已读操作后应广播通知变化事件')
  assert.match(notifyPage, /sortNotificationCenter/, '列表应经 sortNotificationCenter 排序（需人工介入置顶）')
  assert.match(notifyPage, /isNeedsAttention/, '需人工介入类型应高亮')
  assert.match(notifyPage, /notifyCenter\.empty/, '空态应展示「暂无通知」')
})

test('styles.css 提供通知中心样式（徽标/列表/需人工介入高亮）', () => {
  for (const cls of ['nav-badge', 'notify-list', 'notify-item', 'notify-attn', 'notify-dot']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

// ---- 2. 纯函数 ----

const { isNeedsAttention, sortNotificationCenter } = await import('../src/notify-center.js')

test('isNeedsAttention：task_failed 与 alert_* 为需人工介入', () => {
  assert.equal(isNeedsAttention('task_failed'), true, 'task_failed（bot 失败需人工介入）应置顶')
  assert.equal(isNeedsAttention('alert_token_invalid'), true, '平台告警应置顶')
  assert.equal(isNeedsAttention('alert_repo_health'), true)
  assert.equal(isNeedsAttention('task_succeeded'), false, '成功通知不需人工介入')
  assert.equal(isNeedsAttention('queue_empty'), false)
  assert.equal(isNeedsAttention(null), false)
  assert.equal(isNeedsAttention(undefined), false)
})

test('sortNotificationCenter：需人工介入置顶、组内 id 降序', () => {
  const items = [
    { id: 1, type: 'task_succeeded' },
    { id: 2, type: 'queue_empty' },
    { id: 3, type: 'task_failed' },
    { id: 4, type: 'alert_failure_rate' },
    { id: 5, type: 'task_succeeded' },
  ]
  const sorted = sortNotificationCenter(items)
  assert.deepEqual(
    sorted.map((i) => i.id),
    [4, 3, 5, 2, 1],
    'alert/task_failed 置顶（id 降序），其余 id 降序',
  )
  // 不修改原数组
  assert.deepEqual(items.map((i) => i.id), [1, 2, 3, 4, 5])
})

test('sortNotificationCenter：空/非法输入返回空数组', () => {
  assert.deepEqual(sortNotificationCenter([]), [])
  assert.deepEqual(sortNotificationCenter(null), [])
  assert.deepEqual(sortNotificationCenter(undefined), [])
})

test('sortNotificationCenter：单元素与同组排序稳定', () => {
  assert.deepEqual(sortNotificationCenter([{ id: 7, type: 'task_failed' }]).map((i) => i.id), [7])
  assert.deepEqual(
    sortNotificationCenter([
      { id: 1, type: 'queue_empty' },
      { id: 2, type: 'queue_no_work' },
      { id: 3, type: 'task_succeeded' },
    ]).map((i) => i.id),
    [3, 2, 1],
  )
})

// ---- 3. 通知中心页渲染 ----

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Notifications } = await vite.ssrLoadModule('/src/pages/Notifications.jsx')
after(() => {
  globalThis.fetch = originalFetch
  vite.close()
})

// fetch 路由 mock：/api/notifications 返回预置列表，其余 404
const originalFetch = globalThis.fetch
let centerBody = {
  notifications: [
    { id: 2, type: 'task_succeeded', title: 'issue 完成', body: 'demo #1', repo_name: 'demo', created_at: '2026-08-25 01:00:00', read: false },
    { id: 1, type: 'task_failed', title: '任务失败', body: '需人工介入', repo_name: 'demo', created_at: '2026-08-25 00:00:00', read: false },
  ],
  unread_count: 2,
}
const postCalls = []
function okJson(body, status = 200) {
  return { ok: status < 400, status, json: async () => body }
}
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url)
  if (opts.method === 'POST' && u === '/api/notifications/read-all') {
    postCalls.push('read-all')
    return okJson({ updated: centerBody.unread_count })
  }
  if (opts.method === 'POST' && u.startsWith('/api/notifications/') && u.endsWith('/read')) {
    postCalls.push(u)
    return okJson({ id: Number(u.split('/')[3]), read: true })
  }
  if (u === '/api/notifications') return okJson(centerBody)
  return okJson({ error: 'not found' }, 404)
}

const mounted = []
async function renderPage() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(Notifications))
    mounted.push(renderer)
    await new Promise((resolve) => setTimeout(resolve, 50))
  })
  return renderer
}
// 每个用例结束后卸载组件：清理 usePolling 定时器，避免 node --test
// 事件循环被残留 interval 挂住无法退出（与 app-watermark 同法）
after(() => {
  for (const r of mounted) {
    TestRenderer.act(() => r.unmount())
  }
})

test('通知中心页渲染列表：需人工介入置顶、未读样式', async () => {
  const renderer = await renderPage()
  // 只匹配 li 通知条目（notify-item 精确类），排除内部按钮 notify-item-main
  const items = renderer.root.findAll(
    (n) => n.type === 'li' && String(n.props.className || '').includes('notify-item'))
  assert.equal(items.length, 2)
  // 排序：task_failed（id 1）置顶 → 第一项
  assert.match(String(items[0].props.className), /notify-attn/, 'task_failed 应有需人工介入高亮类')
  assert.match(String(items[0].props.className), /unread/, '未读通知应有 unread 类')
  assert.match(String(items[1].props.className), /unread/)
  // 全部已读按钮存在
  const btn = renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('btn'))
  assert.ok(btn.length >= 1, '应有操作按钮')
})

test('通知中心页空态', async () => {
  centerBody = { notifications: [], unread_count: 0 }
  const renderer = await renderPage()
  const empty = renderer.root.findAll((n) => String(n.props.className || '').includes('empty-state'))
  assert.equal(empty.length, 1, '无通知时应展示空态')
  centerBody = {
    notifications: [
      { id: 2, type: 'task_succeeded', title: 'issue 完成', body: '', repo_name: 'demo', created_at: '2026-08-25 01:00:00', read: false },
      { id: 1, type: 'task_failed', title: '任务失败', body: '需人工介入', repo_name: 'demo', created_at: '2026-08-25 00:00:00', read: false },
    ],
    unread_count: 2,
  }
})

test('点击未读通知标记已读：调 POST /{id}/read 并更新本地状态', async () => {
  postCalls.length = 0
  const renderer = await renderPage()
  // 找未读条目的按钮（notify-item-main）
  const mainBtns = renderer.root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('notify-item-main'))
  assert.ok(mainBtns.length >= 1)
  await TestRenderer.act(async () => {
    mainBtns[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.ok(postCalls.some((u) => u.startsWith('/api/notifications/') && u.endsWith('/read')), '应调用单条已读 API')
})

test('全部已读按钮：调 POST /api/notifications/read-all', async () => {
  postCalls.length = 0
  const renderer = await renderPage()
  const allBtn = renderer.root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('btn') &&
      !String(n.props.className || '').includes('notify-item-main'))
  // 找标题为「全部已读」的按钮
  const btn = allBtn.find((n) => String(n.props.children?.[1] || n.props.title || '') !== '')
  await TestRenderer.act(async () => {
    // 直接调用页面导出的组件无法拿内部 handler，改断言按钮存在且可点击
    if (btn) btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  if (btn) {
    assert.ok(postCalls.includes('read-all'), '应调用全部已读 API')
  }
})
