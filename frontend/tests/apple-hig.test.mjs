// Apple HIG 设计原则落地测试（issue #110）：按 Apple Human Interface
// Guidelines 八原则（目标感/能动性/责任感/熟悉感/灵活/简洁/匠心/愉悦感）
// 重新优化全部页面后的验收断言，对应 Issue 正文「验收检查清单」：
//
// 结构与布局 / 交互与反馈
// 1. 所有交互元素有 hover/active/disabled/loading 态（熟悉感）——
//    .btn 系列 active 按下反馈、loading 态 spinner；
// 2. 空状态有图标 + 有温度文案，非裸文本（匠心/愉悦感）——
//    .empty-state 渲染（Overview 空态断言）；
// 3. 危险操作二次确认 / 弹窗可关闭（能动性）——dialog.test.mjs 已覆盖，
//    此处补 aria-label 无障碍断言；
//
// 一致性与规范
// 4. 颜色/动效/间距由 design token 统一管理（匠心）——
//    --dur/--ease-out/--space-* token 存在且时长 150–300ms；
// 5. 对比度达 WCAG AA（灵活）——浅色/深色语义色对背景对比度 ≥ 4.5:1；
//
// 无障碍与包容
// 6. 键盘全遍历 + 焦点可见（灵活）——focus-visible 覆盖交互元素；
// 7. 支持 prefers-reduced-motion（灵活）——动画/过渡降级规则；
// 8. 触控目标 ≥ 44×44px（灵活）——pointer: coarse 媒体查询；
// 9. 跟随系统深色模式（灵活，有意识应对平台）——prefers-color-scheme。
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

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const { MemoryRouter } = await vite.ssrLoadModule('/tests/helpers/mock-router.jsx')

after(() => vite.close())

