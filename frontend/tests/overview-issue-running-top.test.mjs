// 概览页「开放 Issue」板块正在运行的 issue 置顶测试（issue #101）：正在
// 执行的任务（running/retrying，即 LIVE_STATUSES）命中的 issue 从原 bot
// 终态分组中移出，单独成「⚙️ 运行中」组置于仓库 issue 列表最上方（优先
// 于 bot-failed / bot-done / 其他三组展示）；任务结束（键从任务轮询消失）
// 后自动回落原分组。复用 issue #99 的 runningIssueKeys 匹配逻辑，零新增
// 接口。
//
// 断言：
// 1. groupIssuesByBotLabel 纯函数：runningKeys 命中的 issue 归 running
//    组，未命中按 bot 终态标签分组，组内保持原始相对顺序；
// 2. 边界：runningKeys 缺失/null/非 Set、repoId 数字/字符串/undefined、
//    running 与 bot-done/bot-failed 并存（运行中优先于终态分组）、
//    100 条混合数据均不崩且归类正确、总量不丢；
// 3. 渲染：running 组置顶（组标题顺序 running → failed → done →
//    other）、组计数、多个运行中 issue 保持原相对顺序、跨仓库同 iid
//    不误置顶、无活跃任务不渲染 running 组、任务结束后回落原分组、
//    置顶项保留 issue-item-running 高亮类与「⚙️ 运行中」徽章。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const overview = readFileSync(path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, groupIssuesByBotLabel, ISSUE_GROUPS,
        runningIssueKeys, ISSUE_SORT_STORAGE_KEY } =
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


// ---- 数据流源码断言 ----

test('Overview.jsx 分组函数支持 runningKeys/repoId 参数，running 组置顶', () => {
  assert.equal(typeof groupIssuesByBotLabel, 'function',
               '应导出 groupIssuesByBotLabel 分组函数')
  assert.match(overview, /groupIssuesByBotLabel\(r\.issues, runningKeys, r\.repo_id\)/,
               '渲染应传入 runningKeys 与 repo_id 参与分组')
  assert.deepEqual(ISSUE_GROUPS.map((g) => g.key),
                   ['running', 'failed', 'done', 'other'],
                   '组顺序应为 running → failed → done → other（运行中置顶）')
  assert.equal(ISSUE_GROUPS[0].title, '运行中', 'running 组标题应为「运行中」')
})

// ---- groupIssuesByBotLabel 纯函数测试 ----

test('正常路径：runningKeys 命中归 running 组，未命中按终态标签分组', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'feature' }] },
    { iid: 2, labels: [{ name: 'bot-done' }] },
    { iid: 3, labels: [{ name: 'bot-failed' }] },
    { iid: 4, labels: [] },
  ]
  const keys = new Set(['1:1'])
  const g = groupIssuesByBotLabel(issues, keys, 1)
  assert.deepEqual(g.running.map((i) => i.iid), [1], '命中的 issue 归 running 组')
  assert.deepEqual(g.done.map((i) => i.iid), [2], 'bot-done 归 done 组')
  assert.deepEqual(g.failed.map((i) => i.iid), [3], 'bot-failed 归 failed 组')
  assert.deepEqual(g.other.map((i) => i.iid), [4], '无标签归 other 组')
})

test('组内保持原始相对顺序（running 组与其他组均不重排）', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'feature' }] },
    { iid: 2, labels: [{ name: 'bot-done' }] },
    { iid: 3, labels: [{ name: 'feature' }] },
    { iid: 4, labels: [{ name: 'bot-done' }] },
    { iid: 5, labels: [] },
  ]
  // 命中 3 与 5：running 组内保持原始先后（3 在 5 前）
  const g = groupIssuesByBotLabel(issues, new Set(['7:3', '7:5']), 7)
  assert.deepEqual(g.running.map((i) => i.iid), [3, 5], 'running 组保持原序')
  assert.deepEqual(g.done.map((i) => i.iid), [2, 4], 'done 组保持原序')
  assert.deepEqual(g.other.map((i) => i.iid), [1], 'other 组保持原序')
})

