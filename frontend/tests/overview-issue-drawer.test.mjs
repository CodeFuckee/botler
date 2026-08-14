// 概览页「开放 Issue」右边栏测试（issue #85）：
// 点击 issue 项 → 打开右侧抽屉显示 issue 具体信息与正文（Markdown 渲染）；
// 点击 issue 标题不再直接跳转 GitLab，改为抽屉右上角「在 GitLab 中打开」
// 跳转按钮；关闭方式：× 按钮 / 点击遮罩 / Esc 键。
//
// 断言：
// 1. 列表项渲染为按钮（可点击打开抽屉），不再是指向 web_url 的 <a> 链接；
// 2. 点击列表项打开抽屉：显示 #iid、标题、状态、作者、创建/更新时间、
//    标签、正文（Markdown 渲染为结构化文本）；
// 3. 抽屉右上角跳转按钮：href 为 issue web_url、新窗口打开；
// 4. 关闭：× 按钮、点击遮罩（overlay）、Esc 键（isEscapeKey 纯函数）；
// 5. 边界：description 缺失/空 → 「暂无描述」占位；author/created_at
//    缺失（旧版数据）→ 「—」兜底不崩溃；重复点击同一 issue 幂等；
//    切换 issue 时抽屉内容随之更新。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: IssueDrawer, isEscapeKey, ISSUE_STATE_META } =
  await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('issue 列表项不再直接跳转 GitLab，改为按钮打开右边栏', () => {
  // 列表项渲染为 button（onClick 打开抽屉），不再是指向 web_url 的 a 标签
  assert.match(overviewSrc, /issue-link/, '应保留 issue-link 样式类')
  assert.match(overviewSrc, /<button/, '列表项应渲染为 button 元素')
  assert.match(overviewSrc, /setSelectedIssue/, '点击列表项应设置选中 issue 打开抽屉')
  assert.match(overviewSrc, /IssueDrawer/, '应渲染 IssueDrawer 组件')
})

