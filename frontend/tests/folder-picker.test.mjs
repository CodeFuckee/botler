// 服务器目录选择对话框 FolderPicker 测试（issue #104 补测）：组件此前无任何
// 测试覆盖。用于「本地文件夹方式添加仓库」时挑选服务器上的 git 仓库目录。
//
// 断言：
// 1. open=false 渲染 null；open=true 按 initialPath（URL 编码）请求浏览；
//    无 initialPath 时请求空路径（后端默认初始目录）；
// 2. 子目录列表渲染：名称、git 徽章、无权限目录禁用且带提示；
// 3. 隐藏目录默认过滤，「显示隐藏」勾选后展示；
// 4. 上级按钮 parent 为 null 时禁用，点击加载上级目录；
// 5. 路径输入 Enter / 跳转按钮触发加载；空输入回退根目录；
// 6. 空目录提示（含「可勾选显示隐藏」变体）；加载中提示；加载失败错误展示；
// 7. 「选择此文件夹」回调当前路径；ESC / 遮罩点击触发 onClose。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与其他测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: FolderPicker } = await vite.ssrLoadModule('/src/components/FolderPicker.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

const pickerSrc = readFileSync(path.join(ROOT, 'src/components/FolderPicker.jsx'), 'utf8')

after(() => vite.close())

// ---- 目录树夹具 ----

const TREE = {
  '/srv': {
    path: '/srv', parent: '/',
    subdirs: [
      { name: 'repo-a', path: '/srv/repo-a', readable: true, is_git: true },
      { name: 'plain', path: '/srv/plain', readable: true, is_git: false },
      { name: '.hidden', path: '/srv/.hidden', readable: true, is_git: false },
      { name: 'locked', path: '/srv/locked', readable: false, is_git: false },
    ],
  },
  '/': { path: '/', parent: null, subdirs: [] },
}

// 记录每次浏览请求；默认返回 TREE 对应目录（未知路径返回空目录）
function mockBrowse(calls) {
  mock.method(api, 'get', async (url) => {
    calls.push(url)
    const q = new URL(url, 'http://test').searchParams.get('path') || ''
    return TREE[q] || { path: q, parent: '/', subdirs: [] }
  })
}

// ---- 渲染与查找 helper ----

// 安装 window mock（node 环境无 window）：FolderPicker 打开时会注册 ESC
// keydown 监听，捕获 handler 供测试触发；同时暴露卸载记录。
let keydownHandlers = []
const windowMock = {
  addEventListener: (ev, fn) => { if (ev === 'keydown') keydownHandlers.push(fn) },
  removeEventListener: () => {},
}
globalThis.window = windowMock

async function renderPicker(props = {}) {
  const calls = []
  mockBrowse(calls)
  const onSelect = props.onSelect || (() => {})
  const onClose = props.onClose || (() => {})
  let renderer = null
  let renderError = null
  keydownHandlers = []
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(FolderPicker, {
        open: true, initialPath: '/srv', onSelect, onClose, ...props,
      }))
      await new Promise((resolve) => setTimeout(resolve, 20))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError, calls, onSelect, onClose }
}

// 节点内所有可读文本（TestInstance / 元素 / 数组通用）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

function folderItems(renderer) {
  return renderer.root.findAllByType('button')
    .filter((b) => typeof b.props?.className === 'string' && b.props.className.includes('folder-item'))
}

// 提取各子目录项的 folder-name 文本（排除图标/徽章/无权限提示）
function folderNames(renderer) {
  return folderItems(renderer).map((b) => {
    const nameSpan = b.props.children.find((c) =>
      typeof c.props?.className === 'string' && c.props.className === 'folder-name')
    return textOf(nameSpan)
  })
}

// ---- 源码断言 ----

test('源码：路径请求 URL 编码、ESC 关闭、无权限禁用', () => {
  assert.match(pickerSrc, /encodeURIComponent\(path\)/, '浏览请求路径应 URL 编码')
  assert.match(pickerSrc, /e\.key === 'Escape'/, '应监听 ESC 关闭')
  assert.match(pickerSrc, /disabled=\{!d\.readable\}/, '无权限目录应禁用')
})

// ---- 打开与数据加载 ----

test('open=false 渲染 null（不请求浏览接口）', async () => {
  const calls = []
  mockBrowse(calls)
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(FolderPicker, { open: false, onSelect: () => {}, onClose: () => {} }))
  })
  assert.equal(renderer.toJSON(), null)
  assert.equal(calls.length, 0, '关闭状态不应发起浏览请求')
})

