// Apple HIG 布局原则落地测试（issue #111）：按 Issue 正文「布局 (Layout)」
// 章节与「给 Agent 的实现检查清单」逐条验收：
//
// 1. 8pt 网格：布局间距（padding/margin/gap）不出现随机像素值——
//    内容外边距 token 化（--gutter：桌面 20pt / 窄屏 16pt 回落，对应
//    HIG 通用数值速查「iOS 默认约 16pt、大屏 20pt」）；
// 2. 视觉层次：概览页两大板块（开放 Issue / CI/CD 流水线）结构对齐——
//    都有 section 容器 + h2 标题（issue #114：独立任务板块删除，
//    任务信息整合进开放 Issue 板块 running 组的 issue 项内）；
// 3. 布局响应式：窗口缩放不截断不重叠——顶导航窄视口可横向滚动、
//    设置页 kv 表格窄视口列宽降级、内容区外边距窄屏回落；
// 4. 不在内容流里放全宽按钮：.btn 系列无 width:100% 规则；
// 5. 导航层浮于内容之上：.topnav 为 sticky 定位。
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
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/IssueListSection.jsx'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/PipelineSection.jsx'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-*.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// 提取 styles.css 中指定选择器的规则体（取第一个匹配块）
function ruleBody(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = styles.match(new RegExp(esc + '\\s*\\{([^}]*)\\}', 's'))
  return m ? m[1] : null
}

// 提取规则体内某属性的首个值（如 padding: 8px 16px → '8px 16px'）
function propValue(body, prop) {
  const m = body && body.match(new RegExp(prop + ':\\s*([^;]+)'))
  return m ? m[1].trim() : null
}

// 值是否全部落在 4px 网格（4 的倍数）。
// var(--space-N) 引用视为合法（token 定义即 4px 网格值），
// 其余 px 数值逐个检查必须为 4 的倍数。
function onGrid(value) {
  const remainder = value.replace(/var\(--space-\d+\)/g, '')
  const nums = (remainder.match(/-?\d+(?:\.\d+)?px/g) || [])
  return nums.every((v) => Number.parseFloat(v) % 4 === 0)
}

// ---- 检查清单 #2：8pt 网格系统管理间距 ----

