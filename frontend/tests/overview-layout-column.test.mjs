// 概览页「开放 Issue」单列分组布局测试（issue #471 / issue #485）：在
// 现有「仓库卡片」布局之外新增「单列分组」布局。issue #485 起单列分组
// 改为「状态 → 仓库」两级分组——先按 issue 状态分组（进行中 → 完成任务
// → 失败任务 → 其他），状态组内再按仓库分组；仓库子分组可折叠/展开；
// 布局选择与仓库分组折叠偏好均存 localStorage（键 botler.overview.
// layout / botler.overview.collapsedRepos），刷新后保持。
//
// 断言：
// 1. 纯函数：loadIssueLayout / saveIssueLayout 的解析与规范化边界
//    （无存储/损坏数据/未知布局回默认「仓库卡片」）；
//    loadCollapsedRepos / saveCollapsedRepos 的解析与规范化边界
//    （无存储/损坏数据/非字符串元素剔除）；
// 2. 源码：IssueListSection 渲染按布局分支、单列布局组头渲染折叠按钮并
//    调用 toggleGroupCollapsed、布局偏好写入持久化；
// 3. 渲染：默认卡片布局不渲染单列容器；切换布局后按仓库分组单列渲染
//    （组头含折叠按钮/仓库名/计数，组内 issue 列表齐全）；单列布局下
//    点击折叠隐藏组内列表但保留组头；折叠偏好写入 localStorage 并可在
//    预置后初始生效；布局偏好预置「单列分组」后初始即单列渲染。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const sectionSrc = readFileSync(
  path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')
const libSrc = readFileSync(path.join(ROOT, 'src/lib/overview.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-issue-group-collapse.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, loadIssueLayout, saveIssueLayout,
        loadCollapsedRepos, saveCollapsedRepos,
        ISSUE_LAYOUT_STORAGE_KEY, REPO_COLLAPSE_STORAGE_KEY,
        ISSUE_LAYOUTS } =
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

// ---- 源码断言 ----

test('Overview.jsx 导出布局/折叠函数与存储键，渲染按布局分支', () => {
  assert.equal(typeof loadIssueLayout, 'function', '应导出 loadIssueLayout')
  assert.equal(typeof saveIssueLayout, 'function', '应导出 saveIssueLayout')
  assert.equal(typeof loadCollapsedRepos, 'function', '应导出 loadCollapsedRepos')
  assert.equal(typeof saveCollapsedRepos, 'function', '应导出 saveCollapsedRepos')
  assert.equal(ISSUE_LAYOUT_STORAGE_KEY, 'botler.overview.layout',
               '布局存储键应为 botler.overview.layout')
  assert.equal(REPO_COLLAPSE_STORAGE_KEY, 'botler.overview.collapsedRepos',
               '仓库折叠存储键应为 botler.overview.collapsedRepos')
  assert.deepEqual(ISSUE_LAYOUTS.map((l) => l.key), ['cards', 'column'],
                   '布局选项应为 cards（默认）与 column 两种')
  assert.match(sectionSrc, /issueLayout === 'column'/,
               '渲染应按 issueLayout 分支（卡片 vs 单列分组）')
  assert.match(sectionSrc, /setCollapsedRepos\(\(prev\) => toggleGroupCollapsed\(prev, String\(repoId\)\)\)/,
               '单列分组折叠按钮应调用 toggleGroupCollapsed（key 为仓库 id 字符串）')
  assert.match(sectionSrc, /issue-repo-toggle/, '单列分组组头应渲染折叠开关按钮')
  assert.match(sectionSrc, /issues-list-column/, '单列布局容器应使用 issues-list-column 类')
  assert.match(libSrc, /ISSUE_LAYOUT_STORAGE_KEY/, 'lib 应定义布局存储键')
})

// ---- loadIssueLayout 纯函数测试 ----

test('loadIssueLayout：无存储 / storage 为 null / 未存值 / 空串 → 默认 cards', () => {
  assert.equal(loadIssueLayout(undefined), 'cards', '无 storage 应回默认 cards')
  assert.equal(loadIssueLayout(null), 'cards', 'storage 为 null 应回默认 cards')
  assert.equal(loadIssueLayout(makeStorage()), 'cards', '未存值应回默认 cards')
  assert.equal(loadIssueLayout(makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: '' })),
               'cards', '空串应回默认 cards')
})

test('loadIssueLayout：非法 JSON / 未知布局 → 默认 cards；column 正常解析', () => {
  assert.equal(loadIssueLayout(makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: '{oops' })),
               'cards', '非法 JSON 应回默认 cards')
  assert.equal(loadIssueLayout(makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: 'grid' })),
               'cards', '未知布局应回默认 cards')
  assert.equal(loadIssueLayout(makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: 'column' })),
               'column', 'column 布局应正常解析')
})

