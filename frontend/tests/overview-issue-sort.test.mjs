// 概览页「开放 Issue」排序方法切换测试（issue #286）：开放 issue 板块
// 增加排序方法切换组件（调度器执行顺序 / 最近更新 / 创建时间），默认
// 按「调度器执行顺序」排序——与任务调度器派发语义一致（仓库优先级 →
// issue 标签优先级 → issue 创建时间升序，创建早的先处理），方便预判各
// 分组 issue 的处理顺序；排序偏好存 localStorage（键
// botler.overview.issueSort），刷新后保持。
//
// 断言：
// 1. 纯函数：loadIssueSort / saveIssueSort 的存取与规范化边界（无存储/
//    损坏数据/未知排序键回默认、非法键不写入、setItem 抛异常静默）、
//    issueLabelWeight（标签优先级索引、未命中排最后、空优先级回内置默认）、
//    schedulerOrderKey（创建时间优先、缺失按更新时间兜底）、
//    sortIssuesByMethod 三种排序语义（scheduler 标签权重→创建时间升序 /
//    updated 降序 / created 降序）与稳定性、非数组入参兜底；
// 2. 渲染：有 issue 时排序条渲染且默认「调度器执行顺序」选中、默认排序
//    生效（bug 先于 feature）、点击「最近更新」/「创建时间」切换生效并
//    写入 localStorage、预置偏好初始生效、无 issue 不渲染排序条；
// 3. 分组内排序：排序作用于各分组内部，分组结构（bot-done / 其他）不变。
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
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, loadIssueSort, saveIssueSort, issueLabelWeight,
        schedulerOrderKey, sortIssuesByMethod, ISSUE_SORTS,
        ISSUE_SORT_STORAGE_KEY, DEFAULT_ISSUE_PRIORITY } =
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

test('Overview.jsx 导出排序函数与存储键，渲染使用排序逻辑', () => {
  assert.equal(typeof loadIssueSort, 'function', '应导出 loadIssueSort')
  assert.equal(typeof saveIssueSort, 'function', '应导出 saveIssueSort')
  assert.equal(typeof sortIssuesByMethod, 'function', '应导出 sortIssuesByMethod')
  assert.equal(ISSUE_SORT_STORAGE_KEY, 'botler.overview.issueSort',
               '存储键应为 botler.overview.issueSort')
  assert.deepEqual(DEFAULT_ISSUE_PRIORITY, ['bug', 'test', 'feature'],
                   '调度器默认标签优先级应为 bug > test > feature')
  assert.deepEqual(ISSUE_SORTS.map((s) => s.key),
                   ['scheduler', 'updated', 'created'],
                   '排序选项顺序应为 调度器执行顺序 → 最近更新 → 创建时间')
  assert.equal(ISSUE_SORTS[0].label, '调度器执行顺序', '默认排序应为调度器执行顺序')
  assert.match(overview, /sortIssuesByMethod\(r\.issues, issueSort, issuePriority\)/,
               '渲染应调用 sortIssuesByMethod 重排 issue')
  assert.match(overview, /ISSUE_SORTS\.map/, '排序按钮应遍历 ISSUE_SORTS')
  assert.match(overview, /issue-sort-option/, '渲染应包含排序按钮类名')
})

test('i18n：排序文案中英文键齐全', () => {
  for (const k of ['overview.sort', 'overview.sortTitle', 'overview.sortAria']) {
    assert.ok(zhCN[k], `zh-CN 应有 ${k}`)
    assert.ok(enUS[k], `en-US 应有 ${k}`)
  }
  for (const s of ISSUE_SORTS) {
    assert.ok(zhCN[`overview.sortBy.${s.key}`], `zh-CN 应有 overview.sortBy.${s.key}`)
    assert.ok(enUS[`overview.sortBy.${s.key}`], `en-US 应有 overview.sortBy.${s.key}`)
    assert.ok(zhCN[`overview.sortHint.${s.key}`], `zh-CN 应有 overview.sortHint.${s.key}`)
    assert.ok(enUS[`overview.sortHint.${s.key}`], `en-US 应有 overview.sortHint.${s.key}`)
  }
})

// ---- loadIssueSort / saveIssueSort 纯函数测试 ----

test('loadIssueSort：无存储 / storage 为 null / 未存值 / 空串 → 默认调度器执行顺序', () => {
  assert.equal(loadIssueSort(undefined), 'scheduler', '无 storage 应回默认')
  assert.equal(loadIssueSort(null), 'scheduler', 'storage 为 null 应回默认')
  assert.equal(loadIssueSort(makeStorage()), 'scheduler', '未存值应回默认')
  assert.equal(loadIssueSort(makeStorage({ [ISSUE_SORT_STORAGE_KEY]: '' })), 'scheduler',
               '空串应回默认')
})