test('styles.css：内容外边距 token 化（--gutter 桌面 20pt / 窄屏 16pt 回落，HIG 数值速查）', () => {
  const rootBlock = styles.match(/:root\s*\{([^}]*)\}/s)
  assert.ok(rootBlock, '应存在 :root 变量块')
  assert.match(rootBlock[1], /--gutter:\s*20px/,
    '--gutter 应定义桌面内容外边距 20px（HIG 大屏 20pt）')
  const narrow = styles.match(/@media\s*\(max-width:\s*\d+px\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(narrow, '应存在窄视口媒体查询')
  assert.match(narrow[1], /--gutter:\s*16px/,
    '窄视口 --gutter 应回落 16px（HIG iOS 默认 16pt）')
})

test('styles.css：内容容器/导航/卡片外边距引用 --gutter token', () => {
  for (const selector of ['.content', '.topnav', '.card']) {
    const body = ruleBody(selector)
    assert.ok(body, `应存在 ${selector} 规则`)
    assert.match(body, /padding:\s*[^;]*var\(--gutter\)/,
      `${selector} 的 padding 应引用 var(--gutter)`)
  }
})

test('styles.css：按钮/提示条/导航链接等控件间距落在 4px 网格（无随机像素值）', () => {
  const cases = [
    ['.btn', 'padding'],
    ['.alert', 'padding'],
    ['.navlink', 'padding'],
    ['.stat-chip', 'padding'],
    ['.chat-msg', 'padding'],
    ['.comment-item', 'padding'],
    ['.stats-row', 'gap'],
    ['.pagination', 'gap'],
    ['.label-chip', 'gap'],
    ['.repo-item', 'padding'],
  ]
  for (const [selector, prop] of cases) {
    const value = propValue(ruleBody(selector), prop)
    assert.ok(value, `${selector} 应声明 ${prop}`)
    assert.ok(onGrid(value),
      `${selector} 的 ${prop}: ${value} 应全部落在 4px 网格（8pt 网格系统）`)
  }
})

// ---- 检查清单 #4：视觉层次（组件互相对齐，传达条理和层次）----

test('源码：概览页两板块同为 section + h2 结构，独立任务板块已删除（issue #114）', () => {
  assert.ok(!overviewSrc.includes('tasks-section'),
            '任务板块容器应已删除（任务信息整合进开放 Issue 板块）')
  assert.ok(!overviewSrc.includes('<h2>正在执行的任务</h2>'),
            '任务板块 h2 标题应已删除')
  for (const section of ['issues-section', 'pipelines-section']) {
    // h2 标题紧跟 section 开头（源码中 section 与 h2 相邻）
    const m = overviewSrc.match(new RegExp(`className="${section}"[\\s\\S]{0,200}?<h2>`))
    assert.ok(m, `${section} 内应有 h2 板块标题`)
  }
})

test('渲染：概览页两板块 h2 标题齐全且自上而下为 开放 Issue → CI/CD 流水线', async () => {
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
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 两板块 h2 标题均渲染（issue #114：任务板块删除后仅剩两个板块）
    const h2s = root.findAll((n) => n.type === 'h2')
      .map((n) => String(n.children && n.children.join ? n.children.join('') : n.children))
    for (const title of ['开放 Issue', 'CI/CD 流水线']) {
      assert.ok(h2s.some((t) => t === title), `应渲染板块 h2「${title}」（实际 h2：${h2s.join(' / ')}）`)
    }
    assert.ok(!h2s.some((t) => t === '正在执行的任务'),
              '独立任务板块标题不应再渲染')
    // 自上而下顺序（渲染树深度优先 = 视觉顺序）
    const text = JSON.stringify(renderer.toJSON())
    const issuePos = text.indexOf('开放 Issue')
    const pipePos = text.indexOf('CI/CD 流水线')
    assert.ok(issuePos >= 0 && pipePos >= 0, '两板块标题都应存在')
    assert.ok(issuePos < pipePos, '「开放 Issue」应位于「CI/CD 流水线」之前')
    // 板块 h2 样式一致（styles.css 有同名规则）
    assert.match(styles, /\.issues-section h2\s*\{/, '.issues-section h2 应有板块样式')
    assert.match(styles, /\.pipelines-section h2\s*\{/, '.pipelines-section h2 应有板块样式')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 检查清单 #5：布局响应式（窗口缩放/窄视口不截断不重叠）----

test('styles.css：顶导航窄视口可横向滚动（导航项多时不截断溢出）', () => {
  const body = ruleBody('.topnav')
  assert.ok(body, '应存在 .topnav 规则')
  assert.match(body, /overflow-x:\s*auto/, '.topnav 应支持横向滚动（窄视口导航项不截断）')
  assert.match(body, /position:\s*sticky/, '.topnav 应为 sticky（HIG：导航浮于内容之上）')
  assert.match(body, /top:\s*0/, '.topnav 应吸附视口顶部')
})

test('styles.css：设置页 kv 表格窄视口列宽降级（不挤压输入控件）', () => {
  const narrow = styles.match(/@media\s*\(max-width:\s*\d+px\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(narrow, '应存在窄视口媒体查询')
  assert.match(narrow[1], /\.table\.kv th\s*\{[\s\S]*?width:\s*\d+px/,
    '窄视口下 .table.kv 标签列应有降级宽度')
})

// ---- 检查清单 #6：不在内容流里放全宽按钮 ----

test('styles.css：按钮系列无 width:100% 全宽规则（HIG：避免全宽按钮）', () => {
  // .btn 系列（含 .btn-primary/.btn-danger/.btn-sm/.btn-wide/.btn-mini）规则体内
  // 不得出现 width: 100%——按钮应遵循外边距、从一侧嵌入
  const btnRules = [...styles.matchAll(/\.btn[a-z-]*\s*\{([^}]*)\}/g)]
  assert.ok(btnRules.length > 0, '应存在 .btn 系列规则')
  for (const m of btnRules) {
    assert.ok(!/width:\s*100%/.test(m[1]), `.btn 系列规则不应有全宽按钮（规则体：${m[1].slice(0, 80)}）`)
  }
})

// ---- 检查清单 #7：滚动内容延伸到底部、导航层浮于内容之上 ----

test('styles.css：内容区底部留白充足（滚动内容延伸到底部不被遮挡）', () => {
  const value = propValue(ruleBody('.content'), 'padding')
  assert.ok(value, '.content 应声明 padding')
  assert.match(value, /60px/, '.content 底部应保留 60px 留白（HIG：可滚动布局持续滚动到底部）')
})
