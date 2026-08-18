// 概览页「开放 Issue」板块仓库卡片「对账」按钮测试（issue #134）：
// 每个仓库卡片右上角新增「对账」按钮，点击后复用仓库页对账接口
// POST /api/repos/{repo_id}/reconcile（issue #17）立即扫描该仓库，
// 把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入任务队列，
// 并小字展示对账结果。
//
// 断言：
// 1. 渲染：每个仓库卡片头右上角渲染「对账」按钮，与「添加 Issue」按钮
//    并排成组（.issue-repo-actions），操作组整体推右（margin-left: auto）；
// 2. 点击：POST /api/repos/{repo_id}/reconcile 参数正确（repo_id 对应对
//    被点击仓库）；请求中按钮禁用防重复点击并显示「对账中…」；
// 3. 成功：入队 >0 显示「N 个待处理 issue 已入队」+「扫描 N 个 issue」；
//    入队 =0 显示「无需处理」；仓库停用显示后端 note；
// 4. 失败：接口异常显示错误信息；对账为低危操作无需确认对话框。
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
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-add-issue.test.mjs 一致）。
// api 也经 vite 加载，与 Overview 组件内 import 的是同一模块实例，可对 api 做 method mock。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const ISSUES_PAYLOAD = {
  repos: [
    { repo_id: 1, repo_name: 'botler', priority: 10, issues: [
      { iid: 11, title: '已有 issue',
        updated_at: '2026-08-15 01:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/11' },
    ] },
    { repo_id: 2, repo_name: 'shipyard', priority: 20, issues: [] },
  ],
  errors: [], total: 1,
}

// 挂载 Overview：api.get 按路径分流（tasks/pipelines/issues/settings/inspirations）
async function renderOverview({ issuesPayload = ISSUES_PAYLOAD } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
    if (pathname === '/api/settings') return { gitlab: { owner_token_masked: 'glpat-***' } }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function reconcileBtns(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('reconcile-btn'))
}

// 拼接渲染树全部文本（不引入 JSON 分隔符——JSX 中「文本 + 表达式」会
// 拆成多个相邻子节点，JSON.stringify 会在数字/字符串间插入分隔符导致
// 连续文案断言失败）
function treeText(renderer) {
  const walk = (n) => {
    if (n == null) return ''
    if (typeof n === 'string' || typeof n === 'number') return String(n)
    if (Array.isArray(n)) return n.map(walk).join('')
    return walk(n.children)
  }
  return walk(renderer.toJSON())
}

