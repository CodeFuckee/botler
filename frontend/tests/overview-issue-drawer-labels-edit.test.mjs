// 概览页 issue 右边栏「标记编辑」测试（issue #108）：
// 标签行展示当前标记胶囊 + 「编辑标记」按钮；点击进入编辑态并加载
// 项目标记池（GET /api/issues/{project_id}/labels，checkbox 多选、
// 当前标记预勾选）；保存时 diff 出 add/remove 一次 PUT 提交
// （PUT /api/issues/{project_id}/{iid}/labels）；成功后退出编辑态、
// 本地标记即时更新并通知父组件刷新列表；失败保留编辑态可重试；
// 取消不调接口。
//
// 断言：
// 1. 带 project_id 的 issue 显示「编辑标记」按钮；缺 project_id
//    （旧缓存数据）不显示；
// 2. 点击编辑 → 加载标记池，checkbox 勾选态 = 当前标记；
// 3. 勾选新标记 + 取消已勾选 → 保存调用 PUT，add/remove 参数正确；
// 4. 保存成功：退出编辑态、新标记出现/旧标记消失、onLabelsUpdated 触发；
// 5. 无变更保存：不调接口直接退出编辑态；
// 6. 保存失败：错误信息展示、编辑态保留可重试、回调不触发；
// 7. 标记池加载失败：错误 + 重试按钮；重试成功后正常进入编辑态；
// 8. 空标记池：提示「该仓库暂无标记」，仍可保存（仅移除）；
// 9. 取消编辑：不调接口，标记显示恢复原状；
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
// overview-issue-close-button.test.mjs 一致）
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

test('IssueDrawer 源码包含标记编辑的数据流', () => {
  assert.match(drawerSrc, /编辑标记/, '应渲染「编辑标记」按钮文案')
  assert.match(drawerSrc, /label-picker/, '编辑态应复用 label-picker 多选样式')
  assert.match(drawerSrc, /\/labels/, '应调用标记接口路径')
  assert.match(drawerSrc, /api\.put/, '保存应调用 api.put')
  assert.match(drawerSrc, /onLabelsUpdated/, '成功后应通知父组件刷新列表')
})

// ---- 组件渲染 ----

// 渲染 IssueDrawer：props 最小集合（SSR 环境 Esc 监听自动跳过）。
// api.get 按 pathname 路由 mock：detail 返回空 notes（本文件只关注
// 标记编辑），labels 返回 opts.labelPool 配置的标记池（默认空池）；
// opts.labelPoolErrorFn 返回非空错误时模拟加载失败（闭包可变，供
// 「失败→重试成功」用例切换）
async function renderDrawer(issue, opts = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (String(pathname).endsWith('/detail')) return { notes: [] }
    if (String(pathname).endsWith('/labels')) {
      if (opts.labelPoolErrorFn) {
        const err = opts.labelPoolErrorFn()
        if (err) throw err
      }
      return { labels: opts.labelPool || [] }
    }
    throw new Error(`unexpected GET ${pathname}`)
  })
  dialog.installAutoAnswer(() => true)
  const onLabelsUpdated = opts.onLabelsUpdated || (() => {})
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue,
        repoName: 'botler',
        onClose: () => {},
        onLabelsUpdated,
      }))
      await new Promise((resolve) => setTimeout(resolve, 10))
    } catch (e) {
      renderError = e
    }
  })
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  return { renderer, root: renderer.root, onLabelsUpdated }
}

// 带标签的开放 issue（标签颜色为后端归一化后的无 # 6 位 hex）
const LABELED_ISSUE = {
  project_id: 42,
  iid: 94,
  title: '添加标记编辑',
  state: 'opened',
  updated_at: '2026-08-15 17:03:00',
  created_at: '2026-08-15 17:03:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/94',
  description: '需求描述',
  labels: [
    { name: 'feature', color: '6699cc', text_color: 'FFFFFF' },
    { name: 'bug', color: 'ff0000', text_color: 'FFFFFF' },
  ],
}

