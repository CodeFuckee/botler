// 仓库健康巡检徽章与详情弹窗测试（issue #265）。
//
// 需求——仓库列表页每个仓库展示健康徽章：正常（绿「健康」）/ 异常（红
// 「异常」，可点击打开详情弹窗查看检查项明细与历史、手动重检）/ 未知
// （灰「未知」，从未巡检）；详情弹窗提供「重新巡检」按钮（手动重检，
// POST /api/repos/{id}/health-check）。
//
// 断言：
// 1. 渲染：健康仓库显示「健康」徽章、异常仓库显示可点击「异常」徽章、
//    无巡检记录显示「未知」徽章；
// 2. 点击异常徽章：打开详情弹窗并 GET /api/repos/{id}/health，展示
//    检查项明细（webhook/token/项目可达）与错误描述；
// 3. 手动重检：点击「重新巡检」POST /api/repos/{id}/health-check，
//    成功后重新加载详情并刷新仓库列表徽章。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const reposSrc = readFileSync(path.join(ROOT, 'src/pages/Repos.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// react-router-dom mock（与其他仓库页测试一致）
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
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: Repos } = await vite.ssrLoadModule('/src/pages/Repos.jsx')

after(() => vite.close())

const REPOS = [
  { id: 1, name: 'healthy-repo', url: 'https://gitlab.example.com/group/a.git',
    gitlab_project_id: 11, enabled: true, priority: 1,
    health: { status: 'healthy', check_time: '2026-08-24 00:00:00', last_error: null } },
  { id: 2, name: 'broken-repo', url: 'https://gitlab.example.com/group/b.git',
    gitlab_project_id: 22, enabled: true, priority: 100,
    health: { status: 'abnormal', check_time: '2026-08-24 00:00:00',
              last_error: 'webhook 未注册；token token 无效或已过期' } },
  { id: 3, name: 'new-repo', url: 'https://gitlab.example.com/group/c.git',
    gitlab_project_id: 33, enabled: true, priority: 200,
    health: { status: 'unknown', check_time: null, last_error: null } },
]

const HEALTH_DETAIL = {
  repo: REPOS[1],
  latest: { id: 9, status: 'abnormal', check_time: '2026-08-24 00:00:00',
            last_error: 'webhook 未注册；token token 无效或已过期',
            webhook_ok: false, token_ok: false, project_ok: true, repaired: false },
  history: [
    { id: 9, status: 'abnormal', check_time: '2026-08-24 00:00:00',
      last_error: 'webhook 未注册；token token 无效或已过期' },
  ],
}

// 挂载 Repos：api.get 按路径分流，返回 get 调用记录供刷新断言
async function renderRepos({ repos = REPOS } = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname === '/api/repos') return { repos }
    if (pathname === '/api/repos/2/health') return HEALTH_DETAIL
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Repos))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError, getCalls }
}

function hasClass(node, cls) {
  return String(node.props.className || '').split(/\s+/).includes(cls)
}

function badges(renderer, cls) {
  return renderer.root.findAll(
    (n) => typeof n.type === 'string' && hasClass(n, cls))
}

function treeText(renderer) {
  const walk = (n) => {
    if (n == null) return ''
    if (typeof n === 'string' || typeof n === 'number') return String(n)
    if (Array.isArray(n)) return n.map(walk).join('')
    return walk(n.children)
  }
  return walk(renderer.toJSON())
}

function nodeText(node) {
  const walk = (n) => {
    if (n == null) return ''
    if (typeof n === 'string' || typeof n === 'number') return String(n)
    if (Array.isArray(n)) return n.map(walk).join('')
    return walk(n.children)
  }
  return walk(node?.props?.children)
}

// ---- 源码与样式断言 ----

