// 「任务页面翻页组件」测试（issue #50）：任务列表页增加翻页组件，
// 利用后端已有 limit/offset 分页能力逐页浏览任务，替代「最多显示 50 条」。
//
// 断言：
// 1. Tasks.jsx 源码带 offset 分页参数与翻页控件（上一页/下一页/页码）；
// 2. 多页数据渲染翻页组件（第 X / Y 页、上一页/下一页禁用态）；
// 3. 点击「下一页」/页码数字 → 请求带对应 offset、渲染对应页数据；
// 4. 最后一页「下一页」禁用；
// 5. 筛选（状态/仓库/搜索）变化自动重置回第 1 页；
// 6. 单页（total <= 50）与空列表不渲染翻页组件；
// 7. pageNumbers 页码窗口函数边界（少页全显示、多页首尾+当前±1+省略号）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tasksSrc = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 stop-all-button.test.mjs 一致）。
// api 也经 vite 加载，与 Tasks 组件内 import 的是同一模块实例，可对 api 做 method mock。
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
const { default: Tasks, pageNumbers } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

const PAGE_SIZE = 50

// ---- 源码断言 ----

test('任务页源码含 offset 分页参数与翻页控件文案', () => {
  assert.match(tasksSrc, /offset/, '请求应携带 offset 分页参数')
  assert.match(tasksSrc, /上一页/, '应有「上一页」按钮')
  assert.match(tasksSrc, /下一页/, '应有「下一页」按钮')
  assert.match(tasksSrc, /pageNumbers/, '应有页码数字计算（pageNumbers）')
})

// ---- pageNumbers 纯函数边界 ----

test('pageNumbers：页数不超过 7 页时全量显示', () => {
  assert.deepEqual(pageNumbers(1, 1), [1])
  assert.deepEqual(pageNumbers(3, 1), [1, 2, 3])
  assert.deepEqual(pageNumbers(7, 4), [1, 2, 3, 4, 5, 6, 7])
})

test('pageNumbers：多页时显示首尾页 + 当前页 ±1 + 省略号', () => {
  // 当前页在中间：1 … 4 5 6 … 10
  assert.deepEqual(pageNumbers(10, 5), [1, '…', 4, 5, 6, '…', 10])
  // 当前页靠近首页：1 2 3 … 10（不重复首页）
  assert.deepEqual(pageNumbers(10, 2), [1, 2, 3, '…', 10])
  // 当前页靠近尾页：1 … 8 9 10
  assert.deepEqual(pageNumbers(10, 9), [1, '…', 8, 9, 10])
})

test('pageNumbers：极端参数不越界不报错', () => {
  assert.deepEqual(pageNumbers(0, 1), [])
  assert.deepEqual(pageNumbers(1, 99), [1]) // 当前页超界仍返回首页（按钮禁用兜底）
  assert.deepEqual(pageNumbers(2, 2), [1, 2])
})

// ---- 组件渲染 ----

function mkTask(overrides = {}) {
  return {
    id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
    issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
    status: 'succeeded', attempt_count: 1, triggered_by: 'webhook',
    exit_code: 0, error_message: null, error_detail: null,
    resumed: false, commit_sha: null, commit_url: null, log_path: null,
    started_at: '2026-08-13 10:00:00', finished_at: '2026-08-13 10:30:00',
    created_at: '2026-08-13 09:50:00',
    ...overrides,
  }
}

// total 指定总条数时，mock 按 offset 返回对应页数据（每页 1 条便于断言页数据）。
// 返回 { renderer, calls }：calls 记录全部 /api/tasks 请求 pathname。
async function renderAndSettle(total, tasksByPage, extra = {}) {
  const calls = []
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      calls.push(pathname)
      const params = new URLSearchParams(pathname.split('?')[1])
      const offset = Number(params.get('offset') || 0)
      const page = Math.floor(offset / PAGE_SIZE) + 1
      return {
        tasks: tasksByPage[page] || [],
        total,
        stats: { queued: 0, running: 0, retrying: 0, succeeded: total },
      }
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
  return { renderer, renderError, calls }
}

function findButtons(renderer, text) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => JSON.stringify(b.props.children).includes(text))
}