test('打开时按 initialPath 请求浏览（路径 URL 编码）', async () => {
  const { renderError, calls } = await renderPicker()
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  assert.deepEqual(calls[0], '/api/repos/browse?path=%2Fsrv')
})

test('无 initialPath 时请求空路径（后端默认初始目录）', async () => {
  const { renderError, calls } = await renderPicker({ initialPath: undefined })
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  assert.deepEqual(calls[0], '/api/repos/browse?path=')
})

// ---- 子目录列表渲染 ----

test('子目录列表：名称、git 徽章、无权限目录禁用并带提示', async () => {
  const { renderer } = await renderPicker()
  const items = folderItems(renderer)
  // 隐藏目录默认过滤：repo-a / plain / locked 三个可见
  assert.deepEqual(folderNames(renderer), ['repo-a', 'plain', 'locked'])
  const git = items[0].props.children.find((c) => typeof c.props?.className === 'string' && c.props.className.includes('badge-git'))
  assert.ok(git && textOf(git) === 'git', 'git 仓库目录应渲染 git 徽章')
  assert.equal(items[2].props.disabled, true, '无权限目录按钮应禁用')
  assert.match(items[2].props.title, /无权限，无法进入/, '禁用目录应带无权限提示')
})

// ---- 隐藏目录过滤 ----

test('隐藏目录默认过滤，勾选「显示隐藏」后展示', async () => {
  const { renderer } = await renderPicker()
  const checkbox = renderer.root.findAllByType('input')
    .find((i) => i.props.type === 'checkbox')
  assert.equal(checkbox.props.checked, false, '默认不显示隐藏目录')
  await TestRenderer.act(async () => { checkbox.props.onChange({ target: { checked: true } }) })
  assert.deepEqual(folderNames(renderer), ['repo-a', 'plain', '.hidden', 'locked'])
})

// ---- 上级 / 路径跳转 ----

