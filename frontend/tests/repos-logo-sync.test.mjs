// 仓库页「同步到 GitLab」按钮测试（issue #297）。
//
// 需求——仓库设置页面生成 logo 后，增加按钮可以直接把 logo 同步到
// GitLab 作为仓库图标：仅当仓库已生成 logo（repo.logo_path 非空）时
// 展示「同步到 GitLab」按钮，点击调 POST /api/repos/{repo_id}/sync-logo
// （后端把本地 logo 上传为 GitLab 项目头像）；请求中禁用防重复点击，
// 成功展示「已同步到 GitLab（项目路径）」，失败展示后端错误信息。
//
// 断言：
// 1. 渲染：有 logo 的仓库渲染「同步到 GitLab」按钮，未生成 logo 的
//    仓库不渲染；
// 2. 点击：POST /api/repos/{repo_id}/sync-logo 参数正确（repo_id 对应
//    被点击仓库）；请求中按钮禁用并显示「同步中…」；
// 3. 成功：显示「已同步到 GitLab（chenkaidi/botler）」；
// 4. 失败：接口异常显示后端错误信息。
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

function syncBtns(renderer) {
  return renderer.root.findAll((n) => n.type === 'button'
    && String(n.props.title || '').includes('GitLab 仓库图标'))
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

test('源码含「同步到 GitLab」按钮（仅 logo 已生成时展示）', () => {
  assert.match(reposSrc, /同步到 GitLab/, '应有「同步到 GitLab」按钮文案')
  assert.match(reposSrc, /repo\.logo_path &&/, '应仅当 logo 已生成时渲染按钮')
  assert.match(
    reposSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.id\}\/sync-logo`\)/,
    '点击后应调 POST /api/repos/{repo_id}/sync-logo',
  )
  assert.match(
    reposSrc,
    /disabled=\{syncLogoResults\[repo\.id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(reposSrc, /同步中…/, '请求中应显示「同步中…」')
  assert.match(reposSrc, /SyncLogoResult/, '应有同步结果组件')
  assert.match(reposSrc, /已同步到 GitLab/, '成功应显示「已同步到 GitLab」')
})

// ---- 渲染断言 ----

test('有 logo 的仓库渲染同步按钮，未生成的仓库不渲染', async () => {
  const { renderer, renderError } = await renderRepos()
  assert.equal(renderError, null, String(renderError || ''))
  const btns = syncBtns(renderer)
  assert.equal(btns.length, 1, '仅 logo 已生成的仓库应有同步按钮')
  // 第一个仓库（id=1，有 logo）的按钮存在
  assert.ok(btns[0], '应渲染同步按钮')
})

test('点击「同步到 GitLab」调用后端并展示项目路径', async () => {
  const { renderer } = await renderRepos()
  const postCalls = []
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/repos/1/sync-logo') {
      return { ok: true, project: 'chenkaidi/botler',
               avatar_url: 'https://gitlab.example.com/uploads/-/system/project/avatar/11/1.png' }
    }
    throw new Error('unexpected ' + pathname)
  })
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/repos/1/sync-logo'], '应调用同步接口')
  assert.match(treeText(renderer), /已同步到 GitLab/, '成功应显示「已同步到 GitLab」')
  assert.match(treeText(renderer), /chenkaidi\/botler/, '应展示同步到的项目路径')
})

test('请求中按钮禁用并显示「同步中…」', async () => {
  const { renderer } = await renderRepos()
  // post 挂起不返回：保持 loading 状态，断言请求中禁用
  mock.method(api, 'post', async () => new Promise(() => {}))
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.equal(syncBtns(renderer)[0].props.disabled, true, '请求中按钮应禁用')
  assert.match(treeText(renderer), /同步中…/, '请求中应显示「同步中…」')
})

test('同步失败展示后端错误信息', async () => {
  const { renderer } = await renderRepos()
  mock.method(api, 'post', async () => {
    throw new Error('同步到 GitLab 失败: GitLab API 错误 403: Avatar is not allowed')
  })
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /同步到 GitLab 失败/, '应展示后端错误信息')
  assert.match(treeText(renderer), /403/, '应透传 GitLab 错误详情')
})
