// 概览页「开放 Issue」单列分组布局「状态 → 仓库」两级分组测试（issue
// #485）：单列分组改为先按 issue 状态分组——进行中（running）第一 →
// 完成任务（done）第二 → 失败任务（failed）第三 → 其他（other）第四，
// 状态组内再按仓库分组。状态判定复用 groupIssuesByBotLabel 语义
// （running 优先于终态标签）。
//
// 断言：
// 1. COLUMN_ISSUE_GROUPS 组顺序：running → done → failed → other
//    （与卡片布局 ISSUE_GROUPS 的 running → failed → done → other
//    不同，完成任务排在失败任务之前）；
// 2. groupIssuesByStatusThenRepo 纯函数：多仓库混合状态正确归类、
//    组内仓库子分组保持输入仓库顺序、组内 issue 保持原始相对顺序、
//    running 优先于终态标签、零 issue 仓库不出现、状态键齐全；
// 3. 边界：空数组 / 非数组 / null 元素 / 缺 repo_id / 仓库无 issues /
//    issue labels 缺失或非数组 → 不崩且归类正确；
// 4. 渲染：进行中组置顶、状态组顺序与计数、状态组内仓库子分组齐全、
//    全部 issue 不丢失。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-layout-column.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, COLUMN_ISSUE_GROUPS, groupIssuesByStatusThenRepo } =
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

test('IssueListSection 渲染使用「状态 → 仓库」两级分组（issue #485）', () => {
  assert.equal(typeof groupIssuesByStatusThenRepo, 'function',
               '应导出 groupIssuesByStatusThenRepo 分组函数')
  assert.equal(typeof COLUMN_ISSUE_GROUPS, 'object',
               '应导出 COLUMN_ISSUE_GROUPS 状态组定义')
  assert.match(sectionSrc, /COLUMN_ISSUE_GROUPS\.map/,
               '单列布局渲染应按状态组遍历')
  assert.match(sectionSrc, /renderColumnRepoGroup\(rg, g\.key\)/,
               '状态组内应按仓库分组渲染子分组')
})

test('状态组顺序：进行中 → 完成任务 → 失败任务 → 其他（issue #485）', () => {
  assert.deepEqual(COLUMN_ISSUE_GROUPS.map((g) => g.key),
                   ['running', 'done', 'failed', 'other'],
                   '单列分组状态组顺序应为 running/done/failed/other')
})

// ---- groupIssuesByStatusThenRepo 纯函数测试 ----

// 造 repo：repo_id 必填，issues 为 issue 数组
const repo = (id, name, issues) => ({ repo_id: id, repo_name: name, issues })

test('正常路径：多仓库混合状态归入正确状态组，组内按仓库分组', () => {
  const runningKeys = new Set(['1:102', '2:201'])
  const repos = [
    repo(1, 'botler', [
      { iid: 101, labels: [{ name: 'bot-done' }] },
      { iid: 102, labels: [{ name: 'bot-done' }] }, // 运行中优先 → running
      { iid: 103, labels: [{ name: 'feature' }] },
    ]),
    repo(2, 'docs-site', [
      { iid: 201, labels: [{ name: 'bot-failed' }] }, // 运行中优先 → running
      { iid: 202, labels: [{ name: 'bot-failed' }] },
      { iid: 203, labels: [] },
    ]),
    repo(3, 'empty-repo', []),
  ]
  const out = groupIssuesByStatusThenRepo(repos, runningKeys)
  // 状态键齐全
  assert.deepEqual(Object.keys(out), ['running', 'done', 'failed', 'other'])
  // running：repo1 的 102 + repo2 的 201
  assert.deepEqual(out.running.map((r) => [r.repo_id, r.issues.map((i) => i.iid)]),
                   [[1, [102]], [2, [201]]], '进行中组内按仓库分组')
  // done：repo1 的 101
  assert.deepEqual(out.done.map((r) => [r.repo_id, r.issues.map((i) => i.iid)]),
                   [[1, [101]]], '完成任务组只含 bot-done')
  // failed：repo2 的 202
  assert.deepEqual(out.failed.map((r) => [r.repo_id, r.issues.map((i) => i.iid)]),
                   [[2, [202]]], '失败任务组只含 bot-failed')
  // other：repo1 的 103 + repo2 的 203
  assert.deepEqual(out.other.map((r) => [r.repo_id, r.issues.map((i) => i.iid)]),
                   [[1, [103]], [2, [203]]], '其他组含无终态标签 issue')
  // 零 issue 仓库不出现
  assert.ok(!out.running.some((r) => r.repo_id === 3)
    && !out.done.some((r) => r.repo_id === 3)
    && !out.failed.some((r) => r.repo_id === 3)
    && !out.other.some((r) => r.repo_id === 3),
    '零 issue 仓库不应出现在任何状态组')
})