test('running 与 bot-done/bot-failed 并存时归 running 组（运行中优先终态分组）', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'bot-failed' }, { name: 'in-progress' }] },
    { iid: 2, labels: [{ name: 'bot-done' }] },
  ]
  const g = groupIssuesByBotLabel(issues, new Set(['1:1']), 1)
  assert.deepEqual(g.running.map((i) => i.iid), [1],
                   '重试中的 bot-failed issue 应归 running 组（置顶）')
  assert.deepEqual(g.done.map((i) => i.iid), [2], 'bot-done 归 done 组')
  assert.equal(g.failed.length, 0, '运行中的 failed 标签 issue 不再占 failed 组')
})

test('边界：runningKeys 缺失/null/undefined 时全部按原终态分组且不崩', () => {
  const issues = [
    { iid: 1, labels: [{ name: 'feature' }] },
    { iid: 2, labels: [{ name: 'bot-done' }] },
  ]
  for (const keys of [undefined, null]) {
    const g = groupIssuesByBotLabel(issues, keys, 1)
    assert.equal(g.running.length, 0, '缺 runningKeys 时 running 组应为空')
    assert.deepEqual(g.other.map((i) => i.iid), [1], 'issue 回落 other 组')
    assert.deepEqual(g.done.map((i) => i.iid), [2], 'issue 回落 done 组')
  }
})

test('边界：runningKeys 非 Set（数组/字符串/对象）时不崩且全部回落原分组', () => {
  const issues = [{ iid: 1, labels: [] }]
  for (const keys of [['1:1'], '1:1', { has: null }]) {
    const g = groupIssuesByBotLabel(issues, keys, 1)
    assert.equal(g.running.length, 0, '非 Set 键集合不得误归 running 组')
    assert.equal(g.other.length, 1, 'issue 应回落 other 组')
  }
})

test('边界：repoId 数字/字符串统一按字符串键匹配', () => {
  const issues = [{ iid: 2, labels: [] }]
  // 键字符串化后 '5:2' === `${'5'}:${2}`，数字与字符串 repoId 均可命中
  const gNum = groupIssuesByBotLabel(issues, new Set(['5:2']), 5)
  assert.deepEqual(gNum.running.map((i) => i.iid), [2], '数字 repoId 应命中')
  const gStr = groupIssuesByBotLabel(issues, new Set(['5:2']), '5')
  assert.deepEqual(gStr.running.map((i) => i.iid), [2], '字符串 repoId 应命中')
})

test('边界：100 条混合数据分组计数正确、总量不丢', () => {
  const issues = []
  for (let i = 1; i <= 100; i++) {
    const kind = i % 4
    const labels = kind === 0 ? [{ name: 'bot-done' }]
      : kind === 1 ? [{ name: 'bot-failed' }] : [{ name: 'feature' }]
    issues.push({ iid: i, labels })
  }
  // 4n+2 / 4n+3 位置（25 条）命中 running，其余按终态标签分组
  const keys = new Set()
  for (let i = 1; i <= 100; i++) {
    if (i % 4 === 2 || i % 4 === 3) keys.add(`9:${i}`)
  }
  const g = groupIssuesByBotLabel(issues, keys, 9)
  assert.equal(g.running.length, 50, '50 条命中 running 组')
  assert.equal(g.done.length, 25, '25 条归 done')
  assert.equal(g.failed.length, 25, '25 条归 failed')
  assert.equal(g.other.length, 0, '其余全被 running 组吸收')
  assert.equal(g.running.length + g.done.length + g.failed.length + g.other.length,
               100, '分组不得丢失任何 issue')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// Overview 挂载后同时轮询 tasks / pipelines / issues 三个端点，
// mock 按路径分流；tasks 与 issues 数据均可注入。
async function renderOverview(tasksPayload, issuesPayload, storage) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return tasksPayload
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
    throw new Error('unexpected ' + pathname)
  })
  // 排序偏好注入（issue #286）：本组测试聚焦 running 置顶/回落语义，
  // 传入 storage 固定「最近更新」排序可保持 payload 原始相对顺序断言；
  // 未传时删除 global.localStorage 走默认路径（调度器执行顺序）
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

// 简单内存 storage 替身（localStorage 子集，同 overview-issue-filter.test.mjs）
function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  }
}

