// 标记库页面测试（issue #29）：用户反馈「增加一个标记库，用户可以手动添加删除，
// 建议清单作为默认选项不可删除，但页面上看不到标记库页面」。
//
// 第一轮只交付了 docs/labels.md + scripts/sync_labels.py（无 UI），
// 用户澄清要的是 botler Web UI 上的标记库管理页面。本测试断言：
// 1. 存在 /labels 路由与顶部导航入口；
// 2. 页面展示默认标签清单（标记「默认」、无删除按钮）；
// 3. 页面提供自定义标签添加表单与删除按钮；
// 4. 后端 /api/labels 提供 GET/POST/DELETE 接口。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const lazyPages = readFileSync(path.join(ROOT, 'src/pages/lazy.jsx'), 'utf8')
const labels = readFileSync(path.join(ROOT, 'src/pages/Labels.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiLabels = readFileSync(path.join(ROOT, '../backend/botler/api/labels.py'), 'utf8')

test('顶部导航包含「标记库」入口', () => {
  assert.match(app, /NavLink to="\/labels"/, '导航应有到 /labels 的 NavLink')
  assert.match(app, /t\('nav\.labels'\)/, '导航链接应经 t() 国际化')
  assert.equal(zhCN['nav.labels'], '标记库', '中文文案应为「标记库」')
})

test('App.jsx 注册 /labels 路由并挂载 Labels 页面', () => {
  assert.match(app, /Route path="\/labels" element={<Labels \/>}/, '应有 /labels 路由')
  assert.match(app, /from '\.\/pages\/lazy\.jsx'/, 'App 页面应统一经 lazy.jsx 按路由懒加载')
  assert.match(lazyPages, /export const Labels = lazy\(\(\) => import\('\.\/Labels\.jsx'\)\)/, 'lazy.jsx 应包装 Labels 页面')
})

test('标记库页展示默认清单，默认标签标记「默认」且无删除按钮', () => {
  assert.match(labels, /默认标签/, '页面应有「默认标签」区块标题')
  assert.match(labels, /不可删除/, '应注明默认标签不可删除')
  assert.match(labels, /badge-default/, '默认标签应有「默认」徽标样式类')
  // 默认标签渲染分支：removable=false → 只显示徽标，不渲染删除按钮
  assert.match(labels, /removable\s*\?\s*<button/, '删除按钮只在自定义标签分支渲染')
})

test('标记库页提供自定义标签添加表单（名称/颜色/说明/添加按钮）', () => {
  assert.match(labels, /添加自定义标签/, '应有「添加自定义标签」区块')
  assert.match(labels, /标签名（/, '应有标签名输入框')
  assert.match(labels, /#6699cc/, '应有颜色输入框（默认色占位）')
  assert.match(labels, /说明（可选）/, '应有说明输入框')
  assert.match(labels, /btn-primary/, '应有「添加」按钮')
  assert.match(labels, /api\.post\('\/api\/labels'/, '添加走 POST /api/labels')
})

test('标记库页自定义标签可删除（走 DELETE /api/labels/{name}）', () => {
  assert.match(labels, /删除自定义标签/, '应有删除确认提示')
  assert.match(labels, /api\.del\(`\/api\/labels\//, '删除走 DELETE /api/labels/{name}')
  assert.match(labels, /api\.get\('\/api\/labels'/, '列表加载走 GET /api/labels')
})

test('styles.css 提供标签条目样式（label-chip / label-color / label-list）', () => {
  for (const cls of ['label-chip', 'label-color', 'label-list', 'badge-default', 'btn-small']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

test('后端提供 /api/labels 的 GET/POST/DELETE 接口', () => {
  assert.match(apiLabels, /APIRouter\(prefix="\/labels"/, 'labels API 前缀应为 /labels')
  assert.match(apiLabels, /@router\.get\(""\)/, '应有 GET /api/labels')
  assert.match(apiLabels, /@router\.post\(""\)/, '应有 POST /api/labels')
  assert.match(apiLabels, /@router\.delete\("\/\{name\}"\)/, '应有 DELETE /api/labels/{name}')
  assert.match(apiLabels, /默认标签，不可删除/, '删除默认标签应被拒绝')
})

// ---- 默认标签一键同步（issue #307） ----

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: Labels } = await vite.ssrLoadModule('/src/pages/Labels.jsx')
after(() => vite.close())

const LABELS_DATA = {
  default: [
    { name: 'bug', color: '#d9534f', description: '缺陷修复' },
    { name: 'feature', color: '#009966', description: '新功能' },
  ],
  custom: [],
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

function syncBtns(renderer) {
  // 单标签「同步到所有仓库」按钮：title 形如「同步「xxx」到所有已添加仓库」，
  // 与「一键同步全部」（issue #358）按钮区分开
  return renderer.root.findAll((n) => n.type === 'button'
    && String(n.props.title || '').startsWith('同步「'))
}

function allSyncBtn(renderer) {
  // 「一键同步全部」按钮（issue #358）：按固定 title 查找（请求中文本会变「同步中…」）
  return renderer.root.findAll((n) => n.type === 'button'
    && String(n.props.title || '').includes('一键同步全部'))[0]
}

async function renderLabels({ labels = LABELS_DATA } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/labels') return labels
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Labels))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

test('默认标签提供「同步到所有仓库」按钮（issue #307）', () => {
  assert.match(labels, /同步到所有仓库/, '默认标签应有「同步到所有仓库」按钮')
  assert.match(
    labels,
    /api\.post\(`\/api\/labels\/\$\{encodeURIComponent\(label\.name\)\}\/sync`\)/,
    '点击后应调 POST /api/labels/{name}/sync',
  )
  assert.match(
    labels,
    /disabled=\{syncing !== null \|\| syncingAll\}/,
    '同步请求中（单标签或一键同步全部）应禁用按钮',
  )
  assert.match(labels, /同步中…/, '请求中应显示「同步中…」')
  assert.match(labels, /包括启用和未启用的|含启用与未启用的/, '应说明同步范围含启用与未启用仓库')
})

test('后端提供 POST /api/labels/{name}/sync 接口（issue #307）', () => {
  assert.match(apiLabels, /@router\.post\("\/\{name\}\/sync"\)/, '应有 POST /api/labels/{name}/sync')
  assert.match(apiLabels, /DEFAULT_LABELS/, '同步逻辑应基于内置默认清单')
  assert.match(apiLabels, /list_repos\(\)/, '应遍历全部已添加仓库（含启用与未启用）')
  assert.match(apiLabels, /build_repo_client/, '应优先使用 per-repo client')
  assert.match(apiLabels, /c\.gitlab/, '无 per-repo client 时应回退全局 bot client')
})

test('渲染：每个默认标签渲染「同步到所有仓库」按钮', async () => {
  const { renderer, renderError } = await renderLabels()
  assert.equal(renderError, null, String(renderError || ''))
  assert.equal(syncBtns(renderer).length, 2, '默认标签数量应等于同步按钮数量')
})

test('点击「同步到所有仓库」调用后端并展示同步结果', async () => {
  const { renderer } = await renderLabels()
  const postCalls = []
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/labels/bug/sync') {
      return {
        label: { name: 'bug' },
        total_repos: 2,
        created: ['repo-a', 'repo-b'],
        already_exists: [],
        failed: [],
      }
    }
    throw new Error('unexpected ' + pathname)
  })
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/labels/bug/sync'], '应调用同步接口')
  assert.match(treeText(renderer), /已同步「bug」到 2 个仓库/, '成功应显示同步汇总')
  assert.match(treeText(renderer), /新建 2 个/, '应显示新建数量')
})

test('同步失败展示后端错误信息', async () => {
  const { renderer } = await renderLabels()
  mock.method(api, 'post', async () => {
    throw new Error('GitLab API 错误 403: 权限不足')
  })
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /同步「bug」失败/, '应展示失败提示')
  assert.match(treeText(renderer), /403/, '应透传后端错误详情')
})

test('同步请求中按钮禁用并显示「同步中…」', async () => {
  const { renderer } = await renderLabels()
  // post 挂起不返回：保持 loading 状态，断言请求中禁用
  mock.method(api, 'post', async () => new Promise(() => {}))
  const btn = syncBtns(renderer)[0]
  await TestRenderer.act(async () => {
    btn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.equal(syncBtns(renderer)[0].props.disabled, true, '请求中按钮应禁用')
  assert.match(treeText(renderer), /同步中…/, '请求中应显示「同步中…」')
})

// ---- 一键同步全部默认标签（issue #358） ----

test('页面提供「一键同步全部」按钮（issue #358）', () => {
  assert.match(labels, /一键同步全部/, '应有「一键同步全部」按钮文案')
  assert.match(
    labels,
    /api\.post\('\/api\/labels\/sync-all'\)/,
    '点击后应调 POST /api/labels/sync-all',
  )
  assert.match(labels, /syncingAll/, '应有 syncingAll 状态')
  assert.match(labels, /同步全部中…|同步中…/, '请求中应显示同步中提示')
})

test('后端提供 POST /api/labels/sync-all 接口（issue #358）', () => {
  assert.match(apiLabels, /@router\.post\("\/sync-all"\)/, '应有 POST /api/labels/sync-all')
  assert.match(apiLabels, /DEFAULT_LABELS/, '应遍历内置默认清单')
  assert.match(apiLabels, /list_repos\(\)/, '应遍历全部已添加仓库（含启用与未启用）')
  assert.match(apiLabels, /build_repo_client/, '应优先使用 per-repo client')
  assert.match(apiLabels, /total_created/, '响应应含全局新建统计')
  assert.match(apiLabels, /total_failed/, '响应应含全局失败统计')
})

test('渲染：「一键同步全部」按钮出现在默认标签卡片', async () => {
  const { renderer, renderError } = await renderLabels()
  assert.equal(renderError, null, String(renderError || ''))
  assert.ok(allSyncBtn(renderer), '应有一个「一键同步全部」按钮')
})

test('点击「一键同步全部」调用后端并展示汇总', async () => {
  const { renderer } = await renderLabels()
  const postCalls = []
  mock.method(api, 'post', async (pathname) => {
    postCalls.push(pathname)
    if (pathname === '/api/labels/sync-all') {
      return {
        total_repos: 2,
        total_created: 3,
        total_already_exists: 25,
        total_failed: 0,
        labels: [
          { label: 'bug', created: ['repo-a'], already_exists: ['repo-b'], failed: [] },
        ],
      }
    }
    throw new Error('unexpected ' + pathname)
  })
  const allBtn = allSyncBtn(renderer)
  await TestRenderer.act(async () => {
    allBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.deepEqual(postCalls, ['/api/labels/sync-all'], '应调用 sync-all 接口')
  assert.match(treeText(renderer), /已同步全部/, '成功应显示同步汇总')
  assert.match(treeText(renderer), /新建 3 个/, '应显示新建数量')
  assert.match(treeText(renderer), /已存在 25 个/, '应显示已存在数量')
})

test('「一键同步全部」部分失败展示失败明细', async () => {
  const { renderer } = await renderLabels()
  mock.method(api, 'post', async () => ({
    total_repos: 2,
    total_created: 2,
    total_already_exists: 24,
    total_failed: 2,
    labels: [
      { label: 'bug', created: [], already_exists: [], failed: [{ repo: 'repo-b', error: '权限不足' }] },
    ],
  }))
  const allBtn = allSyncBtn(renderer)
  await TestRenderer.act(async () => {
    allBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /失败 2 个/, '应显示失败数量')
  assert.match(treeText(renderer), /repo-b/, '应展示失败仓库')
})

test('「一键同步全部」失败展示错误信息', async () => {
  const { renderer } = await renderLabels()
  mock.method(api, 'post', async () => {
    throw new Error('GitLab API 错误 500: 内部错误')
  })
  const allBtn = allSyncBtn(renderer)
  await TestRenderer.act(async () => {
    allBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  assert.match(treeText(renderer), /同步全部失败|一键同步失败/, '应展示失败提示')
  assert.match(treeText(renderer), /500/, '应透传后端错误详情')
})

test('「一键同步全部」请求中禁用并显示同步中', async () => {
  const { renderer } = await renderLabels()
  mock.method(api, 'post', async () => new Promise(() => {}))
  const allBtn = allSyncBtn(renderer)
  await TestRenderer.act(async () => {
    allBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  const btnAfter = allSyncBtn(renderer)
  assert.equal(btnAfter.props.disabled, true, '请求中按钮应禁用')
  assert.match(treeText(renderer), /同步中…/, '请求中应显示「同步中…」')
})
