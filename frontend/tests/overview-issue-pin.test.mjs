// 概览页「其他」分组 issue 置顶按钮测试（issue #308）：在概览页开放
// issue 组件的「其他」分组里，每个 issue 提供置顶按钮——点击把该 issue
// 移到手动调度顺序最前并保存（复用 issue #287 的手动顺序机制与 PUT
// /api/issues/{project_id}/manual-orders 接口），调度器优先按手动顺序
// 派发，置顶即第一个处理。已置顶（手动顺序首位）时按钮高亮 + aria-pressed
// 标记，重复点击不重复保存；保存失败回滚并提示。置顶不依赖当前排序/
// 过滤视图（仅写手动顺序），非「其他」分组不展示。
//
// 断言：
// 1. 纯函数：pinIssueToTop（空列表/不在列表/在列表/已首位/重复项/非整数
//    iid/非数组兜底，不改动入参）；
// 2. 渲染：仅「其他」分组展示置顶按钮（bot-done 分组不展示）；手动顺序
//    首位 issue 按钮为已置顶态（aria-pressed）；非调度器排序/过滤激活下
//    仍展示；仓库与 issue 均无 project_id 时不展示；
// 3. 交互：点击置顶 → PUT 载荷正确（iid 置顶）+ 展示顺序更新 + 按钮进入
//    已置顶态；已置顶重复点击不重复保存；保存失败回滚并出现错误提示；
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
const { default: Overview, applyManualOrder, pinIssueToTop } =
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