// 固定「最近更新」排序的 storage（issue #286：默认已改为调度器执行顺序，
// 本组测试关注 running 置顶/回落，排序细节由 overview-issue-sort 覆盖）
function updatedSortStorage() {
  return makeStorage({ [ISSUE_SORT_STORAGE_KEY]: 'updated' })
}

function buildTasksPayload(...running) {
  return {
    tasks: running.map(([repoId, iid]) => ({
      id: repoId * 1000 + iid, status: 'running', repo_id: repoId,
      repo_name: 'repo-' + repoId, issue_iid: iid, issue_title: 't' + iid,
      issue_url: `https://gitlab.example.com/r${repoId}/-/issues/${iid}`,
    })),
    total: running.length, stats: {},
  }
}

// 单仓库混合数据：failed / 运行中(feature) / done / 其他各一条，
// 原始顺序 101→104（后端 updated_at 降序）
function buildMixedPayload() {
  return {
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: [
        { iid: 101, title: '失败一',
          updated_at: '2026-08-15 10:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/101',
          labels: [{ name: 'bot-failed' }] },
        { iid: 102, title: '运行一',
          updated_at: '2026-08-15 09:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/102',
          labels: [{ name: 'feature' }, { name: 'in-progress' }] },
        { iid: 103, title: '完成一',
          updated_at: '2026-08-15 08:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/103',
          labels: [{ name: 'bot-done' }] },
        { iid: 104, title: '普通一',
          updated_at: '2026-08-15 07:00:00',
          web_url: 'https://gitlab.example.com/x/-/issues/104',
          labels: [{ name: 'bug' }] },
      ],
    }],
    errors: [], total: 4,
  }
}

