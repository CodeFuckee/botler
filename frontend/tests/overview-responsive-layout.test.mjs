// 概览页响应式布局测试（issue #96、#98）：宽度足够时开放 issue 板块的
// 4 个仓库卡片应一行放下，宽屏内容区充分利用横向空间，两旁只留少量边距。
//
// 修复前：.issues-list 固定 3 列（repeat(3, 1fr)），4 个仓库卡片
// 3+1 分两行；.content 宽屏封顶 1600px（≥1920 视口），2K（2560）屏
// 两边各留 480px 空白。
// issue #96 修复后：.issues-list 自适应列数（auto-fit + minmax(280px,
// 1fr)），宽度足够时 4 卡一行；宽屏断点 ≥1920→1840px、≥2560→2480px。
// issue #98 进一步优化：固定断点（1840/2480）导致 1920~2559 视口区间
// 内容区恒定、留白随视口增宽（2542 视口单边 351px），改为动态跟随
// 视口 max(1440px, calc(100vw - 100px))，≥1540 视口单边恒 50px。
//
// 断言：
// 1. styles.css：.issues-list 使用 auto-fit 自适应网格，列最小宽 280px；
// 2. styles.css：≥1440 宽屏断点动态跟随视口（单边恒 50px），1440~1539
//    平滑过渡下限 1440，无 1840/2480 固定宽屏档（防回退）；
// 3. 模拟 auto-fit 列数：1280/1360/1440/1540/1920/2542/2560 视口下可容
//    ≥4 列（4 卡一行），且每列宽 ≥280 不挤压；窄视口（1000/1120）自动
//    降为 3 列回退；
// 4. Tasks.jsx contentWidthAt 与 CSS 宽屏动态断点同步（新值显式断言防回退）；
// 5. 渲染级：4 个仓库渲染 4 张 issue-repo-card；5 个仓库渲染 5 张
//    （auto-fit 多出的自动换行，不丢卡不崩溃）。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）。
// Tasks.jsx 引用 react-router-dom，与 tasks-responsive-cols.test.mjs 一致
// alias 到 mock-router 避免 SSR 加载真实路由库
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
const { contentWidthAt } = await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- styles.css 常量提取（与 tasks-responsive-cols.test.mjs 风格一致）----

// 解析 var(--space-N) 引用为 :root token 的 px 值（issue #111 间距 token 化）
function resolveSpaceVar(css, ref) {
  const m = ref.match(/^var\((--space-\d+)\)$/)
  if (!m) return null
  const root = css.match(/:root\s*\{([^}]*)\}/s)
  assert.ok(root, 'styles.css 缺少 :root token 定义')
  const v = root[1].match(new RegExp(`${m[1]}:\\s*(\\d+)px`))
  assert.ok(v, `styles.css 缺少 ${m[1]} token 定义`)
  return Number(v[1])
}

// .content 左右 padding 之和（styles.css：padding 引用 --gutter，桌面 20px）
const CONTENT_PAD_X = 2 * resolveSpaceVar(styles, 'var(--gutter)')

// issues-list 网格间距（styles.css 提取，防漂移；支持 px 字面量与 token 引用）
function issuesListGap(css) {
  const m = css.match(/\.issues-list\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 缺少 .issues-list 规则')
  const gap = m[1].match(/gap:\s*(?:(\d+)px|(var\(--space-\d+\)))/)
  assert.ok(gap, '.issues-list 缺少 px 间距')
  return gap[1] ? Number(gap[1]) : resolveSpaceVar(css, gap[2])
}

// .issues-list 的 minmax 最小列宽（styles.css 提取，防漂移）
function issuesListMinCol(css) {
  const m = css.match(/\.issues-list\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 缺少 .issues-list 规则')
  const min = m[1].match(/minmax\((\d+)px,\s*1fr\)/)
  assert.ok(min, '.issues-list 的 grid-template-columns 缺少 minmax(最小宽, 1fr)')
  return Number(min[1])
}

// 提取所有 (min-width, --content-width) 断点，按视口阈值升序
// （与 tasks-responsive-cols.test.mjs 同款正则）。
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
function contentWidthAtCss(viewport, breaks) {
  const def = 1100
  let width = def
  for (const b of breaks) {
    if (viewport >= b.min) width = b.dynamic ? Math.max(b.width, viewport - b.dynamic) : b.width
  }
  return width
}

// 模拟 CSS auto-fit 网格列数：n = floor((可用宽 + gap) / (min + gap))，
// 与浏览器对 repeat(auto-fit, minmax(min, 1fr)) 的轨道数计算一致。
// 少于轨道数的卡片由 auto-fit 收起空轨道，实际列数 = min(卡片数, n)。
function autoFitCols(avail, min, gap) {
  return Math.floor((avail + gap) / (min + gap))
}

// ---- 源码断言 ----

test('styles.css：.issues-list 使用 auto-fit 自适应列网格（宽度足够时 4 卡一行）', () => {
  const m = styles.match(/\.issues-list\s*\{([^}]*)\}/)
  assert.ok(m, 'styles.css 应存在 .issues-list 规则')
  assert.match(
    m[1],
    /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(\d+px,\s*1fr\)\)/,
    '应使用 repeat(auto-fit, minmax(Npx, 1fr)) 自适应列数，而非固定 3 列',
  )
  assert.doesNotMatch(
    m[1],
    /repeat\(3,\s*1fr\)/,
    '不应再使用固定 3 列（4 个仓库会 3+1 分两行）',
  )
})

