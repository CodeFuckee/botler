// 概览页「开放 Issue」板块过滤条测试（issue #230）：开放 issue 聚合按
// 仓库分组展示，支持按状态（全部/开放/进行中）与标签多选过滤，仅过滤
// 条目、保留仓库分组结构；过滤偏好存 localStorage（键
// botler.overview.issueFilter），刷新后保持。
//
// 语义：
// - 状态：all=全部（不过滤）；open=开放（无 running/retrying 任务）；
//   running=进行中（有运行中任务，与置顶 running 组同源判定）；
// - 标签：多选 OR 语义（命中任一选中标签即展示），候选来自未过滤全量
//   数据（过滤后候选标签不因筛选消失）；
// - 持久化：loadIssueFilter 读取（非法值/无存储兜底默认）、
//   saveIssueFilter 写入（非法结构规范化、存储异常静默）；
// - 展示：过滤激活时无匹配条目的仓库整卡隐藏（避免空卡噪音）；全部无
//   匹配时显示「没有匹配过滤条件的 issue」+ 清除过滤；未过滤时零 issue
//   仓库卡片保持仓库级空状态（回归保护）。
//
// 断言：
// 1. 纯函数：load/saveIssueFilter 的解析与规范化边界、issueLabelNames /
//    collectLabelOptions 防御、matchesIssueStatus / matchesIssueLabels /
//    filterIssuesByFilter 的过滤语义与顺序保持；
// 2. 渲染：过滤条（状态按钮 + 标签胶囊 + 清除过滤）渲染、点击过滤生效、
//    多选 OR 并集、状态+标签组合、localStorage 预置初始过滤、点击后写入
//    持久化、无匹配空态、无 issue 不渲染过滤条、零 issue 仓库卡片保留、
//    清除过滤恢复全量。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, loadIssueFilter, saveIssueFilter,
        issueLabelNames, collectLabelOptions, matchesIssueStatus,
        matchesIssueLabels, filterIssuesByFilter,
        ISSUE_FILTER_STORAGE_KEY, ISSUE_STATUS_FILTERS,
        ISSUE_SORT_STORAGE_KEY } =
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

test('Overview.jsx 导出过滤函数与存储键，渲染使用过滤逻辑', () => {
  assert.equal(typeof loadIssueFilter, 'function', '应导出 loadIssueFilter')
  assert.equal(typeof saveIssueFilter, 'function', '应导出 saveIssueFilter')
  assert.equal(typeof collectLabelOptions, 'function', '应导出 collectLabelOptions')
  assert.equal(typeof filterIssuesByFilter, 'function', '应导出 filterIssuesByFilter')
  assert.equal(ISSUE_FILTER_STORAGE_KEY, 'botler.overview.issueFilter',
               '存储键应为 botler.overview.issueFilter')
  assert.deepEqual(ISSUE_STATUS_FILTERS.map((s) => s.key),
                   ['all', 'open', 'running'],
                   '状态选项顺序应为 全部 → 开放 → 进行中')
  assert.equal(ISSUE_STATUS_FILTERS[0].label, '全部')
  assert.equal(ISSUE_STATUS_FILTERS[1].label, '开放')
  assert.equal(ISSUE_STATUS_FILTERS[2].label, '进行中')
  assert.match(overview, /filterIssuesByFilter\(r\.issues, issueFilter, runningKeys, r\.repo_id\)/,
               '渲染应调用 filterIssuesByFilter 过滤条目')
  assert.match(overview, /issue-filter-bar/, '渲染应包含过滤条容器类名')
  assert.match(overview, /ISSUE_STATUS_FILTERS\.map/, '状态按钮应遍历 ISSUE_STATUS_FILTERS')
})

// ---- loadIssueFilter 纯函数测试 ----

