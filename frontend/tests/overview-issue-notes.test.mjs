// 概览页 issue 右边栏「评论与活动」测试（issue #97）：
// 抽屉打开后按需拉取 GET /api/issues/{project_id}/{iid}/detail，
// 在描述下方分「评论」（用户发言，Markdown 渲染）与「活动」（系统
// 事件，纯文本）两个区块展示；覆盖边界：加载中、加载失败重试、
// 空评论/空活动占位、旧数据缺 project_id 不拉接口。
//
// 断言：
// 1. 打开抽屉（带 project_id）→ 调用 detail 接口（路径参数正确）；
// 2. 评论渲染：作者名（name 优先回退 username）、头像、时间（fmtTime
//    格式化）、正文走 Markdown 渲染（粗体 → strong）；
// 3. 活动渲染：system notes 独立分区展示（与评论区分开）；
// 4. 边界：无评论/无活动 → 占位文案；缺 project_id 旧数据 → 不调
//    接口显示占位；接口失败 → 错误提示 + 重试按钮可重新拉取；
// 5. 切换 issue（props 变化）→ 重新拉取 detail；重复渲染幂等。
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
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 打开后拉取 detail 接口，分评论/活动两区渲染', () => {
  assert.match(drawerSrc, /\/detail/, '应调用 detail 详情接口')
  assert.match(drawerSrc, /api\.get/, '应通过 api.get 拉取')
  assert.match(drawerSrc, /评论/, '应渲染「评论」区块')
  assert.match(drawerSrc, /活动/, '应渲染「活动」区块')
  assert.match(drawerSrc, /system/, '应按 system 标志区分评论与活动')
})

// ---- 组件渲染 ----

const OPEN_ISSUE = {
  project_id: 42,
  iid: 97,
  title: '右边栏显示评论与活动',
  state: 'opened',
  updated_at: '2026-08-15 18:00:00',
  created_at: '2026-08-15 18:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/97',
  description: '需求描述',
}

