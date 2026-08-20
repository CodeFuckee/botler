// 概览页 issue 详情右边栏「评论与活动」合并时间线显示测试（issue #342）：
// 设置页「界面显示」卡片新增开关 botler.timeline（localStorage，默认关闭
// = 分开显示，保持 issue #97 现状），开启后概览页 issue 详情右边栏的评论
// （用户发言）与活动（系统事件）不再分两个区块，而是按时间交错合并为一条
// 时间线（类似 GitLab issue 时间线）。
//
// 断言：
// 1. 源码：IssueDrawer 读取 botler.timeline（loadTimelineEnabled）并渲染
//    timeline-list 时间线容器；设置页 UiCard 渲染「合并显示评论与活动
//    （时间线）」开关行（timeline-toggle-input）；
// 2. lib 单元：buildTimeline 合并排序（正常交错、同时间戳按 id、空/null、
//    异常元素防御、缺 created_at 不崩溃）；load/save 开关（默认关闭、
//    '1' 开、'0' 关、存储不可用兜底、读写正确）；
// 3. 渲染（关闭/未配置）：评论与活动仍分两区块（与 issue #97 现状一致，
//    防回归）；
// 4. 渲染（开启）：单一 timeline-list，评论与活动按时间交错排序，评论
//    条目保留作者/头像/时间/Markdown/回复按钮，活动条目保留 linkify
//    提交链接；
// 5. 边界（开启）：只有评论 / 只有活动 / 空 notes / 加载中 / 加载失败
//    重试 / 缺 project_id 不拉接口；
// 6. 标记活动并入时间线（issue #351）：合并时间线模式下标记事件
//    （谁添加/移除了标记）按时间并入时间线，不再独立成「标记活动」区块。
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
// overview-issue-notes.test.mjs 一致）
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
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')
const {
  TIMELINE_STORAGE_KEY,
  loadTimelineEnabled,
  saveTimelineEnabled,
  buildTimeline,
  buildMergedTimeline,
} = await import(path.join(ROOT, 'src/lib/notesTimeline.js'))

// 捕获 api.get 原始实现：node:test 的 mock.method 多次调用后 restoreAll
// 无法可靠恢复（实测 restoreAll 后 api.get 仍返回 mock 结果，导致设置页
// 拉取 /api/settings 异常、渲染空树），改用手工赋值并在每次使用后恢复
const ORIG_API_GET = api.get.bind(api)

after(() => vite.close())

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const uiCardSrc = readFileSync(path.join(ROOT, 'src/components/settings/UiCard.jsx'), 'utf8')
const hookSrc = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')

// 简单 localStorage 兼容存储（内存实现，与 keymap.test.mjs 同款）
function memStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  }
}

// ---- 1. 源码断言 ----

test('源码：IssueDrawer 读取 botler.timeline 并渲染时间线容器', () => {
  assert.match(drawerSrc, /loadTimelineEnabled/, '应读取合并时间线开关')
  assert.match(drawerSrc, /botler\.timeline/, '应引用存储键 botler.timeline')
  assert.match(drawerSrc, /timeline-list/, '应渲染时间线列表容器')
  assert.match(drawerSrc, /评论与活动（时间线）/, '应渲染时间线区块标题')
  assert.match(drawerSrc, /buildMergedTimeline/, '应把标记活动并入时间线合并排序')
  assert.match(drawerSrc, /timeline-label-event/, '应渲染标记事件时间线节点类名')
})

