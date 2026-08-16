// 概览页「灵感」板块测试（issue #131）：位于开放 Issue 下方、CI/CD
// 流水线上方，按仓库随手记录新功能灵感；灵感仅保存在 Botler 本地
// 数据库（/api/inspirations/*），不提交到 GitLab issue。
//
// 断言：
// 1. 源码：Overview.jsx 轮询 GET /api/inspirations/overview，增删改分别
//    调用 POST / PUT / DELETE /api/inspirations；板块源码顺序
//    issues-section < inspirations-section < pipelines-section；
// 2. 渲染：仓库卡片显示灵感内容/条数/更新时间，无灵感仓库显示空状态
//    + 随手记录表单，未启用仓库显示「未启用」徽章，空仓库列表显示
//    板块空状态；板块标题位于「开放 Issue」与「CI/CD 流水线」之间；
// 3. 交互：输入内容提交 → POST /api/inspirations 并刷新列表；编辑 →
//    文本域回填、保存调 PUT、取消退出编辑态；删除 → DELETE 并刷新；
// 4. 边界：空白内容不发请求；接口失败显示错误且不崩溃。
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

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-issues.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview, INSPIRATION_POLL_MS } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const INSPIRATIONS_PAYLOAD = {
  repos: [
    {
      repo_id: 1, repo_name: 'botler', enabled: true, priority: 10,
      inspirations: [
        { id: 11, repo_id: 1, repo_name: 'botler', content: '支持批量处理 issue', updated_at: '2026-08-16 12:00:00' },
      ],
    },
    {
      repo_id: 2, repo_name: 'shipyard', enabled: false, priority: 20,
      inspirations: [],
    },
  ],
}

// ---- 数据流源码断言 ----

test('源码：灵感板块轮询 /api/inspirations/overview 并使用独立轮询常量', () => {
  assert.match(overview, /api\.get\('\/api\/inspirations\/overview'\)/,
               '应调用 /api/inspirations/overview 聚合接口')
  assert.match(overview, /INSPIRATION_POLL_MS/, '应使用 INSPIRATION_POLL_MS 轮询常量')
  assert.equal(INSPIRATION_POLL_MS, 15000, '灵感板块轮询间隔应为 15 秒')
})

test('源码：增删改分别调用 POST / PUT / DELETE /api/inspirations', () => {
  assert.match(overview, /api\.post\('\/api\/inspirations'/,
               '记录新灵感应调用 POST /api/inspirations')
  assert.match(overview, /api\.put\('\/api\/inspirations\/' \+ insp\.id/,
               '编辑保存应调用 PUT /api/inspirations/{id}')
  assert.match(overview, /api\.del\('\/api\/inspirations\/' \+ insp\.id/,
               '删除应调用 DELETE /api/inspirations/{id}')
})

test('源码：灵感板块位于开放 Issue 与 CI/CD 流水线之间', () => {
  const issues = overview.indexOf('className="issues-section"')
  const insp = overview.indexOf('className="inspirations-section"')
  const pipes = overview.indexOf('className="pipelines-section"')
  assert.ok(issues >= 0 && insp >= 0 && pipes >= 0,
            '三个板块标记都应存在')
  assert.ok(issues < insp, '开放 Issue 板块应位于灵感板块之前')
  assert.ok(insp < pipes, '灵感板块应位于 CI/CD 流水线板块之前')
})

// ---- 渲染辅助 ----

// Overview 挂载后轮询 tasks / pipelines / issues / inspirations 四个端点，
// mock 按路径分流；灵感数据可注入。返回 renderer / getCalls / api 方法调用记录。
async function renderOverview({
  inspirationsPayload = INSPIRATIONS_PAYLOAD,
  inspirationsError = null,
} = {}) {
  const getCalls = []
  const postCalls = []
  const putCalls = []
  const delCalls = []
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname === '/api/inspirations/overview') {
      if (inspirationsError) throw new Error(inspirationsError)
      return inspirationsPayload
    }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname, body) => { postCalls.push([pathname, body]); return { id: 99 } })
  mock.method(api, 'put', async (pathname, body) => { putCalls.push([pathname, body]); return {} })
  mock.method(api, 'del', async (pathname) => { delCalls.push(pathname); return null })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 等待首轮四个数据接口的 promise flush
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return {
    renderer, renderError, getCalls, postCalls, putCalls, delCalls,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// 渲染整棵树为扁平文本（深度优先，与视觉顺序一致）
function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}

// 定位元素辅助
function findByClass(renderer, cls) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes(cls))
}
function findButton(renderer, cls) {
  const list = findByClass(renderer, cls)
  assert.ok(list.length > 0, `找不到按钮 ${cls}`)
  return list[0]
}

