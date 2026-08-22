// 概览页 issue 右边栏「添加评论」与「回复评论」测试（issue #125）：
// 评论区块底部新增评论输入区（POST /api/issues/{project_id}/{iid}/
// comments），每条评论下「回复」按钮展开内联回复框（POST
// /api/issues/{project_id}/{iid}/comments/{note_id}/reply）；成功后
// 本地即时追加新评论并叠加评论计数（快照 + 本次新增），无需重新拉取
// 详情；失败保留输入内容可重试。
//
// 断言：
// 1. 源码：输入区占位文案、「发表评论」按钮、api.post 评论/回复路径、
//    「回复」按钮、回复接口路径；
// 2. 渲染：详情加载成功后才显示评论输入区（加载中/失败隐藏）；
//    每条评论显示「回复」按钮；
// 3. 添加评论：输入 → 发表 → api.post 评论接口（路径/正文正确）→
//    新评论即时追加、输入框清空、评论计数 +1；
// 4. 空输入：发表按钮 disabled（不调用接口）；
// 5. 发表失败：错误信息展示、输入内容保留可重试；
// 6. 回复：点击「回复」→ 回复框展开 → 输入 → 发送 → api.post 回复
//    接口（含 note_id）→ 回复即时追加、回复框收起、计数 +1；
// 7. 回复取消：收起回复框且不调用接口。
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
// overview-issue-notes.test.mjs 一致）
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

test('IssueDrawer 渲染评论输入区与回复按钮，调用评论/回复接口', () => {
  assert.match(drawerSrc, /写下你的评论/, '应渲染新评论输入区占位文案')
  assert.match(drawerSrc, /发表评论/, '应渲染「发表评论」按钮')
  assert.match(drawerSrc, /api\.post/, '应通过 api.post 提交')
  assert.match(drawerSrc, /\/comments/, '添加评论应调用 comments 接口')
  assert.match(drawerSrc, /回复/, '应渲染「回复」按钮')
  assert.match(drawerSrc, /reply/, '回复应调用 reply 接口')
})

// ---- 组件渲染 ----

const OPEN_ISSUE = {
  project_id: 42,
  iid: 97,
  title: '右边栏评论与回复',
  state: 'opened',
  updated_at: '2026-08-16 18:00:00',
  created_at: '2026-08-16 18:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/97',
  description: '需求描述',
  user_notes_count: 2,
}

const NOTE_COMMENT = {
  id: 201, body: '原始评论', system: false,
  author: { name: 'code01', username: 'project_bot',
            avatar_url: 'https://gitlab.example.com/a.png' },
  created_at: '2026-08-16 10:00:00',
}