test('上级按钮：parent 为 null 时禁用；点击加载上级目录', async () => {
  // 当前目录为根 '/'（parent=null）时禁用
  const rootPicker = await renderPicker({ initialPath: '/' })
  const upBtn1 = rootPicker.renderer.root.findAllByType('button')
    .find((b) => textOf(b.props.children).includes('上级'))
  assert.equal(upBtn1.props.disabled, true, '根目录的上级按钮应禁用')

  // /srv 的 parent 为 '/'，点击后加载上级
  const { renderer, calls } = await renderPicker()
  const upBtn = renderer.root.findAllByType('button')
    .find((b) => textOf(b.props.children).includes('上级'))
  assert.equal(upBtn.props.disabled, false, '/srv 的上级按钮应可用')
  await TestRenderer.act(async () => {
    upBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.deepEqual(calls[1], '/api/repos/browse?path=%2F', '应加载上级目录 /')
})

test('路径输入 Enter 触发跳转；空输入回退根目录', async () => {
  const { renderer, calls } = await renderPicker()
  const jump = renderer.root.findAllByType('input')
    .find((i) => i.props.placeholder && i.props.placeholder.includes('输入路径后跳转'))
  await TestRenderer.act(async () => {
    jump.props.onChange({ target: { value: ' /tmp ' } })
  })
  assert.equal(jump.props.value, ' /tmp ', '输入值应受控更新')
  await TestRenderer.act(async () => {
    jump.props.onKeyDown({ key: 'Enter' })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.deepEqual(calls[1], '/api/repos/browse?path=%2Ftmp', 'Enter 应加载去空格后的路径')

  // 清空后 Enter → 根目录
  await TestRenderer.act(async () => { jump.props.onChange({ target: { value: '   ' } }) })
  await TestRenderer.act(async () => {
    jump.props.onKeyDown({ key: 'Enter' })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.deepEqual(calls[2], '/api/repos/browse?path=%2F', '空输入 Enter 应回退根目录')
})

test('点击「跳转」按钮加载输入路径', async () => {
  const { renderer, calls } = await renderPicker()
  const jump = renderer.root.findAllByType('input')
    .find((i) => i.props.placeholder && i.props.placeholder.includes('输入路径后跳转'))
  await TestRenderer.act(async () => { jump.props.onChange({ target: { value: '/opt' } }) })
  const goBtn = renderer.root.findAllByType('button')
    .find((b) => textOf(b.props.children) === '跳转')
  await TestRenderer.act(async () => {
    goBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.deepEqual(calls[1], '/api/repos/browse?path=%2Fopt')
})

// ---- 加载 / 空 / 错误状态 ----

test('加载中显示「加载中…」提示', async () => {
  // api.get 挂起，断言加载态；resolve 后断言空目录提示
  let release = null
  mock.method(api, 'get', () => new Promise((resolve) => { release = resolve }))
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(FolderPicker, {
      open: true, initialPath: '/srv', onSelect: () => {}, onClose: () => {},
    }))
  })
  assert.match(textOf(renderer.root), /加载中…/)
  await TestRenderer.act(async () => {
    release({ path: '/srv', parent: '/', subdirs: [] })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.match(textOf(renderer.root), /此文件夹没有子目录/)
})

test('仅剩隐藏目录时提示可勾选「显示隐藏」，勾选后展示隐藏目录', async () => {
  mock.method(api, 'get', async () => ({
    path: '/srv', parent: '/',
    subdirs: [{ name: '.git', path: '/srv/.git', readable: true, is_git: false }],
  }))
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(FolderPicker, {
      open: true, initialPath: '/srv', onSelect: () => {}, onClose: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.match(textOf(renderer.root), /可勾选“显示隐藏”查看隐藏目录/, '仅隐藏目录时应有引导提示')
  const checkbox = renderer.root.findAllByType('input')
    .find((i) => i.props.type === 'checkbox')
  await TestRenderer.act(async () => { checkbox.props.onChange({ target: { checked: true } }) })
  assert.deepEqual(folderNames(renderer), ['.git'], '勾选显示隐藏后应展示隐藏目录')
})

test('空目录提示：默认含「可勾选显示隐藏」，勾选后为普通空文案', async () => {
  const { renderer } = await renderPicker({ initialPath: '/' })
  assert.match(textOf(renderer.root), /此文件夹没有子目录（可勾选“显示隐藏”查看隐藏目录）/)
  const checkbox = renderer.root.findAllByType('input')
    .find((i) => i.props.type === 'checkbox')
  await TestRenderer.act(async () => { checkbox.props.onChange({ target: { checked: true } }) })
  assert.match(textOf(renderer.root), /此文件夹没有子目录/)
  assert.doesNotMatch(textOf(renderer.root), /可勾选/, '显示隐藏模式下不再显示勾选引导')
})

test('浏览失败显示错误信息', async () => {
  mock.method(api, 'get', async () => { throw new Error('无权限访问') })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(FolderPicker, {
      open: true, initialPath: '/srv', onSelect: () => {}, onClose: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  const err = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('alert-error'))
  assert.equal(err.length, 1, '应渲染错误提示')
  assert.match(textOf(err[0]), /无权限访问/)
})

// ---- 选择 / 关闭 ----

test('「选择此文件夹」回调当前浏览路径', async () => {
  let selected = null
  const { renderer, renderError } = await renderPicker({ onSelect: (p) => { selected = p } })
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  const btn = renderer.root.findAllByType('button')
    .find((b) => textOf(b.props.children) === '选择此文件夹')
  await TestRenderer.act(async () => { btn.props.onClick() })
  assert.equal(selected, '/srv', '应回调当前浏览路径')
})

test('ESC 键触发 onClose，其他键不触发', async () => {
  let closed = 0
  const { renderError } = await renderPicker({ onClose: () => { closed += 1 } })
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  assert.ok(keydownHandlers.length >= 1, '打开时应注册 keydown 监听')
  await TestRenderer.act(async () => { keydownHandlers[keydownHandlers.length - 1]({ key: 'Enter' }) })
  assert.equal(closed, 0, '非 ESC 键不应关闭')
  await TestRenderer.act(async () => { keydownHandlers[keydownHandlers.length - 1]({ key: 'Escape' }) })
  assert.equal(closed, 1, 'ESC 应触发 onClose')
})

test('点击遮罩触发 onClose，点击弹窗内容不触发', async () => {
  let closed = 0
  const { renderer } = await renderPicker({ onClose: () => { closed += 1 } })
  const overlay = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('modal-overlay'))
  await TestRenderer.act(async () => { overlay[0].props.onClick() })
  assert.equal(closed, 1, '遮罩点击应触发 onClose')
  // 弹窗内容点击 stopPropagation，不冒泡到遮罩
  const modal = renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes('folder-picker'))
  await TestRenderer.act(async () => { modal[0].props.onClick({ stopPropagation: () => {} }) })
  assert.equal(closed, 1, '弹窗内容点击不应触发 onClose')
})
