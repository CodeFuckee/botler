// 复现测试（issue #73）：添加仓库方式选项顺序。
//
// 需求：「本地文件夹（读取 git remote）放在第一个选项，
// GitLab URL / project_id 放在第二个选项」。
// 当前（修复前）：GitLab URL 在前、本地文件夹在后，
// 且默认选中的 method='local' 对应的是第二个选项，视觉顺序与选中态脱节。
// 期望（修复后）：本地文件夹（读取 git remote）→ GitLab URL / project_id，
// 第一个选项默认选中（本地文件夹方式，渲染本地路径输入框）。
//
// 断言分两层：
// 1. 源码级：Repos.jsx 中两个 add-method 选项的 JSX 起始位置顺序；
// 2. 渲染级：mock /api/repos 后渲染页面，序列化渲染树文本，
//    断言选项文本先后顺序、两个 radio 的 checked 状态（[true, false]），
//    默认渲染本地路径输入框而非 URL 输入框；交互切换后表单随之切换。
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

// react-router-dom mock（与 repos-edit-modal.test.mjs 一致）
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

const LOCAL_LABEL = '本地文件夹（读取 git remote）'
const URL_LABEL = 'GitLab URL / project_id'
const LOCAL_INPUT_PLACEHOLDER = '服务器上的本地 git 仓库文件夹路径'
const URL_INPUT_PLACEHOLDER = 'GitLab 项目 URL 或 project_id'

// ---- 源码级断言 ----

test('源码：两个添加方式选项的 JSX 起始位置', () => {
  const local = source.indexOf(LOCAL_LABEL)
  const url = source.indexOf(URL_LABEL)
  assert.ok(local >= 0, '应存在本地文件夹选项')
  assert.ok(url >= 0, '应存在 GitLab URL 选项')
  assert.ok(
    local < url,
    `「本地文件夹」选项（源码偏移 ${local}）应位于「GitLab URL」选项（偏移 ${url}）之前（issue #73）`,
  )
})

// ---- 渲染级断言 ----

function mockApi() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [] }
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

function radioInputs(renderer) {
  return renderer.root.findAllByType('input').filter((i) => i.props.type === 'radio')
}

function inputPlaceholders(renderer) {
  return renderer.root.findAllByType('input').map((i) => i.props.placeholder)
}

// placeholder 实际值带示例后缀，按前缀匹配
function hasPlaceholderPrefix(renderer, prefix) {
  return inputPlaceholders(renderer).some((p) => p && p.startsWith(prefix))
}

test('渲染：选项文本顺序为 本地文件夹 → GitLab URL（issue #73 需求顺序）', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const text = JSON.stringify(renderer.toJSON())
    const local = text.indexOf(LOCAL_LABEL)
    const url = text.indexOf(URL_LABEL)
    assert.ok(local >= 0 && url >= 0, '两个选项文本都应渲染')
    assert.ok(local < url, '「本地文件夹」选项应渲染在「GitLab URL」选项之前')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：第一个选项（本地文件夹）默认选中，其余未选中', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const radios = radioInputs(renderer)
    assert.equal(radios.length, 3, '应有三个添加方式 radio 选项（本地/URL/远程服务器）')
    assert.deepEqual(
      radios.map((r) => r.props.checked),
      [true, false, false],
      '默认应选中第一个选项（本地文件夹），其余未选中',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：默认展开本地文件夹表单（本地路径输入框），不渲染 URL 输入框', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    assert.ok(
      hasPlaceholderPrefix(renderer, LOCAL_INPUT_PLACEHOLDER),
      '默认应渲染本地路径输入框',
    )
    assert.ok(
      !hasPlaceholderPrefix(renderer, URL_INPUT_PLACEHOLDER),
      '默认不应渲染 URL 输入框',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：切换到第二个选项（GitLab URL）后表单切换为 URL 输入框', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const radios = radioInputs(renderer)
    assert.equal(radios.length, 3)
    await TestRenderer.act(async () => {
      radios[1].props.onChange() // 选中 GitLab URL / project_id
    })
    assert.ok(
      hasPlaceholderPrefix(renderer, URL_INPUT_PLACEHOLDER),
      '切换后应渲染 URL 输入框',
    )
    assert.ok(
      !hasPlaceholderPrefix(renderer, LOCAL_INPUT_PLACEHOLDER),
      '切换后不应再渲染本地路径输入框',
    )
    const checked = radioInputs(renderer).map((r) => r.props.checked)
    assert.deepEqual(checked, [false, true, false], '切换后第二个选项应选中')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