// ---- 渲染级断言 ----

test('渲染：灵感板块位于「开放 Issue」与「CI/CD 流水线」之间', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    const issueTitle = text.indexOf('开放 Issue')
    const inspTitle = text.indexOf('💡 灵感')
    const pipeTitle = text.indexOf('CI/CD 流水线')
    assert.ok(issueTitle >= 0 && inspTitle >= 0 && pipeTitle >= 0,
              `三板块标题都应存在（issue=${issueTitle} insp=${inspTitle} pipe=${pipeTitle}）`)
    assert.ok(issueTitle < inspTitle, '「开放 Issue」应位于「💡 灵感」之前')
    assert.ok(inspTitle < pipeTitle, '「💡 灵感」应位于「CI/CD 流水线」之前')
    // 轮询到了灵感接口
    assert.ok(r.getCalls.includes('/api/inspirations/overview'),
              '应轮询 /api/inspirations/overview')
  } finally {
    await r.unmount()
  }
})

test('渲染：仓库卡片显示灵感内容、条数与更新时间', async () => {
  const r = await renderOverview()
  try {
    const text = treeText(r.renderer)
    assert.ok(text.includes('支持批量处理 issue'), '应渲染灵感内容')
    // JSX 插值会把数字与后缀拆成独立文本节点，分开断言
    assert.ok(text.includes('条灵感'), '应显示灵感条数')
    assert.ok(text.includes('"1"'), '应显示数量 1（JSON 序列化文本节点）')
    assert.ok(text.includes('botler'), '应显示仓库名')
    assert.match(text, /刚刚|分钟前|小时前|天前|月前|年前/, '应显示更新时间（fmtAgo）')
  } finally {
    await r.unmount()
  }
})

test('渲染：无灵感仓库显示空状态与随手记录表单，未启用仓库带徽章', async () => {
  const r = await renderOverview()
  try {
    const text = treeText(r.renderer)
    assert.ok(text.includes('暂无灵感，记一条吧'), '无灵感仓库应显示空状态')
    assert.ok(text.includes('记一条关于该仓库的新功能灵感'), '应渲染随手记录表单')
    assert.ok(text.includes('未启用'), '未启用仓库应显示徽章')
    // 每个仓库卡片都有记录按钮
    const addBtns = findByClass(r.renderer, 'inspiration-add-btn')
    assert.equal(addBtns.length, 2, '两个仓库卡片各有一个记录按钮')
  } finally {
    await r.unmount()
  }
})

test('渲染：无任何仓库时显示板块空状态', async () => {
  const r = await renderOverview({ inspirationsPayload: { repos: [] } })
  try {
    const text = treeText(r.renderer)
    assert.ok(text.includes('暂无灵感（未配置仓库）'), '无仓库应显示板块空状态')
    assert.ok(text.includes('💡 灵感'), '板块标题仍应渲染')
  } finally {
    await r.unmount()
  }
})

test('渲染：接口失败显示错误且不崩溃', async () => {
  const r = await renderOverview({ inspirationsError: '数据库不可用' })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('数据库不可用'), '应显示灵感接口错误')
    assert.ok(text.includes('💡 灵感'), '板块骨架仍应渲染')
  } finally {
    await r.unmount()
  }
})

