// 概览页「其他」分组手动调度顺序测试（issue #287）：在「调度器执行顺序」
// 排序下，「其他」分组（尚未处理/处理中的 issue）支持拖动 issue 上下移动
// 来手动改变调度顺序——拖动后的整组顺序全量保存到后端（PUT
// /api/issues/{project_id}/manual-orders，issue_manual_orders 表），调度器
// 派发时优先按该顺序；overview 聚合结果携带 manual_order 字段供前端渲染。
//
// 断言：
// 1. 纯函数：applyManualOrder（手动顺序前置、缺失 iid 跳过、其余保持原序、
//    空/非数组兜底）、moveItem（上移/下移/同位置/越界/非数组）；
// 2. 渲染：默认「调度器执行顺序」下「其他」分组 li 可拖 + 手柄图标 +
//    组头提示；「最近更新/创建时间」排序 / bot-done 分组 / 过滤激活 /
//    单条目 / 无 project_id 时不启用拖动；overview manual_order 预置时
//    初始渲染即按手动顺序展示（仅调度器排序下生效）；
// 3. 交互：拖拽落点触发 PUT（路径与 iid 载荷正确）、展示顺序更新、保存
//    失败回滚并出现错误提示（点击可关闭）；
// 4. i18n 中英文键齐全。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, applyManualOrder, moveItem } =
  await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

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

