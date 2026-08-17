// 「任务页面响应式列隐藏」测试（issue #70）：宽度不够时按优先级隐藏
// 尝试→来源→创建时间→失败原因→用时列，操作列最右侧出现「⋯」按钮，
// 点击弹出右侧抽屉显示该任务全部数据。
//
// 断言：
// 1. hiddenColumnsForWidth 纯函数：各断点区间的隐藏列集合与优先级顺序；
// 2. 异常宽度输入（NaN/undefined/0/负数）按最窄处理（5 列全隐藏）；
// 3. 渲染：窄视口（mock window.innerWidth）下对应列加 col-hidden、
//    ⋯ 按钮出现、表格 min-width 缩减为剩余列宽总和；
// 4. 渲染：宽视口下无隐藏列、无 ⋯ 按钮；
// 5. 点击 ⋯ → 右侧抽屉显示全部字段（含被隐藏列数据），字段缺失显示「—」；
// 6. JS 列宽常量与 styles.css 的 th:nth-child(n) 列宽规则一致（防双源漂移）；
// 7. contentWidthAt 与 styles.css 的 --content-width 媒体查询断点一致；
// 8. styles.css 含 col-hidden 与 drawer 样式规则。
import { after, mock, test } from 'node:test'

// 渲染树节点 → 纯文本（递归；Lucide 图标等元素无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tasksSrc = readFileSync(path.join(ROOT, 'src/pages/Tasks.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 stop-all-button.test.mjs 一致）。
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
const {
  default: Tasks,
  hiddenColumnsForWidth,
  contentWidthAt,
  HIDDEN_COL_PRIORITY,
  TABLE_MIN_WIDTH,
} = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('react-router-dom')

after(() => vite.close())

// ---- styles.css 常量提取（与 tasks-table-fit-content.test.mjs 风格一致）----

// 提取 .table.tasks-table th:nth-child(n) 的 px 列宽
function colWidth(css, n) {
  const rule = css.match(
    new RegExp(`\\.table\\.tasks-table\\s+th:nth-child\\(${n}\\)\\s*\\{([^}]*)\\}`),
  )
  assert.ok(rule, `styles.css 缺少 th:nth-child(${n}) 列宽规则`)
  const m = rule[1].match(/width\s*:\s*(\d+)px/)
  assert.ok(m, `th:nth-child(${n}) 缺少 px 宽度`)
  return Number(m[1])
}

// 提取 :root 默认 --content-width
function defaultContentWidth(css) {
  const rootBlock = css.match(/:root\s*\{[^}]*\}/)
  assert.ok(rootBlock, 'styles.css 缺少 :root 变量块')
  const m = rootBlock[0].match(/--content-width:\s*(\d+)px/)
  assert.ok(m, ':root 缺少 --content-width 变量')
  return Number(m[1])
}

// 提取所有 (min-width, --content-width) 断点，按视口阈值升序。
// 断点两种形式：固定值（如 --content-width: 1000px）与动态跟随
// （issue #98：--content-width: max(1440px, calc(100vw - 100px))），
// 动态断点记录下限 width 与总边距 dynamic（单边 = dynamic/2）。
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

// 模拟指定视口宽度下生效的 --content-width；
// 动态断点 = max(下限, 视口 − 总边距)，与浏览器对 max()/calc(100vw−Npx) 的计算一致
function contentWidthAtCss(viewport, breaks, defaultWidth) {
  let width = defaultWidth
  for (const b of breaks) {
    if (viewport >= b.min) width = b.dynamic ? Math.max(b.width, viewport - b.dynamic) : b.width
  }
  return width
}

// ---- 源码断言 ----