test('loadIssueFilter：无存储 / storage 为 null / 未存值 / 空串 → 默认值', () => {
  const def = { status: 'all', labels: [] }
  assert.deepEqual(loadIssueFilter(undefined), def, '无 storage 应回默认')
  assert.deepEqual(loadIssueFilter(null), def, 'storage 为 null 应回默认')
  assert.deepEqual(loadIssueFilter(makeStorage()), def, '未存值应回默认')
  assert.deepEqual(loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]: '' })), def,
                   '空串应回默认')
})

test('loadIssueFilter：非法 JSON / 解析为非对象 → 默认值', () => {
  const def = { status: 'all', labels: [] }
  assert.deepEqual(loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]: '{oops' })), def,
                   '非法 JSON 应回默认')
  assert.deepEqual(loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]: '"str"' })), def,
                   '解析为非对象应回默认')
  assert.deepEqual(loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]: '42' })), def,
                   '解析为数字应回默认')
})

test('loadIssueFilter：未知状态回 all、labels 非数组回空数组', () => {
  assert.deepEqual(
    loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]:
      JSON.stringify({ status: 'unknown', labels: ['bug'] }) })),
    { status: 'all', labels: ['bug'] }, '未知状态应回 all，labels 保留')
  assert.deepEqual(
    loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]:
      JSON.stringify({ status: 'running', labels: 'bug' }) })),
    { status: 'running', labels: [] }, 'labels 非数组应回空数组')
  assert.deepEqual(
    loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]:
      JSON.stringify({ status: 'running', labels: [1, null, 'bug', {}] }) })),
    { status: 'running', labels: ['bug'] }, '非字符串标签元素应剔除')
})

test('loadIssueFilter：合法值正常解析（标签多选）', () => {
  const f = loadIssueFilter(makeStorage({ [ISSUE_FILTER_STORAGE_KEY]:
    JSON.stringify({ status: 'open', labels: ['bug', 'need-verify'] }) }))
  assert.deepEqual(f, { status: 'open', labels: ['bug', 'need-verify'] },
                   '合法过滤偏好应原样解析')
})

test('loadIssueFilter：getItem 抛异常（隐私模式）回默认且不抛错', () => {
  const storage = { getItem: () => { throw new Error('denied') } }
  assert.deepEqual(loadIssueFilter(storage), { status: 'all', labels: [] },
                   '读取异常应回默认且不抛错')
})

// ---- saveIssueFilter 纯函数测试 ----

test('saveIssueFilter：写入 JSON 结构（含多选标签）', () => {
  const storage = makeStorage()
  saveIssueFilter(storage, { status: 'running', labels: ['bug', 'feature'] })
  assert.equal(storage.getItem(ISSUE_FILTER_STORAGE_KEY),
               JSON.stringify({ status: 'running', labels: ['bug', 'feature'] }),
               '应写入规范化 JSON')
})

test('saveIssueFilter：非法状态回 all、非字符串标签剔除', () => {
  const storage = makeStorage()
  saveIssueFilter(storage, { status: 'unknown', labels: [1, 'bug', null] })
  assert.equal(storage.getItem(ISSUE_FILTER_STORAGE_KEY),
               JSON.stringify({ status: 'all', labels: ['bug'] }),
               '应规范化后写入')
})

test('saveIssueFilter：无 storage / 参数非法 / setItem 抛异常均静默', () => {
  assert.doesNotThrow(() => saveIssueFilter(null, { status: 'all', labels: [] }))
  assert.doesNotThrow(() => saveIssueFilter(undefined, { status: 'all', labels: [] }))
  assert.doesNotThrow(() => saveIssueFilter(makeStorage(), null))
  const storage = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveIssueFilter(storage, { status: 'all', labels: [] }))
})

// ---- issueLabelNames / collectLabelOptions 纯函数测试 ----