test('styles.css：≥1440 宽屏断点动态跟随视口，单边留白恒 50px（issue #98）', () => {
  const breaks = contentBreakpoints(styles)
  const wide = breaks.find((b) => b.min === 1440)
  assert.ok(wide, '应存在 ≥1440 宽屏断点')
  assert.ok(wide.dynamic, '≥1440 断点应动态跟随视口（max(1440px, calc(100vw - 100px))）')
  assert.equal(
    wide.width,
    1440,
    '动态断点下限应为 1440px（任务表格 min-width 1360px + .content/.card padding 80px 恰好装下）',
  )
  assert.equal(wide.dynamic, 100, '动态断点总边距应为 100px（单边 50px）')
  // ≥1540 视口：内容区 = 视口 − 100，单边留白恒为 50px。
  // 2542 为 issue #98 用户场景（2K 屏窗口化视口）：原固定 1840 档单边留白 351px。
  for (const vp of [1540, 1920, 2542, 2560, 3200, 4000]) {
    const cw = contentWidthAtCss(vp, breaks)
    assert.equal(cw, vp - 100, `视口 ${vp}px 内容区应为 ${vp - 100}px`)
    assert.equal((vp - cw) / 2, 50, `视口 ${vp}px 单边留白应为 50px`)
  }
  // 1440~1539 平滑过渡：内容区取下限 1440，单边留白 0~49.5px 无跳变
  assert.equal(contentWidthAtCss(1440, breaks), 1440, '1440 视口内容区应为 1440px')
  assert.equal(contentWidthAtCss(1539, breaks), 1440, '1539 视口内容区仍应为 1440px（单边 49.5px）')
  // 断点内容区宽度随视口非降（宽屏档不得窄于前一档）
  for (let i = 1; i < breaks.length; i++) {
    assert.ok(
      breaks[i].width >= breaks[i - 1].width,
      `断点 ${breaks[i].min} 的内容区 ${breaks[i].width} 不应窄于前一档 ${breaks[i - 1].width}`,
    )
  }
  // 防回退：≥1440 不应再有固定宽度断点（1840/2480 档已废弃）
  const fixedWide = breaks.filter((b) => b.min >= 1440 && !b.dynamic)
  assert.equal(fixedWide.length, 0, '≥1440 不应存在固定宽度断点（应统一由动态断点覆盖）')
})

test('auto-fit 列数：宽屏视口下 4 个仓库一行放下，每列宽 ≥280 不挤压', () => {
  const gap = issuesListGap(styles)
  const min = issuesListMinCol(styles)
  const breaks = contentBreakpoints(styles)
  // 4 个仓库卡片一行放下：轨道数 ≥4 且每列宽 ≥ 最小宽
  // （1540 为动态断点接管边界、2542 为 issue #98 用户场景视口）
  for (const vp of [1280, 1360, 1440, 1540, 1920, 2542, 2560]) {
    const avail = contentWidthAtCss(vp, breaks) - CONTENT_PAD_X
    const cols = autoFitCols(avail, min, gap)
    const colWidth = (avail - (cols - 1) * gap) / cols
    assert.ok(
      cols >= 4 && colWidth >= min,
      `视口 ${vp}px 下应至少 4 列（实际 ${cols} 列、每列 ${colWidth.toFixed(0)}px），4 个仓库一行放下`,
    )
  }
})