test('任务页源码含响应式列隐藏与 ⋯ 抽屉实现', () => {
  assert.match(tasksSrc, /hiddenColumnsForWidth/, '应有列隐藏计算纯函数')
  assert.match(tasksSrc, /col-hidden/, '隐藏列应加 col-hidden 类')
  assert.match(tasksSrc, /⋯/, '有隐藏列时操作列应有「⋯」按钮')
  assert.match(tasksSrc, /drawer/, '应有右侧抽屉')
  assert.match(tasksSrc, /addEventListener\('resize'/, '应监听窗口 resize 重新计算隐藏列')
})

// ---- 纯函数：正常路径与断点边界 ----

test('hiddenColumnsForWidth：视口 ≥1440 显示全部 12 列', () => {
  // 动态断点（issue #98）下宽屏可用宽度 = 视口 − 180 ≥ 1360，仍全显
  for (const vp of [1440, 1500, 1540, 1600, 1919, 1920, 2542, 2560, 4000]) {
    assert.deepEqual(
      [...hiddenColumnsForWidth(vp)],
      [],
      `视口 ${vp}px 应显示全部列`,
    )
  }
})

test('hiddenColumnsForWidth：断点边界精确匹配各档隐藏列', () => {
  // 隐藏档位由内容区断点与列宽推导：可用宽度 = min(--content-width, 视口) − 80px，
  // 按优先级隐藏直到剩余列宽 ≤ 可用宽度（1360/1272/1196/1031/891/771）。
  // 内容区断点（styles.css）：≥1440→1440（0 列）、≥1360→1360（1 列）、
  // ≥1280→1280（2 列）、≥1120→1120（3 列）、≥1000→1000（4 列）、
  // 默认 1100：视口 971~999 时 4 列、851~970 时 5 列、<851 全隐藏后滚动兜底
  const cases = [
    // [视口宽度, 期望隐藏的列（按优先级顺序）]
    [1439, ['attempt']],
    [1360, ['attempt']], // 可用 1280：恰好装下剩余 1272
    [1359, ['attempt', 'source']],
    [1280, ['attempt', 'source']], // 可用 1200：恰好装下剩余 1196
    [1279, ['attempt', 'source', 'created']],
    [1120, ['attempt', 'source', 'created']], // 可用 1040：恰好装下剩余 1031
    [1119, ['attempt', 'source', 'created', 'reason']],
    [1000, ['attempt', 'source', 'created', 'reason']], // 可用 920：装下剩余 891
    [999, ['attempt', 'source', 'created', 'reason']],
    [971, ['attempt', 'source', 'created', 'reason']], // 可用 891：恰好装下
    [970, ['attempt', 'source', 'created', 'reason', 'duration']],
    [851, ['attempt', 'source', 'created', 'reason', 'duration']], // 可用 771：恰好装下
    [850, ['attempt', 'source', 'created', 'reason', 'duration']], // 全隐藏后横向滚动兜底
  ]
  for (const [vp, expected] of cases) {
    assert.deepEqual(
      [...hiddenColumnsForWidth(vp)],
      expected,
      `视口 ${vp}px 的隐藏列应严格按优先级顺序`,
    )
  }
})

test('hiddenColumnsForWidth：异常输入（NaN/undefined/0/负数）按最窄处理全隐藏', () => {
  for (const bad of [NaN, undefined, null, 0, -1, -500]) {
    assert.equal(
      hiddenColumnsForWidth(bad).size,
      HIDDEN_COL_PRIORITY.length,
      `输入 ${String(bad)} 应按最窄处理（${HIDDEN_COL_PRIORITY.length} 列全隐藏）`,
    )
  }
})

test('HIDDEN_COL_PRIORITY：宽度与 styles.css 列宽规则一致（防双源漂移）', () => {
  // 表格列序：1#、2仓库、3Issue、4标题、5状态、6尝试、7来源、8失败原因、
  // 9提交、10创建时间、11用时、12操作 —— 与 th:nth-child 一一对应
  const nth = { attempt: 6, source: 7, reason: 8, created: 10, duration: 11 }
  assert.equal(HIDDEN_COL_PRIORITY.length, 5, '隐藏优先级应含 5 列')
  for (const c of HIDDEN_COL_PRIORITY) {
    assert.equal(
      c.width,
      colWidth(styles, nth[c.key]),
      `「${c.label}」列 JS 宽度应与 styles.css th:nth-child(${nth[c.key]}) 一致`,
    )
  }
  // 12 列宽度总和（table-wrap.test.mjs 同源断言）
  let total = 0
  for (let n = 1; n <= 12; n++) total += colWidth(styles, n)
  assert.equal(TABLE_MIN_WIDTH, total, 'TABLE_MIN_WIDTH 应等于 12 列宽度总和')
})

test('contentWidthAt：与 styles.css 的 --content-width 断点一致', () => {
  const breaks = contentBreakpoints(styles)
  const def = defaultContentWidth(styles)
  // 覆盖固定断点区间与动态断点两侧边界（1539/1540）、issue #98 用户场景（2542）
  for (const vp of [320, 800, 1100, 1439, 1440, 1539, 1540, 1600, 1919, 1920, 2542, 2560, 4000]) {
    assert.equal(
      contentWidthAt(vp),
      contentWidthAtCss(vp, breaks, def),
      `视口 ${vp}px 的 --content-width 应与 styles.css 断点推导一致`,
    )
  }
})

// ---- styles.css 样式规则断言 ----

test('styles.css 含 col-hidden 隐藏规则与 drawer 抽屉样式', () => {
  assert.match(
    styles,
    /\.table\.tasks-table\s+th\.col-hidden,[\s\S]*?td\.col-hidden\s*\{[^}]*display:\s*none/,
    '隐藏列应 display:none（列保留 DOM 保持 nth-child 索引）',
  )
  assert.match(styles, /\.drawer-overlay\s*\{/, '应有抽屉遮罩样式')
  assert.match(styles, /\.drawer\s*\{/, '应有抽屉面板样式')
})

// ---- 组件渲染 ----

function mkTask(overrides = {}) {
  return {
    id: 3, repo_id: 1, repo_name: 'demo', issue_iid: 9,
    issue_title: '修复登录问题', issue_url: 'https://gitlab.example.com/demo/-/issues/9',
    status: 'failed', attempt_count: 3, triggered_by: 'webhook',
    exit_code: 1, error_message: '重试耗尽后仍失败', error_detail: null,
    resumed: false, commit_sha: 'abc1234', commit_url: 'https://gitlab.example.com/demo/-/commit/abc1234',
    log_path: null,
    started_at: '2026-08-13 10:00:00', finished_at: '2026-08-13 10:30:00',
    created_at: '2026-08-13 09:50:00',
    ...overrides,
  }
}

// mock 视口宽度（SSR 环境无 window；addEventListener 为 no-op，
// 组件挂载时 effect 内会立即按 innerWidth 计算一次）
function withViewport(width) {
  globalThis.window = {
    innerWidth: width,
    addEventListener: () => {},
    removeEventListener: () => {},
  }
}

async function renderAndSettle(tasks, stats = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks, total: tasks.length, stats }
    }
    if (pathname === '/api/repos') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(Tasks)),
      )
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function colHiddenCount(renderer) {
  return renderer.root.findAll((n) =>
    typeof n.props.className === 'string' && n.props.className.includes('col-hidden'),
  ).length
}