// 渲染 IssueDrawer 并等待 detail 拉取完成；postImpl 覆盖 api.post
async function renderDrawer(issue, notes, postImpl = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === `/api/issues/${issue.project_id}/${issue.iid}/detail`) {
      return { notes, engine: 'claude' }
    }
    throw new Error('unexpected ' + pathname)
  })
  if (postImpl) {
    mock.method(api, 'post', postImpl)
  } else {
    mock.method(api, 'post', async () => {
      throw new Error('不应调用 api.post')
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

const findComposer = (root) => root.findAll(
  (n) => n.type === 'textarea' && n.props.placeholder === '写下你的评论…')

const findPostBtn = (root) => root.findAll(
  (n) => n.type === 'button' && toText(n) === '发表评论')

const findReplyBtns = (root) => root.findAll(
  (n) => n.type === 'button'
    && String(n.props.className || '').includes('comment-reply-btn'))

const findReplyBox = (root) => root.findAll(
  (n) => String(n.props.className || '').includes('comment-reply-box'))

// ---- 渲染态 ----

test('详情加载成功后才显示评论输入区；每条评论有「回复」按钮', async () => {
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT])
  try {
    assert.equal(findComposer(root).length, 1, '详情加载成功应显示评论输入区')
    assert.equal(findReplyBtns(root).length, 1, '每条评论应有「回复」按钮')
    const text = drawerText(root)
    assert.ok(text.includes('回复'), '应显示回复入口')
    assert.ok(text.includes('2'), '评论计数应显示快照值')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('详情加载失败：不显示评论输入区（避免对不可知目标发言）', async () => {
  mock.method(api, 'get', async () => { throw new Error('GitLab API 错误: 500') })
  mock.method(api, 'post', async () => { throw new Error('不应调用') })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(IssueDrawer, {
      issue: OPEN_ISSUE, repoName: 'botler',
      onClose: () => {}, onIssueClosed: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  try {
    assert.equal(findComposer(renderer.root).length, 0,
                 '加载失败不应显示评论输入区')
    assert.ok(drawerText(renderer.root).includes('GitLab API 错误: 500'),
              '应显示加载失败错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 添加评论 ----

test('添加评论：输入 → 发表 → 调评论接口、本地追加、清空输入框、计数+1', async () => {
  const NEW_NOTE = {
    id: 301, body: '新评论内容', system: false,
    author: { name: 'code01', username: 'project_bot',
              avatar_url: 'https://gitlab.example.com/a.png' },
    created_at: '2026-08-16 11:00:00',
  }
  const postMock = mock.method(api, 'post', async (pathname, body) => {
    assert.equal(pathname, '/api/issues/42/97/comments')
    assert.deepEqual(body, { body: '新评论内容' })
    return { note: NEW_NOTE }
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT], postMock)
  try {
    const composer = findComposer(root)
    await TestRenderer.act(async () => {
      composer[0].props.onChange({ target: { value: '新评论内容' } })
    })
    // 输入后按钮可用
    assert.equal(findPostBtn(root)[0].props.disabled, false, '有内容应可发表')
    await TestRenderer.act(async () => {
      findPostBtn(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postMock.mock.calls.length, 1, '应调用一次评论接口')
    const text = drawerText(root)
    assert.ok(text.includes('新评论内容'), '新评论应即时追加展示')
    assert.equal(findComposer(root)[0].props.value, '', '成功后输入框应清空')
    assert.ok(text.includes('3'), '评论计数应从 2 增到 3')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('添加评论：空输入时发表按钮 disabled（不调用接口）', async () => {
  const postMock = mock.method(api, 'post', async () => ({ note: {} }))
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [], postMock)
  try {
    const btn = findPostBtn(root)[0]
    assert.equal(btn.props.disabled, true, '空输入应禁用发表按钮')
    assert.equal(postMock.mock.calls.length, 0, '不应调用接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('添加评论失败：错误信息展示、输入内容保留可重试', async () => {
  const postMock = mock.method(api, 'post', async () => {
    throw new Error('GitLab API 错误: 500')
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [], postMock)
  try {
    const composer = findComposer(root)
    // onChange 与 onClick 分两个 act（同一 act 内 React 批处理，
    // 点击时 commentText 尚未刷新，提交会提前 return）
    await TestRenderer.act(async () => {
      composer[0].props.onChange({ target: { value: '失败评论' } })
    })
    await TestRenderer.act(async () => {
      findPostBtn(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const text = drawerText(root)
    assert.ok(text.includes('GitLab API 错误: 500'), '应显示失败错误信息')
    assert.equal(findComposer(root)[0].props.value, '失败评论',
                 '失败后输入内容应保留')
    assert.equal(postMock.mock.calls.length, 1, '应调用过一次接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 回复评论 ----

test('回复评论：点「回复」→ 输入 → 发送 → 调回复接口、本地追加、收起回复框', async () => {
  const REPLY_NOTE = {
    id: 302, body: '这是回复', system: false,
    author: { name: 'code01', username: 'project_bot',
              avatar_url: 'https://gitlab.example.com/a.png' },
    created_at: '2026-08-16 12:00:00',
  }
  const postMock = mock.method(api, 'post', async (pathname, body) => {
    assert.equal(pathname, '/api/issues/42/97/comments/201/reply')
    assert.deepEqual(body, { body: '这是回复' })
    return { note: REPLY_NOTE }
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT], postMock)
  try {
    // 展开回复框
    await TestRenderer.act(async () => {
      findReplyBtns(root)[0].props.onClick()
    })
    const box = findReplyBox(root)
    assert.equal(box.length, 1, '点击回复应展开回复框')
    const replyInput = box[0].findAll((n) => n.type === 'textarea')
    assert.equal(replyInput[0].props.placeholder, '回复 @code01…',
                 '回复框占位应带 @作者 提示')
    await TestRenderer.act(async () => {
      replyInput[0].props.onChange({ target: { value: '这是回复' } })
    })
    const sendBtn = box[0].findAll(
      (n) => n.type === 'button' && toText(n) === '发送回复')
    assert.equal(sendBtn[0].props.disabled, false, '有内容应可发送')
    await TestRenderer.act(async () => {
      sendBtn[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postMock.mock.calls.length, 1, '应调用一次回复接口')
    const text = drawerText(root)
    assert.ok(text.includes('这是回复'), '回复应即时追加展示')
    assert.equal(findReplyBox(root).length, 0, '成功后回复框应收起')
    assert.ok(text.includes('3'), '评论计数应从 2 增到 3')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('回复评论：取消收起回复框且不调用接口', async () => {
  const postMock = mock.method(api, 'post', async () => ({ note: {} }))
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT], postMock)
  try {
    await TestRenderer.act(async () => {
      findReplyBtns(root)[0].props.onClick()
    })
    assert.equal(findReplyBox(root).length, 1, '回复框应展开')
    await TestRenderer.act(async () => {
      const cancelBtn = findReplyBox(root)[0].findAll(
        (n) => n.type === 'button' && toText(n) === '取消')
      cancelBtn[0].props.onClick()
    })
    assert.equal(findReplyBox(root).length, 0, '取消后回复框应收起')
    assert.equal(postMock.mock.calls.length, 0, '取消不应调用接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('回复失败：错误信息展示、回复框保留、输入内容保留', async () => {
  const postMock = mock.method(api, 'post', async () => {
    throw new Error('GitLab API 错误: 502')
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [NOTE_COMMENT], postMock)
  try {
    await TestRenderer.act(async () => {
      findReplyBtns(root)[0].props.onClick()
    })
    let box = findReplyBox(root)
    await TestRenderer.act(async () => {
      box[0].findAll((n) => n.type === 'textarea')[0]
        .props.onChange({ target: { value: '失败的回复' } })
    })
    await TestRenderer.act(async () => {
      box[0].findAll(
        (n) => n.type === 'button' && toText(n) === '发送回复')[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const text = drawerText(root)
    assert.ok(text.includes('GitLab API 错误: 502'), '应显示回复失败信息')
    box = findReplyBox(root)
    assert.equal(box.length, 1, '失败后回复框应保留')
    assert.equal(box[0].findAll((n) => n.type === 'textarea')[0].props.value,
                 '失败的回复', '失败后输入内容应保留')
    assert.equal(postMock.mock.calls.length, 1, '应调用过一次接口')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('评论输入区提供图片附件选择、移除与上传后 Markdown 同步路径', () => {
  assert.match(drawerSrc, /type="file"/, '应提供文件选择控件')
  assert.match(drawerSrc, /accept="image\/png,image\/jpeg,image\/gif,image\/webp"/,
    '文件选择器只能选择受支持的图片格式')
  assert.match(drawerSrc, /选择图片/, '应显示选择图片按钮')
  assert.match(drawerSrc, /移除图片/, '选择后应允许移除图片')
  assert.match(drawerSrc, /\/attachments/, '提交前应调用图片附件上传接口')
  assert.match(drawerSrc, /markdown/, '应使用后端返回的 GitLab Markdown 图片引用')
})

test('添加带图片的评论：先上传附件，再将 GitLab Markdown 与正文一并发表', async () => {
  const image = new Blob(['png-bytes'], { type: 'image/png' })
  Object.defineProperty(image, 'name', { value: '截图.png' })
  const postMock = mock.method(api, 'post', async (pathname, body) => {
    if (pathname === '/api/issues/42/97/attachments') {
      assert.ok(body instanceof FormData, '附件请求应使用 multipart FormData')
      assert.ok(body.get('image') instanceof Blob, '应以 multipart 图片字段上传')
      assert.equal(body.get('image').type, 'image/png')
      return { markdown: '![截图.png](/uploads/hash/截图.png)' }
    }
    assert.equal(pathname, '/api/issues/42/97/comments')
    assert.deepEqual(body, {
      body: '请查看截图\n\n![截图.png](/uploads/hash/截图.png)',
    })
    return {
      note: { id: 303, body: body.body, system: false,
        author: { name: 'code01' }, created_at: '2026-08-16 13:00:00' },
    }
  })
  const { renderer, root } = await renderDrawer(OPEN_ISSUE, [], postMock)
  try {
    await TestRenderer.act(async () => {
      findComposer(root)[0].props.onChange({ target: { value: '请查看截图' } })
    })
    const imageInput = root.findAll(
      (n) => n.type === 'input' && n.props.type === 'file')[0]
    await TestRenderer.act(async () => {
      imageInput.props.onChange({ target: { files: [image], value: 'selected' } })
    })
    await TestRenderer.act(async () => {
      findPostBtn(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(postMock.mock.calls.length, 2, '应依次上传附件、发表评论')
    assert.ok(drawerText(root).includes('截图.png'),
      '本地即时评论应渲染 GitLab 图片引用')
    assert.equal(findComposer(root)[0].props.value, '', '成功后应清空评论正文')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
