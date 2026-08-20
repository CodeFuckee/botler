// 概览页 issue 详情右边栏「标记活动」显示测试（issue #349）：
// 展示谁在什么时间添加/移除了哪个标记——数据源 GitLab
// resource_label_events（后端 detail 接口独立拉取精简后随
// label_events 字段返回，notes 系统活动不含标记加/删事件，实测）。
//
// 断言：
// 1. 源码：IssueDrawer 读取 label_events 并渲染「标记活动」区块
//    （label-events-list / label-event-item）；lib 提供归一化与文案；
// 2. lib 单元：labelEventText（add/remove/未知动作/缺 user/缺标记名
//    兜底）、labelActorName（name 优先回退 username）、buildLabelEvents
//    （排序/空/null/异常元素防御/缺 created_at 不崩溃）；
// 3. 渲染：正常（操作人 + 添加/移除了标记 + 标记名 + 时间）/ 空 /
//    加载中 / 加载失败 / 异常元素跳过；时间线模式开启（botler.timeline）时
//    标记活动并入时间线（issue #351），不再独立展示。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-issue-timeline.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const {
  LABEL_ACTION_META,
  labelActorName,
  labelEventText,
  buildLabelEvents,
} = await import(path.join(ROOT, 'src/lib/labelEvents.js'))

// 捕获 api.get 原始实现（与 overview-issue-timeline.test.mjs 同款：
// node:test 的 mock 恢复不可靠，改用手工赋值并在每次使用后恢复）
const ORIG_API_GET = api.get.bind(api)

after(() => vite.close())

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const libSrc = readFileSync(path.join(ROOT, 'src/lib/labelEvents.js'), 'utf8')

// ---- 1. 源码断言 ----

test('源码：IssueDrawer 渲染标记活动区块并读取 label_events', () => {
  assert.match(drawerSrc, /label_events/, '应读取 detail 响应的 label_events 字段')
  assert.match(drawerSrc, /标记活动/, '应渲染「标记活动」区块标题')
  assert.match(drawerSrc, /label-events-list/, '应渲染标记活动列表容器')
  assert.match(drawerSrc, /label-event-item/, '应渲染标记活动条目类名')
  assert.match(drawerSrc, /buildLabelEvents/, '应按时间归一化排序')
  assert.match(drawerSrc, /labelEventText/, '应使用标记事件文案函数')
})

test('源码：labelEvents lib 提供动作文案/操作人/归一化函数', () => {
  assert.match(libSrc, /LABEL_ACTION_META/, '应导出动作文案映射')
  assert.ok(LABEL_ACTION_META.add.text.includes('添加了标记'), 'add 文案应为「添加了标记」')
  assert.ok(LABEL_ACTION_META.remove.text.includes('移除了标记'), 'remove 文案应为「移除了标记」')
  assert.equal(typeof labelActorName, 'function', '应导出 labelActorName')
  assert.equal(typeof labelEventText, 'function', '应导出 labelEventText')
  assert.equal(typeof buildLabelEvents, 'function', '应导出 buildLabelEvents')
})

// ---- 2. lib 单元测试 ----

test('labelEventText：添加/移除动作文案', () => {
  assert.equal(
    labelEventText({ id: 1, action: 'add', label: 'feature',
                     user: { name: 'chenkaidi', username: 'chenkaidi' } }),
    'chenkaidi 添加了标记 feature')
  assert.equal(
    labelEventText({ id: 2, action: 'remove', label: 'ui',
                     user: { name: 'code01', username: 'code01' } }),
    'code01 移除了标记 ui')
})

test('labelEventText：未知动作/缺 user/缺标记名兜底', () => {
  assert.equal(
    labelEventText({ id: 3, action: 'update', label: 'feature',
                     user: { name: 'a' } }),
    'a 变更了标记 feature', '未知动作应兜底「变更了标记」')
  assert.equal(
    labelEventText({ id: 4, action: 'add', label: 'feature' }),
    '— 添加了标记 feature', '缺 user 应显示「—」')
  assert.equal(
    labelEventText({ id: 5, action: 'add', user: { name: 'a' } }),
    'a 添加了标记 标记', '缺标记名应显示「标记」兜底')
  assert.equal(
    labelEventText(null), '— 变更了标记 标记', '非对象输入不崩溃')
})

test('labelActorName：name 优先回退 username，全无显示「—」', () => {
  assert.equal(labelActorName({ user: { name: '张三', username: 'zhangsan' } }), '张三')
  assert.equal(labelActorName({ user: { username: 'zhangsan' } }), 'zhangsan')
  assert.equal(labelActorName({ user: {} }), '—')
  assert.equal(labelActorName({}), '—', '缺 user 对象应显示「—」')
  assert.equal(labelActorName(null), '—', '非对象输入不崩溃')
})

