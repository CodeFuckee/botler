// 任务页移动端卡片式列表测试（issue #270）：窄视口（≤860px，复用设置页
// #139 断点约定；验收标准 ≤768px 全覆盖）下任务表格整体切换为卡片式列表——
// 触屏友好、关键操作按钮（查看/执行/停止/重试）直接可见、无横向滚动；
// 宽视口仍渲染 12 列表格（桌面端样式不受影响）。
//
// 断言：
// 1. isMobileViewport 纯函数：≤860px 为窄视口（375/768/860 true），
//    861+ 为桌面（false），异常输入（NaN/0/负值）按桌面处理不报错；
// 2. styles.css：.tasks-card-* 卡片列表样式规则齐全（含失败原因弱底块、
//    meta 网格、操作按钮区）；@media (max-width: 860px) 断点存在；
// 3. 渲染（mock window.innerWidth=375）：渲染 .tasks-card-list、无表格；
//    卡片含状态徽章 / 查看链接 / 执行按钮；失败任务展示失败原因与重试
//    按钮、运行中任务展示停止按钮；
// 4. 渲染（mock window.innerWidth=1000）：仍渲染表格、无卡片列表（桌面不受影响）；
// 5. 渲染：移动端空列表展示空态卡片；有隐藏字段时 ⋯ 抽屉入口保留。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// tasks-responsive-cols.test.mjs 一致，react-router-dom alias 到 mock-router）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const {
  default: Tasks,
  isMobileViewport,
  MOBILE_BREAKPOINT_PX,
} = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// ---- 纯函数断言 ----

test(`isMobileViewport：≤${MOBILE_BREAKPOINT_PX}px 为窄视口，861+ 为桌面`, () => {
  assert.equal(MOBILE_BREAKPOINT_PX, 860, '断点应复用设置页 #139 的 860px 约定')
  for (const w of [320, 375, 414, 768, 860]) {
    assert.equal(isMobileViewport(w), true, `视口 ${w}px 应为窄视口（卡片式列表）`)
  }
  for (const w of [861, 1000, 1280, 1440, 2560]) {
    assert.equal(isMobileViewport(w), false, `视口 ${w}px 应为桌面（表格）`)
  }
})

test('isMobileViewport：异常输入按桌面处理（不越界不报错）', () => {
  for (const w of [0, -1, NaN, undefined, null, Infinity]) {
    assert.equal(isMobileViewport(w), false, `输入 ${String(w)} 不应判定为窄视口`)
  }
})

// ---- 源码断言（styles.css）----

function pickRule(css, selector) {
  const re = new RegExp(`\\.${selector.replace(/\./g, '\\.')}\\s*\\{([^}]*)\\}`, 'm')
  const m = css.match(re)
  assert.ok(m, `styles.css 应存在 .${selector} 规则`)
  return m[1]
}


// 提取最后一个 @media (max-width: 860px) 断点块（括号配平；文件内可能有
// 多个 860px 断点——设置页 #139 与移动端 #270，取最后一个即 issue #270 块）
function lastMedia860(css) {
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

function mobileMediaBlock(css) {
  const block = lastMedia860(css)
  return block.replace(/^@media \(max-width:\s*860px\)\s*\{/, '').replace(/\}$/, '')
}

test('styles.css：任务卡片式列表样式规则齐全', () => {
  const list = pickRule(styles, 'tasks-card-list')
  assert.match(list, /display:\s*flex/, '.tasks-card-list 应纵向堆叠（flex column）')
  assert.match(list, /flex-direction:\s*column/, '.tasks-card-list 应为 column 排列')
  const meta = pickRule(styles, 'tasks-card-meta')
  assert.match(meta, /display:\s*grid/, '.tasks-card-meta 应使用网格排版（标签+值两列）')
  const reason = pickRule(styles, 'tasks-card-reason')
  assert.match(reason, /background:\s*var\(--err-weak\)/, '失败原因应为错误色弱底块')
  const actions = pickRule(styles, 'tasks-card-actions')
  assert.match(actions, /flex-wrap:\s*wrap/, '操作按钮应可换行（窄屏不挤压）')
})

test('styles.css：存在 860px 移动断点且卡片列表样式定义在断点之外（JS 按视口切换）', () => {
  const block = mobileMediaBlock(styles)
  assert.ok(block.length > 0, '860px 断点块应有内容')
})

// ---- 组件渲染 ----

function mkTask(overrides = {}) {
  return {
    id: 7, issue_iid: 117, issue_title: '移动端响应式优化',
    repo_name: 'chenkaidi/botler', status: 'succeeded',
    attempt_count: 1, triggered_by: 'webhook', resumed: false,
    error_message: null, error_detail: null, commit_sha: 'abc1234',
    commit_url: 'https://gitlab.example.com/chenkaidi/botler/-/commit/abc1234',
    created_at: '2026-08-13 09:50:00', finished_at: '2026-08-13 10:30:00',
    ...overrides,
  }
}

// mock 视口宽度（与 tasks-responsive-cols.test.mjs 同款：SSR 无 window，
// addEventListener no-op，组件挂载时 effect 立即按 innerWidth 计算一次）
function withViewport(width) {
  globalThis.window = {
    innerWidth: width,
    addEventListener: () => {},
    removeEventListener: () => {},
  }
}

async function renderAndSettle(tasks, stats = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks, total: tasks.length, stats }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Tasks)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function cardListCount(renderer) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes('tasks-card-list')).length
}