test('issueLabelNames：对象标签 / 字符串标签 / 异常元素防御', () => {
  assert.deepEqual(issueLabelNames({ labels: [{ name: 'bug' }, { name: 'ui' }] }),
                   ['bug', 'ui'], '对象标签应取 name')
  assert.deepEqual(issueLabelNames({ labels: ['bug', 'feature'] }),
                   ['bug', 'feature'], '字符串标签应原样保留')
  assert.deepEqual(issueLabelNames({ labels: [null, {}, { name: 'bug' }, { name: '' }] }),
                   ['bug'], 'null/缺 name/空 name 应跳过')
  assert.deepEqual(issueLabelNames({}), [], '无 labels 应回空数组')
  assert.deepEqual(issueLabelNames(null), [], 'issue 为 null 应回空数组')
  assert.deepEqual(issueLabelNames({ labels: 'bug' }), [], 'labels 非数组应回空数组')
})

test('collectLabelOptions：跨仓库去重 + 字典序排序', () => {
  const repos = [
    { repo_id: 1, issues: [{ labels: [{ name: 'bug' }] }, { labels: [{ name: 'feature' }] }] },
    { repo_id: 2, issues: [{ labels: ['bug', 'ui'] }, { labels: [{ name: 'need-verify' }] }] },
    { repo_id: 3, issues: [] },
    { repo_id: 4, issues: null },
    null,
  ]
  assert.deepEqual(collectLabelOptions(repos),
                   ['bug', 'feature', 'need-verify', 'ui'],
                   '应去重并按字典序排序')
  assert.deepEqual(collectLabelOptions([]), [], '空仓库列表应回空数组')
  assert.deepEqual(collectLabelOptions(null), [], 'repos 非数组应回空数组')
})

// ---- matchesIssueStatus / matchesIssueLabels 纯函数测试 ----

test('matchesIssueStatus：all 恒真；open/running 按 runningKeys 判定', () => {
  const keys = new Set(['1:101'])
  assert.equal(matchesIssueStatus({ iid: 101 }, 'all', keys, 1), true, 'all 不过滤')
  assert.equal(matchesIssueStatus({ iid: 101 }, 'running', keys, 1), true,
               'running：命中键应展示')
  assert.equal(matchesIssueStatus({ iid: 102 }, 'running', keys, 1), false,
               'running：未命中不展示')
  assert.equal(matchesIssueStatus({ iid: 101 }, 'open', keys, 1), false,
               'open：命中运行键不展示')
  assert.equal(matchesIssueStatus({ iid: 102 }, 'open', keys, 1), true,
               'open：未命中运行键展示')
})

test('matchesIssueStatus：runningKeys 缺失 / repoId 不匹配 / issue 为空 → 不误判', () => {
  const keys = new Set(['1:101'])
  assert.equal(matchesIssueStatus({ iid: 101 }, 'running', null, 1), false,
               'runningKeys 缺失按未运行处理')
  assert.equal(matchesIssueStatus({ iid: 101 }, 'open', null, 1), true,
               'runningKeys 缺失 open 应展示')
  assert.equal(matchesIssueStatus({ iid: 101 }, 'running', keys, 2), false,
               'repoId 不匹配不应误判运行中')
  assert.equal(matchesIssueStatus(null, 'running', keys, 1), false,
               'issue 为空 running 应为 false')
  assert.equal(matchesIssueStatus(null, 'open', keys, 1), true,
               'issue 为空 open 应为 true（无运行键）')
})

test('matchesIssueLabels：空选恒真；多选 OR 语义', () => {
  const issue = { labels: [{ name: 'bug' }, { name: 'ui' }] }
  assert.equal(matchesIssueLabels(issue, []), true, '未选标签不过滤')
  assert.equal(matchesIssueLabels(issue, null), true, 'labels 为 null 不过滤')
  assert.equal(matchesIssueLabels(issue, ['bug']), true, '命中单个选中标签')
  assert.equal(matchesIssueLabels(issue, ['feature', 'ui']), true,
               '命中任一选中标签即展示（OR）')
  assert.equal(matchesIssueLabels(issue, ['feature', 'docs']), false,
               '未命中任何选中标签不展示')
  assert.equal(matchesIssueLabels({ labels: ['bug'] }, ['bug']), true,
               '字符串标签同样可命中')
})

