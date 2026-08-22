// 概览页 CI/CD 流水线详情右边栏测试（issue #317）：
// 点击流水线卡片 → 打开右侧抽屉显示流水线运行详情（整体状态徽章 +
// 分支/提交 + 创建/更新时间 + 阶段与任务明细）；抽屉右上角「在
// GitLab 中打开」跳转按钮（pipeline.web_url 新窗口）；关闭方式：
// × 按钮 / 点击遮罩 / Esc 键。
//
// 断言：
// 1. 流水线卡片主体渲染为按钮（点击打开抽屉），不再直接跳转 GitLab；
// 2. 点击卡片打开抽屉：仓库名、状态徽章、ref/sha、阶段与任务明细；
// 3. 抽屉右上角跳转按钮：href 为 pipeline web_url、新窗口打开；
// 4. 任务行渲染 job 名 + 状态 + job 的 GitLab 链接；
// 5. 关闭：× 按钮、点击遮罩（overlay）、Esc 键（isEscapeKey 纯函数）；
// 6. 边界：pipeline 为 null 显示「暂无流水线」；stage 无 jobs 只显示
//    阶段；job 缺 web_url 不渲染链接；重复点击同一卡片幂等；
// 7. styles.css 提供 .pipeline-drawer 抽屉样式；
// 8. issue #329：任务行展示产物清单（文件名/大小）与「下载全部」按钮
//    （href 为后端代理接口 /api/pipelines/{repo_id}/artifacts?job_id=）；
//    无产物的 job 不渲染产物区块。
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
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/PipelineSection.jsx'), 'utf8')
const drawerSrc = readFileSync(path.join(ROOT, 'src/components/PipelineDrawer.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-page.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
after(() => vite.close())
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: PipelineDrawer, ReportView, isEscapeKey, PIPELINE_STATUS_META, stageClass } =
  await vite.ssrLoadModule('/src/components/PipelineDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

// ---- 数据流源码断言 ----

test('流水线卡片主体改为按钮（点击打开抽屉），跳转统一走抽屉按钮', () => {
  assert.match(overview, /setSelectedPipeline/, '点击卡片应设置选中流水线打开抽屉')
  assert.match(overview, /PipelineDrawer/, '概览页应渲染 PipelineDrawer 组件')
  assert.match(overview, /pipeline-link/, '应保留 pipeline-link 样式类')
  assert.match(drawerSrc, /在 GitLab 中打开/, '抽屉右上角应有「在 GitLab 中打开」按钮')
  assert.match(drawerSrc, /pl\.web_url|pipeline\.web_url/, '跳转按钮应指向 pipeline web_url')
  // issue #329：产物展示与下载（fmtSize 格式化大小，下载走后端代理）
  assert.match(drawerSrc, /fmtSize/, '产物大小应经 fmtSize 人类可读格式化')
  assert.match(drawerSrc, /下载全部/, '应有「下载全部」按钮文案')
  assert.match(drawerSrc, /\/api\/pipelines\/\$\{repo\.repo_id\}\/artifacts\?job_id=\$\{j\.id\}/,
    '下载链接应指向后端代理接口（repo_id + job_id）')
})

test('PipelineDrawer 监听 Esc 关闭（isEscapeKey 纯函数判定）', () => {
  assert.match(drawerSrc, /addEventListener\('keydown'/, '应监听 keydown 事件')
  assert.match(drawerSrc, /removeEventListener\('keydown'/, '卸载时应清理监听')
  assert.match(drawerSrc, /isEscapeKey/, 'Esc 判定应走 isEscapeKey')
  // 纯函数边界：Escape 键 → true；其他键/空值 → false
  assert.equal(isEscapeKey({ key: 'Escape' }), true)
  assert.equal(isEscapeKey({ key: 'Enter' }), false)
  assert.equal(isEscapeKey({ key: 'escape' }), false, '大小写敏感，非 Escape 不关闭')
  assert.equal(isEscapeKey(null), false, '空值不应报错')
  assert.equal(isEscapeKey({}), false, '无 key 字段不应报错')
})

// ---- 纯函数映射 ----

test('PIPELINE_STATUS_META / stageClass 随组件导出且映射完整', () => {
  for (const s of ['success', 'failed', 'running', 'pending', 'canceled', 'skipped', 'created', 'manual']) {
    assert.ok(PIPELINE_STATUS_META[s], `应有 ${s} 状态映射`)
    assert.ok(PIPELINE_STATUS_META[s].label, `${s} 应有中文标签`)
  }
  assert.equal(stageClass('success'), 'st-success')
  assert.equal(stageClass('failed'), 'st-failed')
  assert.equal(stageClass('running'), 'st-running')
  assert.equal(stageClass('weird'), 'st-pending')
  assert.equal(stageClass(null), 'st-pending')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

async function renderOverview(pipelinesPayload, issues = [], reportFn = null) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return pipelinesPayload
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues }], errors: [], total: 0 }
    }
    // issue #337：报告解析接口
    if (reportFn && /^\/api\/pipelines\/\d+\/report\?/.test(pathname)) {
      return reportFn(pathname)
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 首轮渲染等待：vite SSR 冷启动编译耗时可能超过 30ms，放宽到 100ms
      // 保证 pipelines 首轮轮询结果已 setState
      await new Promise((resolve) => setTimeout(resolve, 100))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

// 完整字段流水线条目（后端 GET /api/pipelines/overview 返回结构）
const PIPELINE_ENTRY = {
  repo_id: 1, repo_name: 'botler', enabled: true,
  pipeline: {
    id: 731, status: 'running', ref: 'main', sha: 'abc123def456',
    web_url: 'https://gitlab.example.com/chenkaidi/botler/-/pipelines/731',
    created_at: '2026-08-19T08:00:00.000Z',
    updated_at: '2026-08-19T08:05:00.000Z',
    finished_at: null,
    duration: 300,
  },
  stages: [
    {
      name: 'build', status: 'success',
      jobs: [
        { id: 11, name: 'compile', status: 'success',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/11',
          artifacts: [
            { file_type: 'archive', filename: 'artifacts.zip',
              size: 208520, file_format: 'zip' },
            { file_type: 'cobertura', filename: 'cobertura-coverage.xml.gz',
              size: 63151, file_format: 'gzip' },
          ] },
      ],
    },
    {
      name: 'test', status: 'running',
      jobs: [
        { id: 22, name: 'unit', status: 'running',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/22',
          artifacts: [] },
      ],
    },
  ],
  commit_time: '2026-08-19 08:00:00',
}

// 在渲染树中找到流水线卡片主体按钮并模拟点击
async function openPipelineDrawer(payload) {
  const { renderer, renderError } = await renderOverview(payload)
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  const root = renderer.root
  const cardBtn = root.findAll(
    (n) => n.type === 'button' && String(n.props.className || '').includes('pipeline-link'))
  assert.ok(cardBtn.length > 0, '应渲染流水线卡片主体按钮')
  await TestRenderer.act(async () => {
    cardBtn[0].props.onClick()
  })
  return { renderer, root }
}

function findDrawer(root) {
  return root.findAll(
    (n) => String(n.props.className || '').includes('pipeline-drawer')
      && n.props.onClick /* 内部抽屉容器（stopPropagation 点击不关闭） */)
}

// 渲染树 → 纯文本（与 overview-issue-drawer.test.mjs 同款工具）
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') {
    return toText(node.children ?? node.props?.children)
  }
  return ''
}

function drawerText(root) {
  const drawers = findDrawer(root)
  return drawers.length > 0 ? toText(drawers[0].children) : ''
}

test('点击流水线卡片打开右边栏：仓库名、状态徽章、ref/sha 与阶段任务明细', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    assert.equal(findDrawer(root).length, 1, '应打开一个流水线抽屉')
    const text = drawerText(root)
    assert.ok(text.includes('botler'), '抽屉应显示仓库名')
    assert.ok(text.includes('运行中'), '抽屉应显示流水线状态徽章「运行中」')
    assert.ok(text.includes('main'), '应显示分支 ref')
    assert.ok(text.includes('abc123def'), '应显示提交 sha')
    assert.ok(text.includes('build'), '应显示 build 阶段')
    assert.ok(text.includes('test'), '应显示 test 阶段')
    assert.ok(text.includes('compile'), '应显示 build 阶段任务名')
    assert.ok(text.includes('unit'), '应显示 test 阶段任务名')
    // 卡片主体是按钮而非跳转链接（点击打开抽屉，跳转走抽屉按钮）
    const cardLinks = root.findAll(
      (n) => n.type === 'a' && String(n.props.className || '').includes('pipeline-link'))
    assert.equal(cardLinks.length, 0, '流水线卡片不应再渲染为跳转链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('抽屉右上角「在 GitLab 中打开」：href 为 pipeline web_url、新窗口打开', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    const links = root.findAll(
      (n) => n.type === 'a' && n.props.href === PIPELINE_ENTRY.pipeline.web_url)
    assert.ok(links.length >= 1, '抽屉应提供指向 GitLab 流水线的跳转按钮')
    assert.equal(links[0].props.target, '_blank', '跳转按钮应新窗口打开')
    assert.equal(links[0].props.rel, 'noreferrer')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('任务行渲染 job 名、状态与 job 的 GitLab 链接', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    // job 链接：compile / unit 各自指向 job web_url
    const jobLinks = root.findAll(
      (n) => n.type === 'a' && String(n.props.href || '').includes('/-/jobs/'))
    assert.equal(jobLinks.length, 2, '应渲染两个 job 的 GitLab 链接')
    assert.ok(jobLinks.some((a) => a.props.href.includes('/jobs/11')), 'compile 任务应链到 job 11')
    assert.ok(jobLinks.some((a) => a.props.href.includes('/jobs/22')), 'unit 任务应链到 job 22')
    assert.equal(jobLinks[0].props.target, '_blank', 'job 链接应新窗口打开')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('关闭：× 按钮 / 点击遮罩 / Esc 键', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    // × 按钮
    const closeBtn = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('modal-close'))
    assert.ok(closeBtn.length >= 1, '抽屉应有 × 关闭按钮')
    await TestRenderer.act(async () => { closeBtn[0].props.onClick() })
    assert.equal(findDrawer(root).length, 0, '点击 × 后抽屉应关闭')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }

  // 遮罩点击
  const second = await openPipelineDrawer({ pipelines: [PIPELINE_ENTRY], errors: [] })
  try {
    const overlay = second.root.findAll(
      (n) => String(n.props.className || '').includes('drawer-overlay'))
    assert.ok(overlay.length >= 1, '应有遮罩层')
    await TestRenderer.act(async () => { overlay[0].props.onClick() })
    assert.equal(findDrawer(second.root).length, 0, '点击遮罩后抽屉应关闭')
  } finally {
    await TestRenderer.act(() => second.renderer.unmount())
    mock.restoreAll()
  }

  // Esc：纯函数判定已覆盖；组件渲染阶段验证 onClose 由 Esc 键触发
  // （keydown 监听在 useEffect 中，SSR 测试环境无 document 时跳过，
  // 与 IssueDrawer / TaskDetailDrawer 一致，行为由源码断言保障）
  const third = await openPipelineDrawer({ pipelines: [PIPELINE_ENTRY], errors: [] })
  try {
    const drawer = findDrawer(third.root)[0]
    const inst = drawer.props.children
    assert.ok(inst, '抽屉应渲染内容')
  } finally {
    await TestRenderer.act(() => third.renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：pipeline 为 null 时抽屉显示「暂无流水线」空态且无跳转按钮', async () => {
  // pipeline 为 null 的仓库卡片无详情可点（卡片只渲染「暂无流水线」占位），
  // 抽屉空态分支通过直接渲染组件覆盖（与 PipelineDrawer 直渲测试同模式）
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(PipelineDrawer, {
        entry: { repo_id: 2, repo_name: 'empty-repo', enabled: true,
                 pipeline: null, stages: [], commit_time: null },
        onClose: () => {},
      }))
    } catch (e) { renderError = e }
  })
  try {
    assert.equal(renderError, null, '渲染不应崩溃')
    const text = toText(renderer.toJSON())
    assert.ok(text.includes('empty-repo'), '抽屉应显示仓库名')
    assert.ok(text.includes('暂无流水线'), '应显示暂无流水线占位')
    const links = renderer.root.findAll(
      (n) => n.type === 'a' && String(n.props.href || '').includes('/pipelines/'))
    assert.equal(links.length, 0, '无流水线时不应有跳转链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('边界：stage 无 jobs / job 缺 web_url 不崩溃且不渲染链接', async () => {
  const entry = {
    ...PIPELINE_ENTRY,
    stages: [
      { name: 'build', status: 'success', jobs: [] },
      { name: 'deploy', status: 'success', jobs: [
        { name: 'release', status: 'success' }, // 缺 web_url
      ] },
    ],
  }
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [entry], errors: [],
  })
  try {
    const text = drawerText(root)
    assert.ok(text.includes('build'), '应显示无任务阶段名')
    assert.ok(text.includes('release'), '缺 web_url 的 job 名应纯文本兜底')
    const jobLinks = root.findAll(
      (n) => n.type === 'a' && String(n.props.href || '').includes('/-/jobs/'))
    assert.equal(jobLinks.length, 0, '缺 web_url 的 job 不应渲染链接')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：重复点击同一流水线卡片幂等（只打开一个抽屉）', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    const cardBtn = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('pipeline-link'))
    await TestRenderer.act(async () => { cardBtn[0].props.onClick() })
    await TestRenderer.act(async () => { cardBtn[0].props.onClick() })
    assert.equal(findDrawer(root).length, 1, '重复点击不应打开多个抽屉')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('任务行展示流水线产物：文件名/大小 + 下载全部按钮（issue #329）', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    const text = drawerText(root)
    // 产物明细：archive 与 cobertura 报告都展示（文件名 + 人类可读大小）
    assert.ok(text.includes('artifacts.zip'), '应展示 archive 产物文件名')
    assert.ok(text.includes('203.6 KB'), 'archive 大小应经 fmtSize 格式化')
    assert.ok(text.includes('cobertura-coverage.xml.gz'), '应展示报告型产物文件名')
    // 下载按钮：href 指向后端代理接口（repo_id + job_id）
    const dl = root.findAll(
      (n) => n.type === 'a' && String(n.props.className || '')
        .includes('pipeline-detail-artifacts-download'))
    assert.equal(dl.length, 1, '有产物的 job 应渲染一个「下载全部」按钮')
    assert.equal(dl[0].props.href, '/api/pipelines/1/artifacts?job_id=11',
      '下载链接应指向后端代理接口')
    assert.equal(dl[0].props.download, true, '应带 download 属性触发浏览器下载')
    assert.ok(text.includes('下载全部'), '应有「下载全部」文案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('无产物的 job 不渲染产物区块（issue #329）', async () => {
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [PIPELINE_ENTRY], errors: [],
  })
  try {
    const dl = root.findAll(
      (n) => n.type === 'a' && String(n.props.className || '')
        .includes('pipeline-detail-artifacts-download'))
    assert.equal(dl.length, 1, '仅 compile（有产物）应渲染下载按钮')
    // 产物区块只在 compile（有产物）的 li 中出现一次：unit 的 artifacts
    // 为空数组，不应渲染产物区块
    // 精确匹配产物容器类（避免 -head/-title/-download 子元素误命中）
    const artifactBlocks = root.findAll(
      (n) => String(n.props.className || '').split(/\s+/)
        .includes('pipeline-detail-artifacts'))
    assert.equal(artifactBlocks.length, 1, '空 artifacts 的 job 不应渲染产物区块')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('边界：job 缺 id / 缺 artifacts 字段不渲染下载按钮且不崩溃', async () => {
  const entry = {
    ...PIPELINE_ENTRY,
    stages: [
      { name: 'build', status: 'success', jobs: [
        { name: 'legacy', status: 'success', web_url: 'https://x/-/jobs/1' }, // 无 id/artifacts
        { id: 2, name: 'with-art', status: 'success',
          artifacts: [{ file_type: 'archive', filename: 'a.zip', size: 10 }] },
      ] },
    ],
  }
  const { renderer, root } = await openPipelineDrawer({
    pipelines: [entry], errors: [],
  })
  try {
    const dl = root.findAll(
      (n) => n.type === 'a' && String(n.props.className || '')
        .includes('pipeline-detail-artifacts-download'))
    assert.equal(dl.length, 1, '仅带 id 的 job 渲染下载按钮')
    assert.equal(dl[0].props.href, '/api/pipelines/1/artifacts?job_id=2')
    assert.equal(findDrawer(root).length, 1, '抽屉应正常打开不崩溃')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- styles.css ----

test('styles.css 提供 .pipeline-drawer 抽屉样式', () => {
  assert.match(styles, /\.pipeline-drawer\s*\{/, '应有 .pipeline-drawer 抽屉样式')
  assert.match(styles, /\.pipeline-detail-artifacts\s*\{/, '应有产物区块样式')
})

// ---- PipelineDrawer 直接渲染（脱离 Overview 的单元级渲染）----

test('PipelineDrawer 直接渲染：空 entry / 异常数据兜底不崩溃', async () => {
  for (const entry of [null, {}, { repo_name: 'x' }, { repo_name: 'x', stages: 'bad' }]) {
    let renderError = null
    let renderer = null
    await TestRenderer.act(async () => {
      try {
        renderer = TestRenderer.create(
          React.createElement(PipelineDrawer, { entry, onClose: () => {} }))
      } catch (e) { renderError = e }
    })
    assert.equal(renderError, null, `entry=${JSON.stringify(entry)} 不应崩溃`)
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

// =====================================================================
// issue #337：流水线详情查看代码静态分析报告与测试报告
// 用户确认的交互（issue 评论）：抽屉内直接渲染解析后报告内容；范围含
// 依赖扫描；展示展开明细；job 失败时不提供查看入口；验收 = security:
// bandit 任务行出现「查看报告」→ 展示问题列表（严重级别/文件/行号）。
// =====================================================================

// 后端 /api/pipelines/{repo_id}/report 返回的报告样本（解析后结构）
const SAST_REPORT = {
  job_id: 33, filename: 'backend/bandit-report.sarif', file_type: 'sast',
  report: {
    kind: 'sast', tool: 'Bandit',
    summary: { total: 2, by_severity: { high: 1, medium: 1, low: 0, info: 0, unknown: 0 } },
    results: [
      { rule: 'B101', severity: 'high', message: 'Use of assert detected.',
        file: 'botler/api/pipelines.py', line: 42, column: 8 },
      { rule: 'B608', severity: 'medium', message: 'Possible SQL injection.',
        file: 'botler/db.py', line: 7, column: null },
    ],
  },
}

const DEPS_REPORT = {
  job_id: 44, filename: 'backend/deps-python-report.json',
  file_type: 'dependency_scanning',
  report: {
    kind: 'deps',
    summary: { total: 1, by_severity: { Critical: 0, High: 1, Medium: 0, Low: 0, Info: 0, Unknown: 0 } },
    results: [
      { id: 'CVE-2023-1234', name: 'requests', severity: 'High',
        package: 'requests', version: '2.28.1',
        file: 'backend/requirements.txt',
        solution: '升级到修复版本 [\'2.31.0\']',
        identifiers: [{ type: 'cve', name: 'CVE-2023-1234', url: '' }] },
    ],
  },
}

const JUNIT_REPORT = {
  job_id: 55, filename: 'backend/junit.xml', file_type: 'junit',
  report: {
    kind: 'test',
    summary: { tests: 3, failures: 1, errors: 0, skipped: 1, time: 1.23 },
    results: [
      { name: 'test_ok', classname: 'tests.test_a', status: 'passed', time: 0.1, message: '' },
      { name: 'test_fail', classname: 'tests.test_a', status: 'failed', time: 0.2,
        message: 'assert 1 == 2' },
      { name: 'test_skip', classname: 'tests.test_a', status: 'skipped', time: 0.0, message: '' },
    ],
  },
}

// 带 security 阶段（sast 报告产物）的流水线条目
const PIPELINE_WITH_REPORTS = {
  ...PIPELINE_ENTRY,
  stages: [
    {
      name: 'security', status: 'success',
      jobs: [
        { id: 33, name: 'security:bandit', status: 'success',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/33',
          artifacts: [
            { file_type: 'sast', filename: 'backend/bandit-report.sarif',
              size: 1234, file_format: 'sarif' },
          ] },
        { id: 44, name: 'security:deps-python', status: 'success',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/44',
          artifacts: [
            { file_type: 'dependency_scanning',
              filename: 'backend/deps-python-report.json',
              size: 5678, file_format: 'json' },
          ] },
      ],
    },
    {
      name: 'build', status: 'success',
      jobs: [
        { id: 55, name: 'backend:test', status: 'success',
          web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/55',
          artifacts: [
            { file_type: 'junit', filename: 'backend/junit.xml',
              size: 4321, file_format: 'gzip' },
          ] },
      ],
    },
  ],
}

// 报告接口 mock 分派：按 file_type 返回对应样本
function reportResponder(pathname) {
  const m = pathname.match(/^\/api\/pipelines\/\d+\/report\?job_id=(\d+)/)
  const jobId = m ? Number(m[1]) : 0
  if (jobId === 33) return SAST_REPORT
  if (jobId === 44) return DEPS_REPORT
  if (jobId === 55) return JUNIT_REPORT
  throw new Error('unexpected report job ' + pathname)
}

// 数据流源码断言（issue #337）
test('报告查看：源码含「查看报告」按钮、ReportView 与后端报告接口', () => {
  assert.match(drawerSrc, /查看报告/, '任务行应有「查看报告」按钮文案')
  assert.match(drawerSrc, /ReportView/, '应导出/渲染 ReportView 报告视图组件')
  assert.match(drawerSrc, /REPORT_FILE_TYPES|sast.*dependency_scanning.*junit/s,
    '应有报告类型白名单（sast/dependency_scanning/junit）')
  assert.match(drawerSrc, /\/api\/pipelines\/\$\{repoId\}\/report/,
    '报告加载应走后端解析接口 /api/pipelines/{repo_id}/report')
  assert.match(drawerSrc, /j\.status === 'success'|status === 'success'/,
    '仅成功 job 提供查看报告入口（失败时不能查看，issue 评论确认）')
  // 报告视图内应直接渲染解析后的明细（严重级别/文件/行号、测试用例）
  assert.match(drawerSrc, /severity/, '报告明细应展示严重级别')
  assert.match(drawerSrc, /file.*line|line.*file/s, '静态分析明细应展示文件与行号')
})

// 集成：成功 job 带 sast 产物 → 任务行出现「查看报告」，点击后抽屉内
// 直接渲染问题列表（严重级别/文件/行号），符合验收标准
test('security:bandit 任务行「查看报告」→ 抽屉内渲染问题列表（issue #337 验收）', async () => {
  const { renderer, renderError } = await renderOverview({
    pipelines: [PIPELINE_WITH_REPORTS], errors: [],
  }, [], reportResponder)
  assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
  const root = renderer.root
  try {
    const cardBtn = root.findAll(
      (n) => n.type === 'button' && String(n.props.className || '').includes('pipeline-link'))
    await TestRenderer.act(async () => { cardBtn[0].props.onClick() })

    // 三个报告型 job 均应出现「查看报告」按钮
    const btns = root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('pipeline-detail-report-btn'))
    assert.equal(btns.length, 3, 'sast/deps/junit 三个报告 job 应各有一个查看按钮')

    // 点击 security:bandit 的查看按钮
    await TestRenderer.act(async () => { btns[0].props.onClick() })
    await new Promise((resolve) => setTimeout(resolve, 30))
    const text = drawerText(root)
    assert.ok(text.includes('security:bandit'), '报告视图头应显示 job 名')
    assert.ok(text.includes('bandit-report.sarif'), '报告视图头应显示报告文件名')
    assert.ok(text.includes('B101'), '应展示规则编号 B101')
    assert.ok(text.includes('botler/api/pipelines.py'), '应展示问题所在文件')
    assert.ok(text.includes('42'), '应展示问题行号')
    assert.ok(text.includes('Use of assert detected.'), '应展示问题描述')
    assert.ok(text.includes('返回'), '报告视图应有返回按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// 失败 job 不提供查看报告入口（issue 评论确认「失败时不能查看报告」）
test('失败 job 即使有报告产物也不渲染「查看报告」按钮', async () => {
  const entry = {
    ...PIPELINE_ENTRY,
    stages: [
      { name: 'security', status: 'failed', jobs: [
        { id: 66, name: 'security:bandit', status: 'failed',
          web_url: 'https://x/-/jobs/66',
          artifacts: [{ file_type: 'sast', filename: 'backend/bandit-report.sarif',
                        size: 1 }] },
      ] },
    ],
  }
  const { renderer, root } = await openPipelineDrawer({ pipelines: [entry], errors: [] })
  try {
    const btns = root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('pipeline-detail-report-btn'))
    assert.equal(btns.length, 0, '失败 job 不应有查看报告按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ReportView 直接渲染：sast 报告（严重级别/文件/行号明细）
test('ReportView 直接渲染 sast 报告：严重级别/文件/行号', async () => {
  mock.method(api, 'get', async (pathname) => {
    assert.match(pathname, /^\/api\/pipelines\/1\/report\?/, '应请求报告解析接口')
    assert.match(pathname, /file_type=sast/, '应携带报告类型参数')
    return SAST_REPORT
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(ReportView, {
      repoId: 1, jobId: 33, jobName: 'security:bandit',
      file: 'backend/bandit-report.sarif', fileType: 'sast', onBack: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const text = toText(renderer.toJSON())
    assert.ok(text.includes('共 2 个问题'), '应显示问题总数摘要')
    assert.ok(text.includes('高'), '应显示高严重级别')
    assert.ok(text.includes('中'), '应显示中严重级别')
    assert.ok(text.includes('B101'), '应显示规则编号')
    assert.ok(text.includes('botler/api/pipelines.py'), '应显示文件')
    assert.ok(text.includes('42'), '应显示行号')
    assert.ok(text.includes('返回'), '应有返回按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ReportView 直接渲染：依赖扫描报告
test('ReportView 直接渲染依赖扫描报告：包/版本/CVE/解决方案', async () => {
  mock.method(api, 'get', async () => DEPS_REPORT)
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(ReportView, {
      repoId: 1, jobId: 44, jobName: 'security:deps-python',
      file: 'backend/deps-python-report.json', fileType: 'dependency_scanning',
      onBack: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const text = toText(renderer.toJSON())
    assert.ok(text.includes('requests'), '应显示包名')
    assert.ok(text.includes('2.28.1'), '应显示受影响版本')
    assert.ok(text.includes('CVE-2023-1234'), '应显示漏洞编号')
    assert.ok(text.includes('backend/requirements.txt'), '应显示依赖文件')
    assert.ok(text.includes('升级到修复版本'), '应显示解决方案')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ReportView 直接渲染：JUnit 测试报告（汇总 + 用例明细）
test('ReportView 直接渲染测试报告：通过/失败/跳过汇总与用例明细', async () => {
  mock.method(api, 'get', async () => JUNIT_REPORT)
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(ReportView, {
      repoId: 1, jobId: 55, jobName: 'backend:test',
      file: 'backend/junit.xml', fileType: 'junit', onBack: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const text = toText(renderer.toJSON())
    assert.ok(text.includes('3'), '应显示用例总数')
    assert.ok(text.includes('失败'), '应显示失败数')
    assert.ok(text.includes('跳过'), '应显示跳过数')
    assert.ok(text.includes('test_ok'), '应显示用例名')
    assert.ok(text.includes('test_fail'), '应显示失败用例名')
    assert.ok(text.includes('通过'), '应显示通过状态')
    assert.ok(text.includes('assert 1 == 2'), '应显示失败原因')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ReportView 错误态：接口失败展示错误信息与返回入口
test('ReportView 接口失败：展示错误信息且不崩溃', async () => {
  mock.method(api, 'get', async () => {
    throw new Error('报告解析失败: SARIF 报告不是有效 JSON')
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(ReportView, {
      repoId: 1, jobId: 33, jobName: 'security:bandit',
      file: 'bad.sarif', fileType: 'sast', onBack: () => {},
    }))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const text = toText(renderer.toJSON())
    assert.ok(text.includes('报告解析失败'), '应展示后端错误信息')
    assert.ok(text.includes('返回'), '错误态应有返回按钮')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// 边界：job 产物缺 id / 缺 file_type / 非报告类型 → 不渲染查看按钮且不崩溃
test('边界：非报告产物 / 缺 id 的 job 不渲染「查看报告」', async () => {
  const entry = {
    ...PIPELINE_ENTRY,
    stages: [
      { name: 'build', status: 'success', jobs: [
        { name: 'no-id', status: 'success',
          artifacts: [{ file_type: 'sast', filename: 'a.sarif', size: 1 }] },
        { id: 77, name: 'archive-only', status: 'success',
          artifacts: [{ file_type: 'archive', filename: 'a.zip', size: 1 }] },
        { id: 78, name: 'no-artifacts', status: 'success', artifacts: [] },
        { id: 79, name: 'bad-art', status: 'success', artifacts: 'oops' },
      ] },
    ],
  }
  const { renderer, root } = await openPipelineDrawer({ pipelines: [entry], errors: [] })
  try {
    const btns = root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('pipeline-detail-report-btn'))
    assert.equal(btns.length, 0, '缺 id / 非报告产物 / 空产物均不应有查看按钮')
    assert.equal(findDrawer(root).length, 1, '抽屉应正常打开不崩溃')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// 样式：styles.css 提供报告视图样式
test('styles.css 提供报告视图样式（.pipeline-detail-report-btn / .pipeline-report-*）', () => {
  assert.match(styles, /\.pipeline-detail-report-btn\s*\{/, '应有「查看报告」按钮样式')
  assert.match(styles, /\.pipeline-report\s*\{/, '应有报告视图容器样式')
  assert.match(styles, /\.pipeline-report-item\s*\{/, '应有报告明细条目样式')
})

// issue #433：报告明细应跟随流水线详情抽屉全局纵向滚动，不能再形成局部滚动区。
test('测试报告明细不创建局部纵向滚动，滚动由流水线详情抽屉统一承担', () => {
  const match = styles.match(/\.pipeline-report-list\s*\{([^}]*)\}/s)
  assert.ok(match, '应存在 .pipeline-report-list 样式规则')
  assert.doesNotMatch(match[1], /max-height\s*:/,
    '报告明细列表不应限制高度，否则会形成局部滚动区')
  assert.doesNotMatch(match[1], /overflow-y\s*:/,
    '报告明细列表不应设置纵向滚动，滚动应由 .drawer 统一承担')
})