// 点击按钮后等待 useEffect 重新拉取
async function click(renderer, button) {
  await TestRenderer.act(async () => {
    button.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
}

function lastOffset(calls) {
  const last = calls[calls.length - 1]
  return Number(new URLSearchParams(last.split('?')[1]).get('offset') || 0)
}

test('多页数据渲染翻页组件：第 1 / 3 页、上一页禁用、下一页可用', async () => {
  const { renderer, renderError } = await renderAndSettle(120, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
    3: [mkTask({ id: 3, issue_iid: 103 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const prev = findButtons(renderer, '上一页')
    const next = findButtons(renderer, '下一页')
    assert.equal(prev.length, 1, '应渲染「上一页」按钮')
    assert.equal(next.length, 1, '应渲染「下一页」按钮')
    assert.equal(prev[0].props.disabled, true, '第 1 页「上一页」应禁用')
    assert.equal(next[0].props.disabled, false, '第 1 页「下一页」应可用')
    const tree = JSON.stringify(renderer.toJSON())
    assert.ok(tree.includes('第 1 / 3 页'), '应显示当前页/总页数（第 1 / 3 页）')
    assert.ok(tree.includes('101'), '应渲染第 1 页数据')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击「下一页」请求 offset=50 并渲染第 2 页数据', async () => {
  const { renderer, renderError, calls } = await renderAndSettle(120, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
    3: [mkTask({ id: 3, issue_iid: 103 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(lastOffset(calls), 0, '首次加载应为第 1 页（offset=0）')
    await click(renderer, findButtons(renderer, '下一页')[0])
    assert.equal(lastOffset(calls), 50, '点击下一页后应请求 offset=50')
    const tree = JSON.stringify(renderer.toJSON())
    assert.ok(tree.includes('102'), '应渲染第 2 页数据')
    assert.ok(tree.includes('第 2 / 3 页'), '应显示第 2 / 3 页')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('点击页码数字跳转到对应页', async () => {
  const { renderer, renderError, calls } = await renderAndSettle(120, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
    3: [mkTask({ id: 3, issue_iid: 103 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    // 页码按钮：文案恰为数字（不含「上一页/下一页」）
    const page3 = renderer.root.findAllByType('button').filter((b) => {
      const s = JSON.stringify(b.props.children)
      return s === '"3"'
    })
    assert.equal(page3.length, 1, '应渲染页码数字 3 按钮')
    await click(renderer, page3[0])
    assert.equal(lastOffset(calls), 100, '点击页码 3 后应请求 offset=100')
    assert.ok(JSON.stringify(renderer.toJSON()).includes('103'), '应渲染第 3 页数据')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('最后一页「下一页」禁用', async () => {
  const { renderer, renderError, calls } = await renderAndSettle(120, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
    3: [mkTask({ id: 3, issue_iid: 103 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    // 直接跳到第 3 页
    await click(renderer, findButtons(renderer, '下一页')[0])
    await click(renderer, findButtons(renderer, '下一页')[0])
    assert.equal(lastOffset(calls), 100)
    const next = findButtons(renderer, '下一页')[0]
    assert.equal(next.props.disabled, true, '最后一页「下一页」应禁用')
    const prev = findButtons(renderer, '上一页')[0]
    assert.equal(prev.props.disabled, false, '最后一页「上一页」应可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('筛选（状态）变化自动重置回第 1 页', async () => {
  const { renderer, renderError, calls } = await renderAndSettle(120, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
    3: [mkTask({ id: 3, issue_iid: 103 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    await click(renderer, findButtons(renderer, '下一页')[0])
    assert.equal(lastOffset(calls), 50)
    // 改变状态筛选 → 应重置回第 1 页（offset=0）
    const select = renderer.root.findAllByType('select')[0]
    await TestRenderer.act(async () => {
      select.props.onChange({ target: { value: 'succeeded' } })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.equal(lastOffset(calls), 0, '筛选变化后应回到第 1 页（offset=0）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('单页（total ≤ 50）不渲染翻页组件', async () => {
  const { renderer, renderError } = await renderAndSettle(30, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(findButtons(renderer, '上一页').length, 0, '单页不应渲染「上一页」')
    assert.equal(findButtons(renderer, '下一页').length, 0, '单页不应渲染「下一页」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('空列表（total=0）显示「暂无任务」且无翻页组件', async () => {
  const { renderer, renderError } = await renderAndSettle(0, {})
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const tree = JSON.stringify(renderer.toJSON())
    assert.ok(tree.includes('暂无任务'), '空列表应显示「暂无任务」占位')
    assert.equal(findButtons(renderer, '上一页').length, 0)
    assert.equal(findButtons(renderer, '下一页').length, 0)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('总数恰好整除页数时总页数正确（100 条 = 2 页）', async () => {
  const { renderer, renderError } = await renderAndSettle(100, {
    1: [mkTask({ id: 1, issue_iid: 101 })],
    2: [mkTask({ id: 2, issue_iid: 102 })],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.ok(
      JSON.stringify(renderer.toJSON()).includes('第 1 / 2 页'),
      '100 条（每页 50）应显示 2 页',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