test('loadIssueSort：未知排序键 / getItem 抛异常 → 默认', () => {
  assert.equal(loadIssueSort(makeStorage({ [ISSUE_SORT_STORAGE_KEY]: 'unknown' })), 'scheduler',
               '未知排序键应回默认（手改/旧版本写入兜底）')
  assert.equal(loadIssueSort(makeStorage({ [ISSUE_SORT_STORAGE_KEY]: 'foo' })), 'scheduler',
               '非法值应回默认')
  const storage = { getItem: () => { throw new Error('denied') } }
  assert.equal(loadIssueSort(storage), 'scheduler', 'getItem 抛异常应回默认')
})

test('loadIssueSort：已知排序键原样返回', () => {
  for (const key of ['scheduler', 'updated', 'created']) {
    assert.equal(loadIssueSort(makeStorage({ [ISSUE_SORT_STORAGE_KEY]: key })), key,
                 `应识别排序键 ${key}`)
  }
})

test('saveIssueSort：无 storage / 非法键不写入；合法键写入；setItem 抛异常静默', () => {
  assert.doesNotThrow(() => saveIssueSort(null, 'updated'))
  assert.doesNotThrow(() => saveIssueSort(undefined, 'updated'))
  assert.doesNotThrow(() => saveIssueSort(makeStorage(), 'unknown'))
  const s1 = makeStorage()
  saveIssueSort(s1, 'unknown')
  assert.equal(s1.getItem(ISSUE_SORT_STORAGE_KEY), null, '非法键不得写入')
  const s2 = makeStorage()
  saveIssueSort(s2, 'created')
  assert.equal(s2.getItem(ISSUE_SORT_STORAGE_KEY), 'created', '合法键应写入')
  const s3 = { setItem: () => { throw new Error('denied') } }
  assert.doesNotThrow(() => saveIssueSort(s3, 'updated'), '存储异常应静默忽略')
})

// ---- issueLabelWeight / schedulerOrderKey 纯函数测试 ----

test('issueLabelWeight：命中配置标签按索引定权，未命中排最后', () => {
  const p = ['bug', 'test', 'feature']
  assert.equal(issueLabelWeight({ labels: [{ name: 'bug' }] }, p), 0, 'bug 权重 0')
  assert.equal(issueLabelWeight({ labels: [{ name: 'test' }] }, p), 1, 'test 权重 1')
  assert.equal(issueLabelWeight({ labels: [{ name: 'feature' }] }, p), 2, 'feature 权重 2')
  assert.equal(issueLabelWeight({ labels: [{ name: 'ui' }] }, p), 3, '未命中排最后')
  assert.equal(issueLabelWeight({ labels: [] }, p), 3, '无标签排最后')
  assert.equal(issueLabelWeight({}, p), 3, 'labels 缺失排最后')
  assert.equal(issueLabelWeight({ labels: [{ name: 'ui' }, { name: 'bug' }] }, p), 0,
               '首个命中标签索引即权重')
  assert.equal(issueLabelWeight({ labels: ['test'] }, p), 1, '标签为纯字符串也兼容')
})

test('issueLabelWeight：优先级空/非数组回内置默认（bug > test > feature）', () => {
  assert.equal(issueLabelWeight({ labels: [{ name: 'bug' }] }, []), 0, '空数组回默认')
  assert.equal(issueLabelWeight({ labels: [{ name: 'bug' }] }, null), 0, 'null 回默认')
  assert.equal(issueLabelWeight({ labels: [{ name: 'ui' }] }, []), 3, '默认未命中排最后')
})

test('issueLabelWeight：自定义优先级生效（设置页 worker.issue_priority）', () => {
  const p = ['ui', 'bug']
  assert.equal(issueLabelWeight({ labels: [{ name: 'ui' }] }, p), 0, 'ui 权重 0')
  assert.equal(issueLabelWeight({ labels: [{ name: 'bug' }] }, p), 1, 'bug 权重 1')
  assert.equal(issueLabelWeight({ labels: [{ name: 'feature' }] }, p), 2, '未列出的 feature 排最后')
})

test('schedulerOrderKey：创建时间优先，缺失按更新时间兜底，全缺失为空串', () => {
  assert.deepEqual(schedulerOrderKey(
    { created_at: '2026-08-01', updated_at: '2026-08-15' }, ['bug']),
    [1, '2026-08-01'], '应优先取创建时间')
  assert.deepEqual(schedulerOrderKey(
    { updated_at: '2026-08-15' }, ['bug']),
    [1, '2026-08-15'], '创建时间缺失应按更新时间兜底')
  assert.deepEqual(schedulerOrderKey({}, ['bug']), [1, ''], '时间全缺失应为空串')
})