test('loadIssueLayout：getItem 抛异常（隐私模式）回默认 cards 且不抛错', () => {
  const storage = { getItem: () => { throw new Error('denied') } }
  assert.equal(loadIssueLayout(storage), 'cards', '读取异常应回默认 cards 且不抛错')
})

// ---- saveIssueLayout 纯函数测试 ----

test('saveIssueLayout：只写合法布局键，非法值不写入', () => {
  const storage = makeStorage()
  saveIssueLayout(storage, 'column')
  assert.equal(storage.getItem(ISSUE_LAYOUT_STORAGE_KEY), 'column',
               'column 应写入存储')
  saveIssueLayout(storage, 'cards')
  assert.equal(storage.getItem(ISSUE_LAYOUT_STORAGE_KEY), 'cards',
               'cards 应写入存储')
  const storage2 = makeStorage()
  saveIssueLayout(storage2, 'grid')
  assert.equal(storage2.getItem(ISSUE_LAYOUT_STORAGE_KEY), null,
               '未知布局不应写入')
})

test('saveIssueLayout：无 storage / 参数非法 / setItem 抛异常均静默', () => {
  assert.doesNotThrow(() => saveIssueLayout(null, 'column'))
  assert.doesNotThrow(() => saveIssueLayout(undefined, 'column'))
  assert.doesNotThrow(() => saveIssueLayout(makeStorage(), null))
  assert.doesNotThrow(() => saveIssueLayout(makeStorage(), undefined))
  const storage = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveIssueLayout(storage, 'column'))
})

// ---- loadCollapsedRepos 纯函数测试 ----

test('loadCollapsedRepos：无存储 / storage 为 null / 未存值 / 空串 → 空 Set（全展开）', () => {
  assert.deepEqual(loadCollapsedRepos(undefined), new Set(), '无 storage 应回空 Set')
  assert.deepEqual(loadCollapsedRepos(null), new Set(), 'storage 为 null 应回空 Set')
  assert.deepEqual(loadCollapsedRepos(makeStorage()), new Set(), '未存值应回空 Set')
  assert.deepEqual(loadCollapsedRepos(makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: '' })),
                   new Set(), '空串应回空 Set')
})

test('loadCollapsedRepos：非法 JSON / 非数组 → 空 Set', () => {
  assert.deepEqual(loadCollapsedRepos(makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: '{oops' })),
                   new Set(), '非法 JSON 应回空 Set')
  assert.deepEqual(loadCollapsedRepos(makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: '"str"' })),
                   new Set(), '解析为非数组应回空 Set')
  assert.deepEqual(loadCollapsedRepos(makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: '42' })),
                   new Set(), '解析为数字应回空 Set')
  assert.deepEqual(loadCollapsedRepos(makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: '{}' })),
                   new Set(), '解析为对象应回空 Set')
})

test('loadCollapsedRepos：合法字符串数组解析为 Set，非字符串元素剔除', () => {
  const s = makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]:
    JSON.stringify(['1', '2', 3, null, true, '']) })
  assert.deepEqual(loadCollapsedRepos(s), new Set(['1', '2']),
                   '仅保留字符串元素，数字/布尔/null/空串剔除')
  const s2 = makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: JSON.stringify(['7']) })
  assert.deepEqual(loadCollapsedRepos(s2), new Set(['7']), '合法单元素正常解析')
})

test('loadCollapsedRepos：getItem 抛异常（隐私模式）回空 Set 且不抛错', () => {
  const storage = { getItem: () => { throw new Error('denied') } }
  assert.deepEqual(loadCollapsedRepos(storage), new Set(), '读取异常应回空 Set 且不抛错')
})

// ---- saveCollapsedRepos 纯函数测试 ----

test('saveCollapsedRepos：写入字符串数组，非字符串剔除', () => {
  const storage = makeStorage()
  saveCollapsedRepos(storage, new Set(['1', '2']))
  assert.equal(storage.getItem(REPO_COLLAPSE_STORAGE_KEY),
               JSON.stringify(['1', '2']), '应写入规范化 JSON 数组')
  const storage2 = makeStorage()
  saveCollapsedRepos(storage2, new Set(['3', 4, null]))
  assert.equal(storage2.getItem(REPO_COLLAPSE_STORAGE_KEY),
               JSON.stringify(['3']), '非字符串元素应剔除')
})

test('saveCollapsedRepos：空 Set 写入空数组', () => {
  const storage = makeStorage()
  saveCollapsedRepos(storage, new Set())
  assert.equal(storage.getItem(REPO_COLLAPSE_STORAGE_KEY),
               JSON.stringify([]), '空 Set 应写入空数组')
})

