// 概览页 issue 右边栏「重试」按钮测试（issue #117）：
// 失败任务（issue 带 bot-failed 标签且无 bot-done）右上角显示「重试」
// 按钮，二次确认后调用后端重新执行该 issue 的任务；成功后显示提示、
// 按钮消失并通知父组件刷新开放 issue 列表；失败显示错误且按钮保留；
// 请求中按钮禁用；任务正在运行（running）时不显示按钮。
//
// 断言：
// 1. isFailedTask 纯函数：bot-failed → true；bot-failed+bot-done →
//    false（bot-done 优先级）；无标签/空标签/坏数据 → false；
// 2. 源码：重试按钮文案、POST /api/issues/{project_id}/{iid}/retry、
//    confirmDialog 二次确认、onRetried 通知父组件；
// 3. 渲染：bot-failed issue 显示重试按钮；bot-done / 普通 issue /
//    running=true 不显示；
// 4. 交互：取消确认不调接口；确认后调接口；成功提示 + 按钮消失 +
//    onRetried 触发；失败显示错误按钮保留；请求中禁用防重复点击；
// 5. Overview 传 running 与 onRetried 给 IssueDrawer。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-issue-close-button.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: IssueDrawer, isFailedTask } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
// 与组件内 import 的同一 dialog.js 模块实例（同一 vite 实例），
// 测试注入 installAutoAnswer 直接作用于组件的确认调用（issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')

after(() => vite.close())

// ---- isFailedTask 纯函数 ----

test('isFailedTask：bot-failed → true，bot-done 优先级更高 → false', () => {
  const label = (name) => ({ name, color: '6699cc', text_color: 'FFFFFF' })
  assert.equal(isFailedTask({ labels: [label('bot-failed')] }), true,
               '仅 bot-failed 应判为失败任务')
  assert.equal(isFailedTask({ labels: [label('feature'), label('bot-failed')] }), true,
               'bot-failed 与其他标签并存仍是失败任务')
  assert.equal(isFailedTask({ labels: [label('bot-failed'), label('bot-done')] }), false,
               'bot-failed + bot-done 并存应视为成功（重试成功场景）')
  assert.equal(isFailedTask({ labels: [label('bot-done')] }), false,
               '仅 bot-done 不是失败任务')
  assert.equal(isFailedTask({ labels: [label('feature')] }), false,
               '无终态标签不是失败任务')
})

test('isFailedTask：空/缺失/坏数据边界 → false 不抛错', () => {
  assert.equal(isFailedTask(null), false)
  assert.equal(isFailedTask(undefined), false)
  assert.equal(isFailedTask({}), false)
  assert.equal(isFailedTask({ labels: [] }), false)
  assert.equal(isFailedTask({ labels: 'bot-failed' }), false, 'labels 非数组按无标签处理')
  assert.equal(isFailedTask({ labels: ['bot-failed'] }), false,
               '标签元素非对象（旧数据）按无标签处理')
  assert.equal(isFailedTask({ labels: [null, { name: 'bot-failed' }] }), true,
               '混入坏元素时正常元素仍生效')
})

// ---- 数据流源码断言 ----

test('IssueDrawer 渲染「重试」按钮并调用 issue 级重试接口', () => {
  assert.match(drawerSrc, /重试/, '应有重试按钮文案')
  assert.match(drawerSrc, /isFailedTask/, '失败任务判定应走 isFailedTask')
  assert.match(drawerSrc, /confirmDialog/, '点击应先二次确认')
  assert.match(
    drawerSrc,
    /api\.post\(`\/api\/issues\/\$\{i\.project_id\}\/\$\{i\.iid\}\/retry`\)/,
    '确认后应调 POST /api/issues/{project_id}/{iid}/retry',
  )
  assert.match(drawerSrc, /onRetried/, '成功后应通知父组件刷新')
  assert.match(drawerSrc, /alert-ok/, '成功后应显示成功提示')
})

