// 仓库页「生成图标」按钮与 logo 展示/放大/下载测试（issue #188）。
//
// 需求——仓库管理（设置）页面每个仓库的右侧增加「生成图标」按钮：点击后
// agent 根据该仓库 README 生成 logo 提示词并调用生图模型生成 logo；生成
// 的 logo 显示在仓库页面每个仓库的最左侧，点击可放大并下载。
//
// 断言：
// 1. 渲染：每个仓库行最左侧渲染 logo（有 logo 显示 <img>，src 带
//    logo_updated_at 缓存击穿参数；无 logo 显示占位框），右侧操作组渲染
//    「生成图标」按钮；
// 2. 点击：POST /api/repos/{repo_id}/generate-logo 参数正确；请求中按钮
//    禁用并显示「生成中…」，成功后刷新仓库列表并显示「已生成 logo」；
// 3. 放大：点击最左侧 logo 打开弹窗，大图展示 + 「下载 logo」链接
//    （/api/repos/{id}/logo?download=1，带 download 属性）；
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
  { id: 1, name: 'botler', url: 'https://gitlab.example.com/group/a.git',
    gitlab_project_id: 11, enabled: true, priority: 1,
    logo_path: '1.png', logo_updated_at: '2026-08-18 10:00:00', logo_mime: 'image/png' },
  { id: 2, name: '普通仓库', url: 'https://gitlab.example.com/group/b.git',
    gitlab_project_id: 22, enabled: true, priority: 100,
    logo_path: null, logo_updated_at: null, logo_mime: null },
]

// 挂载 Repos：api.get 按路径分流，返回 get 调用记录供刷新断言
async function renderRepos({ repos = REPOS } = {}) {
  const getCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
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
  return { renderer, renderError, getCalls }
}

function hasClass(node, cls) {
  return String(node.props.className || '').split(/\s+/).includes(cls)
}

function logoBtns(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'button' && hasClass(n, 'repo-logo-btn'))
}

function generateBtns(renderer) {
  return renderer.root.findAll(
    (n) => n.type === 'button' && hasClass(n, 'logo-btn'))
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

// ---- 源码与样式断言 ----

test('源码含「生成图标」按钮、logo 展示与放大下载弹窗', () => {
  assert.match(reposSrc, /生成图标/, '应有「生成图标」按钮文案')
  assert.match(reposSrc, /logo-btn/, '应有生成图标按钮类名')
  assert.match(
    reposSrc,
    /api\.post\(`\/api\/repos\/\$\{repo\.id\}\/generate-logo`\)/,
    '点击后应调 POST /api/repos/{repo_id}/generate-logo',
  )
  assert.match(
    reposSrc,
    /disabled=\{logoResults\[repo\.id\]\?\.loading\}/,
    '请求中应禁用按钮防重复点击',
  )
  assert.match(reposSrc, /生成中…/, '请求中应显示「生成中…」')
  // logo 展示：最左侧缩略图（repo.logo_path 条件）+ 未生成占位
  assert.match(
    reposSrc,
    /repo\.logo_path && !logoFailed\[repo\.id\] \?/,
    '应条件渲染 logo（有 logo_path 且未加载失败才显示）',
  )
  assert.match(
    reposSrc,
    /\/api\/repos\/\$\{repo\.id\}\/logo\?thumb=1&v=/,
    'logo img src 应带 thumb=1 缩略图参数与 logo_updated_at 缓存击穿参数',
  )
  assert.match(reposSrc, /repo-logo-placeholder/, '未生成 logo 应显示占位框')
  // 放大下载弹窗
  assert.match(reposSrc, /LogoViewModal/, '应有 logo 放大弹窗组件')
  assert.match(
    reposSrc,
    /\/api\/repos\/\$\{repo\.id\}\/logo\?download=1/,
    '下载链接应带 download=1',
  )
  assert.match(styles, /\.repo-logo/, '样式应包含 .repo-logo 相关规则')
  assert.match(styles, /\.repo-logo-modal/, '样式应包含放大弹窗规则')
})

// ---- 渲染断言 ----

test('仓库行最左侧渲染 logo 缩略图（带缓存参数）与未生成占位', async () => {
  const { renderer, renderError } = await renderRepos()
  assert.equal(renderError, null, String(renderError || ''))
  // 有 logo 的仓库：img src = /api/repos/1/logo?v=<encodeURIComponent(updated_at)>
  const logos = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.alt || '').includes('logo'))
  assert.equal(logos.length, 1, '应有 1 个 logo 缩略图（第二个仓库未生成）')
  assert.equal(
    logos[0].props.src,
    `/api/repos/1/logo?thumb=1&v=${encodeURIComponent('2026-08-18 10:00:00')}`,
  )
  // 未生成仓库：占位框
  const placeholders = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('repo-logo-placeholder'))
  assert.equal(placeholders.length, 1, '未生成 logo 的仓库应显示占位框')
  // 每个仓库右侧都有「生成图标」按钮
  assert.equal(generateBtns(renderer).length, 2)
})

