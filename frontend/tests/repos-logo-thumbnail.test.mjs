// 仓库页 logo 缩略图测试（issue #338）。
//
// 需求——仓库页面的图片生成预览图，避免在低网速的时候加载不出来：仓库
// 列表直接加载小尺寸缩略图（GET /api/repos/{id}/logo?thumb=1，后端
// Pillow 实时等比缩放），点击 logo 放大时弹窗加载原图（不带 thumb）；
// 列表图片加 loading="lazy" 懒加载；图片加载失败（404/网络错误/低网速
// 超时）时显示占位图标兜底，避免破图。
//
// 断言：
// 1. 源码：列表 logo img src 带 thumb=1 与缓存击穿参数；img 带
//    loading="lazy" 与 onError 失败兜底；放大弹窗 img src 加载原图
//    （不含 thumb 参数）；
// 2. 渲染：有 logo 仓库的列表 img src 精确等于
//    /api/repos/{id}/logo?thumb=1&v=<updated_at>，并带 loading="lazy"；
// 3. 失败兜底：img onError 触发后该仓库显示占位图标（title「logo 加载
//    失败」），不再渲染破图 img；
// 4. 放大弹窗：点击 logo 打开弹窗，弹窗大图 src 为原图（不含 thumb=1），
//    列表缩略图仍为 thumb=1。
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

function hasClass(node, cls) {
  return String(node.props.className || '').split(/\s+/).includes(cls)
}

// ---- 源码断言 ----

test('源码：列表加载 thumb=1 缩略图 + 懒加载 + 失败兜底，弹窗加载原图', () => {
  // 列表 img：thumb=1 缩略图 + logo_updated_at 缓存击穿参数
  assert.match(
    reposSrc,
    /\/api\/repos\/\$\{repo\.id\}\/logo\?thumb=1&v=/,
    '列表 img src 应带 thumb=1 缩略图参数与缓存击穿参数',
  )
  // 懒加载：低网速下列表图片延迟加载
  assert.match(reposSrc, /loading="lazy"/, '列表 img 应带 loading="lazy" 懒加载')
  // 失败兜底：onError 处理器（原图加载失败显示占位，避免破图）
  assert.match(reposSrc, /onError=/, '列表 img 应有 onError 加载失败兜底')
  // 放大弹窗：加载原图（不带 thumb 参数）
  const modalPart = reposSrc.split('function LogoViewModal').pop() || ''
  assert.match(
    modalPart,
    /\/api\/repos\/\$\{repo\.id\}\/logo\?v=/,
    '放大弹窗 img src 应加载原图（无 thumb）',
  )
  assert.ok(!/thumb/.test(modalPart), '放大弹窗不应带 thumb 参数')
})

// ---- 渲染断言 ----

test('列表渲染 thumb=1 缩略图并带 lazy 加载，弹窗大图为原图', async () => {
  const { renderer, renderError } = await renderRepos()
  assert.equal(renderError, null, String(renderError || ''))
  // 有 logo 的仓库：列表 img src = /api/repos/1/logo?thumb=1&v=<updated_at>
  const logos = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.alt || '').includes('logo'))
  assert.equal(logos.length, 1, '应有 1 个 logo 缩略图（第二个仓库未生成）')
  assert.equal(
    logos[0].props.src,
    `/api/repos/1/logo?thumb=1&v=${encodeURIComponent('2026-08-18 10:00:00')}`,
    '列表缩略图 src 应带 thumb=1 与缓存击穿参数',
  )
  assert.equal(logos[0].props.loading, 'lazy', '列表缩略图应带 lazy 懒加载')
  assert.equal(typeof logos[0].props.onError, 'function', '应有加载失败兜底处理')

  // 点击 logo 打开放大弹窗：弹窗大图加载原图（无 thumb=1）
  const logoBtn = renderer.root.findAll((n) =>
    n.type === 'button' && hasClass(n, 'repo-logo-btn'))[0]
  await TestRenderer.act(async () => {
    logoBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  const modalImgs = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.src || '').includes('/api/repos/1/logo?')
    && !String(n.props.src || '').includes('thumb=1'))
  assert.equal(modalImgs.length, 1, '弹窗内应有 1 张原图大图（不带 thumb）')
  assert.equal(
    modalImgs[0].props.src,
    `/api/repos/1/logo?v=${encodeURIComponent('2026-08-18 10:00:00')}`,
    '弹窗大图应加载原图',
  )
  // 列表缩略图仍带 thumb=1
  const listThumbs = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.src || '').includes('thumb=1'))
  assert.equal(listThumbs.length, 1, '列表缩略图应仍带 thumb=1')
})

test('列表缩略图加载失败显示占位兜底，不渲染破图', async () => {
  const { renderer } = await renderRepos()
  const logos = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.alt || '').includes('logo'))
  assert.equal(logos.length, 1, '初始应渲染 1 个缩略图')
  // 模拟 img 加载失败（404/网络错误/低网速超时）
  await TestRenderer.act(async () => {
    logos[0].props.onError()
  })
  const imgs = renderer.root.findAll((n) => n.type === 'img'
    && String(n.props.alt || '').includes('logo'))
  assert.equal(imgs.length, 0, '加载失败后应移除破图 img')
  const placeholders = renderer.root.findAll((n) =>
    String(n.props.className || '').includes('repo-logo-placeholder'))
  assert.equal(placeholders.length, 2, '两个仓库都应显示占位框')
  const failedTitle = placeholders.find((n) =>
    String(n.props.title || '').includes('加载失败'))
  assert.ok(failedTitle, '失败仓库占位框应提示「logo 加载失败」')
})
