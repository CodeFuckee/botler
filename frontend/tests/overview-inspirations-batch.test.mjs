// 概览页灵感「批量转 issue」测试（issue #247）：多选模式 + 转前预览编辑。
//
// 需求：灵感板块新增多选模式，选中后出现「转为 Issue」批量按钮；弹出
// 批量预览面板——每条可单独编辑标题/描述/标签/目标仓库，或「全部应用
// 默认」统一重置；逐条提交，成功项删除、失败项保留并逐条展示原因；
// 结果汇总提示「N 成功 / M 失败」；单条转 issue 行为不变。
//
// 断言：
// 1. 源码：多选按钮（overview.inspirationSelectMode）、每条复选框
//    （inspiration-select-checkbox）、批量工具栏（inspiration-batch-toolbar）、
//    「转为 Issue」按钮、批量预览弹窗（inspiration-batch-modal）、
//    POST /api/inspirations/batch-add-issues、汇总/失败文案经 i18n；
// 2. 渲染/交互：进入多选显示工具栏与复选框；勾选更新计数；未选时
//    「转为 Issue」禁用；选中后打开预览面板，草稿预填默认值（标题=描述=
//    内容、标签 feature, ui、原仓库）；编辑标题后提交 → POST payload
//    携带覆盖字段、未编辑条目只传 inspiration_id；结果汇总
//    「N 成功 / M 失败」+ 逐条失败原因；失败条目保留、成功条目删除；
//    全部应用默认重置草稿；取消退出多选模式。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const comp = readFileSync(path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview 系测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const PAYLOAD = {
  repos: [
    {
      repo_id: 1, repo_name: 'botler', enabled: true, priority: 10,
      inspirations: [
        { id: 11, repo_id: 1, repo_name: 'botler', content: '灵感一', updated_at: '2026-08-16 12:00:00' },
        { id: 12, repo_id: 1, repo_name: 'botler', content: '灵感二', updated_at: '2026-08-16 12:01:00' },
      ],
    },
    {
      repo_id: 2, repo_name: 'shipyard', enabled: false, priority: 20,
      inspirations: [{ id: 21, repo_id: 2, repo_name: 'shipyard', content: '灵感三', updated_at: '2026-08-16 12:02:00' }],
    },
  ],
}

async function renderOverview({
  batchResult = null,
  batchError = null,
  batchPending = false,
} = {}) {
  const getCalls = []
  const postCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname === '/api/inspirations/overview') return PAYLOAD
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname, body) => {
    postCalls.push([pathname, body])
    if (String(pathname).endsWith('/batch-add-issues')) {
      if (batchError) throw new Error(batchError)
      if (batchPending) return new Promise(() => {})
      return batchResult || {
        succeeded: [],
        failed: [],
        summary: { succeeded: 0, failed: 0 },
      }
    }
    return { id: 99 }
  })
  mock.method(api, 'put', async () => ({}))
  mock.method(api, 'del', async () => null)
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
  return {
    renderer, renderError, getCalls, postCalls,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

function findByClass(renderer, cls) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes(cls))
}
// 精确类名匹配（inspiration-batch-item 会误匹配 -item-head/-item-error 等子串）
function findByExactClass(renderer, cls) {
  return renderer.root.findAll((n) => String(n.props.className || '').trim() === cls)
}
function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}

// 点击「多选」按钮进入多选模式
async function enterSelectMode(renderer) {
  const btn = findByClass(renderer, 'inspiration-select-mode-btn')[0]
  assert.ok(btn, '应存在「多选」按钮')
  await TestRenderer.act(async () => { btn.props.onClick() })
  await new Promise((resolve) => setTimeout(resolve, 5))
}

// 勾选/取消勾选第 index 个复选框
async function toggleCheckbox(renderer, index) {
  const boxes = findByClass(renderer, 'inspiration-select-checkbox')
  assert.ok(boxes.length > index, `找不到第 ${index} 个复选框`)
  await TestRenderer.act(async () => { boxes[index].props.onChange() })
  await new Promise((resolve) => setTimeout(resolve, 5))
}

// ---- 源码断言 ----

