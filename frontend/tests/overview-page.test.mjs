// 概览页面测试（issue #32）：导航入口位于「仓库」tab 左边，页面实时展示
// 正在执行的任务卡片（仓库名 / 对应 issue / agent 实时输出），多任务时以
// 最多 2 行 × 3 列网格排布（超过 6 个继续换行、页面滚动）。
//
// 断言：
// 1. App.jsx 顶部导航「概览」位于「仓库」之前，/overview 路由已注册；
// 2. Overview 页轮询 GET /api/tasks?status=running,retrying 拉活跃任务，
//    每个任务轮询 /api/tasks/{id}/execution 拉实时输出增量（字节偏移续读）；
// 3. 卡片渲染仓库名、issue 链接（task.issue_url）、实时输出尾部；
// 4. 无任务时显示空状态；任务字段缺失兜底（已删除 / — / 暂无输出）；
// 5. styles.css 提供 overview-grid 网格样式（3 列，最多 2 行装下 6 卡）；
// 6. trimLogTail 纯函数边界（空数组 / 超长截尾 / 非法 max）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 app-hooks.test.mjs 一致）。
// api 也经 vite 加载，与 Overview 组件内 import 的是同一模块实例，
// 可对 api.get 做 method mock。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, trimLogTail } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 导航与路由 ----

test('顶部导航「概览」位于「仓库」tab 左边', () => {
  const nav = app.match(/<nav[\s\S]*?<\/nav>/)[0]
  const overviewLink = nav.match(/<NavLink[^>]*to="\/overview"[^>]*>/)
  const reposLink = nav.match(/<NavLink[^>]*to="\/"[^>]*>/)
  assert.ok(overviewLink, '导航应有到 /overview 的 NavLink')
  assert.ok(reposLink, '导航应有到 / 的仓库 NavLink')
  assert.ok(
    overviewLink.index < reposLink.index,
    '「概览」NavLink 应位于「仓库」NavLink 之前（issue 要求放在仓库 tab 左边）',
  )
  assert.match(nav, /概览/, '导航链接文案应为「概览」')
})

test('App.jsx 注册 /overview 路由并挂载 Overview 页面', () => {
  assert.match(app, /Route path="\/overview" element={<Overview \/>}/, '应有 /overview 路由')
  assert.match(app, /import Overview from '\.\/pages\/Overview\.jsx'/, '应导入 Overview 页面组件')
})

// ---- Overview 数据流源码断言 ----