// 项目标记池（含 issue 当前标记 + 未选中的候选标记）
const LABEL_POOL = [
  { name: 'feature', color: '6699cc', text_color: 'FFFFFF' },
  { name: 'bug', color: 'ff0000', text_color: 'FFFFFF' },
  { name: 'test', color: '00ff00', text_color: 'FFFFFF' },
]

// 查找「编辑标记」按钮（标签行内的 btn-small 按钮）
function findEditLabelsButton(root) {
  return root.findAll(
    (n) => n.type === 'button'
      && String(n.props.children).includes('编辑标记'))
}

// 渲染树 → 纯文本（与 overview-issue-close-button.test.mjs 的 toText 一致）
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

// 编辑态 checkbox 列表：type=checkbox 的输入（编辑态未开启时应为 0 个）
function findCheckboxes(root) {
  return root.findAll((n) => n.type === 'input'
    && n.props.type === 'checkbox')
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

test('带 project_id 显示「编辑标记」按钮；缺 project_id 不显示', async () => {
  const { renderer, root } = await renderDrawer(LABELED_ISSUE)
  try {
    assert.equal(findEditLabelsButton(root).length, 1, '应显示编辑按钮')
    assert.ok(drawerText(root).includes('feature'), '当前标记应展示')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
  const legacy = { ...LABELED_ISSUE }
  delete legacy.project_id
  const { renderer: r2, root: root2 } = await renderDrawer(legacy)
  try {
    assert.equal(findEditLabelsButton(root2).length, 0, '无 project_id 不应显示编辑按钮')
  } finally {
    await TestRenderer.act(() => r2.unmount())
  }
})

test('点击「编辑标记」：加载标记池，checkbox 勾选态 = 当前标记', async () => {
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
  })
  try {
    assert.equal(findCheckboxes(root).length, 0, '未进入编辑态不应有 checkbox')
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    assert.equal(boxes.length, 3, '标记池 3 个标记都应渲染 checkbox')
    // label-choice 结构：<label><input/><span class=label-pill>name</span></label>
    const checked = boxes
      .filter((b) => b.props.checked)
      .map((b) => {
        const children = b.parent.props.children
        const pill = children.find((c) => c && c.props && c.props.className === 'label-pill')
        return pill ? pill.props.children : null
      })
    assert.deepEqual(checked.sort(), ['bug', 'feature'],
                     '当前标记应预勾选，候选标记不勾选')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存：勾选新标记 + 取消已勾选 → PUT add/remove 参数正确', async () => {
  const putMock = mock.method(api, 'put', async (pathname, body) => {
    assert.equal(pathname, '/api/issues/42/94/labels')
    assert.deepEqual(body.add.sort(), ['test'], 'add 应为新勾选的标记')
    assert.deepEqual(body.remove.sort(), ['bug'], 'remove 应为取消勾选的标记')
    return { labels: LABEL_POOL.filter((l) => ['feature', 'test'].includes(l.name)) }
  })
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    const byName = {}
    for (const b of boxes) {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      byName[pill.props.children] = b
    }
    await TestRenderer.act(async () => {
      byName['bug'].props.onChange()   // 取消已勾选
      byName['test'].props.onChange()  // 勾选新标记
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

test('保存成功：退出编辑态、标记即时更新、onLabelsUpdated 触发', async () => {
  mock.method(api, 'put', async () => ({
    labels: [LABEL_POOL[0]], // feature
  }))
  const onLabelsUpdated = mock.fn()
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
    onLabelsUpdated,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    const bugBox = boxes.find((b) => {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      return pill && pill.props.children === 'bug'
    })
    await TestRenderer.act(async () => {
      bugBox.props.onChange()
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findCheckboxes(root).length, 0, '成功后应退出编辑态')
    const text = drawerText(root)
    assert.ok(text.includes('feature'), '保留的标记应展示')
    assert.ok(!text.includes('bug'), '移除的标记应消失')
    assert.equal(onLabelsUpdated.mock.callCount(), 1, '应通知父组件刷新列表')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无变更保存：不调接口，直接退出编辑态', async () => {
  const putMock = mock.method(api, 'put', async () => {
    throw new Error('不应调用')
  })
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 0, '无变更不应调用接口')
    assert.equal(findCheckboxes(root).length, 0, '应退出编辑态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存失败：错误信息展示、编辑态保留可重试、回调不触发', async () => {
  mock.method(api, 'put', async () => {
    throw new Error('GitLab API 错误: 500')
  })
  const onLabelsUpdated = mock.fn()
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
    onLabelsUpdated,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    const testBox = boxes.find((b) => {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      return pill && pill.props.children === 'test'
    })
    await TestRenderer.act(async () => {
      testBox.props.onChange()
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('GitLab API 错误: 500'),
              '应显示错误信息')
    assert.equal(findCheckboxes(root).length, 3, '编辑态应保留（可重试）')
    assert.equal(onLabelsUpdated.mock.callCount(), 0, '失败不应通知刷新')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('标记池加载失败：错误 + 重试按钮；重试成功后正常进入编辑态', async () => {
  let fail = true
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
    labelPoolErrorFn: () => (fail ? new Error('网络错误: connect timeout') : null),
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('网络错误'), '应展示加载失败信息')
    const retry = root.findAll(
      (n) => n.type === 'button' && String(n.props.title || '').includes('重新加载'))
    assert.equal(retry.length, 1, '应提供重试按钮')
    fail = false
    await TestRenderer.act(async () => {
      retry[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(findCheckboxes(root).length, 3, '重试成功后应进入编辑态')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('空标记池：提示「该仓库暂无标记」，仍可保存（仅移除）', async () => {
  const putMock = mock.method(api, 'put', async () => ({ labels: [] }))
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: [],
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(drawerText(root).includes('该仓库暂无标记'), '应提示空标记池')
    // 移除当前唯一可操作：取消勾选 feature → remove=['feature']
    const boxes = findCheckboxes(root)
    const featureBox = boxes.find((b) => {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      return pill && pill.props.children === 'feature'
    })
    assert.ok(featureBox, '当前标记应出现在池外展示区（可取消勾选移除）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('取消编辑：不调接口，标记显示恢复原状', async () => {
  const putMock = mock.method(api, 'put', async () => {
    throw new Error('不应调用')
  })
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    const testBox = boxes.find((b) => {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      return pill && pill.props.children === 'test'
    })
    await TestRenderer.act(async () => {
      testBox.props.onChange()
    })
    await TestRenderer.act(async () => {
      findCancelButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(putMock.mock.callCount(), 0, '取消不应调用接口')
    assert.equal(findCheckboxes(root).length, 0, '应退出编辑态')
    const text = drawerText(root)
    assert.ok(text.includes('feature') && text.includes('bug'),
              '标记显示应恢复原状')
    assert.ok(!text.includes('test'), '未保存的新勾选不应残留')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存请求进行中按钮 disabled，防重复提交', async () => {
  let resolvePut
  mock.method(api, 'put', () => new Promise((resolve) => { resolvePut = resolve }))
  const { renderer, root } = await renderDrawer(LABELED_ISSUE, {
    labelPool: LABEL_POOL,
  })
  try {
    await TestRenderer.act(async () => {
      findEditLabelsButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const boxes = findCheckboxes(root)
    const testBox = boxes.find((b) => {
      const pill = b.parent.props.children.find(
        (c) => c && c.props && c.props.className === 'label-pill')
      return pill && pill.props.children === 'test'
    })
    await TestRenderer.act(async () => {
      testBox.props.onChange()
    })
    await TestRenderer.act(async () => {
      findSaveButton(root)[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const btn = findSaveButton(root)[0]
    assert.equal(btn.props.disabled, true, '请求中保存按钮应禁用')
    await TestRenderer.act(async () => {
      resolvePut({ labels: [] })
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