// ---- sortIssuesByMethod 纯函数测试 ----

test('sortIssuesByMethod：scheduler 按标签权重升序 → 创建时间升序', () => {
  const issues = [
    { iid: 1, created_at: '2026-08-02', labels: [{ name: 'feature' }] },
    { iid: 2, created_at: '2026-08-01', labels: [{ name: 'bug' }] },
    { iid: 3, created_at: '2026-08-03', labels: [{ name: 'bug' }] },
    { iid: 4, created_at: '2026-08-01', labels: [] },
  ]
  const out = sortIssuesByMethod(issues, 'scheduler', ['bug', 'test', 'feature'])
  assert.deepEqual(out.map((i) => i.iid), [2, 3, 1, 4],
                   'bug（权重0）先于 feature（权重2）与无标签（权重3）；同权重按创建时间升序')
})

test('sortIssuesByMethod：scheduler 同键保持原相对顺序（稳定排序）', () => {
  const issues = [
    { iid: 1, created_at: '2026-08-01', labels: [{ name: 'bug' }] },
    { iid: 2, created_at: '2026-08-01', labels: [{ name: 'bug' }] },
    { iid: 3, created_at: '2026-08-01', labels: [{ name: 'bug' }] },
  ]
  const out = sortIssuesByMethod(issues, 'scheduler')
  assert.deepEqual(out.map((i) => i.iid), [1, 2, 3], '比较相等时应保持原顺序')
})

test('sortIssuesByMethod：updated 按最后更新时间降序', () => {
  const issues = [
    { iid: 1, updated_at: '2026-08-10' },
    { iid: 2, updated_at: '2026-08-15' },
    { iid: 3, updated_at: '2026-08-12' },
  ]
  assert.deepEqual(sortIssuesByMethod(issues, 'updated').map((i) => i.iid),
                   [2, 3, 1], '最近更新在前')
  assert.deepEqual(sortIssuesByMethod(issues, 'other').map((i) => i.iid),
                   [2, 3, 1], '未知排序键应按 updated 兜底')
})

test('sortIssuesByMethod：created 按创建时间降序（最新创建在前）', () => {
  const issues = [
    { iid: 1, created_at: '2026-08-10' },
    { iid: 2, created_at: '2026-08-15' },
    { iid: 3, created_at: '2026-08-12' },
  ]
  assert.deepEqual(sortIssuesByMethod(issues, 'created').map((i) => i.iid),
                   [2, 3, 1], '最新创建在前')
})

