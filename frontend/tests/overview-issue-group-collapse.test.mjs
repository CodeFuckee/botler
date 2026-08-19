// 概览页「开放 Issue」分组折叠/展开测试（issue #285）：bot-failed /
// bot-done / 其他（含 running）分组头部增加折叠开关，折叠后隐藏组内
// issue 列表、保留组标题与计数，方便用户折叠长列表；折叠偏好存
// localStorage（键 botler.overview.collapsedGroups），刷新后保持。
//
// 断言：
// 1. 纯函数：loadCollapsedGroups 的解析与规范化边界（无存储/损坏数据/
//    未知分组 key 剔除）、saveCollapsedGroups 的写入与静默兜底、
//    toggleGroupCollapsed 的新 Set 切换语义（不改动入参）；
// 2. 渲染：默认全展开（折叠按钮 + issue 列表齐全）、点击折叠隐藏组内
//    列表但保留标题与计数、再次点击展开恢复、组间互不影响、折叠按钮
//    aria-expanded 状态正确、localStorage 预置折叠态初始生效、点击后
//    写入持久化。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issue-groups.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, loadCollapsedGroups, saveCollapsedGroups,
        toggleGroupCollapsed, GROUP_COLLAPSE_STORAGE_KEY, ISSUE_GROUPS } =
  await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// 渲染树节点 → 纯文本（递归；Lucide 图标组件无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

/** 简单内存 storage 替身（localStorage 子集） */
function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  }
}

// ---- 数据流源码断言 ----

test('Overview.jsx 导出折叠函数与存储键，渲染使用折叠逻辑', () => {
  assert.equal(typeof loadCollapsedGroups, 'function', '应导出 loadCollapsedGroups')
  assert.equal(typeof saveCollapsedGroups, 'function', '应导出 saveCollapsedGroups')
  assert.equal(typeof toggleGroupCollapsed, 'function', '应导出 toggleGroupCollapsed')
  assert.equal(GROUP_COLLAPSE_STORAGE_KEY, 'botler.overview.collapsedGroups',
               '存储键应为 botler.overview.collapsedGroups')
  assert.deepEqual(ISSUE_GROUPS.map((g) => g.key),
                   ['running', 'failed', 'done', 'other'],
                   '折叠状态以 ISSUE_GROUPS 分组 key 为准')
  assert.match(overview, /collapsedGroups\.has\(g\.key\)/,
               '渲染应按折叠状态隐藏组内列表')
  assert.match(overview, /toggleGroupCollapsed\(prev, g\.key\)/,
               '折叠按钮点击应调用 toggleGroupCollapsed')
  assert.match(overview, /issue-group-toggle/, '组头应渲染折叠开关按钮')
})

// ---- loadCollapsedGroups 纯函数测试 ----

test('loadCollapsedGroups：无存储 / storage 为 null / 未存值 / 空串 → 空 Set（全展开）', () => {
  assert.deepEqual(loadCollapsedGroups(undefined), new Set(), '无 storage 应回空 Set')
  assert.deepEqual(loadCollapsedGroups(null), new Set(), 'storage 为 null 应回空 Set')
  assert.deepEqual(loadCollapsedGroups(makeStorage()), new Set(), '未存值应回空 Set')
  assert.deepEqual(loadCollapsedGroups(makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: '' })),
                   new Set(), '空串应回空 Set')
})

test('loadCollapsedGroups：非法 JSON / 非数组 → 空 Set', () => {
  assert.deepEqual(loadCollapsedGroups(makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: '{oops' })),
                   new Set(), '非法 JSON 应回空 Set')
  assert.deepEqual(loadCollapsedGroups(makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: '"str"' })),
                   new Set(), '解析为非数组应回空 Set')
  assert.deepEqual(loadCollapsedGroups(makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: '42' })),
                   new Set(), '解析为数字应回空 Set')
  assert.deepEqual(loadCollapsedGroups(makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: '{}' })),
                   new Set(), '解析为对象应回空 Set')
})

test('loadCollapsedGroups：合法数组解析为 Set，未知 key / 非字符串元素剔除', () => {
  const s = makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]:
    JSON.stringify(['failed', 'done', 'unknown', 1, null, '']) })
  assert.deepEqual(loadCollapsedGroups(s), new Set(['failed', 'done']),
                   '仅保留已知分组 key，未知 key 与非字符串元素剔除')
  const s2 = makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: JSON.stringify(['other']) })
  assert.deepEqual(loadCollapsedGroups(s2), new Set(['other']), '合法单 key 正常解析')
})

