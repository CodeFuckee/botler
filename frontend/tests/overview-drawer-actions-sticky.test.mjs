// 概览页右边栏头部操作区固定在顶部测试（issue #331）：
// issue 详情右边栏与流水线详情右边栏的操作按钮——「关闭 issue」、
// 「查看执行的详情」、「在 GitLab 中打开」、「关闭右边栏（×）」——
// 放在右边栏顶部，并且固定在顶部，不随右边栏内容滚动而滚动。
//
// 断言（styles.css 源码级 + 组件渲染级）：
// 1. styles.css：.issue-drawer .modal-header 与 .pipeline-drawer
//    .modal-header 为 sticky 顶部固定（position: sticky / top: 0 /
//    z-index / 不透明背景遮住滚过的内容）；
// 2. 固定规则限定 issue 与流水线两个抽屉，不波及 .task-detail-drawer
//    （任务执行详情第二层右边栏，不在本 issue 范围）；
// 3. IssueDrawer：关闭 issue / 查看执行的详情 / 在 GitLab 中打开 /
//    × 关闭按钮均渲染于头部 .issue-drawer-actions（.modal-header 内）；
// 4. PipelineDrawer：「在 GitLab 中打开」+ × 关闭按钮渲染于头部
//    .issue-drawer-actions；
// 5. 边界：流水线为 null（无 pipeline）时头部操作区仍渲染（× 关闭
//    按钮可用）；移动端底部操作栏（issue #270）sticky bottom 不回归。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview 系测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
after(() => vite.close())
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { default: PipelineDrawer } = await vite.ssrLoadModule('/src/components/PipelineDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

// ---- styles.css 源码断言 ----

// 提取某选择器首个规则体（feature 特征非空时只返回含该特征的规则）
function ruleBody(css, selector, feature) {
  const re = new RegExp(`${selector}\\s*\\{([^}]*)\\}`, 'g')
  let m
  while ((m = re.exec(css))) {
    if (!feature || m[1].includes(feature)) return m[1]
  }
  assert.ok(false, `styles.css 应存在 ${selector} 规则（特征：${feature || '任意'}）`)
}


// TestInstance 子树 → 纯文本（拼接全部叶子字符串，与 overview 系测试一致）
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') {
    const children = node.children ?? node.props?.children
    return toText(children)
  }
  return ''
}

test('styles.css：issue 详情右边栏头部操作区 sticky 固定在顶部', () => {
  const body = ruleBody(styles, '\\.issue-drawer\\s+\\.modal-header', 'position')
  assert.match(body, /position:\s*sticky/, 'issue 抽屉头部应 sticky（不随内容滚动）')
  assert.match(body, /top:\s*0/, 'issue 抽屉头部应吸附抽屉顶部')
  assert.match(body, /z-index/, 'issue 抽屉头部应浮于滚动内容之上（z-index）')
  assert.match(body, /background/, 'issue 抽屉头部应有不透明背景（遮住滚过的内容）')
})

test('styles.css：流水线详情右边栏头部操作区 sticky 固定在顶部', () => {
  const body = ruleBody(styles, '\\.pipeline-drawer\\s+\\.modal-header', 'position')
  assert.match(body, /position:\s*sticky/, '流水线抽屉头部应 sticky（不随内容滚动）')
  assert.match(body, /top:\s*0/, '流水线抽屉头部应吸附抽屉顶部')
  assert.match(body, /z-index/, '流水线抽屉头部应浮于滚动内容之上（z-index）')
  assert.match(body, /background/, '流水线抽屉头部应有不透明背景（遮住滚过的内容）')
})