test('源码：多选按钮/复选框/批量工具栏/转为 Issue 按钮存在且经 i18n', () => {
  assert.match(comp, /tr\('overview\.inspirationSelectMode'\)/, '「多选」按钮应经 t() 国际化')
  assert.match(comp, /inspiration-select-checkbox/, '每条灵感应渲染复选框（多选模式下）')
  assert.match(comp, /inspiration-batch-toolbar/, '应渲染批量工具栏')
  assert.match(comp, /tr\('overview\.inspirationBatchConvert'\)/, '「转为 Issue」按钮应经 t() 国际化')
  assert.equal(zhCN['overview.inspirationSelectMode'], '多选', '中文「多选」文案应保留')
  assert.equal(zhCN['overview.inspirationBatchConvert'], '转为 Issue', '中文「转为 Issue」文案应保留')
  // 未选中时「转为 Issue」禁用
  assert.match(comp, /disabled=\{selectedInspirationIds\.length === 0\}/,
               '未选中灵感时「转为 Issue」应禁用')
})

test('源码：批量预览弹窗字段与「全部应用默认」/汇总文案存在', () => {
  assert.match(comp, /inspiration-batch-modal/, '批量预览应使用弹窗（inspiration-batch-modal）')
  assert.match(comp, /inspiration-batch-title/, '每条应有可编辑标题输入框')
  assert.match(comp, /inspiration-batch-desc/, '每条应有可编辑描述输入框')
  assert.match(comp, /inspiration-batch-labels/, '每条应有可编辑标签输入框')
  assert.match(comp, /inspiration-batch-repo/, '每条应有目标仓库下拉')
  assert.match(comp, /tr\('overview\.inspirationBatchApplyDefaults'\)/,
               '应有「全部应用默认」按钮')
  assert.match(comp, /tr\('overview\.inspirationBatchSummary'/,
               '应有「N 成功 / M 失败」汇总文案（经 t() 国际化）')
  assert.match(comp, /tr\('overview\.inspirationBatchFailureDetail'/,
               '应逐条展示失败原因（经 t() 国际化）')
  assert.equal(zhCN['overview.inspirationBatchSummary'], '批量转 issue 完成：{n} 成功 / {m} 失败',
               '中文汇总文案应保留')
})

test('源码：批量提交调用 POST /api/inspirations/batch-add-issues', () => {
  assert.match(comp, /api\.post\('\/api\/inspirations\/batch-add-issues', \{ items \}\)/,
               '应调用 POST /api/inspirations/batch-add-issues 批量接口')
  // 成功后刷新开放 issue 与灵感列表（成功项已由后端删除）
  const fnStart = comp.indexOf('const submitBatchConvert')
  const fnEnd = comp.indexOf('\n  // 关闭批量预览面板', fnStart)
  const fnBody = comp.slice(fnStart, fnEnd > 0 ? fnEnd : comp.length)
  assert.ok(fnStart >= 0, '应存在 submitBatchConvert 函数')
  assert.match(fnBody, /loadIssues\(\)/, '批量创建成功后应刷新开放 issue 列表')
  assert.match(fnBody, /loadInspirations\(\)/, '批量创建成功后应刷新灵感列表')
  // 草稿默认值：标题=描述=内容、标签 feature, ui、目标仓库=原仓库
  assert.match(comp, /title: ins\.content/, '草稿标题默认=灵感内容')
  assert.match(comp, /labels: 'feature, ui'/, '草稿标签默认 feature, ui（逗号分隔）')
  assert.match(comp, /repo_id: ins\.repo_id/, '草稿目标仓库默认=灵感所属仓库')
})

// ---- 渲染/交互断言 ----

test('渲染：进入多选模式显示工具栏与复选框，勾选更新计数', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    // 初始无复选框（未进入多选模式）
    assert.equal(findByClass(r.renderer, 'inspiration-select-checkbox').length, 0,
                 '未进入多选模式不应渲染复选框')
    await enterSelectMode(r.renderer)
    // 三条灵感各一个复选框 + 工具栏「已选择 0 条灵感」
    assert.equal(findByClass(r.renderer, 'inspiration-select-checkbox').length, 3,
                 '多选模式下应渲染 3 个复选框')
    const text = treeText(r.renderer)
    assert.ok(text.includes('已选择 0 条灵感'), '应显示已选计数')
    assert.ok(text.includes('转为 Issue'), '应显示「转为 Issue」按钮')
    // 勾选第一条 → 计数更新
    await toggleCheckbox(r.renderer, 0)
    assert.ok(treeText(r.renderer).includes('已选择 1 条灵感'), '勾选后计数应更新')
  } finally {
    await r.unmount()
  }
})