test('仓库子分组保持输入仓库相对顺序；组内 issue 保持原始相对顺序', () => {
  const repos = [
    repo(1, 'a', [{ iid: 1, labels: [] }, { iid: 2, labels: [] }]),
    repo(2, 'b', [{ iid: 3, labels: [] }]),
    repo(3, 'c', [{ iid: 4, labels: [] }]),
  ]
  const out = groupIssuesByStatusThenRepo(repos, new Set())
  assert.deepEqual(out.other.map((r) => r.repo_id), [1, 2, 3],
                   '其他组仓库子分组保持输入顺序')
  assert.deepEqual(out.other[0].issues.map((i) => i.iid), [1, 2],
                   '组内 issue 保持原始相对顺序')
})

test('running 优先于终态标签：重试中的 bot-failed / bot-done 归入进行中', () => {
  const runningKeys = new Set(['1:1', '1:2'])
  const repos = [
    repo(1, 'a', [
      { iid: 1, labels: [{ name: 'bot-failed' }, { name: 'bot-done' }] },
      { iid: 2, labels: [{ name: 'bot-failed' }] },
    ]),
  ]
  const out = groupIssuesByStatusThenRepo(repos, runningKeys)
  assert.deepEqual(out.running[0].issues.map((i) => i.iid), [1, 2],
                   '运行中 issue 全部归入进行中组')
  assert.equal(out.failed.length, 0, '失败任务组不应再含运行中 issue')
  assert.equal(out.done.length, 0, '完成任务组不应再含运行中 issue')
})

test('边界：空数组 / 非数组 / null 入参 → 各状态组为空数组且不崩', () => {
  for (const bad of [[], null, undefined, 'x', 42]) {
    const out = groupIssuesByStatusThenRepo(bad, new Set())
    assert.deepEqual(out,
                     { running: [], done: [], failed: [], other: [] },
                     `入参 ${JSON.stringify(bad)} 应返回全空状态组`)
  }
})

test('边界：null / 非对象 / 缺 repo_id 仓库元素跳过且不崩', () => {
  const out = groupIssuesByStatusThenRepo(
    [null, undefined, 'str', 42, {}, { repo_id: null, issues: [] },
     repo(1, 'ok', [{ iid: 1, labels: [] }])],
    new Set())
  assert.deepEqual(out.other.map((r) => r.repo_id), [1],
                   '仅合法仓库进入分组，异常元素跳过')
})

test('边界：仓库 issues 缺失 / null / 非数组 → 该仓库不进入任何状态组', () => {
  const out = groupIssuesByStatusThenRepo(
    [repo(1, 'no-issues'), repo(2, 'null-issues', null),
     repo(3, 'str-issues', 'x'), repo(4, 'ok', [{ iid: 9, labels: [] }])],
    new Set())
  for (const key of ['running', 'done', 'failed', 'other']) {
    assert.ok(!out[key].some((r) => r.repo_id !== 4),
              `${key} 组只应含合法仓库`)
  }
  assert.deepEqual(out.other.map((r) => r.repo_id), [4])
})

test('边界：issue labels 缺失 / null / 非数组归入其他组且不崩', () => {
  const out = groupIssuesByStatusThenRepo(
    [repo(1, 'a', [
      { iid: 1 }, { iid: 2, labels: null }, { iid: 3, labels: 'bot-done' },
    ])],
    new Set())
  assert.deepEqual(out.other[0].issues.map((i) => i.iid), [1, 2, 3],
                   'labels 缺失/异常 issue 全部归入其他组')
})

test('边界：过滤后的仓库输入（子集）同样按状态分组不重排', () => {
  // 模拟过滤后某仓库只剩 1 条 bot-done issue
  const repos = [repo(1, 'a', [{ iid: 5, labels: [{ name: 'bot-done' }] }])]
  const out = groupIssuesByStatusThenRepo(repos, new Set())
  assert.deepEqual(out.done.map((r) => [r.repo_id, r.issues.map((i) => i.iid)]),
                   [[1, [5]]], '过滤后仓库仍按状态正确归类')
  assert.equal(out.other.length, 0, '无其他状态 issue 时其他组为空')
})

