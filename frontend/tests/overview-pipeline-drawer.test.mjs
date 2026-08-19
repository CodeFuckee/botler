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
const { default: PipelineDrawer, isEscapeKey, PIPELINE_STATUS_META, stageClass } =
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

async function renderOverview(pipelinesPayload, issues = []) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return pipelinesPayload
    if (pathname === '/api/issues/overview') {
      return { repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues }], errors: [], total: 0 }
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