test('概览页一次拉取全部正在执行的任务（running+retrying）', () => {
  assert.match(overview, /status:\s*'running,retrying'/, '列表请求应带多值 status=running,retrying')
  assert.match(overview, /api\.get\('\/api\/tasks\?\s*'\s*\+\s*q/, '列表走 GET /api/tasks')
  assert.match(overview, /setInterval/, '列表应定时轮询刷新')
})

test('每个任务卡片独立订阅事件流（SSE 实时输出）', () => {
  assert.match(
    overview,
    /openTaskEventStream\(t\.id/,
    '每个任务卡片应独立订阅事件流 openTaskEventStream(t.id)',
  )
  assert.match(overview, /eventToLine/, '事件应经 eventToLine 转单行文本进卡片')
  assert.match(overview, /trimLogTail/, '日志行应经 trimLogTail 截尾防卡片无限增长')
})

test('卡片渲染仓库名、issue 链接与状态徽标', () => {
  assert.match(overview, /repo_name/, '卡片应显示仓库名')
  assert.match(overview, /（已删除）/, '仓库已删除时应显示占位符')
  assert.match(overview, /issue_url/, '卡片应使用 issue_url')
  assert.match(overview, /href=\{t\.issue_url\}/, '有链接时应渲染为可点击的 <a>')
  assert.match(overview, /issue_title/, '卡片应显示 issue 标题')
  assert.match(overview, /STATUS_META/, '卡片应显示任务状态徽标')
})

test('空状态与日志缺失兜底文案', () => {
  assert.match(overview, /当前没有正在执行的任务/, '无活跃任务时应显示空状态文案')
  assert.match(overview, /（暂无输出）/, '日志为空时应显示占位文案')
})

test('styles.css 提供概览网格样式（3 列，最多 2 行装下 6 卡）', () => {
  assert.match(styles, /\.overview-grid\s*\{/, '应有 .overview-grid 容器样式')
  assert.match(
    styles,
    /\.overview-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*1fr\)/s,
    '网格应为 3 列（2 行 × 3 列 = 最多 6 个任务卡片，超过自动换行滚动）',
  )
  assert.match(styles, /\.overview-card\s*\{/, '应有 .overview-card 卡片样式')
  assert.match(styles, /\.overview-log\s*\{/, '应有 .overview-log 实时输出样式')
})

// ---- trimLogTail 纯函数边界 ----

test('trimLogTail：行数未超上限原样返回', () => {
  const lines = ['a', 'b', 'c']
  assert.deepEqual(trimLogTail(lines, 5), lines)
  assert.deepEqual(trimLogTail([], 5), [])
})

test('trimLogTail：超上限保留最后 max 行（丢弃最旧）', () => {
  assert.deepEqual(trimLogTail(['1', '2', '3', '4', '5'], 3), ['3', '4', '5'])
  assert.equal(trimLogTail(['1', '2'], 2).length, 2)
  assert.deepEqual(trimLogTail(['1', '2', '3'], 1), ['3'])
})

test('trimLogTail：非法输入兜底（null / 非数组 / 非法 max）', () => {
  assert.deepEqual(trimLogTail(null, 5), [])
  assert.deepEqual(trimLogTail('not-array', 5), [])
  assert.deepEqual(trimLogTail(['1', '2', '3'], 0), [])
  assert.deepEqual(trimLogTail(['1', '2', '3'], -1), [])
  assert.deepEqual(trimLogTail(['1', '2', '3'], NaN), [])
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// 事件流连接 mock（SSE 实时输出）：记录实例，可手动 emit 事件驱动卡片渲染
class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.closed = false
    FakeEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
  emit(event) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(event) })
  }
}

async function renderAndSettle(impl, waitMs = 30) {
  mock.method(api, 'get', impl)
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 等待首轮列表 + 事件流订阅的 promise flush
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

test('渲染正在执行任务的卡片：仓库名、issue 链接、实时输出', async () => {
  FakeEventSource.instances = []
  const saved = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  const { renderer, renderError } = await renderAndSettle(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return {
        tasks: [{
          id: 11, repo_id: 1, repo_name: 'shipyard', project_id: 42,
          issue_iid: 7, issue_title: '修复登录问题', status: 'running',
          issue_url: 'https://gitlab.example.com/group/shipyard/-/issues/7',
        }],
        total: 1, stats: { running: 1 },
      }
    }
    throw new Error('unexpected ' + pathname)
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('shipyard'), '卡片应显示仓库名 shipyard')
    assert.ok(text.includes('修复登录问题'), '卡片应显示 issue 标题')
    // JSX 中 `#{t.issue_iid} — {title}` 是分离的文本节点（'#', 7, ' — ', title），
    // 拼接所有 span/a 文本后应组成完整的 issue 描述（有链接时渲染为 <a>）
    const issueText = root
      .findAll((n) => n.type === 'span' || n.type === 'a')
      .flatMap((n) => n.props.children || [])
      .join('')
    assert.ok(issueText.includes('#7'), `卡片应显示 issue 编号（实际文本: ${issueText}）`)
    // 每个活跃任务一个事件流连接（SSE 实时输出）
    assert.equal(FakeEventSource.instances.length, 1, '应为任务创建事件流连接')
    assert.equal(FakeEventSource.instances[0].url, '/api/tasks/11/events',
                 '事件流应订阅 /api/tasks/{id}/events')
    // 推送事件 → 卡片实时输出
    await TestRenderer.act(async () => {
      FakeEventSource.instances[0].emit({ seq: 1, kind: 'text', text: '正在分析 bug…' })
      FakeEventSource.instances[0].emit({ seq: 2, kind: 'tool', tool: 'Bash',
                                          input: { command: 'git status' } })
    })
    const textAfter = JSON.stringify(renderer.toJSON())
    assert.ok(textAfter.includes('正在分析 bug…'), '卡片应显示 agent 实时输出')
    assert.ok(textAfter.includes('Bash'), '卡片应显示工具调用事件')
    assert.ok(
      root.findAllByType('a').some((a) => a.props.href?.includes('/-/issues/7')),
      'issue 应渲染为指向 GitLab 的链接',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
    globalThis.EventSource = saved
  }
})

test('无正在执行任务时显示空状态', async () => {
  const { renderer, renderError } = await renderAndSettle(async () => ({
    tasks: [], total: 0, stats: {},
  }))
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('当前没有正在执行的任务'), '应显示空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('任务字段缺失兜底：仓库已删除 / 无标题 / 无输出', async () => {
  const { renderer, renderError } = await renderAndSettle(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return {
        tasks: [{
          id: 12, repo_id: 1, repo_name: null, project_id: 42,
          issue_iid: 9, issue_title: null, status: 'retrying', issue_url: null,
        }],
        total: 1, stats: { retrying: 1 },
      }
    }
    return { status: 'retrying', log_offset: 0, log_delta: [], transcript: [] }
  })
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('（已删除）'), '仓库名缺失应显示占位')
    assert.ok(text.includes('（暂无输出）'), '日志为空应显示占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('API 失败时显示错误提示而不崩溃', async () => {
  const { renderer, renderError } = await renderAndSettle(async () => {
    throw new Error('网络错误')
  })
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('网络错误'), '应显示 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})
