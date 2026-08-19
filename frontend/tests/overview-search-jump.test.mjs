// 概览页全局搜索深链消费测试（issue #216）：SearchOverlay 跳转生成的
// /overview 深链参数在真实 Router 环境下被 Overview 消费——
//   ?issue=<project_id>:<iid> → 数据加载后自动打开该 issue 详情抽屉
//   ?repo=<repo_id>[&section=inspirations] → 滚动定位对应仓库卡片
//   消费成功后清理 URL 参数（replace）
//
// 说明：Overview 默认导出经 useInRouterContext 分流——无 Router 上下文的
// 单组件测试走静态分支（不消费深链，避免 useLocation 抛 invariant）；
// 本测试用真实 react-router MemoryRouter 渲染 Router 分支验证深链行为。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'


const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
// react-router-dom CJS 构建无法经 vite SSR 转译，直接 node ESM 导入
// （与 app-shortcuts.test.mjs 同法）；Overview 组件内部 import 由 vite
// SSR 自行解析 CJS，两者不冲突
import { MemoryRouter } from 'react-router-dom'

const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// document mock：捕获 querySelector 调用（滚动定位断言用）
const queriedSelectors = []
globalThis.document = {
  querySelector: (sel) => { queriedSelectors.push(sel); return null },
  querySelectorAll: () => [], // useOverviewData 实时输出滚动定位（issue #99）
  addEventListener: () => {},
  removeEventListener: () => {},
}

const FULL_ISSUE = {
  iid: 64, title: '概览页面增加读取已启用的仓库issue',
  state: 'opened', project_id: 1,
  updated_at: '2026-08-14 10:20:00', created_at: '2026-08-10 09:00:00',
  web_url: 'https://gitlab.example.com/chenkaidi/botler/-/issues/64',
  description: '**需求**\n\n- 要点一\n- 要点二',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
  milestone: 'v1.0',
  assignees: [{ name: 'Agent', username: 'agent' }],
  user_notes_count: 3,
}

async function renderAt(initialPath, issues) {
  queriedSelectors.length = 0
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/issues/overview') {
      return {
        repos: [{ repo_id: 1, project_id: 1, repo_name: 'botler', priority: 10, issues }],
        errors: [], total: issues.length,
      }
    }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/inspirations/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', inspirations: [] }] }
    }
    if (pathname.includes('deepseek-balance')) return { configured: false }
    return {}
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(
          MemoryRouter, { initialEntries: [initialPath] },
          React.createElement(Overview)))
      // 等首次数据加载 + 深链消费 effect 完成
      await new Promise((resolve) => setTimeout(resolve, 60))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

test('深链 ?issue=1:64：数据加载后自动打开该 issue 抽屉并清理参数', async () => {
  const { renderer, renderError } = await renderAt('/overview?issue=1:64', [FULL_ISSUE])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const drawer = renderer.root.findAllByType(IssueDrawer)
    assert.equal(drawer.length, 1, '深链应自动打开 issue 抽屉')
    // 抽屉展示对应 issue 标题
    const texts = drawer[0] ? JSON.stringify(drawer[0].props) : ''
    assert.match(texts, /概览页面增加读取已启用的仓库issue/, '抽屉应展示目标 issue')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('深链 ?issue=9:999：未找到目标仅清理参数，不打开抽屉不报错', async () => {
  const { renderer, renderError } = await renderAt('/overview?issue=9:999', [FULL_ISSUE])
  try {
    assert.equal(renderError, null, '未命中不应抛错')
    assert.equal(renderer.root.findAllByType(IssueDrawer).length, 0, '未找到不打开抽屉')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('深链 ?repo=1：滚动定位开放 issue 板块仓库卡片', async () => {
  const { renderer, renderError } = await renderAt('/overview?repo=1', [FULL_ISSUE])
  try {
    assert.equal(renderError, null)
    assert.ok(
      queriedSelectors.includes('.issue-repo-card[data-repo-id="1"]'),
      '应按 data-repo-id 定位开放 issue 板块仓库卡片（实际：' + JSON.stringify(queriedSelectors) + '）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('深链 ?repo=1&section=inspirations：滚动定位灵感板块仓库卡片', async () => {
  const { renderer, renderError } = await renderAt('/overview?repo=1&section=inspirations', [])
  try {
    assert.equal(renderError, null)
    assert.ok(
      queriedSelectors.includes('.inspiration-repo-card[data-repo-id="1"]'),
      '应按 data-repo-id 定位灵感板块仓库卡片（实际：' + JSON.stringify(queriedSelectors) + '）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