test('源码含健康徽章、详情弹窗与重检接口调用', () => {
  assert.match(reposSrc, /HealthBadge/, '应有健康徽章组件')
  assert.match(reposSrc, /HealthDetailModal/, '应有健康详情弹窗组件')
  assert.match(reposSrc, /health-badge health-healthy/, '健康徽章类名')
  assert.match(reposSrc, /health-badge health-abnormal/, '异常徽章类名（可点击）')
  assert.match(reposSrc, /health-badge health-unknown/, '未知徽章类名')
  assert.match(
    reposSrc,
    /api\.get\(`\/api\/repos\/\$\{repo\.id\}\/health`\)/,
    '详情弹窗应调 GET /api/repos/{id}/health',
  )
  assert.match(
    reposSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.id\}\/health-check`\)/,
    '手动重检应调 POST /api/repos/{id}/health-check',
  )
  assert.match(reposSrc, /重新巡检/, '详情弹窗应有「重新巡检」按钮')
  assert.match(styles, /\.health-badge/, '样式应包含 .health-badge 相关规则')
  assert.match(styles, /\.health-healthy/, '样式应包含健康（绿）徽章规则')
  assert.match(styles, /\.health-abnormal/, '样式应包含异常（红）徽章规则')
  assert.match(styles, /\.health-unknown/, '样式应包含未知（灰）徽章规则')
})

// ---- 渲染断言 ----

test('仓库行按最新巡检状态渲染健康徽章（正常/异常/未知）', async () => {
  const { renderer, renderError } = await renderRepos()
  assert.equal(renderError, null, String(renderError || ''))
  assert.equal(badges(renderer, 'health-healthy').length, 1, '健康仓库显示绿色「健康」徽章')
  assert.equal(badges(renderer, 'health-abnormal').length, 1, '异常仓库显示红色「异常」徽章')
  assert.equal(badges(renderer, 'health-unknown').length, 1, '未巡检仓库显示灰色「未知」徽章')
  assert.match(treeText(renderer), /健康/, '展示「健康」文案')
  assert.match(treeText(renderer), /异常/, '展示「异常」文案')
  assert.match(treeText(renderer), /未知/, '展示「未知」文案')
})

test('点击「异常」徽章打开详情弹窗并加载检查明细', async () => {
  const { renderer, getCalls } = await renderRepos()
  const abnormal = badges(renderer, 'health-abnormal')[0]
  await TestRenderer.act(async () => {
    abnormal.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.ok(
    getCalls.includes('/api/repos/2/health'),
    '打开详情弹窗应 GET /api/repos/2/health',
  )
  const text = treeText(renderer)
  assert.match(text, /健康巡检/, '弹窗标题应含「健康巡检」')
  assert.match(text, /webhook/, '应展示 webhook 检查项')
  assert.match(text, /token/, '应展示 token 检查项')
  assert.match(text, /项目可达/, '应展示项目可达检查项')
  assert.match(text, /最近巡检时间/, '应展示最近巡检时间')
  assert.match(text, /巡检历史/, '应展示巡检历史')
})

test('详情弹窗「重新巡检」调用健康检查接口并刷新', async () => {
  const { renderer, getCalls } = await renderRepos()
  const postCalls = []
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/repos/2/health-check') {
      return { ok: true, checked: 1, abnormal: [], repaired: 0, errors: [] }
    }
    throw new Error('unexpected ' + pathname)
  })
  const before = getCalls.filter((p) => p === '/api/repos').length
  const abnormal = badges(renderer, 'health-abnormal')[0]
  await TestRenderer.act(async () => {
    abnormal.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  const recheckBtn = renderer.root.findAll(
    (n) => n.type === 'button' && hasClass(n, 'btn-primary')
      && nodeText(n).includes('重新巡检'))
  assert.equal(recheckBtn.length, 1, '详情弹窗应有「重新巡检」按钮')
  await TestRenderer.act(async () => {
    recheckBtn[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/repos/2/health-check'], '应调手动重检接口')
  const after = getCalls.filter((p) => p === '/api/repos').length
  assert.ok(after > before, '重检成功后应刷新仓库列表徽章')
})