test('点击「生成图标」请求中按钮禁用并显示「生成中…」', async () => {
  const { renderer } = await renderRepos()
  // post 挂起不返回：保持 loading 状态，断言请求中禁用
  mock.method(api, 'post', async () => new Promise(() => {}))
  const btn = generateBtns(renderer)[1] // 第二个仓库（未生成）
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.equal(generateBtns(renderer)[1].props.disabled, true, '请求中按钮应禁用')
  assert.match(treeText(renderer), /生成中…/, '请求中应显示「生成中…」')
})

test('点击「生成图标」调用后端并在成功后刷新列表、显示已生成', async () => {
  const { renderer, getCalls } = await renderRepos()
  const before = getCalls.filter((p) => p === '/api/repos').length
  const postCalls = []
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/repos/2/generate-logo') {
      return { ok: true, logo_path: '2.png', logo_mime: 'image/png',
               logo_updated_at: '2026-08-18 11:00:00', size: 10,
               logo_prompt: 'a minimal logo' }
    }
    throw new Error('unexpected ' + pathname)
  })
  const btn = generateBtns(renderer)[1] // 第二个仓库（未生成）
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/repos/2/generate-logo'], '应调用生成接口')
  assert.match(treeText(renderer), /已生成 logo/, '成功后应显示「已生成 logo」')
  // 成功后刷新仓库列表（再次 GET /api/repos）
  const after = getCalls.filter((p) => p === '/api/repos').length
  assert.ok(after > before, '生成成功后应刷新仓库列表')
})

test('生成失败展示后端错误信息', async () => {
  const { renderer } = await renderRepos()
  mock.method(api, 'post', async () => {
    throw new Error('生图模型调用失败: 模拟故障')
  })
  const btn = generateBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /生图模型调用失败/, '应展示后端错误信息')
})

test('点击 logo 打开放大弹窗并提供下载链接', async () => {
  const { renderer } = await renderRepos()
  const logo = logoBtns(renderer)[0]
  await TestRenderer.act(async () => {
    logo.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  const overlay = renderer.root.findAll((n) =>
    typeof n.props?.className === 'string'
    && n.props.className.includes('modal-overlay'))
  assert.equal(overlay.length, 1, '应打开放大弹窗')
  // 弹窗内大图
  const large = renderer.root.findAll((n) =>
    n.type === 'img' && String(n.props.src || '').includes('/api/repos/1/logo?'))
  assert.equal(large.length, 2, '弹窗内应有放大版 logo（列表缩略图 + 弹窗大图）')
  // 下载链接
  const dl = renderer.root.findAll((n) => n.type === 'a'
    && String(n.props.href || '').includes('/api/repos/1/logo?download=1'))
  assert.equal(dl.length, 1, '弹窗应提供下载链接')
  assert.equal(dl[0].props.download, true, '下载链接应带 download 属性')
  // 关闭按钮
  const close = renderer.root.findAll((n) => n.type === 'button'
    && String(n.props['aria-label'] || '').includes('关闭弹窗'))
  assert.equal(close.length, 1)
  await TestRenderer.act(async () => {
    close[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  assert.equal(
    renderer.root.findAll((n) =>
      typeof n.props?.className === 'string'
      && n.props.className.includes('modal-overlay')).length,
    0, '点击关闭后弹窗应消失',
  )
})
