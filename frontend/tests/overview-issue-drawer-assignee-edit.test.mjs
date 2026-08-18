// 概览页 issue 右边栏「负责人编辑」测试（issue #303）：
// 负责人行展示当前负责人 + 「编辑」按钮；点击进入编辑态并加载项目
// 成员（GET /api/issues/{project_id}/members，下拉选择、当前负责人
// 按 username 预选）；保存时 PUT /api/issues/{project_id}/{iid}/
// assignee 提交 assignee_id（None=清除负责人）同步 GitLab；成功后
// 退出编辑态、本地负责人即时更新并通知父组件刷新列表；失败保留编辑
// 态可重试；取消不调接口。
//
// 断言：
// 1. 带 project_id 的 issue 显示「编辑」按钮；缺 project_id
//    （旧缓存数据）不显示；
// 2. 点击编辑 → 加载成员，下拉预选当前负责人（按 username 匹配）；
// 3. 修改选择 → 保存调用 PUT，assignee_id 参数正确；
// 4. 清除负责人：选择「不指定」→ PUT assignee_id=null；
// 5. 保存成功：退出编辑态、新负责人出现/旧负责人消失、onAssigneeUpdated 触发；
// 6. 保存失败：错误信息展示、编辑态保留可重试、回调不触发；
// 7. 成员加载失败：错误 + 重试按钮；重试成功后正常进入编辑态；
// 8. 空成员池：提示「该仓库暂无成员」，仍可保存（清除负责人）；
// 9. 取消编辑：不调接口，负责人显示恢复原状；
// 10. 保存请求进行中按钮 disabled 防重复提交。
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
// overview-issue-drawer-labels-edit.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const dialog = await vite.ssrLoadModule('/src/dialog.js')

const drawerSrc = readFileSync(path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('IssueDrawer 源码包含负责人编辑的数据流', () => {
  assert.match(drawerSrc, /修改该 issue 的负责人/, '应渲染负责人「编辑」按钮文案')
  assert.match(drawerSrc, /\/members/, '应调用项目成员接口路径')
  assert.match(drawerSrc, /\/assignee/, '应调用负责人更新接口路径')
  assert.match(drawerSrc, /api\.put/, '保存应调用 api.put')
  assert.match(drawerSrc, /onAssigneeUpdated/, '成功后应通知父组件刷新列表')
})

// ---- 组件渲染 ----

// 渲染 IssueDrawer：props 最小集合（SSR 环境 Esc 监听自动跳过）。
// api.get 按 pathname 路由 mock：detail 返回空 notes（本文件只关注
// 负责人编辑），members 返回 opts.memberPool 配置的项目成员；
// opts.memberPoolErrorFn 返回非空错误时模拟加载失败（闭包可变，供
// 「失败→重试成功」用例切换）
async function renderDrawer(issue, opts = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (String(pathname).endsWith('/detail')) return { notes: [] }
    if (String(pathname).endsWith('/members')) {
      if (opts.memberPoolErrorFn) {
        const err = opts.memberPoolErrorFn()
        if (err) throw err
      }
      return { members: opts.memberPool || [] }
    }
    throw new Error(`unexpected GET ${pathname}`)
  })
  dialog.installAutoAnswer(() => true)
  const onAssigneeUpdated = opts.onAssigneeUpdated || (() => {})
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue,
        repoName: 'botler',
        onClose: () => {},
        onAssigneeUpdated,
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  return { renderer, root: renderer.root, onAssigneeUpdated }
}

// 带负责人的开放 issue（负责人为后端精简对象：name/username/avatar_url）
const ASSIGNED_ISSUE = {
  project_id: 42,
  iid: 94,
  title: '修改负责人',
  state: 'opened',
  updated_at: '2026-08-15 17:03:00',
  created_at: '2026-08-15 17:03:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/94',
  description: '需求描述',
  labels: [],
  assignees: [
    { name: 'agent', username: 'agent',
      avatar_url: 'https://gitlab.example.com/agent.png' },
  ],
}

// 项目成员池（含当前负责人 + 候选成员，id 为 GitLab 用户 id）
const MEMBER_POOL = [
  { id: 3, username: 'agent', name: 'agent' },
  { id: 7, username: 'dev', name: '开发' },
  { id: 8, username: 'tester', name: '测试' },
]

// 查找「编辑」按钮（负责人行内的 btn-small 按钮）
function findEditAssigneeButton(root) {
  return root.findAll(
    (n) => n.type === 'button'
      && String(n.props.title || '').includes('修改该 issue 的负责人'))
}

// 渲染树 → 纯文本（与 labels-edit 测试的 toText 一致）
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

// 编辑态下拉：type=select（编辑态未开启时应为 0 个）
function findSelects(root) {
  return root.findAll((n) => n.type === 'select')
}

// 查找「保存」按钮（编辑态 footer 中的 btn-primary 按钮）
function findSaveButton(root) {
  return root.findAll(
    (n) => n.type === 'button'
      && String(n.props.children || '').includes('保存'))
}

// 查找「取消」按钮（编辑态中非保存/非关闭的按钮）
function findCancelButton(root) {
  return root.findAll(
    (n) => n.type === 'button'
      && String(n.props.children || '').includes('取消'))
}

