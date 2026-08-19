// 仓库页「从 GitLab 同步」按钮测试（issue #320）。
//
// 需求——仓库设置页面此前只能把本地生成的 logo 同步到 GitLab（issue
// #297），缺少反向同步：GitLab 侧更新项目图标（头像）后，设置页面无法
// 拉取展示。本次增加「从 GitLab 同步」按钮：点击调
// POST /api/repos/{repo_id}/sync-logo-from-gitlab（后端把 GitLab 项目
// 当前头像拉回落盘为本地仓库 logo 并写 repos 表），成功后刷新列表展示
// GitLab 图标。与「同步到 GitLab」不同，该按钮不依赖本地已生成 logo
// （未生成 logo 的仓库也能把 GitLab 图标拉到设置页）。
//
// 断言：
// 1. 渲染：无论仓库是否已生成本地 logo 都渲染「从 GitLab 同步」按钮；
// 2. 点击：POST /api/repos/{repo_id}/sync-logo-from-gitlab 参数正确
//    （repo_id 对应被点击仓库），成功后重新拉取仓库列表（load）；
// 3. 请求中：按钮禁用并显示「拉取中…」；
// 4. 成功：显示「已从 GitLab 同步图标」；
// 5. 失败：接口异常显示后端错误信息。
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
  { id: 1, name: 'botler', url: 'https://gitlab.example.com/group/a.git',
    gitlab_project_id: 11, enabled: true, priority: 1,
    logo_path: '1.png', logo_updated_at: '2026-08-18 10:00:00', logo_mime: 'image/png' },
  { id: 2, name: '普通仓库', url: 'https://gitlab.example.com/group/b.git',
    gitlab_project_id: 22, enabled: true, priority: 100,
    logo_path: null, logo_updated_at: null, logo_mime: null },
]

// 挂载 Repos：api.get 按路径分流
async function renderRepos({ repos = REPOS } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos }
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
  return { renderer, renderError }
}

function pullBtns(renderer) {
  return renderer.root.findAll((n) => n.type === 'button'
    && String(n.props.title || '').includes('同步到本页面'))
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

// ---- 源码断言 ----

test('源码含「从 GitLab 同步」按钮（不依赖本地 logo，双向同步闭环）', () => {
  assert.match(reposSrc, /从 GitLab 同步/, '应有「从 GitLab 同步」按钮文案')
  assert.match(
    reposSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.id\}\/sync-logo-from-gitlab`\)/,
    '点击后应调 POST /api/repos/{repo_id}/sync-logo-from-gitlab',
  )
  assert.match(reposSrc, /拉取中…/, '请求中应显示「拉取中…」')
  assert.match(
    reposSrc,
    /disabled=\{syncFromGitlabResults\[repo\.id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(reposSrc, /SyncFromGitlabResult/, '应有同步结果组件')
  assert.match(reposSrc, /已从 GitLab 同步图标/, '成功应显示「已从 GitLab 同步图标」')
})

// ---- 渲染断言 ----

test('有/无本地 logo 的仓库都渲染「从 GitLab 同步」按钮', async () => {
  const { renderer, renderError } = await renderRepos()
  assert.equal(renderError, null, String(renderError || ''))
  const btns = pullBtns(renderer)
  assert.equal(btns.length, 2, '两个仓库都应有「从 GitLab 同步」按钮')
})

test('点击「从 GitLab 同步」调用后端并刷新列表', async () => {
  const { renderer } = await renderRepos()
  const postCalls = []
  let listFetches = 0
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') {
      listFetches += 1
      return { repos: REPOS }
    }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/repos/2/sync-logo-from-gitlab') {
      return { ok: true, logo_path: '2.png', logo_mime: 'image/png',
               logo_updated_at: '2026-08-19 10:00:00', size: 10,
               avatar_url: 'https://gitlab.example.com/uploads/avatar/22/1.png' }
    }
    throw new Error('unexpected ' + pathname)
  })
  // 点击无本地 logo 的仓库（id=2）：拉取后应展示 GitLab 图标
  const btn = pullBtns(renderer)[1]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/repos/2/sync-logo-from-gitlab'], '应调用拉取接口')
  assert.ok(listFetches >= 1, '成功后应重新拉取仓库列表（点击后至少再拉取 1 次）')
  assert.match(treeText(renderer), /已从 GitLab 同步图标/, '成功应显示「已从 GitLab 同步图标」')
})

test('请求中按钮禁用并显示「拉取中…」', async () => {
  const { renderer } = await renderRepos()
  // post 挂起不返回：保持 loading 状态，断言请求中禁用
  mock.method(api, 'post', async () => new Promise(() => {}))
  const btn = pullBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.equal(pullBtns(renderer)[0].props.disabled, true, '请求中按钮应禁用')
  assert.match(treeText(renderer), /拉取中…/, '请求中应显示「拉取中…」')
})

test('拉取失败展示后端错误信息', async () => {
  const { renderer } = await renderRepos()
  mock.method(api, 'post', async () => {
    throw new Error('从 GitLab 同步图标失败: GitLab API 错误 403: Forbidden')
  })
  const btn = pullBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /从 GitLab 同步图标失败/, '应展示后端错误信息')
  assert.match(treeText(renderer), /403/, '应透传 GitLab 错误详情')
})