function findDotsButtons(renderer) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => textOf(b.props.children).includes('⋯'))
}

function tableMinWidth(renderer) {
  const table = renderer.root.findByType('table')
  return table.props.style?.minWidth
}

test('窄视口（1000px）：按优先级隐藏 4 列、⋯ 按钮出现、min-width 缩减', async () => {
  withViewport(1000) // 可用 920−…= min(1100,1000)−80=920 → 隐藏 4 列（891 ≤ 920）
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    // 表头 12 列 + 数据行 12 列（display:none 不摘除 DOM，保持 nth-child 索引）
    assert.equal(renderer.root.findAllByType('th').length, 12, '表头应始终渲染 12 列')
    const hiddenTh = renderer.root
      .findAllByType('th')
      .filter((th) => String(th.props.className).includes('col-hidden'))
      .map((th) => th.props.children)
    assert.deepEqual(hiddenTh, ['尝试', '来源', '失败原因', '创建时间'],
      '隐藏列表头应为尝试/来源/失败原因/创建时间（用时仍显示）')
    assert.equal(renderer.root.findAllByType('td').length, 12, '数据行应始终渲染 12 个单元格')
    assert.equal(colHiddenCount(renderer), 8, '表头 4 个 + 数据行 4 个应加 col-hidden')
    // 剩余列宽 = 1360 − 88 − 76 − 165 − 140 = 891
    assert.equal(tableMinWidth(renderer), 891, '表格 min-width 应缩减为剩余列宽总和')
    const dots = findDotsButtons(renderer)
    assert.equal(dots.length, 1, '有隐藏列时操作列最右侧应出现 ⋯ 按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('宽视口（1600px）：无隐藏列、无 ⋯ 按钮、min-width 保持 1360', async () => {
  withViewport(1600)
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(colHiddenCount(renderer), 0, '宽视口不应隐藏任何列')
    assert.equal(findDotsButtons(renderer).length, 0, '无隐藏列时不应出现 ⋯ 按钮')
    assert.equal(tableMinWidth(renderer), 1360, 'min-width 应保持 12 列总和')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('点击 ⋯ 弹出右侧抽屉显示全部数据（含被隐藏列），字段缺失显示「—」', async () => {
  withViewport(1000)
  const { renderer, renderError } = await renderAndSettle([
    mkTask({ id: 7, status: 'failed', error_message: '重试耗尽后仍失败' }),
    mkTask({ id: 8, repo_name: null, status: 'succeeded', error_message: null, commit_url: null }),
  ])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const dots = findDotsButtons(renderer)
    assert.equal(dots.length, 2, '每行任务应有各自的 ⋯ 按钮')

    // 打开第一行（失败任务）的抽屉：被隐藏的失败原因/创建时间/来源/尝试列数据可见
    await TestRenderer.act(() => dots[0].props.onClick())
    const drawers = renderer.root.findAll((n) => n.props.className === 'drawer')
    assert.equal(drawers.length, 1, '点击 ⋯ 应打开抽屉')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('全部数据'), '抽屉标题应标注全部数据')
    assert.ok(text.includes('重试耗尽后仍失败'), '抽屉应显示被隐藏的失败原因列数据')
    assert.ok(text.includes('尝试'), '抽屉应显示尝试字段')
    assert.ok(text.includes('来源'), '抽屉应显示来源字段')
    assert.ok(text.includes('创建时间'), '抽屉应显示创建时间字段')
    assert.ok(text.includes('用时'), '抽屉应显示用时字段')

    // 关闭第一个抽屉，打开第二行（字段缺失任务）：缺失字段显示「—」
    await TestRenderer.act(() =>
      drawers[0].findByType('button').props.onClick(),
    )
    assert.equal(
      renderer.root.findAll((n) => n.props.className === 'drawer').length,
      0,
      '点关闭按钮应关闭抽屉',
    )
    await TestRenderer.act(() => findDotsButtons(renderer)[1].props.onClick())
    const text2 = JSON.stringify(renderer.toJSON())
    assert.ok(text2.includes('—'), '缺失字段（仓库/失败原因/提交）应显示「—」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    delete globalThis.window
  }
})

test('SSR 环境无 window 时按全部显示渲染（不报错）', async () => {
  const { renderer, renderError } = await renderAndSettle([mkTask()])
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.equal(colHiddenCount(renderer), 0, '无 window 时应显示全部列')
    assert.equal(findDotsButtons(renderer).length, 0)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
