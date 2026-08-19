// 任务列表页「导出」按钮测试（issue #228）。
//
// 需求：任务/日志数据无导出能力，新增 GET /api/tasks/export（format=csv|json）
// 供离线分析/归档；任务列表页加「导出」按钮——选择格式（CSV/JSON）后下载
// 当前筛选条件下的全部任务（CSV 带 UTF-8 BOM，Excel 打开中文不乱码）。
//
// 断言：
// 1. Tasks.jsx 源码含「导出」按钮、格式选择（CSV/JSON）与 api.download 调用，
//    且下载 URL 携带当前筛选（status/repo_id/search）；
// 2. 中文文案（locales/zh-CN.json）与源码 t() 国际化 key 双重校验；
// 3. 渲染：默认格式 CSV，点击导出调 api.download('/api/tasks/export?format=csv')；
//    切换格式为 JSON 后导出携带 format=json；设置筛选后 URL 携带筛选参数；
// 4. 导出请求进行中按钮禁用（防重复点击），完成后恢复可用。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const tasksSrc = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// tasks-refresh-button.test.mjs 一致）。api 也经 vite 加载，与 Tasks
// 组件内 import 的是同一模块实例，可对 api.download 做 method mock。
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
const { default: Tasks } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// ---- 源码断言 ----

test('源码含「导出」按钮、格式选择与 api.download 调用', () => {
  assert.match(tasksSrc, /tr\('tasks\.export'\)/, '导出按钮应经 t() 国际化')
  assert.equal(zhCN['tasks.export'], '导出', '中文「导出」文案应保留')
  assert.match(tasksSrc, /tasks\.exportCsv/, '格式选择应含 CSV 选项')
  assert.match(tasksSrc, /tasks\.exportJson/, '格式选择应含 JSON 选项')
  assert.match(tasksSrc, /api\.download\('\/api\/tasks\/export\?'/, '导出应调 api.download')
  assert.match(tasksSrc, /name="download" \/> \{tr\('tasks\.export'\)\}/, '导出按钮应带下载图标')
})

test('导出 URL 携带当前筛选（状态/仓库/搜索）与格式', () => {
  assert.match(tasksSrc, /q\.set\('status', status\)/, '导出应携带状态筛选')
  assert.match(tasksSrc, /q\.set\('repo_id', repoId\)/, '导出应携带仓库筛选')
  assert.match(tasksSrc, /q\.set\('search', search\.trim\(\)\)/, '导出应携带搜索关键词')
  assert.match(tasksSrc, /format: exportFormat/, '导出应携带所选格式')
  assert.match(tasksSrc, /disabled=\{exporting\}/, '请求中应禁用按钮防重复点击')
})

// ---- 组件渲染 ----

// Tasks 页含 react-router Link，渲染需包 MemoryRouter
function renderTasks() {
  return TestRenderer.create(
    React.createElement(MemoryRouter, null, React.createElement(Tasks)),
  )
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

async function renderAndSettle(getImpl, downloadImpl) {
  mock.method(api, 'get', getImpl || (async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  }))
  mock.method(api, 'download', downloadImpl || (async () => {}))
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = renderTasks()
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function findExportFormatSelect(renderer) {
  return renderer.root
    .findAllByType('select')
    .find((s) => (s.props.className || '').includes('tasks-export-format'))
}

function findExportButton(renderer) {
  return renderer.root
    .findAllByType('button')
    .find((b) => textOf(b.props.children).includes('导出'))
}

test('默认格式 CSV，点击导出调 /api/tasks/export?format=csv', async () => {
  const calls = []
  const { renderer, renderError } =
    await renderAndSettle(null, async (path, filename) => calls.push({ path, filename }))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const sel = findExportFormatSelect(renderer)
    assert.ok(sel, '应有导出格式选择')
    assert.equal(sel.props.value, 'csv', '默认格式应为 CSV')
    const btn = findExportButton(renderer)
    assert.ok(btn, '应有导出按钮')
    await TestRenderer.act(async () => { await btn.props.onClick() })
    assert.equal(calls.length, 1, '点击导出应调用一次下载')
    assert.ok(calls[0].path.startsWith('/api/tasks/export?'), '应请求导出接口')
    assert.ok(calls[0].path.includes('format=csv'), '默认应导出 CSV')
    assert.ok(calls[0].filename.endsWith('.csv'), '下载文件名应为 csv 后缀')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('切换格式为 JSON 后导出 format=json，且携带当前筛选', async () => {
  const calls = []
  const getImpl = async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/repos') return { repos: [{ id: 3, name: 'r3' }] }
    throw new Error('unexpected ' + pathname)
  }
  const { renderer, renderError } =
    await renderAndSettle(getImpl, async (path, filename) => calls.push({ path, filename }))
  try {
    assert.equal(renderError, null)
    const [statusSel, repoSel] = renderer.root.findAllByType('select')
    const searchInput = renderer.root.findAllByType('input')
      .find((i) => i.props.placeholder?.includes('搜索'))
    await TestRenderer.act(() => {
      statusSel.props.onChange({ target: { value: 'failed' } })
      repoSel.props.onChange({ target: { value: '3' } })
      searchInput.props.onChange({ target: { value: 'abc' } })
      findExportFormatSelect(renderer).props.onChange({ target: { value: 'json' } })
    })
    await TestRenderer.act(async () => { await findExportButton(renderer).props.onClick() })
    assert.equal(calls.length, 1)
    assert.ok(calls[0].path.includes('format=json'), '切换后应导出 JSON')
    assert.ok(calls[0].path.includes('status=failed'), '导出应携带状态筛选')
    assert.ok(calls[0].path.includes('repo_id=3'), '导出应携带仓库筛选')
    assert.ok(calls[0].path.includes('search=abc'), '导出应携带搜索关键词')
    assert.ok(calls[0].filename.endsWith('.json'), '下载文件名应为 json 后缀')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('导出请求进行中按钮禁用，完成后恢复可用', async () => {
  let resolveDownload = null
  const { renderer, renderError } =
    await renderAndSettle(null, () => new Promise((resolve) => { resolveDownload = resolve }))
  try {
    assert.equal(renderError, null)
    const btn = findExportButton(renderer)
    assert.notEqual(btn.props.disabled, true, '默认应可用')
    let clickPromise = null
    await TestRenderer.act(() => {
      clickPromise = btn.props.onClick()
    })
    // 下载挂起期间按钮应禁用（防重复点击）
    assert.equal(findExportButton(renderer).props.disabled, true, '请求中应禁用')
    await TestRenderer.act(async () => { resolveDownload() ; await clickPromise })
    assert.notEqual(findExportButton(renderer).props.disabled, true, '完成后应恢复可用')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