// ---- 交互级断言 ----

// 向第 index 个仓库卡片的记录表单输入内容并提交
async function addInspiration(renderer, index, content) {
  const textareas = findByClass(renderer, 'inspiration-textarea')
  const form = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('inspiration-add-form'))[index]
  const ta = textareas[index]
  await TestRenderer.act(async () => {
    ta.props.onChange({ target: { value: content } })
  })
  await TestRenderer.act(async () => {
    form.props.onSubmit({ preventDefault() {} })
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
}

test('交互：输入内容提交 → POST /api/inspirations 并刷新列表', async () => {
  const r = await renderOverview()
  try {
    const before = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    await addInspiration(r.renderer, 0, '支持一键归档灵感')
    assert.equal(r.postCalls.length, 1, '应调用一次 POST')
    assert.deepEqual(r.postCalls[0][1], { repo_id: 1, content: '支持一键归档灵感' },
                     'POST 参数应为 {repo_id, content}')
    const after = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    assert.ok(after > before, '提交成功后应重新拉取灵感列表')
  } finally {
    await r.unmount()
  }
})

test('交互：空白内容不发起 POST 请求', async () => {
  const r = await renderOverview()
  try {
    await addInspiration(r.renderer, 0, '   ')
    assert.equal(r.postCalls.length, 0, '空白内容不应调用 POST')
  } finally {
    await r.unmount()
  }
})

test('交互：编辑 → 保存调 PUT，取消退出编辑态', async () => {
  const r = await renderOverview()
  try {
    // 进入编辑态
    const editBtn = findButton(r.renderer, 'inspiration-action-btn')
    await TestRenderer.act(async () => { editBtn.props.onClick() })
    const textareas = findByClass(r.renderer, 'inspiration-textarea')
    // 编辑态 = 2 个仓库卡片的添加表单 + 1 个编辑文本域；文档序首个是编辑文本域
    assert.equal(textareas.length, 3, '两个添加表单 + 一个编辑文本域')
    const editTa = textareas[0]
    assert.equal(editTa.props.value, '支持批量处理 issue', '编辑文本域应回填原内容')
    // 修改并保存
    await TestRenderer.act(async () => {
      editTa.props.onChange({ target: { value: '支持批量处理 issue（改）' } })
    })
    const saveBtn = findButton(r.renderer, 'inspiration-save-btn')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(r.putCalls.length, 1, '应调用一次 PUT')
    assert.deepEqual(r.putCalls[0], ['/api/inspirations/11', { content: '支持批量处理 issue（改）' }],
                     'PUT 参数应为 {content}')
    // 取消路径：再次进入编辑后点取消
    const editBtn2 = findButton(r.renderer, 'inspiration-action-btn')
    await TestRenderer.act(async () => { editBtn2.props.onClick() })
    const cancelBtn = rendererRootFindByText(r.renderer, '取消')
    await TestRenderer.act(async () => { cancelBtn.props.onClick() })
    const tas = findByClass(r.renderer, 'inspiration-textarea')
    assert.equal(tas.length, 2, '取消后应只剩两个添加表单文本域')
  } finally {
    await r.unmount()
  }
})

// 按文本内容找按钮（取消按钮无专属类名）
function rendererRootFindByText(renderer, text) {
  const btns = renderer.root.findAll((n) => n.type === 'button')
  const hit = btns.find((b) => JSON.stringify(b.props.children).includes(text))
  assert.ok(hit, `找不到文本为 ${text} 的按钮`)
  return hit
}

test('交互：删除调 DELETE 并刷新列表', async () => {
  const r = await renderOverview()
  try {
    const delBtn = findButton(r.renderer, 'inspiration-delete-btn')
    await TestRenderer.act(async () => {
      delBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.equal(r.delCalls.length, 1, '应调用一次 DELETE')
    assert.equal(r.delCalls[0], '/api/inspirations/11', '应删除 id=11 的灵感')
  } finally {
    await r.unmount()
  }
})