test('带 project_id 显示「编辑」按钮；缺 project_id 不显示', async () => {
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE)
  try {
    assert.equal(findEditAssigneeButton(root).length, 1, '应显示负责人编辑按钮')
    assert.ok(drawerText(root).includes('agent'), '当前负责人应展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
  const legacy = { ...ASSIGNED_ISSUE }
  delete legacy.project_id
  const { renderer: r2, root: root2 } = await renderDrawer(legacy)
  try {
    assert.equal(findEditAssigneeButton(root2).length, 0, '无 project_id 不应显示编辑按钮')
  } finally {
    await TestRenderer.act(() => r2.unmount())
  }
})

test('点击「编辑」：加载成员，下拉预选当前负责人（按 username 匹配）', async () => {
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
  })
  try {
    assert.equal(findSelects(root).length, 0, '未进入编辑态不应有下拉')
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const selects = findSelects(root)
    assert.equal(selects.length, 1, '编辑态应渲染负责人下拉')
    const sel = selects[0]
    assert.equal(sel.props.value, 3, '当前负责人 agent(id=3) 应预选')
    // 下拉选项：「不指定」+ 全部成员（map 出的选项是嵌套数组，先展平）
    const optionEls = []
    const flatten = (xs) => xs.forEach((x) => (
      Array.isArray(x) ? flatten(x) : optionEls.push(x)))
    flatten(Array.isArray(sel.props.children) ? sel.props.children : [sel.props.children])
    const options = optionEls.filter((c) => c && c.props)
    assert.equal(options.length, MEMBER_POOL.length + 1, '应含「不指定」+ 全部成员')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无负责人 issue 编辑：下拉预选「不指定」', async () => {
  const noAssignee = { ...ASSIGNED_ISSUE, assignees: [] }
  const { renderer, root } = await renderDrawer(noAssignee, {
    memberPool: MEMBER_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const sel = findSelects(root)[0]
    assert.equal(sel.props.value, '', '无负责人应预选「不指定」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('修改选择 → 保存调用 PUT，assignee_id 参数正确', async () => {
  const putMock = mock.method(api, 'put', async (pathname, body) => {
    assert.equal(pathname, '/api/issues/42/94/assignee')
    assert.equal(body.assignee_id, 7, '应提交选中的用户 id')
    return { assignees: [{ name: '开发', username: 'dev' }] }
  })
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '7' } })
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 1, '保存应调用一次 PUT')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('清除负责人：选择「不指定」→ PUT assignee_id=null', async () => {
  const putMock = mock.method(api, 'put', async (pathname, body) => {
    assert.equal(pathname, '/api/issues/42/94/assignee')
    assert.equal(body.assignee_id, null, '清除负责人应提交 null')
    return { assignees: [] }
  })
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '' } })
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 1, '清除应调用一次 PUT')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存成功：退出编辑态、负责人即时更新、onAssigneeUpdated 触发', async () => {
  mock.method(api, 'put', async () => ({
    assignees: [{ name: '开发', username: 'dev',
                  avatar_url: 'https://gitlab.example.com/dev.png' }],
  }))
  const onAssigneeUpdated = mock.fn()
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
    onAssigneeUpdated,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '7' } })
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findSelects(root).length, 0, '成功后应退出编辑态')
    const text = drawerText(root)
    assert.ok(text.includes('开发'), '新负责人应展示')
    assert.ok(!text.includes('agent'), '旧负责人应消失')
    assert.equal(onAssigneeUpdated.mock.callCount(), 1, '应通知父组件刷新列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存失败：错误信息展示、编辑态保留可重试、回调不触发', async () => {
  mock.method(api, 'put', async () => {
    throw new Error('GitLab API 错误: 500')
  })
  const onAssigneeUpdated = mock.fn()
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
    onAssigneeUpdated,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '7' } })
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('GitLab API 错误: 500'),
              '应显示错误信息')
    assert.equal(findSelects(root).length, 1, '编辑态应保留（可重试）')
    assert.equal(onAssigneeUpdated.mock.callCount(), 0, '失败不应通知刷新')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('成员加载失败：错误 + 重试按钮；重试成功后正常进入编辑态', async () => {
  let fail = true
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
    memberPoolErrorFn: () => (fail ? new Error('网络错误: connect timeout') : null),
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('网络错误'), '应展示加载失败信息')
    const retry = root.findAll(
      (n) => n.type === 'button' && String(n.props.title || '').includes('重新加载项目成员'))
    assert.equal(retry.length, 1, '应提供重试按钮')
    fail = false
    await TestRenderer.act(async () => {
      retry[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findSelects(root).length, 1, '重试成功后应进入编辑态')
    assert.equal(findSelects(root)[0].props.value, 3, '重试成功后应预选当前负责人')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('空成员池：提示「该仓库暂无成员」，仍可保存（清除负责人）', async () => {
  const putMock = mock.method(api, 'put', async () => ({ assignees: [] }))
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: [],
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('该仓库暂无成员'), '应提示空成员池')
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 1, '空成员池仍可保存（清除负责人）')
    assert.equal(putMock.mock.calls[0].arguments[1].assignee_id, null)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('取消编辑：不调接口，负责人显示恢复原状', async () => {
  const putMock = mock.method(api, 'put', async () => {
    throw new Error('不应调用')
  })
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '7' } })
    })
    await TestRenderer.act(async () => {
      findCancelButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 0, '取消不应调用接口')
    assert.equal(findSelects(root).length, 0, '应退出编辑态')
    const text = drawerText(root)
    assert.ok(text.includes('agent'), '负责人显示应恢复原状')
    assert.ok(!text.includes('开发'), '未保存的新选择不应残留')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存请求进行中按钮 disabled，防重复提交', async () => {
  let resolvePut
  mock.method(api, 'put', () => new Promise((resolve) => { resolvePut = resolve }))
  const { renderer, root } = await renderDrawer(ASSIGNED_ISSUE, {
    memberPool: MEMBER_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditAssigneeButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSelects(root)[0].props.onChange({ target: { value: '7' } })
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const btn = findSaveButton(root)[0]
    assert.equal(btn.props.disabled, true, '请求中保存按钮应禁用')
    await TestRenderer.act(async () => {
      resolvePut({ assignees: [] })
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