test('Overview.jsx 导出置顶纯函数，渲染使用手动顺序与 PUT 保存', () => {
  assert.equal(typeof pinIssueToTop, 'function', '应导出 pinIssueToTop')
  assert.equal(typeof applyManualOrder, 'function', '应继续导出 applyManualOrder')
  assert.match(overview, /pinIssueToTop\(prevIids, issue\.iid\)/,
               '置顶处理应调用 pinIssueToTop 生成新顺序')
  assert.match(overview, /pinIssue\(r, i\)/, '置顶按钮点击应调用 pinIssue')
  assert.match(overview, /issue-pin/, '置顶按钮应使用 issue-pin 类')
  assert.match(overview, /api\.put\(/, '保存应调用 api.put')
  assert.match(overview, /manual-orders/, 'PUT 路径应含 manual-orders')
  assert.match(overview, /name="pin"/, '置顶按钮应使用 pin 图标')
})

test('i18n：置顶按钮文案中英文键齐全', () => {
  const keys = ['overview.pinIssue', 'overview.pinIssueTitle',
                'overview.pinIssuePinned']
  for (const k of keys) {
    assert.ok(zhCN[k], `zh-CN 应有 ${k}`)
    assert.ok(enUS[k], `en-US 应有 ${k}`)
  }
})

// ---- pinIssueToTop 纯函数测试 ----

test('pinIssueToTop：空列表 / 不在列表直接插到最前', () => {
  assert.deepEqual(pinIssueToTop([], 5), [5], '空列表应得到仅含目标 iid 的数组')
  assert.deepEqual(pinIssueToTop([1, 2, 3], 5), [5, 1, 2, 3],
                   '不在列表的 iid 应插到最前，其余保持原序')
})

test('pinIssueToTop：在列表中移到最前并保序去重', () => {
  assert.deepEqual(pinIssueToTop([1, 2, 3], 2), [2, 1, 3],
                   '中间元素应移到最前，其余相对顺序不变')
  assert.deepEqual(pinIssueToTop([1, 2, 3], 3), [3, 1, 2],
                   '末位元素应移到最前')
  assert.deepEqual(pinIssueToTop([2, 1, 2, 3], 2), [2, 1, 3],
                   '重复 iid 应保序去重')
})

test('pinIssueToTop：已首位返回原序副本（不改动入参）', () => {
  const input = [1, 2, 3]
  const out = pinIssueToTop(input, 1)
  assert.deepEqual(out, [1, 2, 3], '已首位应返回原序副本')
  assert.notEqual(out, input, '应返回新数组（不返回原数组引用）')
  assert.deepEqual(input, [1, 2, 3], '入参不应被修改')
})

test('pinIssueToTop：非数组入参 / 非整数 iid 安全兜底', () => {
  assert.deepEqual(pinIssueToTop(null, 5), [5], 'null 应按空列表处理')
  assert.deepEqual(pinIssueToTop(undefined, 5), [5], 'undefined 应按空列表处理')
  assert.deepEqual(pinIssueToTop([1, 2], null), [1, 2], '非整数 iid 应原样返回副本')
  const before = [1, 2, 3]
  pinIssueToTop(before, 2)
  assert.deepEqual(before, [1, 2, 3], '入参不应被修改')
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
const pinBtns = (root) => findBtns(root, 'issue-pin')
// 置顶按钮没有编号文本，从同 li 内的 issue-link 推导 iid（入参为按钮节点数组）
const pinIids = (btns) => btns.map((b) => {
  let n = b.parent
  while (n && String(n.type) !== 'li') n = n.parent
  const link = n && n.findAll(
    (x) => x.type === 'button' && String(x.props.className || '').includes('issue-link'))
  const m = link && link[0] && textOf(link[0].props.children).match(/#(\d+)/)
  return m ? Number(m[1]) : null
})
const sortBtns = (root) => findBtns(root, 'issue-sort-option')

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

test('渲染：「其他」分组每条 issue 展示置顶按钮', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    assert.equal(pinBtns(root).length, 4, '其他分组每条 issue 应有置顶按钮')
    assert.deepEqual(pinIids(pinBtns(root)).sort(), [1, 2, 3, 4], '置顶按钮应覆盖全部条目')
    assert.deepEqual(pinBtns(root).map((b) => b.props['aria-pressed']),
                     [false, false, false, false], '默认均非已置顶态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：仅「其他」分组展示置顶按钮，bot-done 分组不展示', async () => {
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
    assert.deepEqual(pinIids(pinBtns(root)).sort(), ['2', '3'].map(Number),
                     '仅其他分组条目有置顶按钮（bot-done 分组不展示）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：手动顺序首位 issue 置顶按钮为已置顶态', async () => {
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: PAYLOAD([3, 1, 4, 2]),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const byIid = new Map(pinBtns(root).map((b) => [pinIids([b])[0], b]))
    assert.equal(byIid.get(3).props['aria-pressed'], true, '手动顺序首位应已置顶')
    assert.equal(byIid.get(3).props.className.includes('issue-pin-active'), true,
                 '已置顶按钮应带 active 类')
    for (const iid of [1, 2, 4]) {
      assert.equal(byIid.get(iid).props['aria-pressed'], false,
                   `${iid} 非手动顺序首位不应置顶`)
    }
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：非调度器排序 / 过滤激活下仍展示置顶按钮', async () => {
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 切到「最近更新」排序（拖动手柄消失，但置顶按钮保留）
    await TestRenderer.act(() => sortBtns(root)[1].props.onClick())
    assert.equal(pinBtns(root).length, 4, '非调度器排序下应保留置顶按钮')
    // 状态过滤「开放」激活（拖动禁用，置顶按钮保留）
    await TestRenderer.act(() => findBtns(root, 'issue-filter-status')[1].props.onClick())
    assert.equal(pinBtns(root).length, 4, '过滤激活下应保留置顶按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('渲染：仓库与 issue 均无 project_id 时不展示置顶按钮', async () => {
  const payload = PAYLOAD()
  delete payload.repos[0].project_id
  payload.repos[0].issues = payload.repos[0].issues.map((i) => {
    const { project_id, ...rest } = i
    return rest
  })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: payload })
  try {
    assert.equal(renderError, null)
    assert.equal(pinBtns(renderer.root).length, 0,
                 '无 project_id 时应禁用置顶按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

// ---- 交互：点击置顶 ----

test('交互：点击置顶 → PUT 载荷正确、展示顺序更新、按钮进入已置顶态', async () => {
  const putCalls = []
  mock.method(api, 'put', async (path, body) => {
    putCalls.push([path, body])
    return { project_id: 100, iids: body.iids }
  })
  const { renderer, renderError, restore } = await renderOverview({ issuesPayload: PAYLOAD() })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    // 点击 iid=2 的置顶按钮 → 手动顺序 [2]，展示顺序 [2,1,3,4]
    await TestRenderer.act(() => pinBtns(root)[1].props.onClick())
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.equal(putCalls.length, 1, '应调用一次 PUT')
    assert.equal(putCalls[0][0], '/api/issues/100/manual-orders', 'PUT 路径应正确')
    assert.deepEqual(putCalls[0][1], { iids: [2] }, '应提交置顶后的手动顺序')
    assert.deepEqual(linkIids(root), [2, 1, 3, 4], '展示顺序应更新为置顶后顺序')
    const byIid = new Map(pinBtns(root).map((b) => [pinIids([b])[0], b]))
    assert.equal(byIid.get(2).props['aria-pressed'], true, '置顶后按钮应为已置顶态')
    assert.equal(byIid.get(1).props['aria-pressed'], false, '其余按钮仍为未置顶态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    restore()
  }
})

test('交互：已置顶 issue 重复点击不重复保存', async () => {
  const putCalls = []
  mock.method(api, 'put', async (path, body) => {
    putCalls.push([path, body])
    return { project_id: 100, iids: body.iids }
  })
  const { renderer, renderError, restore } = await renderOverview({
    issuesPayload: PAYLOAD([2, 1, 3, 4]),
  })
  try {
    assert.equal(renderError, null)
    const root = renderer.root
    const byIid = new Map(pinBtns(root).map((b) => [pinIids([b])[0], b]))
    // iid=2 已置顶：点击不应触发保存
    await TestRenderer.act(() => byIid.get(2).props.onClick())
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.equal(putCalls.length, 0, '已置顶重复点击不应再保存')
    // 点击未置顶的 iid=3 → 置顶到最前
    byIid.get(3).props.onClick()
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.equal(putCalls.length, 1, '未置顶 issue 点击应保存一次')
    assert.deepEqual(putCalls[0][1], { iids: [3, 2, 1, 4] }, '应把 iid=3 移到最前')
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
    // 点击 iid=3 置顶 → 保存失败应回滚
    await TestRenderer.act(() => pinBtns(root)[2].props.onClick())
    await TestRenderer.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.deepEqual(linkIids(root), [1, 2, 3, 4], '保存失败应回滚到保存前顺序')
    const err = root.findAll((n) => String(n.props.className || '').includes('issue-manual-error'))
    assert.equal(err.length, 1, '应出现保存失败提示')
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