// ---- filterIssuesByFilter 纯函数测试 ----

test('filterIssuesByFilter：不过滤时返回全部且顺序不变', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'bug' }] },
    { iid: 2, labels: [{ name: 'feature' }] },
    { iid: 3, labels: [] },
  ]
  assert.deepEqual(filterIssuesByFilter(issues, { status: 'all', labels: [] }).map((i) => i.iid),
                   [1, 2, 3], '默认过滤应保留全部')
})

test('filterIssuesByFilter：标签过滤 + 状态过滤 + 组合过滤', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'bug' }] },
    { iid: 2, labels: [{ name: 'feature' }] },
    { iid: 3, labels: [{ name: 'bug' }, { name: 'need-verify' }] },
    { iid: 4, labels: [] },
  ]
  assert.deepEqual(filterIssuesByFilter(issues, { status: 'all', labels: ['bug'] })
    .map((i) => i.iid), [1, 3], '标签过滤应只保留带 bug 标签的条目')
  const keys = new Set(['1:2'])
  assert.deepEqual(filterIssuesByFilter(issues, { status: 'running', labels: [] }, keys, 1)
    .map((i) => i.iid), [2], '状态过滤应只保留运行中条目')
  assert.deepEqual(filterIssuesByFilter(issues, { status: 'open', labels: ['bug'] }, keys, 1)
    .map((i) => i.iid), [1, 3], '组合过滤应同时满足状态与标签')
})

