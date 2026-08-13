// 概览页 CI/CD 流水线状态测试（issue #39）：
// 概览页面展示所有配置仓库（含未启用，issue #39 第二轮）的最新流水线
// 状态（整体状态 + 按 jobs 聚合的 stage 进度），展示方式参考 GitLab
// CI/CD 的 pipeline 阶段图。
//
// 断言：
// 1. Overview 页独立轮询 GET /api/pipelines/overview（15 秒，比任务轮询慢）；
// 2. 每仓库卡片渲染：仓库名、流水线整体状态徽章、stage 节点条
//    （节点 class 按 stage 状态映射 st-success / st-failed / st-running /
//     st-pending / st-canceled），卡片链接到 GitLab pipeline 页面；
// 3. 无流水线仓库显示「暂无流水线」；全部无数据时显示空状态；
// 4. 未启用仓库（enabled=false）卡片显示「未启用」徽章；
// 5. 部分仓库查询失败时 errors 以警告形式展示；
// 6. PIPELINE_STATUS_META / stageClass 纯函数映射边界；
// 7. styles.css 提供 pipeline 卡片与 stage 节点样式。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const Overview = mod.default
const { PIPELINE_STATUS_META, stageClass } = mod
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('概览页独立轮询流水线状态接口（15 秒间隔）', () => {
  assert.match(overview, /\/api\/pipelines\/overview/, '应请求 GET /api/pipelines/overview')
  assert.match(overview, /PIPELINE_POLL_MS\s*=\s*15000/, '流水线轮询间隔应为 15 秒')
  assert.match(overview, /setInterval\(loadPipelines/, '流水线应独立定时轮询')
})

test('卡片渲染仓库名、状态徽章、stage 节点与 pipeline 链接', () => {
  assert.match(overview, /repo_name/, '卡片应显示仓库名')
  assert.match(overview, /暂无流水线/, '无流水线时应显示占位文案')
  assert.match(overview, /stageClass/, 'stage 节点应使用 stageClass 映射样式类')
  assert.match(overview, /pl\.web_url/, '卡片应链接到 pipeline web_url')
})

// ---- 纯函数映射 ----

test('PIPELINE_STATUS_META 覆盖流水线各状态', () => {
  for (const s of ['success', 'failed', 'running', 'pending', 'canceled', 'skipped', 'created', 'manual']) {
    assert.ok(PIPELINE_STATUS_META[s], `应有 ${s} 状态映射`)
    assert.ok(PIPELINE_STATUS_META[s].label, `${s} 应有中文标签`)
    assert.ok(PIPELINE_STATUS_META[s].cls, `${s} 应有徽章样式类`)
  }
})

test('stageClass：stage 状态映射为对应节点样式类', () => {
  assert.equal(stageClass('success'), 'st-success')
  assert.equal(stageClass('failed'), 'st-failed')
  assert.equal(stageClass('running'), 'st-running')
  assert.equal(stageClass('pending'), 'st-pending')
  assert.equal(stageClass('canceled'), 'st-canceled')
  assert.equal(stageClass('created'), 'st-pending')
  assert.equal(stageClass('skipped'), 'st-skipped')
})

test('stageClass：未知状态兜底为 pending 样式（不抛错）', () => {
  assert.equal(stageClass('weird-status'), 'st-pending')
  assert.equal(stageClass(undefined), 'st-pending')
  assert.equal(stageClass(null), 'st-pending')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

async function renderAndSettle(impl, waitMs = 30) {
  mock.method(api, 'get', impl)
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 等待首轮任务 + 流水线轮询的 promise flush
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

// 基础 mock：无活跃任务 + 可配置的流水线数据
function makeApiMock(pipelineData) {
  return async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [], total: 0, stats: {} }
    }
    if (pathname === '/api/pipelines/overview') {
      return pipelineData
    }
    throw new Error('unexpected ' + pathname)
  }
}

test('渲染流水线卡片：仓库名、状态徽章、stage 节点与 GitLab 链接', async () => {
  const data = {
    pipelines: [{
      repo_id: 1, repo_name: 'botler',
      pipeline: {
        id: 731, status: 'running', ref: 'main', sha: 'abc123',
        web_url: 'https://gitlab.example.com/g/botler/-/pipelines/731',
      },
      stages: [
        { name: 'build', status: 'success' },
        { name: 'test', status: 'running' },
        { name: 'deploy', status: 'pending' },
      ],
    }],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('botler'), '卡片应显示仓库名')
    assert.ok(text.includes('build'), '应显示 build stage 节点')
    assert.ok(text.includes('test'), '应显示 test stage 节点')
    assert.ok(text.includes('deploy'), '应显示 deploy stage 节点')
    const stages = root.findAll(
      (n) => n.props?.className && String(n.props.className).split(' ').includes('pipeline-stage'),
    )
    assert.equal(stages.length, 3, '应渲染 3 个 stage 节点')
    assert.ok(stages[0].props.className.includes('st-success'), 'build 节点应为成功样式')
    assert.ok(stages[1].props.className.includes('st-running'), 'test 节点应为运行中样式')
    assert.ok(stages[2].props.className.includes('st-pending'), 'deploy 节点应为待运行样式')
    assert.ok(
      root.findAllByType('a').some((a) => a.props.href?.includes('/pipelines/731')),
      '卡片应链接到 GitLab pipeline 页面',
    )
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无流水线仓库显示「暂无流水线」占位', async () => {
  const data = {
    pipelines: [{ repo_id: 2, repo_name: 'empty-repo', pipeline: null, stages: [] }],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('empty-repo'), '应显示仓库名')
    assert.ok(text.includes('暂无流水线'), '应显示暂无流水线占位')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('未启用仓库卡片显示「未启用」徽章（issue #39 第二轮）', async () => {
  const data = {
    pipelines: [
      {
        repo_id: 1, repo_name: 'enabled-repo', enabled: true,
        pipeline: {
          id: 731, status: 'success', ref: 'main', sha: 'abc123',
          web_url: 'https://gitlab.example.com/g/a/-/pipelines/731',
        },
        stages: [{ name: 'build', status: 'success' }],
      },
      {
        repo_id: 2, repo_name: 'disabled-repo', enabled: false,
        pipeline: null, stages: [],
      },
    ],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 未启用仓库卡片上应出现「未启用」徽章
    const badges = root.findAll(
      (n) => n.type === 'span' && n.children?.some((c) => c === '未启用'),
    )
    assert.equal(badges.length, 1, '应只有一个未启用徽章（disabled-repo 卡片）')
    assert.ok(
      String(badges[0].props.className).includes('badge-muted'),
      '未启用徽章应使用 badge-muted 灰色样式',
    )
    // 启用仓库卡片不出现未启用徽章
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('disabled-repo') && text.includes('enabled-repo'))
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('未启用仓库有流水线：卡片同样渲染状态徽章与 stage 节点', async () => {
  const data = {
    pipelines: [{
      repo_id: 1, repo_name: 'off-repo', enabled: false,
      pipeline: {
        id: 732, status: 'failed', ref: 'main', sha: 'def456',
        web_url: 'https://gitlab.example.com/g/off/-/pipelines/732',
      },
      stages: [
        { name: 'build', status: 'success' },
        { name: 'test', status: 'failed' },
      ],
    }],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('off-repo'), '应显示未启用仓库名')
    assert.ok(text.includes('失败'), '应显示流水线失败状态徽章')
    const stages = root.findAll(
      (n) => n.props?.className && String(n.props.className).split(' ').includes('pipeline-stage'),
    )
    assert.equal(stages.length, 2, '未启用仓库的 stage 节点也应渲染')
    assert.ok(stages[1].props.className.includes('st-failed'), 'test 节点应为失败样式')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('部分仓库查询失败：errors 以警告形式展示且不崩溃', async () => {
  const data = {
    pipelines: [{ repo_id: 1, repo_name: 'a', pipeline: null, stages: [] }],
    errors: ['仓库 b: 模拟 GitLab API 故障'],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('模拟 GitLab API 故障'), '应展示 errors 警告明细')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('全部仓库无流水线时显示空状态文案', async () => {
  const { renderer, renderError } = await renderAndSettle(
    makeApiMock({ pipelines: [], errors: [] }),
  )
  try {
    assert.equal(renderError, null)
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('暂无流水线'), '应显示空状态文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('流水线接口失败：显示错误提示而不崩溃', async () => {
  const { renderer, renderError } = await renderAndSettle(async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    throw new Error('流水线接口网络错误')
  })
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('流水线接口网络错误'), '应显示流水线 API 错误信息')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- styles.css ----

test('styles.css 提供流水线卡片与 stage 节点样式', () => {
  assert.match(styles, /\.pipeline-card\s*\{/, '应有 .pipeline-card 卡片样式')
  assert.match(styles, /\.pipeline-stages\s*\{/, '应有 .pipeline-stages 节点条容器样式')
  assert.match(styles, /\.pipeline-stage\s*\{/, '应有 .pipeline-stage 节点样式')
  for (const cls of ['st-success', 'st-failed', 'st-running', 'st-pending', 'st-canceled', 'st-skipped']) {
    // 状态类作为后代选择器作用于节点（如 .st-success .pipeline-stage-dot）
    assert.match(styles, new RegExp(`\\.${cls}[\\s{]`), `应有 .${cls} 节点状态样式`)
  }
})