test('源码：设置页 UiCard 渲染合并时间线开关行', () => {
  assert.match(uiCardSrc, /合并显示评论与活动（时间线）/, '应渲染开关行文案')
  assert.match(uiCardSrc, /botler\.timeline/, '应引用存储键')
  assert.match(uiCardSrc, /timeline-toggle-input/, '开关应为复选框（独立类名便于测试定位）')
  assert.match(uiCardSrc, /saveTimelineEnabled\(/, '切换应持久化')
  assert.match(hookSrc, /timelineEnabled/, 'hook 应提供开关状态')
  assert.match(hookSrc, /loadTimelineEnabled\(/, 'hook 应从 localStorage 初始化')
})

// ---- 2. lib 单元测试 ----

test('buildTimeline：评论与活动按 created_at 升序交错', () => {
  const out = buildTimeline([
    { id: 5, body: '评论3', system: false, created_at: '2026-08-15 11:00:00' },
    { id: 3, body: '活动2', system: true, created_at: '2026-08-15 09:00:00' },
    { id: 4, body: '评论2', system: false, created_at: '2026-08-15 10:00:00' },
    { id: 1, body: '活动1', system: true, created_at: '2026-08-15 08:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [1, 3, 4, 5], '应按时间交错排序')
  assert.deepEqual(out.map((n) => n.body),
                   ['活动1', '活动2', '评论2', '评论3'], '顺序应为活动→活动→评论→评论')
})

test('buildTimeline：同一时刻按 note id 升序（排序稳定）', () => {
  const out = buildTimeline([
    { id: 9, body: 'b', system: false, created_at: '2026-08-15 10:00:00' },
    { id: 2, body: 'a', system: true, created_at: '2026-08-15 10:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [2, 9], '同时间戳应按 id 升序')
})

test('buildTimeline：空/null/异常元素防御', () => {
  assert.deepEqual(buildTimeline(null), [], 'null 应返回空数组')
  assert.deepEqual(buildTimeline(undefined), [], 'undefined 应返回空数组')
  assert.deepEqual(buildTimeline([]), [], '空数组应返回空数组')
  const out = buildTimeline([
    null,
    'bad',
    { body: '缺 id', system: false, created_at: '2026-08-15 10:00:00' },
    { id: 7, body: 'ok', system: false, created_at: '2026-08-15 09:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [7], '异常元素应跳过')
})

test('buildTimeline：缺 created_at 不崩溃（按空串排最前）', () => {
  const out = buildTimeline([
    { id: 2, body: '有时间', system: false, created_at: '2026-08-15 09:00:00' },
    { id: 1, body: '无时间', system: true },
  ])
  assert.deepEqual(out.map((n) => n.id), [1, 2], '缺 created_at 应排最前且不崩溃')
})

test('buildMergedTimeline：评论/活动/标记事件按时间交错，_kind 标注类型（issue #351）', () => {
  const out = buildMergedTimeline([
    { id: 5, body: '评论', system: false, created_at: '2026-08-15 11:00:00' },
    { id: 3, body: '活动', system: true, created_at: '2026-08-15 09:00:00' },
  ], [
    { id: 4, action: 'add', label: 'feature', created_at: '2026-08-15 10:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [3, 4, 5], '应按时间交错排序')
  assert.deepEqual(out.map((n) => n._kind),
                   ['activity', 'label', 'comment'], '应标注活动/标记/评论类型')
  assert.equal(out[1].label, 'feature', '标记事件应保留 label 字段')
  assert.equal(out[0].body, '活动', '活动应保留 body 字段')
})

test('buildMergedTimeline：同一时刻按 id 升序（排序稳定）', () => {
  const out = buildMergedTimeline([
    { id: 9, body: 'b', system: false, created_at: '2026-08-15 10:00:00' },
  ], [
    { id: 2, action: 'add', label: 'x', created_at: '2026-08-15 10:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [2, 9], '同时间戳应按 id 升序')
  assert.deepEqual(out.map((n) => n._kind), ['label', 'comment'], '类型标注应正确')
})

test('buildMergedTimeline：空/null/异常元素防御', () => {
  assert.deepEqual(buildMergedTimeline(null, null), [], 'null 应返回空数组')
  assert.deepEqual(buildMergedTimeline(undefined, undefined), [], 'undefined 应返回空数组')
  assert.deepEqual(buildMergedTimeline([], []), [], '空数组应返回空数组')
  const out = buildMergedTimeline([
    null,
    'bad',
    { body: '缺 id', system: false, created_at: '2026-08-15 10:00:00' },
    { id: 7, body: 'ok', system: false, created_at: '2026-08-15 09:00:00' },
  ], [
    null,
    42,
    { action: 'add', label: '缺id' },
    { id: 8, action: 'remove', label: 'ok', created_at: '2026-08-15 08:00:00' },
  ])
  assert.deepEqual(out.map((n) => n.id), [8, 7], '异常元素应跳过，notes 与标记事件均保留')
  assert.deepEqual(out.map((n) => n._kind), ['label', 'comment'], '类型标注应正确')
})

test('buildMergedTimeline：缺 created_at 不崩溃（按空串排最前）', () => {
  const out = buildMergedTimeline([
    { id: 2, body: '有时间', system: false, created_at: '2026-08-15 09:00:00' },
  ], [
    { id: 1, action: 'add', label: '无时间' },
  ])
  assert.deepEqual(out.map((n) => n.id), [1, 2], '缺 created_at 应排最前且不崩溃')
})

test('loadTimelineEnabled：默认关闭，仅显式 1 开启', () => {
  assert.equal(loadTimelineEnabled(null), false, '无存储环境应兜底关闭')
  assert.equal(loadTimelineEnabled(undefined), false, 'undefined 存储应兜底关闭')
  assert.equal(loadTimelineEnabled(memStorage()), false, '未写入应默认关闭（分开显示）')
  assert.equal(loadTimelineEnabled(memStorage({ [TIMELINE_STORAGE_KEY]: '1' })), true, '1 应开启')
  assert.equal(loadTimelineEnabled(memStorage({ [TIMELINE_STORAGE_KEY]: '0' })), false, '0 应关闭')
  assert.equal(loadTimelineEnabled(memStorage({ [TIMELINE_STORAGE_KEY]: 'yes' })), false, '非法值应关闭')
})

test('loadTimelineEnabled：getItem 抛异常（隐私模式）兜底关闭', () => {
  const broken = { getItem: () => { throw new Error('denied') } }
  assert.equal(loadTimelineEnabled(broken), false, '读取异常应兜底关闭')
})

test('saveTimelineEnabled：写 1/0，存储不可用静默忽略', () => {
  const storage = memStorage()
  saveTimelineEnabled(storage, true)
  assert.equal(storage.getItem(TIMELINE_STORAGE_KEY), '1', '开启应写 1')
  saveTimelineEnabled(storage, false)
  assert.equal(storage.getItem(TIMELINE_STORAGE_KEY), '0', '关闭应写 0')
  assert.doesNotThrow(() => saveTimelineEnabled(null, true), '无存储环境不抛错')
  const broken = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveTimelineEnabled(broken, true), '写入异常静默忽略')
})

// ---- 3/4/5. IssueDrawer 渲染 ----

const OPEN_ISSUE = {
  project_id: 42,
  iid: 342,
  title: '合并时间线显示评论与活动',
  state: 'opened',
  updated_at: '2026-08-15 18:00:00',
  created_at: '2026-08-15 18:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/342',
  description: '需求描述',
}

const COMMENT_1 = {
  id: 201, body: '**确认** 该方案可行', system: false,
  author: { name: 'code01', username: 'project_bot',
            avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-15 10:00:00',
}
const COMMENT_2 = {
  id: 203, body: '普通评论文本', system: false,
  author: { name: 'code02', username: 'user2',
            avatar_url: 'https://gitlab.example.com/b.png' },
  created_at: '2026-08-15 11:00:00',
}
const ACTIVITY_1 = {
  id: 202, body: 'assigned to @agent', system: true,
  author: { name: 'code01', username: 'project_bot',
            avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-15 09:00:00',
}
const ACTIVITY_2 = {
  id: 204, body: `commit 0123456789abcdef0123456789abcdef01234567`, system: true,
  author: { name: 'code01', username: 'project_bot' },
  created_at: '2026-08-15 12:00:00',
}

// 渲染 IssueDrawer 并等待 detail 拉取完成（notes 为返回数据；storage
// 为注入的 localStorage 兼容对象，null 表示不设置（SSR 默认无存储））
async function renderDrawer(issue, notes, storage = null, getImpl = null) {
  if (storage !== null) globalThis.localStorage = storage
  const impl = getImpl || (async (pathname) => {
    if (pathname === `/api/issues/${issue.project_id}/${issue.iid}/detail`) {
      return { notes }
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

test('默认（未开启）：评论与活动仍分两区块，与 issue #97 现状一致', async () => {
  // 不注入 localStorage（SSR 无存储）→ 分开显示
  const { root } = await renderDrawer(OPEN_ISSUE, [COMMENT_1, ACTIVITY_1], null)
  const text = drawerText(root)
  assert.ok(text.includes('评论'), '应渲染「评论」区块标题')
  assert.ok(text.includes('活动'), '应渲染「活动」区块标题')
  assert.ok(!text.includes('评论与活动（时间线）'), '不应渲染时间线标题')
  assert.equal(findByClass(root, 'timeline-list').length, 0, '不应渲染时间线容器')
  assert.equal(findByClass(root, 'comment-item').length, 1, '评论应进评论区')
  assert.equal(findByClass(root, 'activity-item').length, 1, '活动应进活动区')
})

test('开启后：评论与活动合并为一条时间线并按时间交错排序', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE,
    [COMMENT_1, ACTIVITY_1, COMMENT_2, ACTIVITY_2],
    memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  const text = drawerText(root)
  assert.ok(text.includes('评论与活动（时间线）'), '应渲染时间线区块标题')
  assert.equal(findByClass(root, 'timeline-list').length, 1, '应渲染单一时间线容器')
  assert.equal(findByClass(root, 'comment-list').length, 0, '不应渲染分开的评论区')
  assert.equal(findByClass(root, 'activity-list').length, 0, '不应渲染分开的活动区')
  // 交错顺序：活动(09:00) → 评论(10:00) → 评论(11:00) → 活动(12:00)
  const items = findByClass(root, 'timeline-item').map((n) => toText(n))
  assert.equal(items.length, 4, '时间线应含全部 4 条')
  assert.ok(items[0].includes('assigned to @agent'), '第 1 条应为 09:00 活动')
  assert.ok(items[1].includes('确认'), '第 2 条应为 10:00 评论（Markdown 渲染为纯文本）')
  assert.ok(items[2].includes('普通评论文本'), '第 3 条应为 11:00 评论')
  assert.ok(items[3].includes('0123456789abcdef'), '第 4 条应为 12:00 活动')
})

test('开启后：评论条目保留作者/头像/时间/Markdown/回复按钮', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [COMMENT_1], memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  const text = drawerText(root)
  assert.ok(text.includes('code01'), '应显示评论作者名')
  assert.ok(text.includes('2026-08-15'), '应显示评论时间（fmtTime 格式化）')
  assert.equal(findByClass(root, 'comment-avatar').length, 1, '应显示作者头像')
  const strongs = root.findAll((n) => n.type === 'strong')
  assert.ok(strongs.some((s) => toText(s).includes('确认')),
            'Markdown 粗体应渲染为 strong 且含「确认」')
  assert.equal(findByClass(root, 'comment-reply-btn').length, 1, '应保留回复按钮')
})

test('开启后：活动条目保留 linkify 提交链接', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [ACTIVITY_2], memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  const links = findByClass(root, 'commit-link')
  assert.equal(links.length, 1, '活动中的提交 SHA 应渲染为链接')
  assert.match(links[0].props.href, /\/-\/commit\//, '链接应指向 GitLab 提交页')
})

test('开启后：只有评论 / 只有活动均正常渲染', async () => {
  const onlyComments = await renderDrawer(OPEN_ISSUE, [COMMENT_1, COMMENT_2],
    memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  assert.equal(findByClass(onlyComments.root, 'timeline-item').length, 2, '只有评论应渲染 2 条')
  assert.equal(findByClass(onlyComments.root, 'timeline-comment').length, 2, '应为评论节点')
  const onlyActivities = await renderDrawer(OPEN_ISSUE, [ACTIVITY_1],
    memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  assert.equal(findByClass(onlyActivities.root, 'timeline-item').length, 1, '只有活动应渲染 1 条')
  assert.equal(findByClass(onlyActivities.root, 'timeline-activity').length, 1, '应为活动节点')
})

test('开启后：标记活动并入时间线按时间交错（issue #351）', async () => {
  // detail 返回 活动(09:00) + 评论(10:00) + 标记活动(10:30)：时间线应为
  // 活动 → 评论 → 标记活动，标记事件为文本节点（timeline-label-event），
  // 且不再渲染独立的「标记活动」区块
  const LABEL_EVENT = {
    id: 205, action: 'add', label: 'feature',
    user: { name: 'code01', username: 'code01' },
    created_at: '2026-08-15 10:30:00',
  }
  const { root } = await renderDrawer(OPEN_ISSUE, [COMMENT_1, ACTIVITY_1],
    memStorage({ [TIMELINE_STORAGE_KEY]: '1' }),
    async (pathname) => {
      if (pathname === `/api/issues/${OPEN_ISSUE.project_id}/${OPEN_ISSUE.iid}/detail`) {
        return { notes: [ACTIVITY_1, COMMENT_1], label_events: [LABEL_EVENT] }
      }
      throw new Error('unexpected ' + pathname)
    })
  const items = findByClass(root, 'timeline-item').map((n) => toText(n))
  assert.equal(items.length, 3, '时间线应含 3 条（活动/评论/标记活动）')
  assert.ok(items[0].includes('assigned to @agent'), '第 1 条应为 09:00 活动')
  assert.ok(items[1].includes('确认'), '第 2 条应为 10:00 评论')
  assert.ok(items[2].includes('code01 添加了标记 feature'), '第 3 条应为 10:30 标记活动')
  assert.equal(findByClass(root, 'timeline-label-event').length, 1, '应渲染标记事件时间线节点')
  assert.equal(findByClass(root, 'label-events-list').length, 0, '不应再渲染独立标记活动列表')
  assert.ok(!drawerText(root).includes('标记活动'), '不应再显示独立「标记活动」区块标题')
})

test('开启后：空 notes 与空标记活动显示「暂无评论、活动与标记活动」占位（issue #351）', async () => {
  const { root } = await renderDrawer(OPEN_ISSUE, [], memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  assert.ok(drawerText(root).includes('暂无评论、活动与标记活动'), '空时间线应显示合并占位文案')
})

test('开启后：加载中/加载失败重试/缺 project_id 兜底', async () => {
  // 加载中：notes 为 null（api.get 挂起）——先渲染，detail 未返回
  globalThis.localStorage = memStorage({ [TIMELINE_STORAGE_KEY]: '1' })
  api.get = async () => new Promise(() => {})  // 永不 resolve：detail 保持加载态
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(IssueDrawer, {
      issue: OPEN_ISSUE, repoName: 'botler', onClose: () => {}, onIssueClosed: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 5))
  })
  try {
    assert.ok(drawerText(renderer.root).includes('加载中…'), '加载中应显示占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    delete globalThis.localStorage
    api.get = ORIG_API_GET
  }
  // 加载失败 + 重试：首次抛错显示错误横幅，重试成功后渲染时间线。
  // 内联渲染保持 api.get mock 直到重试点击完成（renderDrawer 的 finally
  // 会立即恢复原始 api.get，重试点击将打到真实 fetch 导致失败）
  let calls = 0
  globalThis.localStorage = memStorage({ [TIMELINE_STORAGE_KEY]: '1' })
  api.get = async () => {
    calls += 1
    if (calls === 1) throw new Error('网络错误')
    return { notes: [COMMENT_1, ACTIVITY_1] }
  }
  let retryRenderer = null
  await TestRenderer.act(async () => {
    retryRenderer = TestRenderer.create(React.createElement(IssueDrawer, {
      issue: OPEN_ISSUE, repoName: 'botler', onClose: () => {}, onIssueClosed: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  try {
    const root = retryRenderer.root
    assert.ok(drawerText(root).includes('网络错误'), '应显示错误信息')
    const retry = findByClass(root, 'notes-retry')
    assert.equal(retry.length, 1, '应显示重试按钮')
    await TestRenderer.act(async () => {
      retry[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findByClass(root, 'timeline-item').length, 2, '重试成功后应渲染时间线')
    assert.equal(findByClass(root, 'timeline-list').length, 1, '重试后仍在时间线模式')
  } finally {
    await TestRenderer.act(() => retryRenderer.unmount())
    api.get = ORIG_API_GET
    delete globalThis.localStorage
  }
  // 缺 project_id 旧数据：不调接口，显示占位
  const legacy = await renderDrawer({ ...OPEN_ISSUE, project_id: undefined }, [], memStorage({ [TIMELINE_STORAGE_KEY]: '1' }))
  assert.ok(drawerText(legacy.root).includes('无法加载（缺少仓库信息）'), '缺 project_id 应显示占位')
})

// ---- 设置页开关 ----

test('设置页：合并时间线开关默认关闭，切换写 1/0', async () => {
  // 前序抽屉测试会残留 api.get mock，先恢复原始实现，避免设置页拉取
  // /api/settings 被 mock 拦截（settings 加载失败页面停在加载态）
  api.get = ORIG_API_GET
  const storage = memStorage()
  globalThis.localStorage = storage
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    const pathname = String(p)
    if (opts?.method === 'PUT') return { ok: true, status: 200, json: async () => ({}) }
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          worker: { issue_priority: ['bug'] }, sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '', theme: 'system' }, notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {}, ai_providers: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-18 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const toggle = () => renderer.root.findAll(
      (n) => n.type === 'input' && n.props.type === 'checkbox')
      .find((t) => String(t.props.className || '').includes('timeline-toggle-input'))
    assert.ok(toggle(), '应渲染合并时间线复选框')
    assert.equal(toggle().props.checked, false, '未配置默认应不勾选（分开显示）')
    TestRenderer.act(() => toggle().props.onChange({ target: { checked: true } }))
    assert.equal(storage.getItem(TIMELINE_STORAGE_KEY), '1', '勾选应写 1')
    assert.equal(toggle().props.checked, true, '勾选后状态应更新')
    TestRenderer.act(() => toggle().props.onChange({ target: { checked: false } }))
    assert.equal(storage.getItem(TIMELINE_STORAGE_KEY), '0', '取消勾选应写 0')
  } finally {
    global.fetch = originalFetch
    delete globalThis.localStorage
    await TestRenderer.act(() => renderer.unmount())
  }
})