test('交互：未选中时「转为 Issue」禁用，不打开面板', async () => {
  const r = await renderOverview()
  try {
    await enterSelectMode(r.renderer)
    const convertBtn = findByClass(r.renderer, 'inspiration-batch-convert-btn')[0]
    assert.equal(convertBtn.props.disabled, true, '未选中时「转为 Issue」应禁用')
    // 浏览器对 disabled 按钮不派发点击（直接调 onClick 会绕过禁用语义，
    // 这里只断言 disabled 属性与 handler 的 0 选中守卫）
    const fnStart = comp.indexOf('const openBatchConvert')
    const fnEnd = comp.indexOf('\n  // 编辑草稿', fnStart)
    const fnBody = comp.slice(fnStart, fnEnd > 0 ? fnEnd : comp.length)
    assert.match(fnBody, /selectedInspirationIds/, '批量入口应依赖已选灵感列表')
    assert.equal(findByClass(r.renderer, 'inspiration-batch-modal').length, 0,
                 '未选中时不应有批量预览面板')
  } finally {
    await r.unmount()
  }
})

test('交互：勾选两条 → 打开预览面板，草稿预填默认值且每条可编辑', async () => {
  const r = await renderOverview()
  try {
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0) // 灵感一（id=11）
    await toggleCheckbox(r.renderer, 2) // 灵感三（id=21，shipyard 仓库）
    const convertBtn = findByClass(r.renderer, 'inspiration-batch-convert-btn')[0]
    assert.equal(convertBtn.props.disabled, false, '已选中时「转为 Issue」应可用')
    await TestRenderer.act(async () => { convertBtn.props.onClick() })
    await new Promise((resolve) => setTimeout(resolve, 5))
    // 面板打开：两条草稿
    const modal = findByClass(r.renderer, 'inspiration-batch-modal')
    assert.equal(modal.length, 1, '应打开批量预览弹窗')
    const items = findByExactClass(r.renderer, 'inspiration-batch-item')
    assert.equal(items.length, 2, '应有 2 条预览条目')
    const text = treeText(r.renderer)
    assert.ok(text.includes('批量转 issue 预览'), '应显示预览标题')
    // 草稿预填默认值：标题=描述=灵感内容、标签 feature, ui、目标仓库=原仓库
    const titles = findByClass(r.renderer, 'inspiration-batch-title')
    assert.equal(titles[0].props.value, '灵感一', '第一条标题默认=灵感内容')
    const descs = findByClass(r.renderer, 'inspiration-batch-desc')
    assert.equal(descs[0].props.value, '灵感一', '第一条描述默认=灵感内容')
    const labels = findByClass(r.renderer, 'inspiration-batch-labels')
    assert.equal(labels[0].props.value, 'feature, ui', '第一条标签默认 feature, ui')
    const repos = findByClass(r.renderer, 'inspiration-batch-repo')
    assert.equal(Number(repos[0].props.value), 1, '第一条目标仓库默认=灵感所属仓库')
    // 编辑第一条标题
    await TestRenderer.act(async () => {
      titles[0].props.onChange({ target: { value: '改后的标题一' } })
    })
    assert.equal(findByClass(r.renderer, 'inspiration-batch-title')[0].props.value,
                 '改后的标题一', '标题编辑应生效')
  } finally {
    await r.unmount()
  }
})

test('交互：提交批量 → POST payload 携带覆盖字段，展示「N 成功 / M 失败」汇总', async () => {
  const r = await renderOverview({
    batchResult: {
      succeeded: [
        { inspiration_id: 11, issue: { iid: 77, title: '改后的标题一', web_url: 'https://gitlab.example.com/x/-/issues/77' } },
      ],
      failed: [
        { inspiration_id: 21, error: '仓库未启用' },
      ],
      summary: { succeeded: 1, failed: 1 },
    },
  })
  try {
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0) // id=11
    await toggleCheckbox(r.renderer, 2) // id=21
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-convert-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 5))
    // 编辑第一条标题 + 第二条标签
    const titles = findByClass(r.renderer, 'inspiration-batch-title')
    await TestRenderer.act(async () => {
      titles[0].props.onChange({ target: { value: '改后的标题一' } })
    })
    const labels = findByClass(r.renderer, 'inspiration-batch-labels')
    await TestRenderer.act(async () => {
      labels[1].props.onChange({ target: { value: 'bug, feature' } })
    })
    // 提交
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-submit-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.equal(r.postCalls.length, 1, '应调用一次批量接口')
    assert.equal(r.postCalls[0][0], '/api/inspirations/batch-add-issues',
                 '应提交到批量接口')
    // payload：编辑过的条目携带覆盖字段，未编辑条目只传 inspiration_id
    assert.deepEqual(r.postCalls[0][1], {
      items: [
        { inspiration_id: 11, title: '改后的标题一' },
        { inspiration_id: 21, labels: ['bug', 'feature'] },
      ],
    }, '批量 payload 应携带与默认值不同的覆盖字段')
    // 汇总：1 成功 / 1 失败
    const text = treeText(r.renderer)
    assert.ok(text.includes('批量转 issue 完成：1 成功 / 1 失败'), '应展示成功/失败汇总')
    assert.ok(text.includes('灵感 #21：仓库未启用'), '应逐条展示失败原因')
    // 失败条目仍在面板中标记失败原因；成功条目无失败标记
    const itemErrors = findByClass(r.renderer, 'inspiration-batch-item-error')
    assert.equal(itemErrors.length, 1, '只有失败条目显示失败原因')
  } finally {
    await r.unmount()
  }
})