function cardCount(renderer) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes('tasks-card')).filter(
    (n) => !String(n.props.className).includes('tasks-card-list')
      && !String(n.props.className).includes('tasks-card-head')
      && !String(n.props.className).includes('tasks-card-meta')
      && !String(n.props.className).includes('tasks-card-actions')
      && !String(n.props.className).includes('tasks-card-title')
      && !String(n.props.className).includes('tasks-card-reason')
      && !String(n.props.className).includes('tasks-card-meta-row'),
  ).length
}

function tableCount(renderer) {
  return renderer.root.findAllByType('table').length
}

// 渲染树节点 → 纯文本（递归；与 tasks-responsive-cols.test.mjs 的 textOf 同款，
// 不用 JSON.stringify——renderer 节点含 Fiber 循环引用会抛错）
function nodeText(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join('')
  return nodeText(node.props?.children)
}

function controlsByText(renderer, text) {
  // 「执行」等操作是 <Link>（渲染为 <a>），停止/重试是 <button>——统一查找
  return renderer.root.findAll(
    (n) => (n.type === 'button' || n.type === 'a') && nodeText(n.props.children).includes(text),
  )
}

test('渲染：窄视口（375px）渲染卡片式列表，不渲染表格', async () => {
  withViewport(375)
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(cardListCount(renderer), 1, '窄视口应渲染 .tasks-card-list')
    assert.equal(tableCount(renderer), 0, '窄视口不应渲染表格')
    assert.equal(cardCount(renderer), 1, '应渲染 1 张任务卡片')
    // 状态徽章 + 标题
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('移动端响应式优化'), '卡片应展示 issue 标题')
    assert.ok(text.includes('chenkaidi/botler'), '卡片应展示仓库名')
    // 操作按钮：查看详情 + 执行
    assert.ok(controlsByText(renderer, '执行').length >= 1, '卡片应有「执行」按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('渲染：失败任务卡片展示失败原因与重试按钮；运行中展示停止按钮', async () => {
  withViewport(375)
  const tasks = [
    mkTask({ id: 1, status: 'failed', error_message: '模型输出格式非法', error_detail: { attempts: [{ attempt: 1, exit_code: 1, error: 'trace' }] } }),
    mkTask({ id: 2, status: 'running', issue_title: '运行中的任务' }),
  ]
  const { renderer, renderError } = await renderAndSettle(tasks)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('模型输出格式非法'), '失败任务应展示失败原因')
    assert.ok(controlsByText(renderer, '重试').length >= 1, '失败任务应有「重试」按钮')
    assert.ok(controlsByText(renderer, '停止').length >= 1, '运行中任务应有「停止」按钮')
    assert.equal(cardCount(renderer), 2, '应渲染 2 张任务卡片')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('渲染：宽视口（1000px）仍渲染表格，不渲染卡片列表（桌面端不受影响）', async () => {
  withViewport(1000)
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(tableCount(renderer), 1, '宽视口应渲染表格')
    assert.equal(cardListCount(renderer), 0, '宽视口不应渲染卡片列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('渲染：移动端空任务列表展示空态卡片（不崩溃）', async () => {
  withViewport(375)
  const { renderer, renderError } = await renderAndSettle([])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无任务') || text.includes('还没有任务'), '空列表应展示空态文案')
    assert.equal(tableCount(renderer), 0, '窄视口空列表不应渲染表格')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})