test('渲染：运行中的 issue 置顶为「⚙️ 运行中」组，其余组顺序不变', async () => {
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload([1, 102]), buildMixedPayload())
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['运行中', 'bot-failed', 'bot-done', '其他'],
                     'running 组应置顶，其后为 failed → done → other')
    const counts = root.findAll((n) => n.props.className === 'issue-group-count')
    // issue #268：计数经 t('overview.groupCount', { n }) 插值为单字符串
    assert.deepEqual(counts.map((c) => c.props.children),
                     ['1 个', '1 个', '1 个', '1 个'],
                     '各组计数均应正确')
    // 置顶项保留 issue #99 的高亮类与「运行中」徽章
    const runningItems = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 1, '置顶的列表项应保留高亮类')
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 1, '置顶项应保留「运行中」徽章')
    // 运行中的 issue 仍可点击打开右边栏，标签胶囊照常渲染
    const pills = root.findAll((n) => n.props.className === 'label-pill')
    assert.deepEqual(pills.map((p) => p.props.children).sort(),
                     ['bug', 'feature', 'in-progress'], '标签胶囊应照常渲染')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：多个运行中 issue 同归 running 组且保持原始相对顺序', async () => {
  const { renderer, renderError, restore } =
    await renderOverview(buildTasksPayload([1, 102], [1, 104]),
                         buildMixedPayload(), updatedSortStorage())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['运行中', 'bot-failed', 'bot-done'],
                     '102 与 104 均置顶后 other 组为空不渲染')
    const counts = root.findAll((n) => n.props.className === 'issue-group-count')
    assert.deepEqual(counts.map((c) => c.props.children),
                     ['2 个', '1 个', '1 个'], 'running 组计数应为 2')
    // running 组内列表项顺序与原始 issue 顺序一致（102 在 104 前）
    const runningItems = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 2, '两条列表项应高亮')
    const titlesInGroup = runningItems.map((n) => {
      const btn = n.findAll((x) => x.type === 'button'
        && String(x.props.className || '').includes('issue-link'))[0]
      return (btn?.props.children || [])
        .filter((c) => typeof c === 'string').join('')
    })
    assert.deepEqual(titlesInGroup, ['运行一', '普通一'],
                     'running 组内保持原始相对顺序')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：无活跃任务时不渲染 running 组（与 issue #80 分组一致）', async () => {
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload(), buildMixedPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'],
                     '无任务时不得渲染 running 组标题')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：跨仓库同 iid 不误置顶（repo_id 参与分组键）', async () => {
  const issues = {
    repos: [
      { repo_id: 1, repo_name: 'botler', priority: 10,
        issues: [
          { iid: 101, title: '仓库一的 101',
            updated_at: '2026-08-15 10:00:00',
            web_url: 'https://gitlab.example.com/x/-/issues/101',
            labels: [{ name: 'feature' }] },
        ] },
      { repo_id: 2, repo_name: 'shipyard', priority: 20,
        issues: [
          { iid: 101, title: '仓库二的 101',
            updated_at: '2026-08-15 09:00:00',
            web_url: 'https://gitlab.example.com/y/-/issues/101',
            labels: [] },
        ] },
    ],
    errors: [], total: 2,
  }
  // 任务只在 repo 2：repo 1 的同 iid 不得置顶
  const { renderer, renderError } =
    await renderOverview(buildTasksPayload([2, 101]), issues)
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['其他', '运行中'],
                     'repo 1 无 running 组，repo 2 的 101 置顶')
    const runningItems = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 1, '仅 repo 2 的 issue 置顶高亮')
    const runningTitles = runningItems.map((n) => {
      const btn = n.findAll((x) => x.type === 'button'
        && String(x.props.className || '').includes('issue-link'))[0]
      return (btn?.props.children || [])
        .filter((c) => typeof c === 'string').join('')
    })
    assert.deepEqual(runningTitles, ['仓库二的 101'], '置顶项应为 repo 2 的 issue')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：任务结束后（任务列表清空）issue 回落原分组', async () => {
  const mixed = buildMixedPayload()
  // 有任务：102 置顶（固定「最近更新」排序，保持 payload 原始顺序）
  const first = await renderOverview(buildTasksPayload([1, 102]), mixed,
                                     updatedSortStorage())
  try {
    assert.equal(first.renderError, null)
    const titles = first.renderer.root.findAll(
      (n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['运行中', 'bot-failed', 'bot-done', '其他'],
                     '任务活跃时 102 应置顶')
  } finally {
    await TestRenderer.act(() => first.renderer.unmount())
    mock.restoreAll()
    first.restore()
  }
  // 任务结束：102 回落「其他」组（带 feature/in-progress 标签）
  const second = await renderOverview(buildTasksPayload(), mixed,
                                      updatedSortStorage())
  try {
    assert.equal(second.renderError, null)
    const root = second.renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'],
                     '任务结束后 running 组消失，issue 回落原分组')
    const runningItems = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item-running'))
    assert.equal(runningItems.length, 0, '任务结束后不得保留高亮')
    // other 组应包含回落的两条：102（feature）与 104（bug）
    const otherTitles = root.findAll((n) => n.type === 'li'
      && String(n.props.className || '').includes('issue-item')).map((n) => {
      const btn = n.findAll((x) => x.type === 'button'
        && String(x.props.className || '').includes('issue-link'))[0]
      return (btn?.props.children || [])
        .filter((c) => typeof c === 'string').join('')
    }).filter((t) => t === '运行一' || t === '普通一')
    assert.deepEqual(otherTitles, ['运行一', '普通一'], '回落 issue 仍在 other 组渲染')
  } finally {
    await TestRenderer.act(() => second.renderer.unmount())
    mock.restoreAll()
    second.restore()
  }
})

test('渲染：任务与 issue 全不匹配时不影响任何分组', async () => {
  const tasks = {
    tasks: [
      { id: 1, status: 'running', repo_id: 9, repo_name: 'other',
        issue_iid: 999, issue_title: '别的仓库的任务',
        issue_url: 'https://gitlab.example.com/o/-/issues/999' },
    ],
    total: 1, stats: {},
  }
  const { renderer, renderError } =
    await renderOverview(tasks, buildMixedPayload())
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const titles = root.findAll((n) => n.props.className === 'issue-group-title')
    assert.deepEqual(titles.map((t) => textOf(t.props.children).trim()),
                     ['bot-failed', 'bot-done', '其他'],
                     '不匹配的任务不得产生 running 组')
    const badges = root.findAll(
      (n) => n.props.className === 'issue-status issue-status-running')
    assert.equal(badges.length, 0, '不得显示运行徽章')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