test('IssueDrawer 监听 Esc 关闭（isEscapeKey 纯函数判定）', () => {
  assert.match(drawerSrc, /addEventListener\('keydown'/, '应监听 keydown 事件')
  assert.match(drawerSrc, /removeEventListener\('keydown'/, '卸载时应清理监听')
  assert.match(drawerSrc, /isEscapeKey/, 'Esc 判定应走 isEscapeKey')
  // 纯函数边界：Escape 键 → true；其他键/空值 → false
  assert.equal(isEscapeKey({ key: 'Escape' }), true)
  assert.equal(isEscapeKey({ key: 'Enter' }), false)
  assert.equal(isEscapeKey({ key: 'escape' }), false, '大小写敏感，非 Escape 不关闭')
  assert.equal(isEscapeKey(null), false, '空值不应报错')
  assert.equal(isEscapeKey({}), false, '无 key 字段不应报错')
})

test('IssueDrawer 状态徽章映射覆盖 opened 状态', () => {
  assert.ok(ISSUE_STATE_META.opened, 'opened 状态应有徽章映射')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

async function renderOverview(issuesPayload) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return issuesPayload
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

// 完整字段 issue（issue #85 后端透传字段齐全）
const FULL_ISSUE = {
  iid: 64, title: '概览页面增加读取已启用的仓库issue',
  state: 'opened',
  updated_at: '2026-08-14 10:20:00',
  created_at: '2026-08-10 09:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
  description: '**需求**\n\n- 要点一\n- 要点二',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [
    { name: 'feature', color: '428BCA', text_color: 'FFFFFF' },
    { name: 'ui', color: '69D100', text_color: 'FFFFFF' },
  ],
  milestone: 'v1.0',
  assignees: [{ name: 'Agent', username: 'agent',
                avatar_url: 'https://gitlab.example.com/avatar/agent.png' }],
  user_notes_count: 3,
}

function issuesPayload(issues) {
  return {
    repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues }],
    errors: [], total: issues.length,
  }
}

// 在 Overview 渲染树中找到 issue 列表项按钮并模拟点击
async function openDrawer(issues) {
  const { renderer, renderError } = await renderOverview(issuesPayload(issues))
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  const root = renderer.root
  const itemBtn = root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
  assert.ok(itemBtn.length > 0, '应渲染 issue 列表项按钮')
  await TestRenderer.act(async () => {
    itemBtn[0].props.onClick()
  })
  return { renderer, root }
}

function findDrawer(root) {
  return root.findAll(
    (n) => String(n.props.className || '').includes('issue-drawer')
      && n.props.onClick /* 内部抽屉容器（stopPropagation 点击不关闭） */)
}

// 渲染树 → 纯文本（toJSON 树与 TestInstance 通用）：
// JSX 文本插值（如 `#{i.iid}`）会被拆成 "#" 与数字两个节点，
// JSON 序列化后不连续，断言需先拼成纯文本
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

// 抽屉子树纯文本（仅断言抽屉内容，避免列表项同名文本干扰）
function drawerText(root) {
  const drawers = findDrawer(root)
  return drawers.length > 0 ? toText(drawers[0].children) : ''
}

test('点击 issue 列表项打开右边栏，标题不再是跳转链接', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    // 抽屉打开：显示 #iid 与标题
    const text = drawerText(root)
    assert.ok(text.includes('#64'), '抽屉应显示 issue 编号')
    assert.ok(text.includes('概览页面增加读取已启用的仓库issue'), '抽屉应显示 issue 标题')
    // 抽屉右上角跳转按钮：href 为 web_url、新窗口打开
    const gitlabLinks = root.findAll(
      (n) => n.type === 'a' && n.props.href === FULL_ISSUE.web_url)
    assert.equal(gitlabLinks.length, 1, '仅右上角跳转按钮应指向 GitLab')
    assert.equal(gitlabLinks[0].props.target, '_blank', '跳转按钮应新窗口打开')
    assert.equal(gitlabLinks[0].props.rel, 'noreferrer')
    // 列表项本身是按钮而非链接（点击不再直接跳转）
    const listItemLinks = root.findAll(
      (n) => n.type === 'a' && String(n.props.className || '').includes('issue-link'))
    assert.equal(listItemLinks.length, 0, '列表项不应再渲染为跳转链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('右边栏展示 issue 具体信息：状态/作者/时间/标签/里程碑/评论数', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('开放'), '应显示状态徽章「开放」')
    assert.ok(text.includes('Chen'), '应显示作者（name 优先）')
    assert.ok(text.includes('v1.0'), '应显示里程碑')
    assert.ok(text.includes('feature'), '应显示标签胶囊')
    assert.ok(text.includes('ui'), '应显示第二个标签')
    assert.ok(text.includes('Agent'), '应显示 assignee 姓名')
    assert.ok(text.includes('botler'), '应显示所属仓库名')
    // 创建/更新时间经 fmtTime 格式化（UTC 无后缀 + 浏览器时区 → 无法精确断言，
    // 只断言渲染了时间字符串而非原始空值）
    assert.ok(!text.includes('2026-08-14 10:20:00'), '原始 UTC 时间不应原样出现（应经 fmtTime 格式化）')
    const drawer = findDrawer(root)[0]
    const labels = drawer.findAll((n) => n.props.className === 'label-pill')
    assert.equal(labels.length, 2, '抽屉内应渲染两个标签胶囊')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('右边栏正文走 Markdown 渲染：粗体与列表结构化展示', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    // Markdown 渲染后：**需求** → <strong>，列表 → <ul>/<li>（限定抽屉子树）
    const drawer = findDrawer(root)[0]
    const strong = drawer.findAll((n) => n.type === 'strong')
    assert.ok(strong.some((s) => toText(s.props.children) === '需求'),
              '**粗体** 应渲染为 strong')
    const list = drawer.findAll((n) => n.type === 'ul')
    assert.ok(list.length > 0, '列表语法应渲染为 ul')
    const liText = drawer.findAll((n) => n.type === 'li')
      .map((li) => toText(li.props.children)).join('|')
    assert.ok(liText.includes('要点一') && liText.includes('要点二'), '列表项内容应完整渲染')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('关闭抽屉：× 按钮关闭', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    const closeBtn = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('modal-close'))
    assert.equal(closeBtn.length, 1, '抽屉应有 × 关闭按钮')
    await TestRenderer.act(async () => {
      closeBtn[0].props.onClick()
    })
    assert.equal(findDrawer(root).length, 0, '× 点击后抽屉应卸载')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('关闭抽屉：点击遮罩（overlay）关闭', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    const overlay = root.findAll(
      (n) => String(n.props.className || '') === 'drawer-overlay')
    assert.equal(overlay.length, 1, '应有遮罩层')
    await TestRenderer.act(async () => {
      overlay[0].props.onClick()
    })
    assert.equal(findDrawer(root).length, 0, '点击遮罩后抽屉应卸载')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('切换点击另一条 issue：抽屉内容随之更新', async () => {
  const second = { ...FULL_ISSUE, iid: 63, title: '对账遇 token 失效时兜底',
                   web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/63',
                   description: null }
  const { renderer, root } = await openDrawer([FULL_ISSUE, second])
  try {
    // 断言范围限定在抽屉子树：列表项始终显示全部标题，不能整页断言
    assert.equal(findDrawer(root).length, 1, '初始应有一个抽屉')
    assert.ok(drawerText(root).includes('#64'), '初始应显示第一条 issue')
    const itemBtns = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    assert.equal(itemBtns.length, 2, '应有两条可点击的列表项')
    await TestRenderer.act(async () => {
      itemBtns[1].props.onClick()
    })
    const text = drawerText(root)
    assert.ok(text.includes('#63') && text.includes('对账遇 token 失效时兜底'),
              '切换后抽屉应显示第二条 issue')
    assert.ok(!text.includes('概览页面增加读取已启用的仓库issue'), '旧内容不应残留')
    // 第二条 description 为 null → 占位文案
    assert.ok(text.includes('暂无描述'), '空正文应显示占位文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('重复点击同一 issue：抽屉保持打开不崩溃（幂等）', async () => {
  const { renderer, root } = await openDrawer([FULL_ISSUE])
  try {
    const itemBtns = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('issue-link'))
    await TestRenderer.act(async () => {
      itemBtns[0].props.onClick()
      itemBtns[0].props.onClick()
    })
    assert.ok(drawerText(root).includes('#64'), '重复点击后抽屉应保持打开')
    assert.equal(findDrawer(root).length, 1, '应只有一个抽屉实例')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('旧版数据（无 description/author/created_at）打开抽屉不崩溃', async () => {
  const legacy = {
    iid: 7, title: '修复登录问题',
    updated_at: '2026-08-13 01:00:00',
    web_url: 'https://gitlab.example.com/chenkaidi/shipyard/-/issues/7',
  }
  const { renderer, root } = await openDrawer([legacy])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('修复登录问题'), '旧数据 issue 应正常打开抽屉')
    assert.ok(text.includes('暂无描述'), '无 description 应显示占位')
    // 无作者/无状态/无时间 → 均以「—」兜底不崩溃
    assert.ok(text.includes('—'), '缺失字段应以「—」兜底')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('description 为空字符串与纯空白：显示占位不渲染空 Markdown', async () => {
  for (const description of ['', '   ']) {
    const { renderer, root } = await openDrawer(
      [{ ...FULL_ISSUE, description }])
    try {
      const text = drawerText(root)
      assert.ok(text.includes('暂无描述'), `description=${JSON.stringify(description)} 应显示占位`)
      // Markdown 组件对空内容返回 null，不渲染空段落
      const drawer = findDrawer(root)[0]
      const paragraphs = drawer.findAll((n) => n.type === 'p')
      assert.ok(!paragraphs.some(
        (p) => p.props.className === undefined && p.props.children == null),
        '空正文不应渲染空段落')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})