test('loadCollapsedGroups：getItem 抛异常（隐私模式）回空 Set 且不抛错', () => {
  const storage = { getItem: () => { throw new Error('denied') } }
  assert.deepEqual(loadCollapsedGroups(storage), new Set(), '读取异常应回空 Set 且不抛错')
})

// ---- saveCollapsedGroups 纯函数测试 ----

test('saveCollapsedGroups：写入合法 key 数组，未知 key 剔除', () => {
  const storage = makeStorage()
  saveCollapsedGroups(storage, new Set(['failed', 'done']))
  assert.equal(storage.getItem(GROUP_COLLAPSE_STORAGE_KEY),
               JSON.stringify(['failed', 'done']), '应写入规范化 JSON 数组')
  const storage2 = makeStorage()
  saveCollapsedGroups(storage2, new Set(['failed', 'nope', 'x']))
  assert.equal(storage2.getItem(GROUP_COLLAPSE_STORAGE_KEY),
               JSON.stringify(['failed']), '未知 key 应剔除')
})

test('saveCollapsedGroups：空 Set 写入空数组', () => {
  const storage = makeStorage()
  saveCollapsedGroups(storage, new Set())
  assert.equal(storage.getItem(GROUP_COLLAPSE_STORAGE_KEY),
               JSON.stringify([]), '空 Set 应写入空数组')
})

test('saveCollapsedGroups：无 storage / 参数非法 / setItem 抛异常均静默', () => {
  assert.doesNotThrow(() => saveCollapsedGroups(null, new Set(['done'])))
  assert.doesNotThrow(() => saveCollapsedGroups(undefined, new Set(['done'])))
  assert.doesNotThrow(() => saveCollapsedGroups(makeStorage(), null))
  assert.doesNotThrow(() => saveCollapsedGroups(makeStorage(), undefined))
  const storage = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveCollapsedGroups(storage, new Set(['done'])))
})

// ---- toggleGroupCollapsed 纯函数测试 ----

test('toggleGroupCollapsed：未折叠 → 折叠；已折叠 → 展开；返回新 Set 不改入参', () => {
  const empty = new Set()
  const after1 = toggleGroupCollapsed(empty, 'done')
  assert.deepEqual(after1, new Set(['done']), '首次点击应折叠该分组')
  assert.deepEqual(empty, new Set(), '入参不应被修改（新 Set 语义）')
  const after2 = toggleGroupCollapsed(after1, 'done')
  assert.deepEqual(after2, new Set(), '再次点击应展开该分组')
  assert.deepEqual(after1, new Set(['done']), '展开操作不应改动旧状态')
})

test('toggleGroupCollapsed：null / undefined 入参按空 Set 处理', () => {
  assert.deepEqual(toggleGroupCollapsed(null, 'failed'), new Set(['failed']))
  assert.deepEqual(toggleGroupCollapsed(undefined, 'failed'), new Set(['failed']))
})

test('toggleGroupCollapsed：多组互不影响，仅切换目标 key', () => {
  const cur = new Set(['failed'])
  const next = toggleGroupCollapsed(cur, 'done')
  assert.deepEqual(next, new Set(['failed', 'done']), '应追加新折叠组')
  assert.ok(next.has('failed'), '原折叠组应保留')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询多个端点，mock 按路径分流；storage 可注入
// localStorage（同 overview-issue-filter.test.mjs 模式）。
async function renderOverview({ issuesPayload, storage } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload || { repos: [], errors: [], total: 0 }
    if (pathname === '/api/settings') return { gitlab: {} }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    if (pathname === '/api/settings/deepseek-balance') return { configured: false, balance: null, error: null }
    throw new Error('unexpected ' + pathname)
  })
  const realStorage = global.localStorage
  if (storage !== undefined) global.localStorage = storage
  else delete global.localStorage
  const restore = () => {
    if (realStorage === undefined) delete global.localStorage
    else global.localStorage = realStorage
  }
  let renderer = null
  let renderError = null
  try {
    await TestRenderer.act(async () => {
      try {
        renderer = TestRenderer.create(React.createElement(Overview))
        await new Promise((resolve) => setTimeout(resolve, 30))
      } catch (e) {
        renderError = e
      }
    })
  } catch (e) {
    renderError = e
  }
  return { renderer, renderError, restore }
}

// 按 className 子串查找按钮
const findBtns = (root, cls) => root.findAll(
  (n) => n.type === 'button' && String(n.props.className || '').includes(cls))
