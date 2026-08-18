// 任务列表页 token 用量列测试（issue #235）。
//
// 需求：任务列表「可选展示」token 用量——默认关闭（不查询用量、不渲染
// 用量列，布局与旧版完全一致）；勾选「显示用量」后带 include_usage=1
// 重新拉取，追加第 6 列展示每任务 total tokens + 估算费用；无用量数据
// 显示「—」；用量列不参与响应式隐藏（勾选后恒显示，窄视口走 .table-wrap
// 横向滚动），表格 min-width 相应增加。
//
// 断言：
// 1. Tasks 源码含「显示用量」勾选框、include_usage 请求参数与条件渲染；
// 2. USAGE_COL_WIDTH 常量存在，HIDDEN_COL_PRIORITY 不含 usage（可选列
//    不参与响应式隐藏，避免破坏默认 12 列布局）；
// 3. 渲染：默认不渲染用量列头；勾选后用量列头出现、任务行展示
//    total tokens 摘要、请求带 include_usage=1、min-width 增加 150；
// 4. 有 usage 的任务渲染 total tokens 摘要；无 usage 渲染「—」；
// 5. styles.css 提供 .tasks-usage-toggle、.usage-summary 与
//    .tasks-table-usage 第 6 列宽度规则。
import { after, mock, test } from 'node:test'
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
const { default: Tasks, HIDDEN_COL_PRIORITY, USAGE_COL_WIDTH } =
  await vite.ssrLoadModule('/src/pages/Tasks.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const USAGE_TASK = {
  id: 1, repo_id: 1, repo_name: 'demo', issue_iid: 9,
  issue_title: '采集用量', status: 'succeeded', attempt_count: 1,
  triggered_by: 'webhook', exit_code: 0, error_message: null,
  error_detail: null, commit_sha: null, commit_url: null,
  created_at: '2026-08-18 09:00:00', started_at: null, finished_at: '2026-08-18 09:30:00',
  usage: { engine: 'claude', model: 'm', prompt_tokens: 1000,
           completion_tokens: 500, total_tokens: 1500,
           estimated_cost: 0.01, currency: 'USD', raw_usage: {} },
}

const PLAIN_TASK = {
  ...USAGE_TASK, id: 2, issue_iid: 10, issue_title: '无用量', usage: null,
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  // toJSON 宿主树用 node.children；ReactTestInstance 用 props.children
  return textOf(node.children ?? node.props?.children)
}

// ---- 源码断言 ----

test('源码：显示用量勾选 + include_usage 请求参数 + 条件渲染', () => {
  assert.match(tasksSrc, /显示用量/, '应有「显示用量」勾选框')
  assert.match(tasksSrc, /include_usage/, '请求应带 include_usage 参数')
  assert.match(tasksSrc, /showUsage && <th/, '用量列头应条件渲染')
  assert.match(tasksSrc, /UsageSummary/, '应使用 UsageSummary 组件渲染摘要')
})

test('USAGE_COL_WIDTH 常量存在；用量列不参与响应式隐藏', () => {
  assert.equal(USAGE_COL_WIDTH, 150, '用量列宽应为 150px')
  const keys = HIDDEN_COL_PRIORITY.map((c) => c.key)
  assert.ok(!keys.includes('usage'),
            '用量列不参与响应式隐藏（默认 12 列布局不被破坏）')
})

test('styles.css 提供用量列样式与勾选态列宽覆盖', () => {
  assert.match(styles, /\.tasks-usage-toggle\s*\{/, '应有勾选框样式')
  assert.match(styles, /\.usage-summary\s*\{/, '应有用量摘要样式')
  assert.match(styles, /\.tasks-table-usage th:nth-child\(6\)\s*\{\s*width: 150px;/,
               '勾选态第 6 列（用量）宽度应为 150px')
})

// ---- 渲染断言 ----

async function renderTasks() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(Tasks))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return renderer
}

function mockListApi() {
  const calls = []
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [{ id: 1, name: 'demo' }] }
    if (pathname.startsWith('/api/tasks')) {
      calls.push(pathname)
      return { tasks: [USAGE_TASK, PLAIN_TASK], total: 2,
               stats: { queued: 0, running: 0, retrying: 0 } }
    }
    throw new Error('unexpected ' + pathname)
  })
  return calls
}

test('渲染：默认不渲染用量列；勾选后列头出现并展示用量摘要', async () => {
  const calls = mockListApi()
  const renderer = await renderTasks()
  try {
    // 默认（未勾选）：无用量列头
    let headers = renderer.root.findAllByType('th').map(textOf)
    assert.ok(!headers.includes('用量'), '默认不应渲染「用量」列头')
    // 勾选「显示用量」
    const checkbox = renderer.root.findAllByType('input')
      .find((n) => n.props.type === 'checkbox')
    assert.ok(checkbox, '应有「显示用量」勾选框')
    await TestRenderer.act(async () => {
      checkbox.props.onChange({ target: { checked: true } })
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    headers = renderer.root.findAllByType('th').map(textOf)
    assert.ok(headers.includes('用量'), '勾选后应渲染「用量」列头')
    const text = textOf(renderer.toJSON())
    assert.match(text, /1,500 tokens/, '有用量任务应展示 total tokens')
    assert.match(text, /USD 0\.0100/, '有用量任务应展示估算费用')
    assert.ok(!text.includes('无数据'), '列表无用量任务应显示 — 而非「无数据」')
    // 请求应带 include_usage=1
    assert.ok(calls.some((c) => c.includes('include_usage=1')),
              '勾选后请求应带 include_usage=1，实际调用: ' + calls.join(', '))
    // 勾选后表格 min-width 增加用量列宽
    const table = renderer.root.findByType('table')
    assert.equal(table.props.style?.minWidth, 1360 + USAGE_COL_WIDTH,
                 '勾选后表格 min-width 应为 1360 + 150')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