test('buildLabelEvents：按 created_at 升序、同一时刻按 id 升序', () => {
  const out = buildLabelEvents([
    { id: 3, action: 'add', label: 'c', created_at: '2026-08-15 11:00:00' },
    { id: 1, action: 'add', label: 'a', created_at: '2026-08-15 09:00:00' },
    { id: 2, action: 'remove', label: 'b', created_at: '2026-08-15 10:00:00' },
  ])
  assert.deepEqual(out.map((e) => e.id), [1, 2, 3], '应按时间升序')
  const same = buildLabelEvents([
    { id: 9, label: 'x', created_at: '2026-08-15 10:00:00' },
    { id: 2, label: 'y', created_at: '2026-08-15 10:00:00' },
  ])
  assert.deepEqual(same.map((e) => e.id), [2, 9], '同一时刻应按 id 升序')
})

test('buildLabelEvents：空/null/异常元素防御', () => {
  assert.deepEqual(buildLabelEvents(null), [], 'null 应返回空数组')
  assert.deepEqual(buildLabelEvents(undefined), [], 'undefined 应返回空数组')
  assert.deepEqual(buildLabelEvents([]), [], '空数组应返回空数组')
  const out = buildLabelEvents([
    null,
    'bad',
    { action: 'add', label: 'x' },  // 缺 id
    { id: 7, action: 'remove', label: 'ok' },
  ])
  assert.deepEqual(out.map((e) => e.id), [7], '异常元素应跳过')
})

test('buildLabelEvents：缺 created_at 不崩溃（按空串排最前）', () => {
  const out = buildLabelEvents([
    { id: 2, action: 'add', label: '有时间', created_at: '2026-08-15 09:00:00' },
    { id: 1, action: 'add', label: '无时间' },
  ])
  assert.deepEqual(out.map((e) => e.id), [1, 2], '缺 created_at 应排最前且不崩溃')
})

// ---- 3. IssueDrawer 渲染 ----

const OPEN_ISSUE = {
  project_id: 42,
  iid: 349,
  title: '右边栏显示标记相关活动',
  state: 'opened',
  updated_at: '2026-08-15 18:00:00',
  created_at: '2026-08-15 18:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/349',
  description: '展示谁添加了标记，谁移除了标记',
}

const LABEL_EVENT_ADD = {
  id: 1, action: 'add', label: 'feature',
  user: { name: 'chenkaidi', username: 'chenkaidi',
          avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-15 10:00:00',
}
const LABEL_EVENT_REMOVE = {
  id: 2, action: 'remove', label: 'ui',
  user: { name: 'code01', username: 'code01',
          avatar_url: 'https://gitlab.example.com/b.png' },
  created_at: '2026-08-15 11:00:00',
}

// 渲染 IssueDrawer 并等待 detail 拉取完成（detail 返回 notes 与
// label_events；getImpl 可注入返回/抛错/挂起以覆盖加载中与失败态）
async function renderDrawer(issue, labelEvents, notes = [], getImpl = null) {
  const impl = getImpl || (async (pathname) => {
    if (pathname === `/api/issues/${issue.project_id}/${issue.iid}/detail`) {
      return { notes, label_events: labelEvents }
    }
    throw new Error('unexpected ' + pathname)
  })
  api.get = impl
  let renderer = null
  let renderError = null
  try {
    await TestRenderer.act(async () => {
      try {
        renderer = TestRenderer.create(React.createElement(IssueDrawer, {
          issue, repoName: 'botler', onClose: () => {}, onIssueClosed: () => {},
        }))
        await new Promise((resolve) => setTimeout(resolve, 10))
      } catch (e) {
        renderError = e
      }
    })
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    return { renderer, root: renderer.root }
  } finally {
    api.get = ORIG_API_GET
  }
}

// 渲染树 → 纯文本（与 overview-issue-drawer.test.mjs 的 toText 一致）
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') {
    const children = node.children ?? node.props?.children
    return toText(children)
  }
  return ''
}

function drawerText(root) {
  return toText(root.children)
}

function findByClass(root, cls) {
  return root.findAll(
    (n) => typeof n.props?.className === 'string'
      && n.props.className.split(/\s+/).includes(cls))
}

test('渲染：标记活动区块展示操作人/动作/标记名/时间', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [LABEL_EVENT_ADD, LABEL_EVENT_REMOVE])
  const text = drawerText(root)
  assert.ok(text.includes('标记活动'), '应渲染「标记活动」区块标题')
  assert.ok(text.includes('chenkaidi 添加了标记 feature'), '应展示添加事件文案')
  assert.ok(text.includes('code01 移除了标记 ui'), '应展示移除事件文案')
  assert.equal(findByClass(root, 'label-events-list').length, 1, '应渲染标记活动列表')
  const items = findByClass(root, 'label-event-item')
  assert.equal(items.length, 2, '应渲染两条标记事件')
  assert.ok(toText(items[0]).includes('2026-08-15'), '第 1 条应显示事件时间')
  assert.ok(toText(items[1]).includes('2026-08-15'), '第 2 条应显示事件时间')
})