// 分组折叠开关按钮
const toggleBtns = (root) => findBtns(root, 'issue-group-toggle')
// 组标题
const groupTitles = (root) => root.findAll(
  (n) => n.props.className === 'issue-group-title')
// 组内 issue 列表
const issueLists = (root) => root.findAll(
  (n) => n.props.className === 'issue-list')
// 组计数
const groupCounts = (root) => root.findAll(
  (n) => n.props.className === 'issue-group-count')

// 混合仓库：bot-failed 1 条、bot-done 2 条、其他 2 条
const MIXED_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', priority: 10,
    issues: [
      { iid: 101, title: '失败一',
        updated_at: '2026-08-15 10:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/101',
        labels: [{ name: 'bot-failed' }] },
      { iid: 102, title: '完成一',
        updated_at: '2026-08-15 09:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/102',
        labels: [{ name: 'bot-done' }, { name: 'feature' }] },
      { iid: 103, title: '普通一',
        updated_at: '2026-08-15 08:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/103',
        labels: [{ name: 'bug' }] },
      { iid: 104, title: '完成二',
        updated_at: '2026-08-15 07:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/104',
        labels: [{ name: 'bot-done' }] },
      { iid: 105, title: '普通二',
        updated_at: '2026-08-15 06:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/105' },
    ],
  }],
  errors: [], total: 5,
}

test('渲染：默认全展开——每组都有折叠按钮、issue 列表与计数齐全', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const toggles = toggleBtns(root)
    assert.equal(toggles.length, 3, '三种状态组均应渲染折叠开关')
    assert.deepEqual(toggles.map((b) => b.props['aria-expanded']),
                     [true, true, true], '默认应全部为展开态')
    assert.equal(issueLists(root).length, 3, '默认应渲染全部组内列表')
    assert.deepEqual(groupTitles(root).map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'], '组标题顺序不变')
    assert.deepEqual(groupCounts(root).map((c) => c.props.children),
                     ['1 个', '2 个', '2 个'], '组计数保持不变')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击 bot-failed 组开关 → 该组列表隐藏、标题与计数保留、其他组不受影响', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(issueLists(root).length, 3)
    // 第一个开关对应 bot-failed 组
    const failedToggle = toggleBtns(root)[0]
    await TestRenderer.act(() => failedToggle.props.onClick())
    assert.equal(issueLists(root).length, 2, '折叠后应隐藏 bot-failed 组内列表')
    assert.deepEqual(groupTitles(root).map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'], '折叠组标题应保留')
    assert.deepEqual(groupCounts(root).map((c) => c.props.children),
                     ['1 个', '2 个', '2 个'], '折叠组计数应保留')
    const toggles = toggleBtns(root)
    assert.equal(toggles[0].props['aria-expanded'], false, 'bot-failed 组应为折叠态')
    assert.deepEqual(toggles.slice(1).map((b) => b.props['aria-expanded']),
                     [true, true], '其余组应保持展开态')
    // 折叠不丢 issue 数据：展开后仍能恢复全部 5 条
    await TestRenderer.act(() => toggleBtns(root)[0].props.onClick())
    assert.equal(issueLists(root).length, 3, '再次点击应展开恢复')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：localStorage 预置折叠偏好 → 初始渲染即折叠对应组', async () => {
  const storage = makeStorage({ [GROUP_COLLAPSE_STORAGE_KEY]: JSON.stringify(['done']) })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(issueLists(root).length, 2, '预置折叠 done 组后应只剩两组列表')
    const toggles = toggleBtns(root)
    assert.deepEqual(toggles.map((b) => b.props['aria-expanded']),
                     [true, false, true], '仅 done 组应为折叠态')
    assert.deepEqual(groupTitles(root).map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'], '折叠组标题仍保留')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击折叠后偏好写入 localStorage（刷新后保持）', async () => {
  const storage = makeStorage()
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 挂载时初始空 Set 会先写入空数组
    await TestRenderer.act(() => toggleBtns(root)[2].props.onClick()) // 折叠「其他」组
    assert.equal(storage.getItem(GROUP_COLLAPSE_STORAGE_KEY),
                 JSON.stringify(['other']), '点击后应持久化折叠偏好')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：折叠态折叠按钮文案切换为「展开该分组」', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const failedToggle = toggleBtns(root)[0]
    assert.equal(failedToggle.props['aria-label'], '折叠该分组',
                 '展开态 aria-label 应为「折叠该分组」')
    await TestRenderer.act(() => failedToggle.props.onClick())
    const after = toggleBtns(root)[0]
    assert.equal(after.props['aria-label'], '展开该分组',
                 '折叠态 aria-label 应为「展开该分组」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