test('styles.css：固定规则限定两个抽屉，不波及第二层与其他 modal-header 使用者', () => {
  // 第二层任务执行详情抽屉（.task-detail-drawer）不在本 issue 范围，
  // 其头部不应被新增 sticky 规则命中
  assert.ok(!/\.task-detail-drawer\s+\.modal-header/.test(styles),
            '不应为第二层抽屉新增 sticky 头部规则')
  // 固定规则必须带抽屉限定选择器，不能是裸 .modal-header（会被弹窗/其他
  // 抽屉误伤）
  assert.ok(!/^\s*\.modal-header\s*\{[^}]*position:\s*sticky/m.test(styles),
            '裸 .modal-header 不应被改为 sticky')
  // issue 抽屉基础头部 actions 规则保持存在（桌面按钮在头部）
  assert.ok(/\.issue-drawer-actions\s*\{/.test(styles), '应保留 .issue-drawer-actions 规则')
})

// 提取最后一个 @media (max-width: 860px) 断点块（括号配平；与
// responsive-mobile-layout.test.mjs 的 lastMedia860 同逻辑）
function mobileMediaBlock(css) {
  const re = /@media \(max-width:\s*860px\)\s*\{/g
  let start = -1
  let m
  while ((m = re.exec(css))) start = m.index
  assert.ok(start >= 0, 'styles.css 应存在 @media (max-width: 860px) 断点')
  let depth = 0
  let end = -1
  for (let i = start; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > start, '860px 断点块应有闭合大括号')
  return css.slice(start, end + 1)
}

test('styles.css：移动端底部操作栏 sticky bottom 不回归（issue #270）', () => {
  const block = mobileMediaBlock(styles)
  const m = block.match(/\.drawer-bottom-actions\s*\{([^}]*)\}/)
  assert.ok(m, '860px 断点内应有 .drawer-bottom-actions 规则')
  assert.match(m[1], /position:\s*sticky/, '底部操作栏保持 sticky 常驻')
  assert.match(m[1], /bottom:\s*0/, '底部操作栏保持吸附底部')
})

// ---- 组件渲染断言 ----

// IssueDrawer 直接渲染（api.get 走 mock：detail 返回空 notes 等）
async function renderIssueDrawer(issue, onClose = () => {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.endsWith('/detail')) {
      return { notes: [], engine: 'claude', task_id: null,
               task_duration_seconds: null, task_status: null }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue, repoName: 'botler',
        onClose, onIssueClosed: () => {}, onLabelsUpdated: () => {},
        onAssigneeUpdated: () => {}, onPrioritized: () => {},
      }))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

const FULL_ISSUE = {
  iid: 64, title: '概览页面增加读取已启用的仓库issue',
  state: 'opened',
  updated_at: '2026-08-14 10:20:00',
  created_at: '2026-08-10 09:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
  description: '**需求**\n\n- 要点一',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
  milestone: 'v1.0',
  assignees: [{ name: 'Agent', username: 'agent' }],
  user_notes_count: 3,
  project_id: 1,
}

