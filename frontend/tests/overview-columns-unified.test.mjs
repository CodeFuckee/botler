// 概览页区块列数统一测试（issue #107，issue #114 适配）：开放 Issue 与
// CI/CD 流水线板块的网格列数应统一，以开放 Issue 板块为标准
// （auto-fit + minmax(280px, 1fr)，宽屏下 4 列一行）。
// issue #114：独立任务板块（.overview-grid）已删除，任务信息整合进
// 开放 Issue 板块 running 组的 issue 项内，不再有第三个网格板块。
//
// 修复前：.issues-list 已是自适应网格（issue #96：宽屏 4 列），
// 而 .overview-grid / .pipelines-list 仍是固定 3 列（repeat(3, 1fr)），
// 同一页面「有些是三列，有些又是 4 列」。
//
// 断言：
// 1. styles.css：两板块容器（.issues-list / .pipelines-list）均使用
//    repeat(auto-fit, minmax(Npx, 1fr))，.overview-grid 样式已删除；
// 2. 两者的 minmax 最小列宽与网格间距完全一致（以开放 issue 板块为标准，
//    一处改动不得漂移）；
// 3. .pipelines-list 不再使用固定 3 列（防回退）；
// 4. 列数模拟：相同视口宽度下两板块的 auto-fit 列数一致（宽屏 ≥4 列、
//    窄视口自动降列不溢出）；
// 5. 渲染级：多仓库流水线渲染的卡片数不丢卡（auto-fit 布局
//    与卡片渲染解耦，布局改动不丢数据）。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- styles.css 常量提取（与 overview-responsive-layout.test.mjs 风格一致）----

// 概览页两板块网格容器：开放 Issue（标准）/ CI/CD 流水线
// （issue #114：任务板块删除后不再有 .overview-grid）
const GRID_SELECTORS = ['.issues-list', '.pipelines-list']

// 提取指定选择器规则的网格定义：{ columns, min, gap }
// columns 为 grid-template-columns 原文；min/gap 解析自
// repeat(auto-fit, minmax(Npx, 1fr)) 与 gap: Npx，无法解析返回 null
function gridSpec(css, selector) {
  const m = css.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`))
  assert.ok(m, `styles.css 缺少 ${selector} 规则`)
  const body = m[1]
  const colMatch = body.match(/grid-template-columns:\s*([^;]+);/)
  const columns = colMatch ? colMatch[1].trim() : null
  const minMatch = columns && columns.match(/minmax\((\d+)px,\s*1fr\)/)
  const gapMatch = body.match(/gap:\s*(\d+)px/)
  return {
    columns,
    min: minMatch ? Number(minMatch[1]) : null,
    gap: gapMatch ? Number(gapMatch[1]) : null,
  }
}

// .content 左右 padding 之和（styles.css：padding: 24px 20px 60px）
const CONTENT_PAD_X = 40

// 提取所有 (min-width, --content-width) 断点（与
// overview-responsive-layout.test.mjs 同款正则）
function contentBreakpoints(css) {
  const breaks = []
  const re = /@media \(min-width:\s*(\d+)px\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}/g
  let m
  while ((m = re.exec(css))) {
    const cw = m[2].match(/--content-width:\s*(\d+)px/)
    if (cw) {
      breaks.push({ min: Number(m[1]), width: Number(cw[1]), dynamic: null })
    } else {
      const dyn = m[2].match(/--content-width:\s*max\((\d+)px,\s*calc\(100vw\s*-\s*(\d+)px\)\)/)
      if (dyn) {
        breaks.push({ min: Number(m[1]), width: Number(dyn[1]), dynamic: Number(dyn[2]) })
      }
    }
  }
  return breaks.sort((a, b) => a.min - b.min)
}

// 模拟指定视口宽度下生效的 --content-width（与 responsive-layout 测试一致）
function contentWidthAtCss(viewport, breaks) {
  const def = 1100
  let width = def
  for (const b of breaks) {
    if (viewport >= b.min) width = b.dynamic ? Math.max(b.width, viewport - b.dynamic) : b.width
  }
  return width
}

// 模拟 CSS auto-fit 网格列数（与 responsive-layout 测试一致）
function autoFitCols(avail, min, gap) {
  return Math.floor((avail + gap) / (min + gap))
}

// ---- 源码断言 ----

test('两板块容器均使用 auto-fit 自适应网格（以开放 Issue 板块为标准）', () => {
  for (const sel of GRID_SELECTORS) {
    const spec = gridSpec(styles, sel)
    assert.match(
      spec.columns || '',
      /repeat\(auto-fit,\s*minmax\(\d+px,\s*1fr\)\)/,
      `${sel} 应使用 repeat(auto-fit, minmax(Npx, 1fr))，与开放 Issue 板块列数标准一致`,
    )
  }
  assert.ok(!styles.includes('.overview-grid {'),
            '任务板块网格 .overview-grid 样式应已删除（issue #114）')
})

test('.pipelines-list 不再使用固定 3 列（防回退）', () => {
  const spec = gridSpec(styles, '.pipelines-list')
  assert.doesNotMatch(
    spec.columns || '',
    /repeat\(3,\s*1fr\)/,
    '.pipelines-list 不应再使用固定 3 列（与开放 Issue 板块列数不一致）',
  )
})

test('两板块 minmax 最小列宽与网格间距完全一致（一处改动不得漂移）', () => {
  const std = gridSpec(styles, '.issues-list')
  assert.ok(std.min, '.issues-list 的 minmax 应有最小列宽（开放 Issue 板块标准）')
  for (const sel of ['.pipelines-list']) {
    const spec = gridSpec(styles, sel)
    assert.ok(spec.min, `${sel} 的 grid-template-columns 应含 minmax(最小宽, 1fr)`)
    assert.equal(spec.min, std.min, `${sel} 最小列宽 ${spec.min}px 应与标准 ${std.min}px 一致`)
    assert.equal(spec.gap, std.gap, `${sel} 网格间距 ${spec.gap}px 应与标准 ${std.gap}px 一致`)
  }
})

test('列数模拟：相同视口下两板块 auto-fit 列数一致（宽屏 ≥4、窄屏降列不溢出）', () => {
  const std = gridSpec(styles, '.issues-list')
  const breaks = contentBreakpoints(styles)
  for (const vp of [1000, 1120, 1280, 1360, 1440, 1540, 1920, 2542, 2560]) {
    const avail = contentWidthAtCss(vp, breaks) - CONTENT_PAD_X
    const stdCols = autoFitCols(avail, std.min, std.gap)
    for (const sel of ['.pipelines-list']) {
      const spec = gridSpec(styles, sel)
      const cols = autoFitCols(avail, spec.min, spec.gap)
      assert.equal(
        cols,
        stdCols,
        `视口 ${vp}px 下 ${sel} 列数 ${cols} 应与开放 Issue 板块 ${stdCols} 一致`,
      )
    }
    // 宽屏（≥1280 可用宽度下开放 issue 板块可容 ≥4 列）两板块同列数
    if (vp >= 1280) {
      assert.ok(stdCols >= 4, `视口 ${vp}px 下开放 Issue 板块应至少 4 列（实际 ${stdCols}）`)
    }
    // 窄视口至少 1 列且每列 ≥ 最小宽（不产生水平溢出）
    assert.ok(stdCols >= 1, `视口 ${vp}px 下应至少 1 列`)
    const colWidth = (avail - (stdCols - 1) * std.gap) / stdCols
    assert.ok(colWidth >= std.min, `视口 ${vp}px 下每列宽 ${colWidth.toFixed(0)}px 应 ≥${std.min}px`)
  }
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

async function renderOverview({ tasks = [], pipelines = [], repos = [] } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks, total: tasks.length, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines, errors: [] }
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

function mkTask(id) {
  return {
    id, repo_id: 1, repo_name: 'botler', project_id: 123,
    issue_iid: id, issue_title: `任务 issue ${id}`, status: 'running',
    issue_url: `https://gitlab.example.com/chenkaidi/botler/-/issues/${id}`,
    engine: '',
  }
}