test('sortIssuesByMethod：边界——非数组 / 空数组 / 元素缺失不崩', () => {
  assert.deepEqual(sortIssuesByMethod(null, 'scheduler'), [], '非数组应回空数组')
  assert.deepEqual(sortIssuesByMethod(undefined, 'scheduler'), [], 'undefined 应回空数组')
  assert.deepEqual(sortIssuesByMethod([], 'scheduler'), [], '空数组应回空数组')
  assert.deepEqual(sortIssuesByMethod([null, { iid: 1, labels: [] }, undefined], 'scheduler')
    .map((i) => i && i.iid), [null, 1, undefined], '含 null/undefined 元素不崩且稳定')
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
// issue 列表项按钮（点击打开右边栏）
const issueLinks = (root) => findBtns(root, 'issue-link')
// 从 issue-link 按钮文本提取 iid 数组（徽章文案不影响断言）
const linkIids = (root) => issueLinks(root).map((b) => {
  const m = textOf(b.props.children).match(/#(\d+)/)
  return m ? Number(m[1]) : null
})
// 排序按钮（调度器执行顺序 / 最近更新 / 创建时间）
const sortBtns = (root) => findBtns(root, 'issue-sort-option')

// 单仓库四类标签 issue：bug / test / feature / 无标签，各带创建时间
// 与更新时间（scheduler 排序 = 权重升序 → 创建时间升序）
const SORT_PAYLOAD = {
  repos: [{
    repo_id: 1, repo_name: 'botler', priority: 10,
    issues: [
      { iid: 1, title: 'bug 一', created_at: '2026-08-01', updated_at: '2026-08-10',
        web_url: 'https://gitlab.example.com/x/-/issues/1',
        labels: [{ name: 'bug' }] },
      { iid: 2, title: 'feature 一', created_at: '2026-08-02', updated_at: '2026-08-09',
        web_url: 'https://gitlab.example.com/x/-/issues/2',
        labels: [{ name: 'feature' }] },
      { iid: 3, title: 'test 一', created_at: '2026-08-03', updated_at: '2026-08-08',
        web_url: 'https://gitlab.example.com/x/-/issues/3',
        labels: [{ name: 'test' }] },
      { iid: 4, title: '无标签一', created_at: '2026-08-04', updated_at: '2026-08-07',
        web_url: 'https://gitlab.example.com/x/-/issues/4',
        labels: [] },
    ],
  }],
  errors: [], total: 4,
}

test('渲染：排序条渲染，默认选中「调度器执行顺序」且默认排序生效', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: SORT_PAYLOAD })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const btns = sortBtns(root)
    assert.equal(btns.length, 3, '应渲染三个排序选项')
    assert.deepEqual(btns.map((b) => textOf(b.props.children).trim()),
                     ['调度器执行顺序', '最近更新', '创建时间'], '排序选项文案应齐全')
    assert.ok(btns[0].props.className.includes('active'),
              '默认「调度器执行顺序」应为选中态')
    assert.equal(btns[0].props['aria-pressed'], true, '默认选中项 aria-pressed 应为 true')
    // 默认调度器执行顺序：bug（0）→ test（1）→ feature（2）→ 无标签（3）
    assert.deepEqual(linkIids(root), [1, 3, 2, 4],
                     '默认应按调度器执行顺序展示（bug 先于 test/feature/无标签）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击「最近更新」→ 按更新时间降序并持久化偏好', async () => {
  const storage = makeStorage()
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: SORT_PAYLOAD, storage,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const updatedBtn = sortBtns(root)[1]
    await TestRenderer.act(() => updatedBtn.props.onClick())
    assert.deepEqual(linkIids(root), [1, 2, 3, 4], '最近更新应展示最新更新在前')
    const after = sortBtns(root)
    assert.ok(after[1].props.className.includes('active'), '「最近更新」应为选中态')
    assert.equal(after[0].props.className.includes('active'), false,
                 '原默认选项应取消选中')
    assert.equal(storage.getItem(ISSUE_SORT_STORAGE_KEY), 'updated',
                 '点击后应持久化排序偏好')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：点击「创建时间」→ 按创建时间降序（最新创建在前）', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: SORT_PAYLOAD })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => sortBtns(root)[2].props.onClick())
    assert.deepEqual(linkIids(root), [4, 3, 2, 1], '创建时间降序应最新创建在前')
    assert.ok(sortBtns(root)[2].props.className.includes('active'),
              '「创建时间」应为选中态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：localStorage 预置排序偏好 → 初始渲染即按该排序且选中态正确', async () => {
  const storage = makeStorage({ [ISSUE_SORT_STORAGE_KEY]: 'created' })
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: SORT_PAYLOAD, storage,
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.deepEqual(linkIids(root), [4, 3, 2, 1], '预置 created 应初始按创建时间降序')
    const btns = sortBtns(root)
    assert.ok(btns[2].props.className.includes('active'), '「创建时间」应为初始选中态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：排序作用于各分组内部，分组结构不变', async () => {
  const payload = {
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 101, title: 'done feature', created_at: '2026-08-01',
          updated_at: '2026-08-10', web_url: 'https://gitlab.example.com/x/-/issues/101',
          labels: [{ name: 'bot-done' }, { name: 'feature' }] },
        { iid: 102, title: 'done bug', created_at: '2026-08-02',
          updated_at: '2026-08-09', web_url: 'https://gitlab.example.com/x/-/issues/102',
          labels: [{ name: 'bot-done' }, { name: 'bug' }] },
        { iid: 103, title: '其他 bug', created_at: '2026-08-03',
          updated_at: '2026-08-08', web_url: 'https://gitlab.example.com/x/-/issues/103',
          labels: [{ name: 'bug' }] },
      ],
    }],
    errors: [], total: 3,
  }
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: payload })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 分组顺序 bot-done → 其他；bot-done 组内按调度器顺序：bug（102）先于
    // feature（101）
    assert.deepEqual(linkIids(root), [102, 101, 103],
                     '分组结构保持，组内按调度器执行顺序（bug 先于 feature）')
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['bot-done', '其他'], '组标题与分组结构不变')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：无开放 issue 时不渲染排序条', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: { repos: [], errors: [], total: 0 },
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(sortBtns(root).length, 0, '无 issue 时不应渲染排序条')
    assert.equal(findBtns(root, 'issue-filter-bar').length, 0, '无 issue 时不应渲染过滤条')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