test('交互：提交后刷新灵感与开放 issue 列表并清空已选', async () => {
  const r = await renderOverview()
  try {
    const inspBefore = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    const issuesBefore = r.getCalls.filter((p) => p === '/api/issues/overview').length
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0)
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-convert-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 5))
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-submit-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    const inspAfter = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    const issuesAfter = r.getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(inspAfter > inspBefore, '提交成功后应刷新灵感列表（成功项已删除）')
    assert.ok(issuesAfter > issuesBefore, '提交成功后应刷新开放 issue 列表')
  } finally {
    await r.unmount()
  }
})

test('交互：「全部应用默认」重置草稿为默认值', async () => {
  const r = await renderOverview()
  try {
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0)
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-convert-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 5))
    // 改乱标题/标签/目标仓库
    const titles = findByClass(r.renderer, 'inspiration-batch-title')
    const labels = findByClass(r.renderer, 'inspiration-batch-labels')
    const repos = findByClass(r.renderer, 'inspiration-batch-repo')
    await TestRenderer.act(async () => {
      titles[0].props.onChange({ target: { value: '乱写的标题' } })
      labels[0].props.onChange({ target: { value: 'x, y' } })
      repos[0].props.onChange({ target: { value: '2' } })
    })
    // 全部应用默认
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-apply-defaults-btn')[0].props.onClick()
    })
    assert.equal(findByClass(r.renderer, 'inspiration-batch-title')[0].props.value, '灵感一',
                 '「全部应用默认」应重置标题为灵感内容')
    assert.equal(findByClass(r.renderer, 'inspiration-batch-labels')[0].props.value, 'feature, ui',
                 '「全部应用默认」应重置标签为 feature, ui')
    assert.equal(Number(findByClass(r.renderer, 'inspiration-batch-repo')[0].props.value), 1,
                 '「全部应用默认」应重置目标仓库为原仓库')
  } finally {
    await r.unmount()
  }
})

test('交互：取消退出多选模式并关闭面板', async () => {
  const r = await renderOverview()
  try {
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0)
    // 工具栏「取消」退出多选
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-cancel-btn')[0].props.onClick()
    })
    assert.equal(findByClass(r.renderer, 'inspiration-select-checkbox').length, 0,
                 '取消后应退出多选模式（复选框消失）')
    // 再次进入并打开面板后，面板「取消」关闭面板并退出多选
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0)
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-convert-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 5))
    assert.equal(findByClass(r.renderer, 'inspiration-batch-modal').length, 1, '面板应打开')
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-close-btn')[0].props.onClick()
    })
    assert.equal(findByClass(r.renderer, 'inspiration-batch-modal').length, 0, '面板应关闭')
    assert.equal(findByClass(r.renderer, 'inspiration-select-checkbox').length, 0,
                 '关闭面板应退出多选模式（复选框消失）')
  } finally {
    await r.unmount()
  }
})

test('交互：请求级失败显示错误且草稿保留可重试', async () => {
  const r = await renderOverview({ batchError: '网络错误' })
  try {
    await enterSelectMode(r.renderer)
    await toggleCheckbox(r.renderer, 0)
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-convert-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 5))
    await TestRenderer.act(async () => {
      findByClass(r.renderer, 'inspiration-batch-submit-btn')[0].props.onClick()
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    const text = treeText(r.renderer)
    assert.ok(text.includes('网络错误'), '请求失败应显示错误')
    assert.equal(findByExactClass(r.renderer, 'inspiration-batch-item').length, 1,
                 '失败后草稿保留（面板不关闭）可重试')
  } finally {
    await r.unmount()
  }
})