test('IssueDrawer：四个操作按钮均渲染于头部 .issue-drawer-actions', async () => {
  const { renderer, renderError } = await renderIssueDrawer(FULL_ISSUE)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 头部操作区容器：.issue-drawer-actions（位于 .modal-header 内）
    const actions = root.findAll(
      (n) => String(n.props.className || '').includes('issue-drawer-actions'))
    assert.ok(actions.length >= 1, '抽屉应有头部 .issue-drawer-actions 操作区')
    // 桌面端按钮语义：关闭 issue / 查看执行的详情 / 在 GitLab 中打开 / ×
    const head = actions[0]
    const text = toText(head)
    assert.match(text, /关闭 issue/, '头部应有「关闭 issue」按钮')
    assert.match(text, /查看执行的详情/, '头部应有「查看执行的详情」按钮')
    assert.match(text, /在 GitLab 中打开/, '头部应有「在 GitLab 中打开」按钮')
    const closeBtn = head.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('modal-close'))
    assert.ok(closeBtn.length >= 1, '头部应有 × 关闭右边栏按钮')
    // × 关闭按钮应位于头部操作区内（点击关闭抽屉）
    let closed = false
    const { renderer: r2 } = await renderIssueDrawer(FULL_ISSUE, () => { closed = true })
    try {
      const head2 = r2.root.findAll(
        (n) => String(n.props.className || '').includes('issue-drawer-actions'))[0]
      await TestRenderer.act(async () => {
        head2.findAll(
          (n) => n.type === 'button'
            && String(n.props.className || '').includes('modal-close'))[0].props.onClick()
      })
      assert.ok(closed, '头部 × 点击应触发关闭右边栏回调')
    } finally {
      await TestRenderer.act(() => r2.unmount())
    }
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// PipelineDrawer 直接渲染
function renderPipelineDrawer(entry) {
  return TestRenderer.create(React.createElement(PipelineDrawer, {
    entry, onClose: () => {},
  }))
}

const FULL_PIPELINE_ENTRY = {
  repo_id: 1,
  repo_name: 'botler',
  enabled: true,
  pipeline: {
    id: 99, status: 'success', ref: 'main', sha: 'abcdef1234',
    created_at: '2026-08-14 10:00:00', updated_at: '2026-08-14 10:05:00',
    finished_at: '2026-08-14 10:06:00', duration: 300,
    web_url: 'https://gitlab.example.com/chenkaidi/botler/-/pipelines/99',
  },
  stages: [],
}

test('PipelineDrawer：「在 GitLab 中打开」与 × 渲染于头部 .issue-drawer-actions', () => {
  const renderer = renderPipelineDrawer(FULL_PIPELINE_ENTRY)
  try {
    const root = renderer.root
    const actions = root.findAll(
      (n) => String(n.props.className || '').includes('issue-drawer-actions'))
    assert.ok(actions.length >= 1, '流水线抽屉应有头部 .issue-drawer-actions 操作区')
    const head = actions[0]
    const text = toText(head)
    assert.match(text, /在 GitLab 中打开/, '头部应有「在 GitLab 中打开」按钮')
    const closeBtn = head.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('modal-close'))
    assert.ok(closeBtn.length >= 1, '头部应有 × 关闭右边栏按钮')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('PipelineDrawer 边界：pipeline 为 null 时头部操作区仍渲染（× 可用）', () => {
  const renderer = renderPipelineDrawer({ repo_id: 1, repo_name: 'botler' })
  try {
    const root = renderer.root
    const actions = root.findAll(
      (n) => String(n.props.className || '').includes('issue-drawer-actions'))
    assert.ok(actions.length >= 1, 'pipeline 缺失时头部操作区仍应渲染')
    const head = actions[0]
    const closeBtn = head.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('modal-close'))
    assert.ok(closeBtn.length >= 1, 'pipeline 缺失时头部仍应有 × 关闭按钮')
    assert.match(toText(root), /暂无流水线/, '正文应显示空态')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

// 竖屏（issue #334）：在竖屏显示时，概览页 issue 详情右边栏的「关闭右边栏
// （×）」按钮放在右边栏顶部并固定在顶部；「关闭 issue / 查看执行的详情 /
// 在 GitLab 中打开」按钮放在右边栏底部并固定在底部，均不随右边栏内容滚动
// 而滚动。issue #270 移动端响应式把 ≤860px 视口下头部操作区
// .issue-drawer-actions 整体 display:none、按钮下沉到底部操作栏；issue #333
// 把四个按钮全部恢复显示在顶部；本 issue 进一步拆分——竖屏
// （orientation: portrait）下头部操作区仅保留 × 关闭按钮，其余操作按钮置于
// 底部 sticky 操作栏（thumb 可及）。头部 sticky 规则（.issue-drawer
// .modal-header，issue #331）不受媒体查询影响天然生效。修复前无此拆分
// 规则，本用例必失败。

// 提取最后一个「max-width: 860px 且 orientation: portrait」断点块（括号配平）
function portraitMediaBlock(css) {
  const re = /@media \(max-width:\s*860px\)\s+and\s+\(orientation:\s*portrait\)\s*\{/g
  let start = -1
  let m
  while ((m = re.exec(css))) start = m.index
  assert.ok(start >= 0,
            'styles.css 应存在 @media (max-width: 860px) and (orientation: portrait) 断点')
  let depth = 0
  let end = -1
  for (let i = start; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > start, 'portrait 断点块应有闭合大括号')
  return css.slice(start, end + 1)
}

test('styles.css：竖屏下 issue 抽屉头部仅保留 × 关闭按钮、其余按钮置于底部操作栏（issue #334）', () => {
  const block = portraitMediaBlock(styles)
  // 头部操作区（.issue-drawer-actions）在竖屏下恢复显示（覆盖 860px 断点的
  // display:none）——限定 issue 抽屉，不波及任务执行详情第二层抽屉
  const actions = block.match(/\.drawer\.issue-drawer\s+\.issue-drawer-actions\s*\{([^}]*)\}/)
  assert.ok(actions, 'portrait 断点内应声明 issue 抽屉头部操作区规则')
  assert.match(actions[1], /display:\s*flex/, '竖屏下头部操作区应恢复显示（flex）')
  // 375px 竖屏按钮较多时头部允许换行，避免溢出 sticky 头部（截断/横向滚动）
  assert.match(actions[1], /flex-wrap:\s*wrap/, '竖屏下头部操作区应允许按钮换行')
  assert.match(actions[1], /flex-shrink:\s*1/,
               '竖屏下头部操作区应可收缩（覆盖基础规则 flex-shrink:0）')
  // 竖屏下头部操作区只保留 × 关闭按钮——其余按钮（关闭 issue / 查看执行的
  // 详情 / 在 GitLab 中打开）移到底部 sticky 操作栏，不在头部重复
  const headHide = block.match(/\.drawer\.issue-drawer\s+\.issue-drawer-actions\s+\.btn:not\(\.modal-close\)\s*\{([^}]*)\}/)
  assert.ok(headHide, 'portrait 断点内应声明头部操作区「非 × 按钮」隐藏规则')
  assert.match(headHide[1], /display:\s*none/, '竖屏下头部操作区非 × 按钮应隐藏（仅保留 ×）')
  // 头部（.modal-header）整体允许换行：标题一行、关闭按钮一行
  const header = block.match(/\.drawer\.issue-drawer\s+\.modal-header\s*\{([^}]*)\}/)
  assert.ok(header, 'portrait 断点内应声明 issue 抽屉头部换行规则')
  assert.match(header[1], /flex-wrap:\s*wrap/, '竖屏下头部应允许换行（标题与按钮分行）')
  // 底部操作栏（.drawer-bottom-actions）在竖屏下恢复显示（sticky 常驻底部，
  // 继承 860px 断点的 flex/sticky/bottom 布局），承载「关闭 issue / 查看执行
  // 的详情 / 在 GitLab 中打开」按钮——覆盖 issue #333 的 display:none
  const bottom = block.match(/\.drawer\.issue-drawer\s+\.drawer-bottom-actions\s*\{([^}]*)\}/)
  assert.ok(bottom, 'portrait 断点内应声明 issue 抽屉底部操作栏显示规则')
  assert.match(bottom[1], /display:\s*flex/, '竖屏下底部操作栏应恢复显示（flex）')
  // 底部操作栏不再重复展示 ×（× 已固定在顶部）——避免两处关闭入口
  const bottomClose = block.match(/\.drawer\.issue-drawer\s+\.drawer-bottom-actions\s+\.modal-close\s*\{([^}]*)\}/)
  assert.ok(bottomClose, 'portrait 断点内应声明底部操作栏 × 隐藏规则')
  assert.match(bottomClose[1], /display:\s*none/, '竖屏下底部操作栏应隐藏 ×（避免与顶部重复）')
})
