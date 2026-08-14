// 仓库页「设置」按钮与编辑弹窗测试（issue #51）。
//
// 需求：仓库页面增加设置按钮，可重新编辑仓库（名称 name、启用状态
// enabled、优先级 priority）；优先级整数 1~999，默认 100，数字越小
// 越优先。列表展示各仓库优先级。
//
// 实现前状态（本测试应当失败）：
// - 仓库列表没有「设置」按钮、不展示优先级
// - 没有编辑弹窗组件，保存请求无处发出
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// react-router-dom mock（与其他页面测试一致）
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

// ---- API mock ----

const REPOS = [
  { id: 1, name: '高优先级仓库', url: 'https://gitlab.example.com/group/a.git',
    gitlab_project_id: 11, enabled: true, priority: 1 },
  { id: 2, name: '普通仓库', url: 'https://gitlab.example.com/group/b.git',
    gitlab_project_id: 22, enabled: true, priority: 100 },
]

function mockApi(repos = REPOS) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos }
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

function findButtons(renderer, text) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => String(b.props.children).includes(text))
}

function findModal(renderer) {
  return renderer.root.findAll((node) =>
    typeof node.props?.className === 'string'
    && node.props.className.includes('modal-overlay'))
}

function findInputs(renderer) {
  return renderer.root.findAllByType('input')
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

// 弹窗内所有可读文本（用于断言优先级提示/错误信息）
function modalText(renderer) {
  return textOf(findModal(renderer)[0])
}

// 精确文本匹配的 span 节点（badge「优先级 N」）
function findTextSpans(renderer, text) {
  return renderer.root.findAll((node) => node.type === 'span'
    && textOf(node.props?.children) === text)
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

// ---- 设置按钮与优先级展示 ----

test('仓库列表每项有设置按钮并展示优先级', async () => {
  mockApi()
  const { renderer, renderError } = await renderRepos()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
    const settings = findButtons(renderer, '设置')
    assert.equal(settings.length, REPOS.length, '每个仓库应有一个设置按钮')
    assert.equal(findTextSpans(renderer, '优先级 1').length, 1, '应展示仓库优先级')
    assert.equal(findTextSpans(renderer, '优先级 100').length, 1, '应展示仓库优先级')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 弹窗打开与字段回填 ----

test('点击设置打开弹窗，字段回填当前仓库值', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  try {
    await click(renderer, findButtons(renderer, '设置')[0])
    const modals = findModal(renderer)
    assert.equal(modals.length, 1, '应弹出编辑弹窗')
    assert.match(modalText(renderer), /高优先级仓库/, '弹窗应显示仓库名（字段回填）')

    const inputs = findInputs(renderer)
    const nameInput = inputs.find((i) => i.props.value === '高优先级仓库')
    const priorityInput = inputs.find((i) => i.props.value === '1')
    const enabledInput = inputs.find((i) => i.props.type === 'checkbox')
    assert.ok(nameInput, '名称输入框应回填仓库名')
    assert.ok(priorityInput, '优先级输入框应回填优先级')
    assert.equal(enabledInput.props.checked, true, '启用状态应回填')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 保存 ----

test('修改后保存：PUT 携带 name/enabled/priority，成功后关闭弹窗', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  let putBody = null
  let putPath = null
  mock.method(api, 'put', async (pathname, body) => {
    putPath = pathname
    putBody = body
    return {}
  })
  try {
    await click(renderer, findButtons(renderer, '设置')[1]) // 普通仓库（id=2）
    const inputs = findInputs(renderer)
    const nameInput = inputs.find((i) => i.props.value === '普通仓库')
    const priorityInput = inputs.find((i) => i.props.value === '100')
    const enabledInput = inputs.find((i) => i.props.type === 'checkbox')

    await change(renderer, nameInput, '改名后的仓库')
    await change(renderer, priorityInput, '50')
    await TestRenderer.act(async () => {
      enabledInput.props.onChange({ target: { checked: false } })
    })

    await click(renderer, findButtons(renderer, '保存')[0])

    assert.equal(putPath, '/api/repos/2', '应 PUT 到对应仓库')
    assert.deepEqual(putBody, { name: '改名后的仓库', enabled: false, priority: 50 })
    assert.equal(findModal(renderer).length, 0, '保存成功后弹窗应关闭')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('取消关闭弹窗且不发请求', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  let putCalled = false
  mock.method(api, 'put', async () => { putCalled = true })
  try {
    await click(renderer, findButtons(renderer, '设置')[0])
    assert.equal(findModal(renderer).length, 1)
    await click(renderer, findButtons(renderer, '取消')[0])
    assert.equal(findModal(renderer).length, 0, '取消后弹窗应关闭')
    assert.equal(putCalled, false, '取消不应发保存请求')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('保存失败：弹窗保留并展示错误', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  mock.method(api, 'put', async () => {
    throw new Error('优先级需为 1~999 之间的整数')
  })
  try {
    await click(renderer, findButtons(renderer, '设置')[0])
    await click(renderer, findButtons(renderer, '保存')[0])
    assert.equal(findModal(renderer).length, 1, '保存失败弹窗不应关闭')
    assert.match(modalText(renderer), /优先级需为 1~999/, '弹窗应展示后端错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 边界情况：输入校验 ----

test('优先级非法输入：前端校验拦截，不发请求', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  let putCalled = false
  mock.method(api, 'put', async () => { putCalled = true })
  try {
    await click(renderer, findButtons(renderer, '设置')[0])
    const priorityInput = findInputs(renderer).find((i) => i.props.value === '1')
    for (const bad of ['0', '1000', '-5', 'abc', '']) {
      await change(renderer, priorityInput, bad)
      await click(renderer, findButtons(renderer, '保存')[0])
      assert.equal(putCalled, false, `priority=${JSON.stringify(bad)} 不应发请求`)
      assert.match(modalText(renderer), /1~999/, '应提示优先级范围')
      assert.equal(findModal(renderer).length, 1, '校验失败弹窗不应关闭')
    }
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('名称为空：前端校验拦截，不发请求', async () => {
  mockApi()
  const { renderer } = await renderRepos()
  let putCalled = false
  mock.method(api, 'put', async () => { putCalled = true })
  try {
    await click(renderer, findButtons(renderer, '设置')[0])
    const nameInput = findInputs(renderer).find((i) => i.props.value === '高优先级仓库')
    await change(renderer, nameInput, '')
    await click(renderer, findButtons(renderer, '保存')[0])
    assert.equal(putCalled, false, '空名称不应发请求')
    assert.match(modalText(renderer), /名称/, '应提示名称必填')
    assert.equal(findModal(renderer).length, 1, '校验失败弹窗不应关闭')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