const NOTE_COMMENT = {
  id: 201, body: '**确认** 该方案可行', system: false,
  author: { name: 'code01', username: 'project_bot',
            avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-15 10:00:00',
}
const NOTE_ACTIVITY = {
  id: 202, body: 'assigned to @agent', system: true,
  author: { name: 'code01', username: 'project_bot',
            avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-15 09:00:00',
}

// 渲染 IssueDrawer 并等待 detail 拉取完成（notes 为返回数据；
// 传入 getImpl 可覆盖 api.get mock 行为）
async function renderDrawer(issue, notes, getImpl = null) {
  if (getImpl) {
    mock.method(api, 'get', getImpl)
  } else {
    mock.method(api, 'get', async (pathname) => {
      if (pathname === `/api/issues/${issue.project_id}/${issue.iid}/detail`) {
        return { notes }
      }
      throw new Error('unexpected ' + pathname)
    })
  }
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue, repoName: 'botler', onClose: () => {}, onIssueClosed: () => {},
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  return { renderer, root: renderer.root }
}

// 渲染树 → 纯文本（与 overview-issue-drawer.test.mjs 的 toText 一致）
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

function drawerText(root) {
  return toText(root.children)
}

test('打开抽屉拉取 detail 接口（路径按 project_id/iid 拼接）', async () => {
  // issue #118：抽屉打开还会拉取 /api/settings（执行引擎行），
  // 计数只统计 detail 路径调用
  const getMock = mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/settings') return { worker: { engine: 'claude' } }
    assert.equal(pathname, '/api/issues/42/97/detail')
    return { notes: [] }
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [], getMock)
  try {
    const detailCalls = getMock.mock.calls.filter(
      (c) => String(c.arguments[0]).includes('/detail')).length
    assert.equal(detailCalls, 1, '应调用一次 detail 接口')
    assert.ok(drawerText(root).includes('#97'), '抽屉应显示 issue 编号')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('评论区块：作者/头像/时间/正文渲染，正文走 Markdown', async () => {
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('评论'), '应渲染「评论」区块标题')
    assert.ok(text.includes('code01'), '应显示评论作者名')
    // 时间经 fmtTime 格式化，原始 UTC 无后缀串不应原样出现
    assert.ok(!text.includes('2026-08-15 10:00:00'),
              '评论时间应经 fmtTime 格式化')
    // Markdown 渲染：**确认** → strong
    const strong = root.findAll(
      (n) => n.type === 'strong' && toText(n.props.children) === '确认')
    assert.ok(strong.length > 0, '评论正文 **粗体** 应渲染为 strong')
    // 头像：avatar_url 渲染 img
    const imgs = root.findAll(
      (n) => n.type === 'img' && n.props.src === NOTE_COMMENT.author.avatar_url)
    assert.ok(imgs.length > 0, '评论应显示作者头像')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('活动区块：system notes 独立展示（不与评论混排）', async () => {
  const { renderer, root } = await renderDrawer(
    OPEN_ISSUE, [NOTE_COMMENT, NOTE_ACTIVITY])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('活动'), '应渲染「活动」区块标题')
    assert.ok(text.includes('assigned to @agent'), '应显示系统活动文本')
    // 评论与活动分区：评论列表与活动列表是不同容器
    const comments = root.findAll(
      (n) => String(n.props.className || '').includes('comment-item'))
    const activities = root.findAll(
      (n) => String(n.props.className || '').includes('activity-item'))
    assert.equal(comments.length, 1, '非 system 评论应进评论区')
    assert.equal(activities.length, 1, 'system 事件应进活动区')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('空评论与空活动：分别显示占位文案', async () => {
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('暂无评论'), '无评论应显示占位')
    assert.ok(text.includes('暂无活动'), '无活动应显示占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('加载中：detail 未返回前显示加载中文案', async () => {
  let resolveGet
  const getImpl = () => new Promise((resolve) => { resolveGet = resolve })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, null, getImpl)
  try {
    assert.ok(drawerText(root).includes('加载中'), '请求挂起时应显示「加载中」')
    await TestRenderer.act(async () => {
      resolveGet({ notes: [] })
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('暂无评论'), '返回后应渲染占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('接口失败：显示错误信息与重试按钮，重试可恢复', async () => {
  // issue #118：/api/settings 正常返回（执行引擎行），detail 首调失败
  let calls = 0
  const getImpl = async (pathname) => {
    if (pathname === '/api/settings') return { worker: { engine: 'claude' } }
    calls += 1
    if (calls === 1) throw new Error('GitLab API 错误: 500')
    return { notes: [NOTE_COMMENT] }
  }
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, null, getImpl)
  try {
    assert.ok(drawerText(root).includes('GitLab API 错误: 500'),
              '应显示加载失败错误信息')
    const retryBtn = root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('notes-retry'))
    assert.equal(retryBtn.length, 1, '应显示重试按钮')
    await TestRenderer.act(async () => {
      retryBtn[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(calls, 2, '点击重试应重新调用接口')
    assert.ok(drawerText(root).includes('code01'), '重试成功后应渲染评论')
    assert.ok(!drawerText(root).includes('GitLab API 错误'),
              '成功后错误信息应消失')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('旧数据缺 project_id：不调接口，显示占位文案', async () => {
  const legacy = { ...OPEN_ISSUE }
  delete legacy.project_id
  // issue #118：执行引擎行仍需拉取 /api/settings（与 project_id 无关），
  // 断言的是 detail 接口不被调用
  const getMock = mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/settings') return { worker: { engine: 'claude' } }
    throw new Error('不应调用 detail 接口')
  })
  const { renderer, root } = await renderDrawer(legacy, null, getMock)
  try {
    const detailCalls = getMock.mock.calls.filter(
      (c) => String(c.arguments[0]).includes('/detail')).length
    assert.equal(detailCalls, 0, '缺 project_id 不应调用 detail 接口')
    const text = drawerText(root)
    assert.ok(text.includes('缺少仓库信息'), '应显示无法加载占位文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('切换 issue：props 变化重新拉取对应 detail', async () => {
  const other = { ...OPEN_ISSUE, iid: 98, title: '另一个 issue' }
  const getMock = mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/settings') return { worker: { engine: 'claude' } }
    if (pathname === '/api/issues/42/97/detail') return { notes: [NOTE_COMMENT] }
    if (pathname === '/api/issues/42/98/detail') return { notes: [NOTE_ACTIVITY] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(IssueDrawer, {
      issue: OPEN_ISSUE, repoName: 'botler',
      onClose: () => {}, onIssueClosed: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  try {
    assert.ok(drawerText(renderer.root).includes('code01'),
              '初始应渲染第一条 issue 的评论')
    await TestRenderer.act(async () => {
      renderer.update(React.createElement(IssueDrawer, {
        issue: other, repoName: 'botler',
        onClose: () => {}, onIssueClosed: () => {},
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const text = drawerText(renderer.root)
    assert.ok(text.includes('assigned to @agent'), '切换后应渲染新 issue 的活动')
    assert.ok(!text.includes('code01'), '旧评论不应残留')
    const detailCalls = getMock.mock.calls.filter(
      (c) => String(c.arguments[0]).includes('/detail')).length
    assert.equal(detailCalls, 2, '应分别拉取两条 issue 的 detail')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('评论作者缺 name：回退 username；无头像：首字母兜底', async () => {
  const note = {
    id: 203, body: '只有 username', system: false,
    author: { username: 'only_name' }, created_at: '2026-08-15 08:00:00',
  }
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [note])
  try {
    const text = drawerText(root)
    assert.ok(text.includes('only_name'), '作者 name 缺失应回退 username')
    const fallback = root.findAll(
      (n) => String(n.props.className || '').includes('avatar-fallback'))
    assert.ok(fallback.length > 0, '无 avatar_url 应渲染首字母兜底')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('评论正文为空：渲染占位不崩', async () => {
  const note = { id: 204, body: null, system: false,
                 author: { name: 'x' }, created_at: null }
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [note])
  try {
    assert.ok(drawerText(root).includes('（无内容）'), '空正文应显示占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
