// 设置页「远程服务器」卡片测试（remotes 段，远程项目 SSH 主机清单）。
//
// 断言分两层：
// 1. 源码级：RemotesCard 组件存在（名称/主机/端口/用户/私钥/附加选项
//    表单字段、卡片内独立保存 PUT remotes 整段列表、连通性测试 POST
//    remotes-test、删除行）；Settings.jsx 注册 settings-remotes 区块；
// 2. 渲染级：mock /api/settings 后渲染卡片，已配置主机成行展示；
//    「保存」提交 { remotes: [...] }；编辑表单字段可交互；测试按钮
//    调 remotes-test 并展示结果。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const cardSrc = readFileSync(
  path.join(ROOT, 'src/components/settings/RemotesCard.jsx'), 'utf8')
const settingsSrc = readFileSync(
  path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: RemotesCard } = await vite.ssrLoadModule(
  '/src/components/settings/RemotesCard.jsx')

after(() => vite.close())

// ---- 源码级断言 ----

test('源码：RemotesCard 表单字段齐全（name/host/port/user/key_path/extra_options）', () => {
  for (const field of ['name', 'host', 'port', 'user', 'key_path', 'extra_options']) {
    assert.ok(cardSrc.includes(`'${field}'`) || cardSrc.includes(`"${field}"`),
              `表单应包含字段 ${field}`)
  }
})

test('源码：卡片内独立保存整段提交 remotes；测试走 remotes-test 接口', () => {
  assert.match(cardSrc, /api\.put\('\/api\/settings', \{ remotes: list \}\)/,
               '保存应 PUT { remotes: [...] }（整段列表替换）')
  assert.match(cardSrc, /api\.post\('\/api\/settings\/remotes-test'/,
               '测试应 POST /api/settings/remotes-test')
  assert.match(cardSrc, /远程服务器/, '应包含远程服务器说明文案')
})

test('源码：Settings.jsx 注册 settings-remotes 区块（导航自动生成）', () => {
  assert.match(settingsSrc, /id="settings-remotes"/,
               '设置页应有 settings-remotes 区块')
  assert.match(settingsSrc, /<RemotesCard \/>/, '应挂载 RemotesCard 组件')
})

// ---- 渲染级断言 ----

function mockApi(handlers) {
  mock.method(api, 'get', async (pathname) => {
    if (handlers.get && handlers.get[pathname]) return handlers.get[pathname]()
    throw new Error('unexpected GET ' + pathname)
  })
  if (handlers.put || handlers.post) {
    mock.method(api, 'put', async (pathname, body) => {
      if (handlers.put && handlers.put[pathname]) return handlers.put[pathname](body)
      throw new Error('unexpected PUT ' + pathname)
    })
    mock.method(api, 'post', async (pathname, body) => {
      if (handlers.post && handlers.post[pathname]) return handlers.post[pathname](body)
      throw new Error('unexpected POST ' + pathname)
    })
  }
}

async function renderCard() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(RemotesCard))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

const buttonByText = (renderer, text) => renderer.root.findAllByType('button')
  .find((b) => {
    const children = JSON.stringify(b.props.children || '')
    return children.includes(text)
  })

test('渲染：已配置主机成行展示（名称/地址/凭据标记）', async () => {
  mockApi({ get: { '/api/settings': async () => ({
    remotes: [
      { name: 'build', host: '10.0.0.9', port: 22, user: 'bot', key_path: '/k/id' },
      { name: 'gpu', host: '10.0.0.3', port: 2200, user: '', key_path: '' },
    ],
  }) } })
  const { renderer, renderError } = await renderCard()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = JSON.stringify(renderer.toJSON()).replaceAll('","', '')
    assert.ok(text.includes('build'), '应展示主机名 build')
    assert.ok(text.includes('bot@10.0.0.9'), '应展示 user@host')
    assert.ok(text.includes(':2200'), '非 22 端口应展示')
    assert.ok(text.includes('密钥已配置'), '配置了私钥的行应标记')
    assert.ok(text.includes('系统默认凭据'), '未配置私钥的行应标记')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：保存提交 { remotes: [...] }（整段列表）', async () => {
  const puts = []
  mockApi({
    get: { '/api/settings': async () => ({
      remotes: [{ name: 'build', host: '10.0.0.9', port: 22, user: 'bot',
                  key_path: '', extra_options: [] }],
    }) },
    put: { '/api/settings': async (body) => {
      puts.push(body)
      return { remotes: body.remotes }
    } },
  })
  const { renderer, renderError } = await renderCard()
  try {
    assert.equal(renderError, null)
    const saveBtn = buttonByText(renderer, '保存')
    assert.ok(saveBtn, '应存在保存按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.equal(puts.length, 1)
    assert.deepEqual(puts[0].remotes,
                     [{ name: 'build', host: '10.0.0.9', port: 22, user: 'bot',
                        key_path: '', extra_options: [] }])
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：测试按钮调用 remotes-test 并展示结果', async () => {
  const posts = []
  mockApi({
    get: { '/api/settings': async () => ({
      remotes: [{ name: 'build', host: '10.0.0.9', port: 22, user: 'bot',
                  key_path: '', extra_options: [] }],
    }) },
    post: { '/api/settings/remotes-test': async (body) => {
      posts.push(body)
      return { ok: true,
               connectivity: { ok: true, latency_ms: 42, detail: 'SSH 连接正常' },
               zcode: { ok: true, detail: 'zcode 1.0.0' } }
    } },
  })
  const { renderer, renderError } = await renderCard()
  try {
    assert.equal(renderError, null)
    const testBtn = buttonByText(renderer, '测试')
    assert.ok(testBtn, '应存在测试按钮')
    await TestRenderer.act(async () => {
      testBtn.props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.deepEqual(posts, [{ name: 'build' }])
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('SSH 连接正常（42ms）'), '应展示连通性结果')
    assert.ok(text.includes('zcode 1.0.0'), '应展示 zcode 探测结果')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