test('渲染：无标记活动 → 空占位，不影响评论/活动', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [])
  const text = drawerText(root)
  assert.ok(text.includes('暂无标记活动'), '应显示空占位文案')
  assert.ok(text.includes('评论'), '评论区块不受影响')
  assert.ok(text.includes('活动'), '活动区块不受影响')
  assert.equal(findByClass(root, 'label-event-item').length, 0, '不应渲染事件条目')
})

test('渲染：detail 加载中 → 标记活动区块显示加载中', async () => {
  // getImpl 挂起（永不 resolve），10ms 后仍处于加载中态
  const { root } = await renderDrawer(OPEN_ISSUE, [], [], async () => new Promise(() => {}))
  const text = drawerText(root)
  assert.ok(text.includes('加载中…'), '应显示加载中占位')
  assert.equal(findByClass(root, 'label-event-item').length, 0, '不应渲染事件条目')
})

test('渲染：detail 加载失败 → 标记活动区块显示加载失败 + 重试横幅', async () => {
  const { root } = await renderDrawer(
    OPEN_ISSUE, [], [], async () => { throw new Error('模拟加载失败') })
  const text = drawerText(root)
  assert.ok(text.includes('加载失败'), '应显示加载失败占位')
  assert.equal(findByClass(root, 'notes-retry').length, 1, '应渲染重试按钮')
})

test('渲染：异常事件元素防御性跳过，正常事件仍展示', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [
    LABEL_EVENT_ADD,
    null,
    'bad',
    { action: 'add', label: '缺id' },
  ])
  const items = findByClass(root, 'label-event-item')
  assert.equal(items.length, 1, '异常元素应跳过，只渲染合法事件')
  assert.ok(toText(items[0]).includes('chenkaidi 添加了标记 feature'), '合法事件应正常展示')
})

test('渲染：合并时间线模式开启时，标记活动并入时间线，不再独立展示（issue #351）', async () => {
  // 注入 localStorage 开启 botler.timeline（与 timeline 测试同模式）
  const storage = { getItem: (k) => (k === 'botler.timeline' ? '1' : null) }
  globalThis.localStorage = storage
  try {
    const { root } = await renderDrawer(OPEN_ISSUE, [LABEL_EVENT_ADD])
    const text = drawerText(root)
    assert.ok(text.includes('评论与活动（时间线）'), '应渲染合并时间线区块')
    assert.ok(text.includes('chenkaidi 添加了标记 feature'), '标记事件应并入时间线展示')
    assert.equal(findByClass(root, 'timeline-label-event').length, 1, '应渲染标记事件时间线节点')
    assert.ok(!text.includes('标记活动'), '不应再渲染独立的「标记活动」区块标题')
    assert.equal(findByClass(root, 'label-events-list').length, 0, '不应再渲染独立标记活动列表')
  } finally {
    delete globalThis.localStorage
  }
})

test('渲染：合并时间线模式下标记活动与评论/活动按时间交错（issue #351）', async () => {
  // 注入 localStorage 开启 botler.timeline；detail 返回 1 条评论(10:30) +
  // 1 条标记活动(11:00) + 1 条活动(12:00)，时间线应严格按时间交错
  const storage = { getItem: (k) => (k === 'botler.timeline' ? '1' : null) }
  globalThis.localStorage = storage
  try {
    const { root } = await renderDrawer(OPEN_ISSUE, [LABEL_EVENT_REMOVE], [
      { id: 201, body: '一条评论', system: false,
        author: { name: 'u', username: 'u' }, created_at: '2026-08-15 10:30:00' },
      { id: 202, body: '一条活动', system: true,
        author: { name: 'u', username: 'u' }, created_at: '2026-08-15 12:00:00' },
    ])
    const items = findByClass(root, 'timeline-item').map((n) => toText(n))
    assert.equal(items.length, 3, '时间线应含评论/标记活动/活动 3 条')
    assert.ok(items[0].includes('一条评论'), '第 1 条应为 10:30 评论')
    assert.ok(items[1].includes('code01 移除了标记 ui'), '第 2 条应为 11:00 标记活动')
    assert.ok(items[2].includes('一条活动'), '第 3 条应为 12:00 活动')
  } finally {
    delete globalThis.localStorage
  }
})

test('渲染：合并时间线模式下仅标记活动 → 全部并入时间线，无独立区块（issue #351）', async () => {
  const storage = { getItem: (k) => (k === 'botler.timeline' ? '1' : null) }
  globalThis.localStorage = storage
  try {
    const { root } = await renderDrawer(OPEN_ISSUE, [LABEL_EVENT_ADD, LABEL_EVENT_REMOVE])
    const items = findByClass(root, 'timeline-item')
    assert.equal(items.length, 2, '仅标记活动也应全部并入时间线')
    assert.ok(toText(items[0]).includes('添加了标记 feature'), '第 1 条应为添加事件')
    assert.ok(toText(items[1]).includes('移除了标记 ui'), '第 2 条应为移除事件')
    assert.equal(findByClass(root, 'label-events-list').length, 0, '不应渲染独立列表')
  } finally {
    delete globalThis.localStorage
  }
})
