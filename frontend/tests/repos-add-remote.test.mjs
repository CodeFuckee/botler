// 仓库页「远程服务器」添加方式测试（远程项目 + zcode 引擎联动）：
// 项目代码位于其他服务器上的文件夹，botler 经 SSH 读取 remote 识别
// GitLab 项目，任务在远程工作目录执行（引擎建议 zcode）。
//
// 断言分两层：
// 1. 源码级：Repos.jsx 含第三个 add-method 选项（顺序在最后）、远程
//    表单字段（主机下拉/路径输入/读取 remote 按钮）、提交 payload 带
//    remote_host/remote_path/remote_name、校验逻辑（未选主机/非绝对
//    路径/未选 remote）；
// 2. 渲染级：mock /api/repos + /api/settings 后渲染页面，第三个选项
//    存在；切到远程方式后渲染主机下拉与路径输入框；主机清单来自
//    settings.remotes；未配置主机时展示引导文案。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = readFileSync(path.join(ROOT, 'src/pages/Repos.jsx'), 'utf8')

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

const REMOTE_LABEL = '远程服务器（SSH，代码在其他服务器上）'
const REMOTE_PATH_PLACEHOLDER = '远程服务器上的项目绝对路径'

// ---- 源码级断言 ----

test('源码：第三个添加方式为「远程服务器」，排在本地与 URL 之后', () => {
  const local = source.indexOf('本地文件夹（读取 git remote）')
  const url = source.indexOf('GitLab URL / project_id')
  const remote = source.indexOf(REMOTE_LABEL)
  assert.ok(local >= 0 && url >= 0 && remote >= 0, '三个添加方式选项都应存在')
  assert.ok(local < url && url < remote,
    '选项顺序应为 本地文件夹 → GitLab URL → 远程服务器')
})

test('源码：远程方式提交 payload 带 remote_host / remote_path / remote_name', () => {
  assert.match(
    source,
    /remote_host: method === 'remote' \? form\.remote_host : undefined/,
    '提交应带 remote_host（仅远程方式）')
  assert.match(
    source,
    /remote_path: method === 'remote' \? form\.remote_path\.trim\(\) : undefined/,
    '提交应带 remote_path（仅远程方式）')
  assert.match(
    source,
    /remote_name: method === 'local' \|\| method === 'remote' \? form\.remote_name : undefined/,
    'remote_name 应在本地与远程两种方式下提交')
})

test('源码：远程方式前端校验（主机必选/绝对路径/remote 必选）', () => {
  assert.match(source, /请选择远程服务器/, '未选主机应有校验提示')
  assert.match(source, /项目绝对路径（以 \/ 开头）/, '非绝对路径应有校验提示')
  assert.match(source, /请先读取并选择一个 remote/, '未选 remote 应有校验提示')
})

test('源码：读取 remote 经 /api/repos/discover 传 remote_host + remote_path', () => {
  const fn = source.slice(source.indexOf('const discoverRemote'),
                          source.indexOf('const toggle'))
  assert.match(fn, /remote_host: form\.remote_host/, 'discover 应传 remote_host')
  assert.match(fn, /remote_path: form\.remote_path\.trim\(\)/,
               'discover 应传 remote_path')
  assert.match(fn, /\/api\/repos\/discover/, '应调用 discover 接口')
})

test('源码：主机清单来自 settings.remotes，未配置时展示引导', () => {
  assert.match(source, /setRemoteHosts\(s\.remotes \|\| \[\]\)/,
               '远程主机下拉数据源应为 /api/settings 的 remotes')
  assert.match(source, /设置 → 执行引擎 → 远程服务器/, '未配置主机时应展示设置页引导')
})

// ---- 渲染级断言 ----

function mockApi() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname === '/api/settings') {
      return { remotes: [{ name: 'build', host: '10.0.0.9', user: 'bot', port: 22 }] }
    }
    throw new Error('unexpected ' + pathname)
  })
}

async function renderRepos() {
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

test('渲染：第三个选项存在且默认未选中；默认不渲染远程表单', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes(REMOTE_LABEL), '应渲染「远程服务器」选项')
    const radios = renderer.root.findAllByType('input')
      .filter((i) => i.props.type === 'radio')
    assert.equal(radios.length, 3, '应有三个添加方式 radio')
    assert.equal(radios[2].props.checked, false, '远程方式默认未选中')
    assert.ok(
      !JSON.stringify(renderer.toJSON()).includes(REMOTE_PATH_PLACEHOLDER),
      '默认不应渲染远程路径输入框')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：切到远程方式后渲染主机下拉（含已配置主机）与路径输入框', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const radios = renderer.root.findAllByType('input')
      .filter((i) => i.props.type === 'radio')
    await TestRenderer.act(async () => {
      radios[2].props.onChange()
    })
    const text = JSON.stringify(renderer.toJSON()).replaceAll('","', '')
    assert.ok(text.includes(REMOTE_PATH_PLACEHOLDER), '应渲染远程路径输入框')
    assert.ok(text.includes('build（bot@10.0.0.9）'),
              '主机下拉应包含 settings.remotes 配置的主机')
    assert.ok(
      !text.includes('尚未配置远程服务器'),
      '已有配置主机时不应展示引导文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：未配置任何主机时切到远程方式展示设置页引导', async () => {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname === '/api/settings') return { remotes: [] }
    throw new Error('unexpected ' + pathname)
  })
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null)
    const radios = renderer.root.findAllByType('input')
      .filter((i) => i.props.type === 'radio')
    await TestRenderer.act(async () => {
      radios[2].props.onChange()
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('尚未配置远程服务器'), '应展示设置页引导文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