// ---- 组件渲染 ----

async function renderOverview({ issuesPayload, tasksPayload, storage } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return tasksPayload || { tasks: [], total: 0, stats: {} }
    }
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

const columnLists = (root) => root.findAll(
  (n) => String(n.props.className || '').includes('issues-list-column'))
const statusGroupTitles = (root) => root.findAll(
  (n) => String(n.props.className || '').includes('issue-group-title'))

// 三仓库混合状态 + 一个运行中任务（repo1 的 102）
const MIXED_PAYLOAD = {
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
        { iid: 103, title: 'botler 三',
          updated_at: '2026-08-15 08:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/103',
          labels: [{ name: 'bug' }] },
      ],
    },
    {
      repo_id: 2, repo_name: 'docs-site', priority: 20,
      issues: [
        { iid: 201, title: 'docs 一',
          updated_at: '2026-08-15 07:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/docs-site/-/issues/201',
          labels: [{ name: 'bot-failed' }] },
        { iid: 202, title: 'docs 二',
          updated_at: '2026-08-15 06:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/docs-site/-/issues/202',
          labels: [{ name: 'bot-failed' }] },
      ],
    },
    {
      repo_id: 3, repo_name: 'frontend-kit', priority: 30,
      issues: [
        { iid: 301, title: 'kit 一',
          updated_at: '2026-08-15 05:00:00',
          web_url: 'https://gitlab.example.com/chenkaidi/frontend-kit/-/issues/301',
          labels: [] },
      ],
    },
  ],
  errors: [], total: 6,
}

test('渲染：进行中组置顶、状态组顺序与计数正确、组内仓库子分组齐全、issue 不丢失', async () => {
  const storage = makeStorage({ 'botler.overview.layout': 'column' })
  const tasksPayload = {
    tasks: [{ id: 9001, repo_id: 1, issue_iid: 102, status: 'running' }],
    total: 1, stats: {},
  }
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: MIXED_PAYLOAD, tasksPayload, storage })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    assert.equal(columnLists(root).length, 1, '应渲染单列分组容器')
    // 状态组顺序：进行中 → 完成任务 → 失败任务 → 其他（全部非空）
    assert.deepEqual(statusGroupTitles(root).map((n) => textOf(n.props.children).trim()),
                     ['运行中', 'bot-done', 'bot-failed', '其他'],
                     '状态组顺序应为 运行中/bot-done/bot-failed/其他')
    // 状态组计数：进行中 1（102）/ 完成任务 1（101）/ 失败任务 2
    // （201、202）/ 其他 2（103、301）
    const groupCounts = root.findAll(
      (n) => String(n.props.className || '').includes('issue-group-count'))
    assert.deepEqual(groupCounts.map((n) => textOf(n.props.children).trim()),
                     ['1 个', '1 个', '2 个', '2 个'], '状态组计数应为各组 issue 数之和')
    // 仓库子分组：进行中 1 个（repo1）+ 完成任务 1 个（repo1）+
    // 失败任务 1 个（repo2）+ 其他 2 个（repo1、repo3）
    const repoGroups = root.findAll(
      (n) => String(n.props.className || '').split(' ').includes('issue-repo-group'))
    assert.equal(repoGroups.length, 5, '状态组内仓库子分组总数应为 5')
    // 全部 6 条 issue 不丢失
    const items = root.findAll((n) => String(n.props.className || '').includes('issue-item'))
    assert.equal(items.length, 6, '全部 issue 均应渲染')
    const titles = items.map((n) => textOf(n.props.children).trim())
    for (const t of ['botler 一', 'botler 二', 'botler 三', 'docs 一', 'docs 二', 'kit 一']) {
      assert.ok(titles.some((x) => x.includes(t)), `组内应包含 issue「${t}」`)
    }
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：无任何 issue 时单列分组不渲染状态组（空态兜底）', async () => {
  const storage = makeStorage({ 'botler.overview.layout': 'column' })
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: { repos: [], errors: [], total: 0 }, storage })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(columnLists(root).length, 0, '无开放 issue 不应渲染单列容器')
    assert.equal(statusGroupTitles(root).length, 0, '不应渲染任何状态组')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