// 点击第 index 个仓库卡片的「对账」按钮并等待请求完成（postImpl 为
// api.post 的 mock 实现；默认返回成功结果）
async function clickReconcile(renderer, index = 0, postImpl) {
  const btns = reconcileBtns(renderer)
  assert.ok(btns.length > index, `找不到第 ${index} 个对账按钮`)
  mock.method(api, 'post', postImpl)
  await TestRenderer.act(async () => {
    btns[index].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
}

// ---- 源码与样式断言 ----

test('源码含「对账」按钮、对账接口调用与结果提示', () => {
  assert.match(overviewSrc, /reconcile-btn/, '应有对账按钮类名')
  assert.match(overviewSrc, /tr\('overview\.reconcile'\)/, '对账按钮应经 t() 国际化')
  assert.equal(zhCN['overview.reconcile'], '对账', '中文「对账」文案应保留')
  assert.match(
    overviewSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.repo_id\}\/reconcile`\)/,
    '点击后应调 POST /api/repos/{repo_id}/reconcile',
  )
  assert.match(
    overviewSrc,
    /disabled=\{reconcileResults\[r\.repo_id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(overviewSrc, /tr\('overview\.reconcileEnqueued'/, '「N 个待处理 issue 已入队」应经 t() 国际化')
  assert.ok(zhCN['overview.reconcileEnqueued'].includes('个待处理 issue 已入队'), '中文文案应保留')
  assert.match(overviewSrc, /tr\('overview\.reconcileNoop'\)/, '「无需处理」应经 t() 国际化')
  assert.equal(zhCN['overview.reconcileNoop'], '无需处理', '中文「无需处理」文案应保留')
  assert.match(overviewSrc, /ReconcileResult/, '应渲染对账结果组件')
})

test('对账为低危操作：无需确认对话框', () => {
  const seg = overviewSrc.slice(
    overviewSrc.indexOf('// 对账（issue #134）'),
    overviewSrc.indexOf('// 各任务信息块实时输出自动滚动到底部'),
  )
  assert.ok(
    !seg.includes('window.confirm') && !seg.includes('confirmDialog'),
    '对账按钮不应要求确认（与仓库页对账按钮一致）',
  )
})

test('styles.css：操作组推右、对账按钮与结果样式', () => {
  const actions = styles.match(/\.issue-repo-actions\s*\{([^}]*)\}/)
  assert.ok(actions, 'styles.css 应有 .issue-repo-actions 规则')
  assert.match(actions[1], /margin-left:\s*auto/, '操作组应推到卡片头最右侧')
  assert.match(actions[1], /inline-flex/, '操作组应为 inline-flex 并排按钮')
  assert.match(actions[1], /gap:\s*var\(--space-2\)/, '按钮间距应使用间距 token')
  const btn = styles.match(/\.reconcile-btn\s*\{([^}]*)\}/)
  assert.ok(btn, 'styles.css 应有 .reconcile-btn 规则')
  assert.match(btn[1], /white-space:\s*nowrap/, '对账按钮不应换行')
  assert.ok(styles.includes('.reconcile-result'), 'styles.css 应有 .reconcile-result 规则')
})

// ---- 渲染与点击 ----

test('渲染：每个仓库卡片头右上角渲染「对账」按钮，与添加按钮并排成组', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const btns = reconcileBtns(renderer)
    assert.equal(btns.length, 2, '两个仓库卡片各应有一个对账按钮')
    const actions = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('issue-repo-actions'))
    assert.equal(actions.length, 2, '每个仓库卡片应有一个操作组容器')
    const groupCls = actions[0].findAll((n) => n.type === 'button')
      .map((b) => String(b.props.className || ''))
    assert.ok(groupCls.some((c) => c.includes('reconcile-btn')), '操作组应含对账按钮')
    assert.ok(groupCls.some((c) => c.includes('add-issue-btn')), '操作组应含添加 Issue 按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击：POST /api/repos/{repo_id}/reconcile 参数正确（对应对被点击仓库）', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    const postCalls = []
    await clickReconcile(renderer, 1, async (pathname) => {
      postCalls.push(pathname)
      return { ok: true, scanned: 3, enqueued: 1 }
    })
    assert.deepEqual(postCalls, ['/api/repos/2/reconcile'],
                     '应只调用被点击仓库（repo_id=2）的对账接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('请求中：按钮禁用并显示「对账中…」防重复点击，完成后恢复', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    let resolvePost = null
    mock.method(api, 'post', () => new Promise((resolve) => { resolvePost = resolve }))
    const btns = reconcileBtns(renderer)
    await TestRenderer.act(async () => {
      btns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const loading = reconcileBtns(renderer)
    assert.equal(loading[0].props.disabled, true, '请求中应禁用按钮防重复点击')
    assert.ok(treeText(renderer).includes('对账中'), '请求中应显示「对账中…」')
    // 请求完成：结果展示 + 按钮恢复可点击
    await TestRenderer.act(async () => {
      resolvePost({ ok: true, scanned: 2, enqueued: 0 })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const after = reconcileBtns(renderer)
    assert.equal(Boolean(after[0].props.disabled), false, '请求完成后应恢复可点击')
    assert.ok(treeText(renderer).includes('无需处理'), '完成后应展示对账结果')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 成功 / 边界 / 失败 ----

test('成功：入队 >0 显示「N 个待处理 issue 已入队」与扫描数', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickReconcile(renderer, 0, async () => ({ ok: true, scanned: 5, enqueued: 2 }))
    const text = treeText(renderer)
    assert.ok(text.includes('2 个待处理 issue 已入队'), '应显示入队数')
    assert.ok(text.includes('扫描 5 个 issue'), '应显示扫描数')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('成功：入队 =0 显示「无需处理」但保留扫描数', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickReconcile(renderer, 0, async () => ({ ok: true, scanned: 8, enqueued: 0 }))
    const text = treeText(renderer)
    assert.ok(text.includes('无需处理'), '无待处理应显示无需处理')
    assert.ok(text.includes('扫描 8 个 issue'), '应显示扫描数')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：仓库停用时显示后端 note 提示', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickReconcile(renderer, 0, async () => ({
      ok: true, scanned: 0, enqueued: 0, note: '仓库已停用，未扫描',
    }))
    assert.ok(treeText(renderer).includes('仓库已停用，未扫描'), '应显示停用 note')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('失败：接口异常显示错误信息', async () => {
  const { renderer, renderError } = await renderOverview()
  try {
    assert.equal(renderError, null)
    await clickReconcile(renderer, 0, async () => {
      throw new Error('对账失败: token 无效或已过期（401）')
    })
    assert.ok(treeText(renderer).includes('对账失败'), '应显示对账失败错误')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