function mkIssue(id) {
  return {
    iid: id, title: `任务 issue ${id}`,
    updated_at: '2026-08-15 10:00:00',
    web_url: `https://gitlab.example.com/chenkaidi/botler/-/issues/${id}`,
    labels: [],
  }
}

function mkPipeline(id) {
  return {
    repo_id: id, repo_name: `repo-${id}`, enabled: true,
    commit_time: '2026-08-15 10:00:00',
    pipeline: {
      id, status: 'success', ref: 'main', sha: 'abc'.padEnd(40, '0'),
      web_url: `https://gitlab.example.com/repo-${id}/-/pipelines/${id}`,
    },
    stages: [{ name: 'test', status: 'success' }],
  }
}

function countCards(renderer, cls) {
  return renderer.root.findAll((n) => {
    const c = n.props && n.props.className
    return typeof c === 'string' && c.split(' ').includes(cls)
  }).length
}

test('渲染：多任务信息块不丢块（issue #114：任务信息整合进 issue 项内）', async () => {
  const ids = [1, 2, 3, 4, 5]
  const { renderer, renderError } = await renderOverview({
    tasks: ids.map(mkTask),
    repos: [{
      repo_id: 1, repo_name: 'botler', priority: 10,
      issues: ids.map(mkIssue),
    }],
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(countCards(renderer, 'issues-list'), 1, '应渲染 issues-list 容器')
    assert.equal(countCards(renderer, 'issue-task'), 5, '5 个运行中 issue 应渲染 5 个任务信息块')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：多仓库流水线卡片不丢卡（auto-fit 布局与渲染解耦）', async () => {
  const { renderer, renderError } = await renderOverview({ pipelines: [1, 2, 3, 4, 5].map(mkPipeline) })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(countCards(renderer, 'pipelines-list'), 1, '应渲染 pipelines-list 容器')
    assert.equal(countCards(renderer, 'pipeline-card'), 5, '5 个仓库应渲染 5 张流水线卡片')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
