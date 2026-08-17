// 仓库设置弹窗「仓库用户」测试（issue #153）：仓库设置页面增加通过读取
// remote url 来获取仓库用户，灵感组件「添加 Issue」时将该用户设为默认
// 分配人（后端在提交时按用户名解析为 GitLab 用户 id）。
//
// 断言：
// 1. 弹窗展示「仓库用户」字段：显示 repo.remote_username（remote url 用户名）；
//    未配置时显示「未读取到（remote URL 无用户名）」占位；
// 2. 「重新读取 remote URL」按钮调 POST /api/repos/{id}/remote-user，
//    成功后更新展示的用户名；失败展示错误；
// 3. 提示文案说明：用户名来源 remote URL、作为灵感「添加 Issue」的默认分配人；
// 4. 读取中按钮禁用（防重复点击）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: RepoEditModal } = await vite.ssrLoadModule('/src/components/RepoEditModal.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const modalSrc = readFileSync(path.join(ROOT, 'src/components/RepoEditModal.jsx'), 'utf8')
after(() => vite.close())

const REPO = {
  id: 5,
  name: 'botler',
  enabled: true,
  priority: 100,
  url: 'https://home.chenkaidi.top:509/chenkaidi/botler.git',
  remote_username: 'agent',
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

async function renderModal(repo = REPO) {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(RepoEditModal, {
      repo,
      onClose: () => {},
      onSaved: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  return renderer
}

function modalText(renderer) {
  return textOf(renderer.toJSON())
}

function readButton(renderer) {
  return renderer.root.findAllByType('button')
    .find((b) => textOf(b).includes('重新读取 remote URL'))
}

// ---- 源码断言 ----

test('源码：弹窗含「仓库用户」字段、重新读取按钮与分配人说明', () => {
  assert.match(modalSrc, /仓库用户/, '应展示「仓库用户」字段')
  assert.match(modalSrc, /重新读取 remote URL/, '应有「重新读取 remote URL」按钮')
  assert.match(modalSrc, /api\.post\(`\/api\/repos\/\$\{repo\.id\}\/remote-user`\)/,
    '读取按钮应调 POST /api/repos/{id}/remote-user')
  assert.match(modalSrc, /灵感组件「添加 Issue」时将该用户设为默认分配人/,
    '说明应写明该用户作为灵感「添加 Issue」的默认分配人')
  assert.match(modalSrc, /remote_url|remote url/, '说明应写明用户名来源为 remote URL')
})

test('源码：读取中禁用按钮、失败展示错误', () => {
  assert.match(modalSrc, /disabled=\{readingRemote\}/, '读取中按钮应禁用')
  assert.match(modalSrc, /setError\(e\.message\)/, '读取失败应展示错误')
})

// ---- 渲染断言 ----

test('已配置仓库用户：展示 remote_username', async () => {
  const renderer = await renderModal()
  const text = modalText(renderer)
  assert.ok(text.includes('仓库用户'), '应有「仓库用户」字段')
  assert.ok(text.includes('agent'), '应展示仓库用户名 agent')
  assert.ok(!text.includes('未读取到'), '已配置时不应显示占位')
})

test('未配置仓库用户：显示占位提示', async () => {
  const renderer = await renderModal({ ...REPO, remote_username: null })
  const text = modalText(renderer)
  assert.ok(text.includes('未读取到（remote URL 无用户名）'), '应显示未读取到占位')
})

test('点击重新读取：调接口并更新用户名', async () => {
  const calls = []
  mock.method(api, 'post', async (url) => {
    calls.push(url)
    return { remote_username: 'carol' }
  })
  try {
    const renderer = await renderModal({ ...REPO, remote_username: 'agent' })
    await TestRenderer.act(async () => {
      readButton(renderer).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    assert.deepEqual(calls, ['/api/repos/5/remote-user'], '应请求读取仓库用户接口')
    assert.ok(modalText(renderer).includes('carol'), '应更新展示为重新读取的用户名')
    assert.ok(!modalText(renderer).includes('agent'), '旧用户名应被替换')
  } finally {
    mock.restoreAll()
  }
})

test('读取失败：展示错误信息', async () => {
  mock.method(api, 'post', async () => { throw new Error('读取失败') })
  try {
    const renderer = await renderModal()
    await TestRenderer.act(async () => {
      readButton(renderer).props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    assert.ok(modalText(renderer).includes('读取失败'), '应展示错误信息')
  } finally {
    mock.restoreAll()
  }
})