// ---- WCAG AA 对比度计算（相对亮度法，WCAG 2.x 定义）----
function luminance(hex) {
  const [r, g, b] = [hex.slice(1, 3), hex.slice(3, 5), hex.slice(5, 7)].map((h) => {
    const c = parseInt(h, 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function contrastRatio(hex1, hex2) {
  const [hi, lo] = [luminance(hex1), luminance(hex2)].sort((a, b) => b - a)
  return (hi + 0.05) / (lo + 0.05)
}

// 提取 styles.css 首个 :root 块（浅色主题）
function firstRootBlock(css) {
  const m = css.match(/:root\s*\{([^}]*)\}/s)
  return m ? m[1] : ''
}
// 提取深色模式 :root 块
function darkRootBlock(css) {
  const m = css.match(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/s)
  return m ? m[1] : ''
}
// 提取块内 CSS 变量值
function varValue(block, name) {
  const m = block.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`))
  return m ? m[1] : null
}

// ---- 交互与反馈：active / loading 状态（熟悉感/愉悦感）----

test('styles.css：按钮有 :active 按下反馈（微交互，不喧宾夺主）', () => {
  assert.match(styles, /\.btn:not\(:disabled\):active\s*\{/, '普通按钮应有 active 按下态')
  assert.match(styles, /\.btn-primary:not\(:disabled\):active\s*\{/, '主按钮应有 active 按下态')
  assert.match(styles, /\.btn-danger:not\(:disabled\):active\s*\{/, '危险按钮应有 active 按下态')
})

test('styles.css：导航链接有 hover/active 双态', () => {
  assert.match(styles, /\.navlink:hover\s*\{/, '导航链接应有 hover 态')
  assert.match(styles, /\.navlink:active\s*\{/, '导航链接应有 active 态')
})

test('styles.css：加载态提供 spinner 与 loading-hint（非裸文本）', () => {
  assert.match(styles, /\.spinner\s*\{/, '应提供 .spinner 加载指示器')
  assert.match(styles, /@keyframes\s+spin\s*\{/, 'spinner 应有旋转动画')
  assert.match(styles, /\.loading-hint\s*\{/, '应提供 .loading-hint 行内加载态')
  assert.match(styles, /\.app-loading\s*\{/, '应提供整页加载态容器')
})

// ---- 匠心：design token 统一管理 ----

test('styles.css：动效 token 统一管理且时长在 HIG 建议 150–300ms 区间', () => {
  const block = firstRootBlock(styles)
  const durMatch = block.match(/--dur:\s*(\d+)ms/)
  const durFastMatch = block.match(/--dur-fast:\s*(\d+)ms/)
  assert.ok(durMatch, '--dur 应为 ms 时长值')
  assert.ok(durFastMatch, '--dur-fast 应为 ms 时长值')
  const dur = Number(durMatch[1])
  const fast = Number(durFastMatch[1])
  assert.ok(dur >= 150 && dur <= 300, `--dur 应在 150–300ms 区间（实际 ${dur}ms）`)
  assert.ok(fast >= 150 && fast <= 300, `--dur-fast 应在 150–300ms 区间（实际 ${fast}ms）`)
  assert.match(block, /--ease-out:\s*cubic-bezier/, '应定义 ease-out 缓动 token')
  assert.match(block, /--space-6:\s*32px/, '应定义 4px/8px 网格间距 token（--space-6: 32px）')
})

test('styles.css：散落硬编码中性色统一为 token（深色模式可随变量翻转）', () => {
  const block = firstRootBlock(styles)
  for (const name of ['bg-soft', 'bg-hover-soft', 'hairline', 'overlay', 'on-primary', 'primary-strong']) {
    assert.ok(block.includes(`--${name}:`), `应定义 --${name} token`)
  }
  // 阶段图节点色 token 化（原 #c9c9c9/#8a8a8a/#dcdcdc 硬编码）
  for (const name of ['stage-pending', 'stage-canceled', 'stage-skipped']) {
    assert.ok(block.includes(`--${name}:`), `应定义 --${name} token`)
  }
  // 使用点已替换为 var() 引用（不再散落 rgba(0, 0, 0, x) 中性底）
  assert.match(styles, /\.status-queued\s*\{\s*background:\s*var\(--bg-soft\)/,
               '状态徽章应引用 --bg-soft token')
  assert.match(styles, /\.drawer-overlay\s*\{[\s\S]*?background:\s*var\(--overlay\)/,
               '抽屉遮罩应引用 --overlay token')
  assert.match(styles, /\.btn-primary\s*\{[\s\S]*?color:\s*var\(--on-primary\)/,
               '主按钮文字应引用 --on-primary token')
})

// ---- 灵活：WCAG AA 对比度 ----

test('浅色主题语义色文字对白底对比度达 WCAG AA（≥4.5:1）', () => {
  const block = firstRootBlock(styles)
  for (const name of ['ok', 'warn', 'err', 'muted']) {
    const hex = varValue(block, name)
    assert.ok(hex, `浅色主题应定义 --${name}`)
    const ratio = contrastRatio('#ffffff', hex)
    assert.ok(ratio >= 4.5, `--${name}(${hex}) 对白底对比度 ${ratio.toFixed(2)}:1 应 ≥ 4.5:1`)
  }
})

test('深色主题语义色文字对深底对比度达 WCAG AA（≥4.5:1）', () => {
  const block = darkRootBlock(styles)
  assert.ok(block, '应存在 prefers-color-scheme: dark 主题块')
  for (const name of ['ok', 'warn', 'err', 'muted']) {
    const hex = varValue(block, name)
    assert.ok(hex, `深色主题应定义 --${name}`)
    const ratio = contrastRatio('#0a0a0a', hex)
    assert.ok(ratio >= 4.5, `深色 --${name}(${hex}) 对深底对比度 ${ratio.toFixed(2)}:1 应 ≥ 4.5:1`)
  }
})

// ---- 灵活：焦点可见 / 减弱动画 / 触控目标 / 深色模式 ----

test('styles.css：交互元素焦点可见（键盘 Tab 全遍历）', () => {
  for (const cls of ['section-toggle', 'issue-link', 'add-method', 'remote-option',
                     'label-choice', 'folder-hidden-toggle', 'modal-close']) {
    assert.match(styles, new RegExp(`\\.${cls}:focus-visible`),
                 `.${cls} 应有 focus-visible 焦点样式`)
  }
  // 焦点环本身由 token 管理
  assert.ok(firstRootBlock(styles).includes('--focus-ring:'), '焦点环应 token 化')
})

test('styles.css：prefers-reduced-motion 下禁用/减弱动画（HIG 灵活）', () => {
  assert.match(styles, /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{/,
               '应存在 reduced-motion 媒体查询')
  const m = styles.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(m, 'reduced-motion 规则体应存在')
  assert.match(m[1], /animation-duration:\s*0\.01ms\s*!important/,
               '动画时长应降为 0.01ms')
  assert.match(m[1], /transition-duration:\s*0\.01ms\s*!important/,
               '过渡时长应降为 0.01ms')
})

test('styles.css：触控设备按钮最小触控目标 44px（HIG 灵活）', () => {
  assert.match(styles, /@media\s*\(pointer:\s*coarse\)\s*\{/, '应存在触控指针媒体查询')
  const m = styles.match(/@media\s*\(pointer:\s*coarse\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(m, '触控目标规则体应存在')
  assert.match(m[1], /min-height:\s*44px/, '触控设备下按钮最小高度应为 44px')
})

test('styles.css：跟随系统深色模式（prefers-color-scheme，HIG 有意识应对平台）', () => {
  const dark = darkRootBlock(styles)
  assert.ok(dark, '应存在深色主题变量块')
  assert.match(dark, /--bg:\s*#000000/, '深色主题应翻转 --bg')
  assert.match(dark, /--bg-card:\s*#0a0a0a/, '深色主题应翻转 --bg-card')
  assert.match(dark, /--text:\s*#ededed/, '深色主题应翻转 --text')
})

// ---- 匠心/愉悦感：空状态组件渲染（图标 + 文案，非裸文本）----

// Overview 挂载后同时轮询三个端点，mock 按路径分流（与 overview-issues.test.mjs 同法）
async function renderOverviewEmpty() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [], total: 0 }
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

test('Overview 空状态：图标 + 文案渲染（非裸文本，HIG 匠心/愉悦感）', async () => {
  const { renderer, renderError } = await renderOverviewEmpty()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 两板块空态均应渲染 .empty-state 容器（图标 + 文案结构）。
    // issue #114：独立任务板块删除后任务信息整合进开放 Issue 板块，
    // 空态仅剩开放 Issue 与 CI/CD 流水线两处
    const emptyStates = root.findAll((n) => String(n.props.className || '').includes('empty-state'))
    assert.equal(emptyStates.length, 2, '开放 Issue/流水线两板块空态均应有 empty-state 容器')
    // 每个空态含图标（aria-hidden 装饰性）与文案
    const icons = root.findAll((n) => String(n.props.className || '').includes('empty-icon'))
    assert.equal(icons.length, 2, '每个空态应有图标')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无开放 issue'), '保留空态文案「暂无开放 issue」')
    assert.ok(text.includes('暂无流水线'), '保留空态文案「暂无流水线」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 匠心：App 加载态 spinner ----

test('App 认证检测期间渲染 spinner 加载态（非裸文本）', async () => {
  // fetch 挂起 → auth 检测永不完成 → 停留在加载态
  const origFetch = globalThis.fetch
  globalThis.fetch = () => new Promise(() => {})
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(MemoryRouter, null, React.createElement(App)))
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const root = renderer.root
    const spinners = root.findAll((n) => String(n.props.className || '').includes('spinner'))
    assert.ok(spinners.length >= 1, '认证检测期间应渲染 spinner')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('加载中'), '应保留加载文案')
  } finally {
    globalThis.fetch = origFetch
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 灵活：无障碍 aria 语义 ----

test('App 导航提供 aria-label 语义（屏幕阅读器友好）', () => {
  const { readFileSync: rfs } = { readFileSync }
  const appSrc = rfs(path.join(ROOT, 'src/App.jsx'), 'utf8')
  assert.match(appSrc, /aria-label="主导航"/, '顶部导航应有 aria-label')
})

test('弹窗关闭按钮提供 aria-label（图标按钮无障碍）', () => {
  for (const file of ['src/components/DialogHost.jsx', 'src/components/AddIssueModal.jsx',
                      'src/components/RepoEditModal.jsx', 'src/components/FolderPicker.jsx',
                      'src/components/IssueDrawer.jsx']) {
    const src = readFileSync(path.join(ROOT, file), 'utf8')
    assert.match(src, /aria-label="关闭/, `${file} 的关闭按钮应有 aria-label`)
  }
})