test('saveCollapsedRepos：无 storage / 参数非法 / setItem 抛异常均静默', () => {
  assert.doesNotThrow(() => saveCollapsedRepos(null, new Set(['1'])))
  assert.doesNotThrow(() => saveCollapsedRepos(undefined, new Set(['1'])))
  assert.doesNotThrow(() => saveCollapsedRepos(makeStorage(), null))
  assert.doesNotThrow(() => saveCollapsedRepos(makeStorage(), undefined))
  const storage = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveCollapsedRepos(storage, new Set(['1'])))
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询多个端点，mock 按路径分流；storage 可注入
// localStorage（同 overview-issue-group-collapse.test.mjs 模式）。
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
// 布局切换按钮
const layoutBtns = (root) => findBtns(root, 'issue-layout-option')
// 单列分组折叠开关按钮
const repoToggleBtns = (root) => findBtns(root, 'issue-repo-toggle')
// 单列分组容器
const columnLists = (root) => root.findAll(
  (n) => String(n.props.className || '').includes('issues-list-column'))
// 仓库分组（单列布局，精确类名匹配避免误命中 issue-repo-group-head）
const repoGroups = (root) => root.findAll(
  (n) => String(n.props.className || '').split(' ').includes('issue-repo-group'))
// 组内 issue 列表
const issueLists = (root) => root.findAll(
  (n) => n.props.className === 'issue-list')

// 混合仓库：仓库 1 两条、仓库 2 三条
const TWO_REPO_PAYLOAD = {
  repos: [
    {
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 101, title: 'botler 一',
          updated_at: '2026-08-15 10:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/101',
          labels: [{ name: 'bot-done' }] },
        { iid: 102, title: 'botler 二',
          updated_at: '2026-08-15 09:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/102',
          labels: [] },
      ],
    },
    {
      repo_id: 2, repo_name: 'docs-site', priority: 20,
      issues: [
        { iid: 201, title: 'docs 一',
          updated_at: '2026-08-15 08:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/docs-site/-/issues/201',
          labels: [{ name: 'bot-failed' }] },
        { iid: 202, title: 'docs 二',
          updated_at: '2026-08-15 07:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/docs-site/-/issues/202',
          labels: [] },
        { iid: 203, title: 'docs 三',
          updated_at: '2026-08-15 06:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/docs-site/-/issues/203',
          labels: [{ name: 'bug' }] },
      ],
    },
  ],
  errors: [], total: 5,
}