test('Overview 向 IssueDrawer 传递 running 与 onRetried', () => {
  assert.match(overviewSrc, /running,\s*\}/, '选中 issue 时应携带 running 标记')
  assert.match(overviewSrc, /running=\{selectedIssue\.running\}/,
               '应把 running 传给 IssueDrawer')
  assert.match(overviewSrc, /onRetried=\{\(\) => loadIssues\(\)\}/,
               '重试成功后应刷新开放 issue 列表')
})

// ---- 组件渲染 ----

// 渲染 IssueDrawer：props 最小集合（SSR 环境 Esc 监听自动跳过）。
// 抽屉打开时按需拉取评论/活动详情，此处统一 mock 为空列表
async function renderDrawer(issue, opts = {}) {
  mock.method(api, 'get', async () => ({ notes: [] }))
  // 默认注入「用户点确定」：单测环境未挂载 DialogHost，confirmDialog 由
  // autoAnswer 直接应答；取消路径用例传 confirm: false 覆盖
  dialog.installAutoAnswer(() => opts.confirm !== false)
  const onRetried = opts.onRetried || (() => {})
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue,
        repoName: 'botler',
        onClose: () => {},
        onIssueClosed: () => {},
        onLabelsUpdated: () => {},
        running: opts.running ?? false,
        onRetried,
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  return { renderer, root: renderer.root, onRetried }
}

function mkIssue(overrides = {}) {
  return {
    project_id: 42,
    iid: 117,
    title: '失败任务重试按钮',
    state: 'opened',
    updated_at: '2026-08-16 13:41:00',
    created_at: '2026-08-16 13:41:00',
    web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/117',
    description: '重试测试',
    labels: [{ name: 'bot-failed', color: '6699cc', text_color: 'FFFFFF' }],
    ...overrides,
  }
}

// 按钮子树纯文本（安全提取，避免 JSON.stringify 遇 React 元素 _owner
// 循环引用；Overview 整树含嵌套元素的按钮也能用）
function btnText(node) {
  if (node == null) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(btnText).join('')
  if (typeof node === 'object') {
    return btnText(node.children ?? node.props?.children)
  }
  return ''
}

function findRetryButtons(root) {
  return root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('btn')
      && btnText(n.props.children).includes('重试'))
}

// 抽屉子树纯文本（限定抽屉内容，避免列表项同名文本干扰）
function drawerText(root) {
  const drawers = root.findAll(
    (n) => String(n.props.className || '').includes('issue-drawer')
      && n.props.onClick)
  return drawers.length > 0
    ? (function toText(node) {
        if (node == null) return ''
        if (typeof node === 'string' || typeof node === 'number') return String(node)
        if (Array.isArray(node)) return node.map(toText).join('')
        if (typeof node === 'object') return toText(node.children ?? node.props?.children)
        return ''
      })(drawers[0].children)
    : ''
}

