// 添加仓库表单调度优先级设置选项测试（issue #161）。
//
// 需求：「添加仓库页面增加一个优先级设置选项」——后端 POST /api/repos 早已支持
// priority 字段（issue #51：1~999 整数、缺省 100），仓库设置弹窗也可编辑优先级，
// 唯独「添加仓库」表单没有暴露该选项，新添加的仓库只能取后端默认 100，无法在
// 添加时直接设置调度优先级。
// 期望（修复后）：添加仓库表单新增「调度优先级」输入项，默认值 '100'（与后端
// 缺省一致）；提交时 POST /api/repos 携带 priority（留空则不带，后端默认 100）；
// 非法输入（非整数、<1、>999）前端拦截并提示「优先级需为 1~999 之间的整数」且
// 不发请求；添加成功后表单重置回默认 100。
//
// 断言分两层：
// 1. 源码级：Repos.jsx 存在优先级输入框 placeholder、1~999 校验逻辑与提交 body
//    携带 priority；
// 2. 渲染级：mock /api/repos 后渲染页面，断言默认值 100、提交携带 priority、
//    留空不带、非法输入拦截、成功后重置。
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

// react-router-dom mock（与 repos-add-method-order.test.mjs 一致）
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

const PRIORITY_PLACEHOLDER = '调度优先级（默认 100；1~999 整数，数字越小越优先）'
const URL_INPUT_PLACEHOLDER = 'GitLab 项目 URL 或 project_id'
const ERROR_TEXT = '优先级需为 1~999 之间的整数'

// ---- 源码级断言 ----

test('源码：添加表单包含优先级输入框、1~999 校验，提交 body 携带 priority', () => {
  assert.ok(source.includes(PRIORITY_PLACEHOLDER), '应存在调度优先级输入框 placeholder（issue #161）')
  assert.ok(source.includes(ERROR_TEXT), '应存在优先级 1~999 前端校验提示')
  assert.ok(
    source.includes("webhook_url: form.webhook_url.trim() || undefined,\n        priority,"),
    'POST /api/repos 提交 body 应携带 priority（紧邻 webhook_url 之后）',
  )
})

// ---- 渲染级断言 ----

function mockApi({ postImpl } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', postImpl || (async () => ({})))
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

function findInputByPlaceholderPrefix(renderer, prefix) {
  return renderer.root
    .findAllByType('input')
    .find((i) => i.props.placeholder && i.props.placeholder.startsWith(prefix))
}

function findButtons(renderer, text) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => String(b.props.children).includes(text))
}

async function change(renderer, input, value) {
  await TestRenderer.act(async () => {
    input.props.onChange({ target: { value } })
  })
}

async function click(renderer, button) {
  await TestRenderer.act(async () => {
    await button.props.onClick()
  })
}

// 切到 GitLab URL / project_id 方式并回填 URL（添加请求只依赖这两个字段）
async function fillUrlForm(renderer, url) {
  const radios = renderer.root.findAllByType('input').filter((i) => i.props.type === 'radio')
  assert.equal(radios.length, 3, '应有三个添加方式 radio 选项（本地/URL/远程服务器）')
  await TestRenderer.act(async () => {
    radios[1].props.onChange() // 选中 GitLab URL / project_id
  })
  await change(renderer, findInputByPlaceholderPrefix(renderer, URL_INPUT_PLACEHOLDER), url)
}

test('渲染：添加表单默认展示调度优先级输入框，默认值 100', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const input = findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER)
    assert.ok(input, '应渲染调度优先级输入框（issue #161）')
    assert.equal(input.props.value, '100', '优先级默认值应为 100（与后端缺省一致）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：修改优先级后添加，POST /api/repos 携带 priority', async () => {
  const posts = []
  mockApi({ postImpl: async (pathname, body) => { posts.push({ pathname, body }); return {} } })
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    await fillUrlForm(renderer, 'https://gitlab.example.com/group/demo.git')
    await change(renderer, findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER), '50')
    await click(renderer, findButtons(renderer, '添加')[0])
    assert.equal(posts.length, 1, '应恰好发一次添加请求')
    assert.equal(posts[0].pathname, '/api/repos')
    assert.equal(posts[0].body.priority, 50, 'POST body 应携带 priority=50')
    assert.equal(posts[0].body.url, 'https://gitlab.example.com/group/demo.git')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：优先级留空时 POST body 不携带 priority（后端默认 100）', async () => {
  const posts = []
  mockApi({ postImpl: async (pathname, body) => { posts.push({ pathname, body }); return {} } })
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    await fillUrlForm(renderer, 'https://gitlab.example.com/group/demo.git')
    await change(renderer, findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER), '')
    await click(renderer, findButtons(renderer, '添加')[0])
    assert.equal(posts.length, 1, '应恰好发一次添加请求')
    assert.equal(posts[0].body.priority, undefined, '留空优先级不应携带 priority（后端按默认 100）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('交互：非法优先级（越界/非整数）前端拦截提示，不发请求', async () => {
  // 注意：'100.0' 数值上等于整数 100，与编辑弹窗校验行为一致（Number('100.0')=100
  // 且 isInteger 为真），属合法输入，不在非法列表中。
  for (const bad of ['0', '1000', '1.5', 'abc', '-1']) {
    let postCalled = false
    mockApi({ postImpl: async () => { postCalled = true; return {} } })
    const { renderer, renderError } = await renderRepos()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      await fillUrlForm(renderer, 'https://gitlab.example.com/group/demo.git')
      await change(renderer, findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER), bad)
      await click(renderer, findButtons(renderer, '添加')[0])
      assert.equal(postCalled, false, `priority=${JSON.stringify(bad)} 不应发请求`)
      const text = JSON.stringify(renderer.toJSON())
      assert.match(text, /1~999/, `priority=${JSON.stringify(bad)} 应提示 1~999 范围错误`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  }
})

test('交互：添加成功后优先级输入框重置为默认 100', async () => {
  mockApi({ postImpl: async () => ({}) })
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    await fillUrlForm(renderer, 'https://gitlab.example.com/group/demo.git')
    await change(renderer, findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER), '50')
    await click(renderer, findButtons(renderer, '添加')[0])
    const priorityInput = findInputByPlaceholderPrefix(renderer, PRIORITY_PLACEHOLDER)
    assert.equal(priorityInput.props.value, '100', '添加成功后优先级应重置为默认 100')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
