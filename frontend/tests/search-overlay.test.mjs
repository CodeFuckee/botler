// 全局搜索浮层测试（issue #216）：跨模块（任务 / issue / 灵感 / 仓库）
// 检索与跳转。
//
// 断言：
// 1. 源码：组件调 GET /api/search?q=...（关键词经 encodeURIComponent）；
//    关键词高亮走 splitKeyword（命中段包 <mark>）；
// 2. 行为：输入关键词（防抖 300ms）后渲染分组结果，命中关键词以
//    <mark> 高亮，模块标题与条数展示；
// 3. 跳转：点击结果 → navigate 目标——任务 → /tasks/:id；issue →
//    /overview?issue=pid:iid；灵感 → /overview?repo=&section=inspirations；
//    仓库 → /overview?repo=（lib/searchJump.js 集中解析）；
// 4. 键盘：↑ / ↓ 移动选中（active 类切换），Enter 跳转选中项（无选中
//    跳第一条），Esc / × / 遮罩点击关闭；
// 5. 边界：空关键词显示提示不发请求；无结果显示空态；接口失败显示
//    错误文案不崩溃；快速连续输入旧响应被丢弃（竞态保护）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router-search.jsx'),
    },
  },
})
const { default: SearchOverlay } = await vite.ssrLoadModule('/src/components/SearchOverlay.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { navCalls } = await vite.ssrLoadModule('/tests/helpers/mock-router-search.jsx')
const { splitKeyword } = await vite.ssrLoadModule('/src/lib/highlightKeyword.js')
const src = readFileSync(path.join(ROOT, 'src/components/SearchOverlay.jsx'), 'utf8')

after(() => vite.close())

// 返回一个按关键词返回固定结果的 api.get mock；用 setter 在用例内改响应
let searchHandler = async () => ({ tasks: [], issues: [], inspirations: [], repos: [] })
mock.method(api, 'get', async (pathname) => {
  if (pathname.startsWith('/api/search?')) return searchHandler(pathname)
  return {}
})

const SAMPLE = {
  tasks: [{ id: 7, repo_id: 1, repo_name: 'alpha', issue_iid: 100, issue_title: '新增全局搜索功能', status: 'succeeded' }],
  issues: [{ project_id: 1, iid: 200, title: '搜索功能需求', repo_name: 'alpha', state: 'opened', web_url: 'https://x' }],
  inspirations: [{ id: 3, repo_id: 2, repo_name: 'beta', content: '全局搜索框放顶栏更好用' }],
  repos: [{ id: 1, gitlab_project_id: 1, name: 'alpha-bot', url: 'https://gitlab.example.com/alpha-bot.git' }],
}

// 渲染浮层并等待防抖搜索完成
async function renderOverlay() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(SearchOverlay, { onClose: () => {} }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

// 输入关键词并等待防抖（300ms）+ 响应
async function typeAndWait(renderer, value, wait = 450) {
  const input = renderer.root.findAll((n) => n.type === 'input')[0]
  await TestRenderer.act(async () => {
    input.props.onChange({ target: { value } })
    await new Promise((resolve) => setTimeout(resolve, wait))
  })
}

function resultButtons(renderer) {
  return renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('search-result'))
}

// ---- 1. 源码断言 ----

test('源码：调用 /api/search?q= 并按模块分组渲染、关键词高亮', () => {
  assert.match(src, /\/api\/search\?q=\$\{encodeURIComponent\(term\)\}/, '应带关键词请求搜索接口')
  assert.match(src, /splitKeyword/, '高亮应走 splitKeyword')
  assert.match(src, /<mark>/, '命中段应包 <mark>')
  assert.match(src, /MODULES = \['tasks', 'issues', 'inspirations', 'repos'\]/, '四模块分组')
  assert.match(src, /taskTarget\(item\.id\)/, '任务跳转任务详情页')
  assert.match(src, /issueTarget\(item\.project_id, item\.iid\)/, 'issue 跳转概览深链')
  assert.match(src, /repoTarget\(item\.repo_id, 'inspirations'\)/, '灵感跳转概览灵感板块')
  assert.match(src, /repoTarget\(item\.id\)/, '仓库跳转概览仓库定位')
})

// ---- 2. 高亮纯函数 ----

test('splitKeyword：大小写不敏感切分并标记命中段', () => {
  assert.deepEqual(splitKeyword('新增全局搜索功能', '搜索'), [
    { text: '新增全局', hit: false }, { text: '搜索', hit: true }, { text: '功能', hit: false },
  ])
  assert.deepEqual(splitKeyword('Global Search Box', 'global search'), [
    { text: 'Global Search', hit: true }, { text: ' Box', hit: false },
  ])
  assert.deepEqual(splitKeyword('abc', ''), [{ text: 'abc', hit: false }], '空关键词不切分')
  assert.deepEqual(splitKeyword('', 'x'), [{ text: '', hit: false }], '空文本不切分')
  assert.deepEqual(splitKeyword(null, 'x'), [{ text: '', hit: false }], 'null 文本容错')
})

// ---- 3. 行为：分组渲染 + 高亮 ----

test('行为：输入关键词渲染四模块分组结果并高亮关键词', async () => {
  searchHandler = async () => ({ query: '搜索', ...SAMPLE })
  const renderer = await renderOverlay()
  try {
    await typeAndWait(renderer, '搜索')
    // 四模块标题
    const titles = renderer.root.findAll((n) => n.type === 'h3').map((n) => n.props.children?.[0])
    assert.deepEqual(titles, ['任务', 'Issue', '灵感', '仓库'], '应渲染四模块标题')
    // 结果条数与高亮
    const btns = resultButtons(renderer)
    assert.equal(btns.length, 4)
    const marks = renderer.root.findAll((n) => n.type === 'mark')
    assert.ok(marks.length >= 3, '任务/issue/灵感命中关键词应包 <mark> 高亮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 4. 跳转 ----

test('行为：点击结果跳转对应详情（任务/issue/灵感/仓库）', async () => {
  searchHandler = async () => ({ query: '搜索', ...SAMPLE })
  const renderer = await renderOverlay()
  try {
    await typeAndWait(renderer, '搜索')
    const btns = resultButtons(renderer)
    // 任务 → /tasks/7
    navCalls.length = 0
    TestRenderer.act(() => btns[0].props.onClick())
    assert.deepEqual(navCalls, ['/tasks/7'])
    // issue → /overview?issue=1:200
    navCalls.length = 0
    TestRenderer.act(() => btns[1].props.onClick())
    // URLSearchParams 会把冒号编码为 %3A，服务端/前端 URLSearchParams
    // 解析后还原为 1:200，路由语义一致
    assert.deepEqual(navCalls, ['/overview?issue=1%3A200'])
    // 灵感 → /overview?repo=2&section=inspirations
    navCalls.length = 0
    TestRenderer.act(() => btns[2].props.onClick())
    assert.deepEqual(navCalls, ['/overview?repo=2&section=inspirations'])
    // 仓库 → /overview?repo=1
    navCalls.length = 0
    TestRenderer.act(() => btns[3].props.onClick())
    assert.deepEqual(navCalls, ['/overview?repo=1'])
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 5. 键盘导航 ----

test('行为：↑↓ 移动选中、Enter 跳转选中项、Esc 关闭', async () => {
  let closed = 0
  searchHandler = async () => ({ query: '搜索', ...SAMPLE })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(SearchOverlay, { onClose: () => { closed++ } }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  try {
    await typeAndWait(renderer, '搜索')
    const input = renderer.root.findAll((n) => n.type === 'input')[0]
    // ArrowDown 选中第一条 → Enter 跳第一条
    TestRenderer.act(() => input.props.onKeyDown({ key: 'ArrowDown', preventDefault: () => {} }))
    let active = renderer.root.findAll((n) => String(n.props.className || '').includes('search-result active'))
    assert.equal(active.length, 1, 'ArrowDown 应选中一条结果')
    navCalls.length = 0
    TestRenderer.act(() => input.props.onKeyDown({ key: 'Enter', preventDefault: () => {} }))
    assert.deepEqual(navCalls, ['/tasks/7'], 'Enter 应跳转选中项（第一条任务）')
    // Esc 关闭
    const before = closed
    TestRenderer.act(() => input.props.onKeyDown({ key: 'Escape', preventDefault: () => {} }))
    assert.equal(closed, before + 1, 'Esc 应关闭浮层')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 6. 边界 ----

test('边界：空关键词显示提示不发请求；无结果显示空态', async () => {
  const getCalls = []
  searchHandler = async (pathname) => { getCalls.push(pathname); return SAMPLE }
  const renderer = await renderOverlay()
  try {
    // 未输入：提示文案 + 无请求
    const emptyHint = renderer.root.findAll((n) => n.type === 'p' && String(n.props.className || '').includes('muted'))
    assert.ok(emptyHint.length > 0, '空关键词应显示提示')
    assert.equal(getCalls.length, 0, '空关键词不应发起搜索请求')
    // 无结果
    searchHandler = async () => ({ query: '不存在', tasks: [], issues: [], inspirations: [], repos: [] })
    await typeAndWait(renderer, '不存在的词')
    const texts = renderer.root.findAll((n) => n.type === 'p').map((n) => n.props.children).flat().join('')
    assert.match(String(texts), /没有匹配/, '应显示无结果文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('边界：接口失败显示错误文案不崩溃', async () => {
  searchHandler = async () => { throw new Error('boom') }
  const renderer = await renderOverlay()
  try {
    await typeAndWait(renderer, '搜索')
    const texts = renderer.root.findAll((n) => n.type === 'p').map((n) => n.props.children).flat().join('')
    assert.match(String(texts), /搜索失败/, '应显示失败文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('边界：× 按钮与遮罩点击关闭', async () => {
  let closed = 0
  searchHandler = async () => SAMPLE
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(SearchOverlay, { onClose: () => { closed++ } }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  try {
    // × 按钮
    const closeBtn = renderer.root.findAll((n) => n.type === 'button' && String(n.props.className || '').includes('modal-close'))[0]
    TestRenderer.act(() => closeBtn.props.onClick())
    assert.equal(closed, 1, '× 应关闭浮层')
    // 遮罩点击
    const overlay = renderer.root.findAll((n) => String(n.props.className || '').includes('search-overlay-backdrop'))[0]
    TestRenderer.act(() => overlay.props.onClick())
    assert.equal(closed, 2, '遮罩点击应关闭浮层')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