test('渲染：默认卡片布局——不渲染单列分组容器，布局按钮两组齐全', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    assert.equal(columnLists(root).length, 0, '默认布局不应渲染单列容器')
    assert.equal(repoGroups(root).length, 0, '默认布局不应渲染仓库分组')
    const btns = layoutBtns(root)
    assert.equal(btns.length, 2, '应渲染两种布局切换按钮')
    assert.deepEqual(btns.map((b) => b.props['aria-pressed']), [true, false],
                     '默认应选中「仓库卡片」布局')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击「单列分组」→ 按状态分组、组内按仓库分组单列渲染（组头 + issue 列表齐全）', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => layoutBtns(root)[1].props.onClick())
    assert.equal(columnLists(root).length, 1, '切换后应渲染单列分组容器')
    // 状态组：完成任务（repo1 的 101）/ 失败任务（repo2 的 201）/ 其他
    // （repo1 的 102 + repo2 的 202、203）；进行中无运行任务不渲染
    const statusTitles = root.findAll(
      (n) => String(n.props.className || '').includes('issue-group-title'))
    assert.deepEqual(statusTitles.map((n) => textOf(n.props.children).trim()),
                     ['bot-done', 'bot-failed', '其他'],
                     '状态组顺序应为 进行中→完成任务→失败任务→其他（空组不渲染）')
    assert.equal(repoGroups(root).length, 4,
                 '状态组内按仓库分组：完成任务 1 + 失败任务 1 + 其他 2 = 4 个仓库子分组')
    const toggles = repoToggleBtns(root)
    assert.equal(toggles.length, 4, '每个仓库子分组应有折叠开关')
    assert.deepEqual(toggles.map((b) => b.props['aria-expanded']),
                     [true, true, true, true], '默认应全部为展开态')
    assert.equal(issueLists(root).length, 4, '应渲染 4 个仓库子分组的组内列表')
    // 组内 issue 标题齐全（两个仓库共 5 条，全部 issue 不丢失）
    const items = root.findAll((n) => String(n.props.className || '').includes('issue-item'))
    assert.equal(items.length, 5, '单列布局应渲染全部 5 条 issue')
    const titles = items.map((n) => textOf(n.props.children).trim())
    for (const t of ['botler 一', 'botler 二', 'docs 一', 'docs 二', 'docs 三']) {
      assert.ok(titles.some((x) => x.includes(t)), `组内应包含 issue「${t}」`)
    }
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：单列布局下点击仓库子分组折叠开关 → 该组列表隐藏、组头保留、其他组不受影响', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => layoutBtns(root)[1].props.onClick())
    assert.equal(issueLists(root).length, 4)
    // 折叠第一个仓库子分组（完成任务组内 repo1，仅 1 条 issue）。
    // 折叠偏好按仓库 id 全局生效：repo1 出现在完成任务/其他两个状态组，
    // 两个子分组应同时折叠（test 17 同源语义）
    await TestRenderer.act(() => repoToggleBtns(root)[0].props.onClick())
    assert.equal(issueLists(root).length, 2, '折叠后应隐藏该仓库全部状态子分组列表（repo2 两子分组保留）')
    assert.equal(repoGroups(root).length, 4, '折叠子分组容器应保留')
    const toggles = repoToggleBtns(root)
    assert.deepEqual(toggles.map((b) => b.props['aria-expanded']),
                     [false, true, false, true],
                     'repo1 的两个子分组（完成任务/其他）应为折叠态，repo2 保持展开')
    const items = root.findAll((n) => String(n.props.className || '').includes('issue-item'))
    assert.equal(items.length, 3, '折叠后只应显示 repo2 的 3 条 issue')
    // 折叠不丢数据：展开后恢复 5 条
    await TestRenderer.act(() => repoToggleBtns(root)[0].props.onClick())
    assert.equal(issueLists(root).length, 4, '再次点击应展开恢复')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击折叠后偏好写入 localStorage；预置折叠偏好初始生效', async () => {
  // 无预置：点击折叠 → 写入
  const storage = makeStorage()
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => layoutBtns(root)[1].props.onClick())
    // 折叠第二个仓库子分组（失败任务组内 docs-site，repo_id=2）
    await TestRenderer.act(() => repoToggleBtns(root)[1].props.onClick())
    assert.equal(storage.getItem(REPO_COLLAPSE_STORAGE_KEY),
                 JSON.stringify(['2']), '点击折叠后应持久化仓库折叠偏好')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
  // 预置：初始即折叠对应仓库子分组（仓库 1 出现在完成任务与其它两个
  // 状态组，两处应同时折叠——折叠偏好按仓库 id 全局生效）
  const storage2 = makeStorage({ [REPO_COLLAPSE_STORAGE_KEY]: JSON.stringify(['1']) })
  const { renderer: r2, renderError: e2, restore: restore2 } =
    await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD, storage: storage2 })
  try {
    assert.equal(e2, null)
    const root = r2.root
    await TestRenderer.act(() => layoutBtns(root)[1].props.onClick())
    assert.equal(issueLists(root).length, 2, '预置折叠仓库 1 后应只剩仓库 2 的两个子分组列表')
    const toggles = repoToggleBtns(root)
    assert.deepEqual(toggles.map((b) => b.props['aria-expanded']),
                     [false, true, false, true],
                     '仓库 1 的两个子分组（完成任务/其他）应为折叠态')
  } finally {
    await TestRenderer.act(() => r2.unmount())
    mock.restoreAll()
    restore2()
  }
})

test('渲染：布局偏好预置「单列分组」→ 初始即单列渲染；切换回卡片后持久化', async () => {
  const storage = makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: 'column' })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(columnLists(root).length, 1, '预置 column 布局应初始即单列渲染')
    const btns = layoutBtns(root)
    assert.deepEqual(btns.map((b) => b.props['aria-pressed']), [false, true],
                     '应选中「单列分组」布局')
    // 切回卡片布局 → 持久化覆盖
    await TestRenderer.act(() => btns[0].props.onClick())
    assert.equal(columnLists(root).length, 0, '切回后不应渲染单列容器')
    assert.equal(storage.getItem(ISSUE_LAYOUT_STORAGE_KEY), 'cards',
                 '切换布局应持久化新偏好')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：单列布局组头保留仓库操作按钮（对账/自省/发掘/添加 Issue）', async () => {
  const storage = makeStorage({ [ISSUE_LAYOUT_STORAGE_KEY]: 'column' })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: TWO_REPO_PAYLOAD, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 每个仓库组头都应有添加 Issue 按钮（reconcile/introspect/discover 同组）
    const addBtns = root.findAll((n) =>
      n.type === 'button' && String(n.props.className || '').includes('add-issue-btn'))
    assert.equal(addBtns.length, 4,
                 '4 个仓库子分组组头均应保留「添加 Issue」按钮（按仓库 id 全局）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