test('bot-failed issue 渲染重试按钮，bot-done/普通 issue/running 不渲染', async () => {
  const label = (name) => ({ name, color: '6699cc', text_color: 'FFFFFF' })
  const cases = [
    { name: 'bot-failed', issue: mkIssue(), running: false, expect: 1 },
    { name: 'bot-failed+running', issue: mkIssue(), running: true, expect: 0 },
    { name: 'bot-failed+bot-done', issue: mkIssue({ labels: [label('bot-failed'), label('bot-done')] }), running: false, expect: 0 },
    { name: 'bot-done', issue: mkIssue({ labels: [label('bot-done')] }), running: false, expect: 0 },
    { name: '普通 issue', issue: mkIssue({ labels: [label('feature')] }), running: false, expect: 0 },
    { name: '无标签', issue: mkIssue({ labels: [] }), running: false, expect: 0 },
  ]
  for (const c of cases) {
    const { renderer, root } = await renderDrawer(c.issue, { running: c.running })
    try {
      const btns = findRetryButtons(root)
      // issue #270：移动端底部操作栏与头部渲染同一组按钮（renderer 不应用
      // CSS 两处都可见）——「不显示」仍断言精确 0，「显示」断言 ≥1
      if (c.expect === 0) {
        assert.equal(btns.length, 0, `${c.name}：不应渲染重试按钮`)
      } else {
        assert.ok(btns.length >= 1, `${c.name}：应渲染重试按钮`)
      }
      if (c.expect > 0) {
        assert.notEqual(btns[0].props.disabled, true, '无重试请求时按钮应可用')
        assert.ok(drawerText(root).includes('重试'), '按钮文案应为「重试」')
      }
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})

test('确认框取消时不调用重试接口', async () => {
  const postCalls = []
  mock.method(api, 'post', async (p) => { postCalls.push(p); return { task_id: 1 } })
  const { renderer, root } = await renderDrawer(mkIssue(), { confirm: false })
  try {
    await TestRenderer.act(async () => {
      findRetryButtons(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postCalls.length, 0, '取消确认后不应调用重试接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('确认后调用重试接口，成功显示提示、按钮消失并通知父组件', async () => {
  const postCalls = []
  mock.method(api, 'post', async (p) => {
    postCalls.push(p)
    return { task_id: 7, status: 'queued', mode: 'retried' }
  })
  let onRetriedCalls = 0
  const { renderer, root, onRetried } = await renderDrawer(mkIssue(), {
    onRetried: () => { onRetriedCalls += 1 },
  })
  try {
    await TestRenderer.act(async () => {
      findRetryButtons(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.deepEqual(postCalls, ['/api/issues/42/117/retry'],
                     '应调用 issue 级重试接口（project_id/iid 定位）')
    assert.equal(findRetryButtons(root).length, 0, '成功后重试按钮应消失防重复点击')
    assert.ok(drawerText(root).includes('任务 #7 已重新入队'), '应显示成功提示')
    assert.equal(onRetriedCalls, 1, '成功后应通知父组件刷新开放列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('重试失败显示错误信息，按钮保留可重试且不通知父组件', async () => {
  mock.method(api, 'post', async () => { throw new Error('该 issue 已有任务在执行中') })
  let onRetriedCalls = 0
  const { renderer, root, onRetried } = await renderDrawer(mkIssue(), {
    onRetried: () => { onRetriedCalls += 1 },
  })
  try {
    await TestRenderer.act(async () => {
      findRetryButtons(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('该 issue 已有任务在执行中'), '应显示错误信息')
    assert.ok(findRetryButtons(root).length >= 1, '失败后按钮应保留可重试')
    assert.equal(onRetriedCalls, 0, '失败不应通知父组件')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('重试请求进行中按钮禁用（防重复点击）', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  mock.method(api, 'post', async () => { await gate; return { task_id: 7 } })
  const { renderer, root } = await renderDrawer(mkIssue())
  try {
    let clickPromise
    await TestRenderer.act(async () => {
      findRetryButtons(root)[0].props.onClick()
      clickPromise = (async () => {
        await new Promise((resolve) => setTimeout(resolve, 10))
      })()
    })
    await clickPromise
    const btn = findRetryButtons(root)[0]
    assert.equal(btn.props.disabled, true, '请求进行中按钮应禁用')
    assert.ok(btnText(btn.props.children).includes('重试中'), '应显示「重试中…」')
    // 放行请求，清理状态
    await TestRenderer.act(async () => {
      release()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('Overview 列表点击 bot-failed issue 打开抽屉并携带 running 标记', async () => {
  // 复现 issue #99 的 runningKeys 匹配：任务列表有一条 running 任务
  // 命中该 issue → 点击打开抽屉应带 running=true（重试按钮不显示）
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [{ id: 9, repo_id: 1, issue_iid: 117,
                         status: 'running', repo_name: 'botler' }],
               total: 1, stats: {} }
    }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10,
                         issues: [mkIssue()] }], errors: [], total: 1 }
    }
    if (pathname.startsWith('/api/issues/')) return { notes: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      assert.fail(`Overview 渲染抛错：${e.message}`)
    }
  })
  try {
    const root = renderer.root
    const itemBtn = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    assert.ok(itemBtn.length > 0, '应渲染 issue 列表项')
    await TestRenderer.act(async () => { itemBtn[0].props.onClick() })
    // 抽屉内不应有重试按钮（任务 running 中）
    const retryBtns = findRetryButtons(root)
    assert.equal(retryBtns.length, 0, '运行中的失败 issue 不应显示重试按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