test('auto-fit 列数：窄视口自动降列回退，不产生水平溢出', () => {
  const gap = issuesListGap(styles)
  const min = issuesListMinCol(styles)
  const breaks = contentBreakpoints(styles)
  for (const vp of [1000, 1120]) {
    const avail = contentWidthAtCss(vp, breaks) - CONTENT_PAD_X
    const cols = autoFitCols(avail, min, gap)
    const colWidth = (avail - (cols - 1) * gap) / cols
    assert.ok(
      cols >= 1 && colWidth >= min,
      `视口 ${vp}px 下应至少 1 列且每列 ≥${min}px（实际 ${cols} 列、每列 ${colWidth.toFixed(0)}px）`,
    )
  }
})

// ---- contentWidthAt 与 CSS 断点同步（新值显式断言，防回退到固定宽屏档）----

test('contentWidthAt：与 styles.css 宽屏动态断点同步（≥1540 单边恒 50px）', () => {
  assert.equal(contentWidthAt(1920), 1820, '1920 视口内容区应为 1820px（单边 50px）')
  assert.equal(contentWidthAt(1919), 1819, '1919 视口内容区应为 1819px（单边 50px）')
  // 下限边界：1539 视口 max(1440, 1439) = 1440（单边 49.5px）
  assert.equal(contentWidthAt(1539), 1440, '1539 视口内容区取下限 1440px')
  // issue #98 用户场景：2542 视口内容区 2442（原固定 1840 档单边留白 351px）
  assert.equal(contentWidthAt(2542), 2442, '2542 视口内容区应为 2442px（单边 50px）')
  assert.equal(contentWidthAt(2560), 2460, '2560 视口内容区应为 2460px（单边 50px）')
  assert.equal(contentWidthAt(2559), 2459, '2559 视口内容区应为 2459px（单边 50px）')
  // 超大视口不再封顶 2480，单边仍为 50px
  assert.equal(contentWidthAt(4000), 3900, '超大视口应动态跟随（4000−100），不再封顶 2480px')
  // 与 CSS 断点推导动态对比（覆盖动态断点两侧边界与用户场景视口）
  const breaks = contentBreakpoints(styles)
  for (const vp of [1280, 1439, 1440, 1539, 1540, 1919, 1920, 2542, 2560, 3200, 4000]) {
    assert.equal(
      contentWidthAt(vp),
      contentWidthAtCss(vp, breaks),
      `视口 ${vp}px 的 contentWidthAt 应与 styles.css 断点推导一致`,
    )
  }
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

function mkRepo(id, name) {
  return {
    repo_id: id, repo_name: name, priority: id * 10,
    issues: [
      {
        iid: 1, title: '示例 issue', updated_at: '2026-08-15 10:00:00',
        web_url: `https://gitlab.example.com/chenkaidi/${name}/-/issues/1`,
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

function repoCardCount(renderer) {
  return renderer.root.findAll((n) => {
    const cls = n.props && n.props.className
    return typeof cls === 'string' && cls.split(' ').includes('issue-repo-card')
  }).length
}

test('渲染：4 个仓库渲染 4 张仓库卡片（不丢卡不崩溃，布局交由 CSS auto-fit 一行容纳）', async () => {
  const repos = [mkRepo(1, 'botler'), mkRepo(2, 'shipyard'), mkRepo(3, 'hermes'), mkRepo(4, 'dsh')]
  const { renderer, renderError } = await renderOverview(repos)
  try {
    assert.equal(renderError, null, '渲染不应抛错')
    assert.equal(repoCardCount(renderer), 4, '4 个仓库应渲染 4 张 issue-repo-card')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：5 个仓库渲染 5 张卡片（超出单行容量的由 auto-fit 自动换行，不丢卡）', async () => {
  const repos = ['a', 'b', 'c', 'd', 'e'].map((n, i) => mkRepo(i + 1, n))
  const { renderer, renderError } = await renderOverview(repos)
  try {
    assert.equal(renderError, null, '渲染不应抛错')
    assert.equal(repoCardCount(renderer), 5, '5 个仓库应渲染 5 张 issue-repo-card')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：单仓库边界渲染 1 张卡片', async () => {
  const { renderer, renderError } = await renderOverview([mkRepo(1, 'botler')])
  try {
    assert.equal(renderError, null, '渲染不应抛错')
    assert.equal(repoCardCount(renderer), 1, '1 个仓库应渲染 1 张 issue-repo-card')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
