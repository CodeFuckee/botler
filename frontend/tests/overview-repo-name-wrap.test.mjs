// 概览页「开放 Issue」区域仓库名完整显示测试（issue #102）：
// 竖屏平板（窄视口）下仓库卡片头 .issue-repo-head 为单行 flex，
// 仓库名 span 因 overflow:hidden + text-overflow:ellipsis +
// white-space:nowrap（flex 子项自动最小尺寸为 0）被优先级 badge、
// issue 计数、添加按钮等固定项压缩至几乎不可见，仓库名显示不出来。
//
// 修复前：.issue-repo-head 不换行；.issue-repo-name nowrap+ellipsis
// 截断，窄卡片（auto-fit 降为 1 列 ≈280px）中仓库名被压没。
// 修复后：.issue-repo-head flex-wrap: wrap 允许换行；.issue-repo-name
// 独占首行（flex-basis: 100%）+ overflow-wrap: anywhere 任意断行，
// 任何视口下仓库名（含深路径 group/subgroup/project）完整显示。
//
// 断言：
// 1. styles.css：.issue-repo-head 允许换行（flex-wrap: wrap）；
// 2. styles.css：.issue-repo-name 不再省略截断（无 nowrap/ellipsis），
//    且 overflow-wrap: anywhere 支持长路径任意断行；
// 3. styles.css：.issue-repo-name 独占首行（flex-basis: 100%），
//    badge/计数/按钮换到第二行，仓库名始终有整行宽度；
// 4. 渲染级：长仓库名（深路径）完整渲染进 issue-repo-name 节点，
//    字符不丢失。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与
// overview-responsive-layout.test.mjs 一致）。
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
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 源码断言 ----

test('styles.css：.issue-repo-head 允许换行（窄卡片中仓库名不再被压没）', () => {
  const m = styles.match(/\.issue-repo-head\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 应存在 .issue-repo-head 规则')
  assert.match(
    m[1],
    /flex-wrap:\s*wrap/,
    '头部应 flex-wrap: wrap 允许换行，否则窄卡片中仓库名被固定项压缩至不可见',
  )
})

test('styles.css：.issue-repo-name 不再 nowrap+ellipsis 截断，长路径可任意断行', () => {
  const m = styles.match(/\.issue-repo-name\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 应存在 .issue-repo-name 规则')
  assert.doesNotMatch(
    m[1],
    /white-space:\s*nowrap/,
    '仓库名不应 nowrap（nowrap 导致被 flex 压缩后省略号截断）',
  )
  assert.doesNotMatch(
    m[1],
    /text-overflow:\s*ellipsis/,
    '仓库名不应省略号截断（要求完整显示）',
  )
  assert.match(
    m[1],
    /overflow-wrap:\s*anywhere/,
    '仓库名应 overflow-wrap: anywhere——深路径（group/subgroup/project）无空格时任意断行完整显示',
  )
})

test('styles.css：.issue-repo-name 独占首行（flex-basis: 100%）', () => {
  const m = styles.match(/\.issue-repo-name\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 应存在 .issue-repo-name 规则')
  assert.match(
    m[1],
    /flex-basis:\s*100%/,
    '仓库名应 flex-basis: 100% 独占首行——badge/计数/按钮换到第二行，仓库名任何视口都有整行宽度',
  )
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

function mkRepo(id, name) {
  return {
    repo_id: id, repo_name: name, priority: id * 10,
    issues: [
      {
        iid: 1, title: '示例 issue', updated_at: '2026-08-15 10:00:00',
        web_url: `https://gitlab.example.com/${name}/-/issues/1`,
        labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
        milestone: null, assignees: [], user_notes_count: 0,
      },
    ],
  }
}

async function renderOverview(repos) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos, errors: [], total: repos.length }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function repoNameNodes(renderer) {
  return renderer.root.findAll((n) => {
    const cls = n.props && n.props.className
    return typeof cls === 'string' && cls.split(' ').includes('issue-repo-name')
  })
}

test('渲染：长仓库名（深路径）完整渲染进 issue-repo-name，字符不丢失', async () => {
  const deep = 'very-long-group-name/very-long-subgroup/very-long-project-name'
  const { renderer, renderError } = await renderOverview([mkRepo(1, deep)])
  try {
    assert.equal(renderError, null, '渲染不应抛错')
    const nodes = repoNameNodes(renderer)
    assert.equal(nodes.length, 1, '应渲染 1 个 issue-repo-name 节点')
    // JSX：<span><Icon name="folder" /> {r.repo_name}</span> → 文本应含完整仓库名
    const text = nodes[0].children.join('')
    assert.ok(
      text.includes(deep),
      `issue-repo-name 应包含完整仓库名 ${deep}（实际 ${text}），渲染层不得丢字符`,
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：多个仓库各渲染完整仓库名', async () => {
  const names = ['chenkaidi/botler', 'group/subgroup/shipyard', 'very-long-group-name/very-long-project-name']
  const { renderer, renderError } = await renderOverview(names.map((n, i) => mkRepo(i + 1, n)))
  try {
    assert.equal(renderError, null, '渲染不应抛错')
    const nodes = repoNameNodes(renderer)
    assert.equal(nodes.length, names.length, `应渲染 ${names.length} 个 issue-repo-name 节点`)
    nodes.forEach((node, i) => {
      const text = node.children.join('')
      assert.ok(
        text.includes(names[i]),
        `第 ${i + 1} 个仓库名应完整（实际 ${text}）`,
      )
    })
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