test('Overview.jsx 导出拖动排序纯函数，渲染使用手动顺序逻辑', () => {
  assert.equal(typeof applyManualOrder, 'function', '应导出 applyManualOrder')
  assert.equal(typeof moveItem, 'function', '应导出 moveItem')
  assert.match(overview, /applyManualOrder\(items, manualIids\)/,
               '分组渲染应调用 applyManualOrder 应用手动顺序')
  assert.match(overview, /commitManualReorder\(r, ordered, idx\)/,
               '落点应调用 commitManualReorder 提交重排')
  assert.match(overview, /api\.put\(/, '保存应调用 api.put')
  assert.match(overview, /manual-orders/, 'PUT 路径应含 manual-orders')
  assert.match(overview, /gripVertical/, '拖动手柄应使用 gripVertical 图标')
})

test('i18n：手动排序文案中英文键齐全', () => {
  const keys = ['overview.manualOrderHint', 'overview.manualOrderTitle',
                'overview.manualOrderAria', 'overview.manualOrderSaving',
                'overview.manualOrderSaved', 'overview.manualOrderError',
                'overview.manualOrderDisabledSort',
                'overview.manualOrderDisabledFilter']
  for (const k of keys) {
    assert.ok(zhCN[k], `zh-CN 应有 ${k}`)
    assert.ok(enUS[k], `en-US 应有 ${k}`)
  }
})

// ---- applyManualOrder 纯函数测试 ----

const ISSUES = [
  { iid: 1, title: '一' }, { iid: 2, title: '二' },
  { iid: 3, title: '三' }, { iid: 4, title: '四' },
]

test('applyManualOrder：手动顺序前置，其余保持原序', () => {
  assert.deepEqual(applyManualOrder(ISSUES, [3, 1]).map((i) => i.iid),
                   [3, 1, 2, 4], '手动列表中的 issue 应排在前面')
  assert.deepEqual(applyManualOrder(ISSUES, [4, 2, 1, 3]).map((i) => i.iid),
                   [4, 2, 1, 3], '完整手动顺序应原样生效')
})

test('applyManualOrder：手动列表缺失的 iid 自动跳过，其余保持原序', () => {
  assert.deepEqual(applyManualOrder(ISSUES, [9, 1]).map((i) => i.iid),
                   [1, 2, 3, 4], '不存在的 iid 应跳过，其余保持原序')
  assert.deepEqual(applyManualOrder(ISSUES, [1, 9, 2]).map((i) => i.iid),
                   [1, 2, 3, 4], '缺失 iid 不影响其余顺序')
})

test('applyManualOrder：空/非数组手动列表原样返回，非数组入参回空数组', () => {
  assert.deepEqual(applyManualOrder(ISSUES, []).map((i) => i.iid),
                   [1, 2, 3, 4], '空手动列表应原样返回')
  assert.deepEqual(applyManualOrder(ISSUES, null).map((i) => i.iid),
                   [1, 2, 3, 4], 'null 手动列表应原样返回')
  assert.deepEqual(applyManualOrder(null, [1]), [], '非数组 items 应回空数组')
  assert.deepEqual(applyManualOrder(undefined, [1]), [], 'undefined 应回空数组')
})

test('applyManualOrder：含 null 元素与重复 iid 不崩且稳定', () => {
  const mixed = [null, { iid: 1 }, { iid: 1 }, undefined, { iid: 2 }]
  const out = applyManualOrder(mixed, [2])
  assert.deepEqual(out.map((i) => i && i.iid), [2, null, 1, 1, undefined],
                   '重复 iid 只放一次手动位置，其余按原序')
})

// ---- moveItem 纯函数测试 ----

test('moveItem：下移/上移/同位置', () => {
  assert.deepEqual(moveItem(ISSUES, 0, 2).map((i) => i.iid), [2, 3, 1, 4],
                   '首位移到第三位，其余相对顺序不变')
  assert.deepEqual(moveItem(ISSUES, 2, 0).map((i) => i.iid), [3, 1, 2, 4],
                   '第三位移到首位，其余相对顺序不变')
  assert.deepEqual(moveItem(ISSUES, 1, 1).map((i) => i.iid), [1, 2, 3, 4],
                   '同位置应返回原序副本')
})

test('moveItem：越界 / 非数组 / 空数组安全兜底', () => {
  assert.deepEqual(moveItem(ISSUES, -1, 2).map((i) => i.iid), [1, 2, 3, 4],
                   '负索引越界应原样返回')
  assert.deepEqual(moveItem(ISSUES, 0, 99).map((i) => i.iid), [1, 2, 3, 4],
                   '超长目标索引越界应原样返回')
  assert.deepEqual(moveItem(null, 0, 1), [], '非数组应回空数组')
  assert.deepEqual(moveItem([], 0, 1), [], '空数组应回空数组')
  const out = moveItem(ISSUES, 0, 2)
  assert.notEqual(out, ISSUES, '应返回新数组（不返回原数组引用）')
  const before = ISSUES.map((i) => i.iid)
  moveItem(ISSUES, 0, 2)
  assert.deepEqual(ISSUES.map((i) => i.iid), before, '入参不应被修改')
})

// ---- 组件渲染 ----

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

const findBtns = (root, cls) => root.findAll(
  (n) => n.type === 'button' && String(n.props.className || '').includes(cls))
const issueLinks = (root) => findBtns(root, 'issue-link')
const linkIids = (root) => issueLinks(root).map((b) => {
  const m = textOf(b.props.children).match(/#(\d+)/)
  return m ? Number(m[1]) : null
})
// 「其他」分组可拖的 li（draggable=true）
const dragItems = (root) => root.findAll(
  (n) => n.type === 'li' && n.props.draggable === true)
const dragHandles = (root) => root.findAll(
  (n) => n.props && String(n.props.className || '').includes('issue-drag-handle'))

// 单仓库四类 issue：全部无 bot 终态标签 → 均在「其他」分组
const PAYLOAD = (manualOrder) => ({
  repos: [{
    repo_id: 1, repo_name: 'botler', priority: 10, project_id: 100,
    manual_order: manualOrder || [],
    issues: [
      { iid: 1, title: '一', created_at: '2026-08-01', updated_at: '2026-08-04',
        web_url: 'https://gitlab.example.com/x/-/issues/1', labels: [], project_id: 100 },
      { iid: 2, title: '二', created_at: '2026-08-02', updated_at: '2026-08-03',
        web_url: 'https://gitlab.example.com/x/-/issues/2', labels: [], project_id: 100 },
      { iid: 3, title: '三', created_at: '2026-08-03', updated_at: '2026-08-02',
        web_url: 'https://gitlab.example.com/x/-/issues/3', labels: [], project_id: 100 },
      { iid: 4, title: '四', created_at: '2026-08-04', updated_at: '2026-08-01',
        web_url: 'https://gitlab.example.com/x/-/issues/4', labels: [], project_id: 100 },
    ],
  }],
  errors: [], total: 4,
})

test('渲染：默认「调度器执行顺序」下「其他」分组可拖 + 手柄 + 组头提示', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    assert.equal(dragItems(root).length, 4, '其他分组全部条目应可拖')
    assert.equal(dragHandles(root).length, 4, '每条应渲染拖动手柄')
    const note = root.findAll((n) => String(n.props.className || '').includes('issue-drag-note'))
    assert.equal(note.length, 1, '组头应渲染拖动提示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：切到「最近更新」排序后不再可拖（无手柄）', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => sortBtns(root)[1].props.onClick())
    assert.equal(dragItems(root).length, 0, '非调度器排序下不应可拖')
    assert.equal(dragHandles(root).length, 0, '不应渲染拖动手柄')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

const sortBtns = (root) => findBtns(root, 'issue-sort-option')

test('渲染：bot-done 分组不可拖，仅「其他」分组可拖', async () => {
  const payload = {
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10, project_id: 100,
      manual_order: [],
      issues: [
        { iid: 1, title: 'done', created_at: '2026-08-01', updated_at: '2026-08-10',
          web_url: 'https://gitlab.example.com/x/-/issues/1',
          labels: [{ name: 'bot-done' }] },
        { iid: 2, title: '其他一', created_at: '2026-08-02', updated_at: '2026-08-09',
          web_url: 'https://gitlab.example.com/x/-/issues/2', labels: [] },
        { iid: 3, title: '其他二', created_at: '2026-08-03', updated_at: '2026-08-08',
          web_url: 'https://gitlab.example.com/x/-/issues/3', labels: [] },
      ],
    }],
    errors: [], total: 2,
  }
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: payload })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 只有「其他」分组的 issue 2/3 可拖；bot-done 分组不可拖
    assert.deepEqual(dragItems(root).map((n) => textOf(n.props.children).match(/#(\d+)/)?.[1]).sort(),
                     ['2', '3'], '仅其他分组条目可拖')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：过滤激活时禁用拖动', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 点击状态过滤「开放」→ issueFilterActive=true
    await TestRenderer.act(() => findBtns(root, 'issue-filter-status')[1].props.onClick())
    assert.equal(dragItems(root).length, 0, '过滤激活时不应可拖')
    assert.equal(dragHandles(root).length, 0, '过滤激活时不应渲染手柄')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：单条目不启用拖动', async () => {
  const payload = PAYLOAD([1])
  payload.repos[0].issues = [payload.repos[0].issues[0]]
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: payload })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(dragItems(root).length, 0, '单条目无需拖动')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：overview manual_order 预置 → 调度器排序下初始即按手动顺序展示', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: PAYLOAD([3, 1, 4, 2]),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.deepEqual(linkIids(root), [3, 1, 4, 2],
                     '调度器排序下应优先按手动顺序展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：非调度器排序下 manual_order 不影响展示顺序', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: PAYLOAD([3, 1, 4, 2]),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    await TestRenderer.act(() => sortBtns(root)[2].props.onClick()) // 创建时间
    assert.deepEqual(linkIids(root), [4, 3, 2, 1],
                     '创建时间排序下应按时间展示（手动顺序仅影响调度）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：仓库无 project_id 时（issue 有 project_id）仍可拖，两者皆无则禁用', async () => {
  const p1 = PAYLOAD()
  delete p1.repos[0].project_id
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: p1 })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    assert.equal(dragItems(root).length, 4,
                 '仓库无 project_id 但 issue 带 project_id 时仍可拖')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
  // 两者皆无：禁用拖动
  const p2 = PAYLOAD()
  delete p2.repos[0].project_id
  p2.repos[0].issues = p2.repos[0].issues.map((i) => {
    const { project_id, ...rest } = i
    return rest
  })
  const r2 = await renderOverview({ issuesPayload: p2 })
  try {
    assert.equal(r2.renderError, null)
    assert.equal(dragItems(r2.renderer.root).length, 0,
                 '仓库与 issue 均无 project_id 时应禁用拖动')
  } finally {
    await TestRenderer.act(() => r2.renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

// ---- 交互：拖拽落点保存 ----

test('交互：拖拽落点 → PUT 保存整组顺序并更新展示', async () => {
  const putCalls = []
  mock.method(api, 'put', async (path, body) => {
    putCalls.push([path, body])
    return { project_id: 100, iids: body.iids }
  })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 拖起 iid=1（索引 0），落到 iid=3（索引 2）上 → 顺序 [2,3,1,4]
    let items = dragItems(root)
    await TestRenderer.act(() => {
      items[0].props.onDragStart({
        dataTransfer: { effectAllowed: '', setData() {} },
      })
    })
    items = dragItems(root) // 重新查询拿到新闭包的落点处理器
    await TestRenderer.act(() => {
      items[2].props.onDrop({ preventDefault() {} })
    })
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.equal(putCalls.length, 1, '应调用一次 PUT')
    assert.equal(putCalls[0][0], '/api/issues/100/manual-orders', 'PUT 路径应正确')
    assert.deepEqual(putCalls[0][1], { iids: [2, 3, 1, 4] }, '应提交拖动后的整组顺序')
    assert.deepEqual(linkIids(root), [2, 3, 1, 4], '展示顺序应更新为拖动后顺序')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('交互：保存失败 → 回滚顺序并出现错误提示（点击关闭）', async () => {
  mock.method(api, 'put', async () => {
    throw new Error('网络错误')
  })
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: PAYLOAD([1, 2, 3, 4]),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    let items = dragItems(root)
    await TestRenderer.act(() => {
      items[0].props.onDragStart({
        dataTransfer: { effectAllowed: '', setData() {} },
      })
    })
    items = dragItems(root)
    await TestRenderer.act(() => {
      items[1].props.onDrop({ preventDefault() {} })
    })
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.deepEqual(linkIids(root), [1, 2, 3, 4], '保存失败应回滚到保存前顺序')
    const err = root.findAll((n) => String(n.props.className || '').includes('issue-manual-error'))
    assert.equal(err.length, 1, '应出现保存失败提示')
    // 点击关闭后提示消失
    await TestRenderer.act(() => err[0].props.onClick())
    assert.equal(root.findAll(
      (n) => String(n.props.className || '').includes('issue-manual-error')).length,
    0, '点击后提示应关闭')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})