test('filterIssuesByFilter：边界——issues 非数组 / filter 为 null 不崩', () => {
  assert.deepEqual(filterIssuesByFilter(null, { status: 'all', labels: [] }), [],
                   'issues 非数组应回空数组')
  assert.deepEqual(filterIssuesByFilter([{ iid: 1, labels: [] }], null).map((i) => i.iid),
                   [1], 'filter 为 null 应按默认过滤')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询 tasks / pipelines / issues 等多个端点，
// mock 按路径分流；issues / tasks 数据可注入，storage 可注入 localStorage。
async function renderOverview({ issuesPayload, tasksPayload, storage } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return tasksPayload || { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload || { repos: [], errors: [], total: 0 }
    if (pathname === '/api/settings') return { gitlab: {} }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    if (pathname === '/api/settings/deepseek-balance') return { configured: false, balance: null, error: null }
    if (pathname === '/api/issues/completion-stats') return { completed_count: 0, avg_seconds: null, trend: [] }
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
// issue 列表项按钮（点击打开右边栏）
const issueLinks = (root) => findBtns(root, 'issue-link')
// 从 issue-link 按钮文本提取 iid 数组（徽章文案不影响断言）
const linkIids = (root) => issueLinks(root).map((b) => {
  const m = textOf(b.props.children).match(/#(\d+)/)
  return m ? Number(m[1]) : null
})
// 过滤条内的按钮（按类名前缀细分）
const statusBtns = (root) => findBtns(root, 'issue-filter-status')
const labelChips = (root) => findBtns(root, 'issue-filter-label-chip')

// 混合仓库：运行中 bug 1 条、普通 feature 1 条、need-verify 1 条、
// 无标签 1 条、bot-done 1 条
const MIXED_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', priority: 10,
    issues: [
      { iid: 101, title: '运行中 bug',
        updated_at: '2026-08-15 10:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/101',
        labels: [{ name: 'bug' }, { name: 'bot-failed' }] },
      { iid: 102, title: '普通 feature',
        updated_at: '2026-08-15 09:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/102',
        labels: [{ name: 'feature' }] },
      { iid: 103, title: '待验证',
        updated_at: '2026-08-15 08:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/103',
        labels: [{ name: 'need-verify' }, { name: 'bug' }] },
      { iid: 104, title: '无标签',
        updated_at: '2026-08-15 07:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/104',
        labels: [] },
      { iid: 105, title: '已完成',
        updated_at: '2026-08-15 06:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/105',
        labels: [{ name: 'bot-done' }] },
    ],
  }],
  errors: [], total: 5,
}

// 运行中任务：命中 1:101（issue 101 运行中）
const RUNNING_TASKS = {
  tasks: [{ id: 1, status: 'running', repo_id: 1, issue_iid: 101 }],
  total: 1, stats: {},
}

test('渲染：过滤条显示状态按钮、标签候选胶囊与「暂无标签」兜底', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: MIXED_PAYLOAD })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const statuses = statusBtns(root)
    assert.deepEqual(statuses.map((b) => textOf(b.props.children).trim()),
                     ['全部', '开放', '进行中'], '状态按钮应依次为 全部/开放/进行中')
    assert.ok(statuses[0].props.className.includes('active'),
              '默认「全部」应为选中态')
    const chips = labelChips(root)
    assert.deepEqual(chips.map((c) => textOf(c.props.children).trim()).sort(),
                     ['bot-done', 'bot-failed', 'bug', 'feature', 'need-verify'],
                     '标签候选应覆盖全量数据中的全部标签（含 bot 终态标签）')
    assert.equal(findBtns(root, 'issue-filter-reset').length, 0,
                 '未启用过滤时不应显示「清除过滤」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击「进行中」仅展示运行中 issue，仓库卡片保留', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, tasksPayload: RUNNING_TASKS,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(issueLinks(root).length, 5, '初始应展示 5 条 issue')
    const runningBtn = statusBtns(root)[2]
    await TestRenderer.act(() => runningBtn.props.onClick())
    assert.deepEqual(linkIids(root), [101], '进行中过滤应只剩运行中的 issue 101')
    const cards = root.findAll((n) => n.props.className === 'card issue-repo-card')
    assert.equal(cards.length, 1, '仓库分组结构应保留')
    assert.equal(findBtns(root, 'issue-filter-reset').length, 1,
                 '启用过滤后应显示「清除过滤」')
    const headText = textOf(root.findAll(
      (n) => String(n.props.className || '').includes('issue-repo-head'))[0].props.children)
    assert.ok(headText.includes('匹配 1 个'), '过滤激活时仓库头应显示匹配数量')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击标签胶囊按标签过滤，多选为 OR 并集', async () => {
  // 固定「最近更新」排序（issue #286 默认已改为调度器执行顺序），
  // 本测试聚焦过滤语义，排序细节由 overview-issue-sort 测试覆盖
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD,
    storage: makeStorage({ [ISSUE_SORT_STORAGE_KEY]: 'updated' }),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const bugChip = labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'bug')
    assert.ok(bugChip, '应存在 bug 标签胶囊')
    await TestRenderer.act(() => bugChip.props.onClick())
    assert.deepEqual(linkIids(root), [101, 103],
                     '选 bug 应只展示带 bug 标签的 issue（101/103）')
    const activeBug = labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'bug')
    assert.ok(activeBug.props.className.includes('active'),
              '选中的 bug 胶囊应有 active 态')
    const featureChip = labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'feature')
    await TestRenderer.act(() => featureChip.props.onClick())
    assert.deepEqual(linkIids(root), [101, 102, 103],
                     '多选 OR 语义：bug + feature 应展示并集 101/102/103')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：状态 + 标签组合过滤同时生效', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, tasksPayload: RUNNING_TASKS,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => statusBtns(root)[2].props.onClick()) // 进行中
    const bugChip = labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'bug')
    await TestRenderer.act(() => bugChip.props.onClick())
    assert.deepEqual(linkIids(root), [101],
                     '进行中 + bug 应只展示运行中且带 bug 的 issue 101')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：localStorage 预置过滤偏好 → 初始渲染即生效', async () => {
  const storage = makeStorage({
    [ISSUE_FILTER_STORAGE_KEY]: JSON.stringify({ status: 'all', labels: ['need-verify'] }),
  })
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, storage,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.deepEqual(linkIids(root), [103],
                     '预置 need-verify 过滤应初始只展示 issue 103')
    const chips = labelChips(root)
    const active = chips.find((c) => c.props.className.includes('active'))
    assert.equal(textOf(active.props.children).trim(), 'need-verify',
                 '预置标签胶囊应为选中态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击过滤后偏好写入 localStorage（刷新后保持）', async () => {
  const storage = makeStorage()
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, tasksPayload: RUNNING_TASKS, storage,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => statusBtns(root)[2].props.onClick()) // 进行中
    await TestRenderer.act(() => labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'bug').props.onClick())
    assert.equal(storage.getItem(ISSUE_FILTER_STORAGE_KEY),
                 JSON.stringify({ status: 'running', labels: ['bug'] }),
                 '状态与标签选择应持久化到 localStorage')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：过滤无匹配时显示「没有匹配过滤条件的 issue」，清除过滤恢复全量', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, tasksPayload: RUNNING_TASKS,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => statusBtns(root)[2].props.onClick()) // 进行中
    const featureChip = labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'feature')
    await TestRenderer.act(() => featureChip.props.onClick())
    assert.equal(issueLinks(root).length, 0, '运行中 + feature 无交集应无条目')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('没有匹配过滤条件的 issue'),
              '应显示无匹配空态文案')
    const resetBtn = findBtns(root, 'issue-filter-reset')[0]
    assert.ok(resetBtn, '无匹配空态应提供「清除过滤」')
    await TestRenderer.act(() => resetBtn.props.onClick())
    assert.equal(issueLinks(root).length, 5, '清除过滤后应恢复全部 5 条')
    assert.equal(findBtns(root, 'issue-filter-reset').length, 0,
                 '恢复全量后「清除过滤」应消失')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：无任何 issue 时不渲染过滤条，保持「暂无开放 issue」空态', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: { repos: [], errors: [], total: 0 },
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(statusBtns(root).length, 0, '无 issue 时不应渲染过滤条')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无开放 issue'), '应保持全局空态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：未过滤时零 issue 仓库卡片保持仓库级空状态（回归保护）', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: {
      repos: [
        { repo_id: 1, repo_name: 'botler', priority: 10,
          issues: [{ iid: 1, title: '有 issue',
                     updated_at: '2026-08-15 02:00:00',
                     web_url: 'https://gitlab.example.com/x/-/issues/1',
                     labels: [{ name: 'bug' }] }] },
        { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
      ],
      errors: [], total: 1,
    },
  })
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('该仓库暂无开放 issue'),
              '未过滤时空仓库卡片应保持仓库级空状态文案')
    assert.equal(issueLinks(renderer.root).length, 1, '非空仓库 issue 应正常展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：过滤激活时无匹配条目的仓库整卡隐藏，匹配仓库仅展示匹配条目', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: {
      repos: [
        { repo_id: 1, repo_name: 'botler', priority: 10,
          issues: [{ iid: 1, title: 'bug 一',
                     updated_at: '2026-08-15 02:00:00',
                     web_url: 'https://gitlab.example.com/x/-/issues/1',
                     labels: [{ name: 'bug' }] },
                   { iid: 2, title: 'feature 一',
                     updated_at: '2026-08-15 01:00:00',
                     web_url: 'https://gitlab.example.com/x/-/issues/2',
                     labels: [{ name: 'feature' }] }] },
        { repo_id: 2, repo_name: 'shipyard', priority: 20,
          issues: [{ iid: 10, title: 'feature 二',
                     updated_at: '2026-08-15 00:00:00',
                     web_url: 'https://gitlab.example.com/x/-/issues/10',
                     labels: [{ name: 'feature' }] }] },
      ],
      errors: [], total: 3,
    },
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => labelChips(root).find(
      (c) => textOf(c.props.children).trim() === 'bug').props.onClick())
    const cards = root.findAll((n) => n.props.className === 'card issue-repo-card')
    assert.equal(cards.length, 1, '无 bug 标签的仓库 2 应整卡隐藏')
    assert.deepEqual(linkIids(root), [1], '匹配仓库仅展示带 bug 标签的条目')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
