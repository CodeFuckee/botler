// 概览页「灵感」板块测试（issue #131/#293）：灵感组件保持原始位置——
// 位于「开放 Issue」板块下方、CI/CD 流水线上方（issue #293 明确
// 「灵感组件还是和原来一样，放在开放issue组建的下方」；issue #184
// 曾把灵感板块整体移入右侧常驻边栏，本次回退该布局调整）；灵感 AI
// 对话面板保持右侧边栏抽屉形式（drawer-overlay + drawer chat-drawer，
// issue #166/#184）；灵感仅保存在 Botler 本地数据库
// （/api/inspirations/*），不提交到 GitLab issue。
//
// 断言：
// 1. 源码：Overview.jsx 轮询 GET /api/inspirations/overview，增删改分别
//    调用 POST / PUT / DELETE /api/inspirations；单列布局——灵感板块
//    （inspirations-section）位于开放 Issue（issues-section）与 CI/CD
//    流水线（pipelines-section）之间，无 overview-layout / overview-main
//    / overview-sidebar 双栏标记；对话面板为右侧抽屉；
// 2. 渲染：仓库卡片显示灵感内容/条数/更新时间，无灵感仓库显示空状态
//    + 随手记录表单，未启用仓库显示「未启用」徽章，空仓库列表显示
//    板块空状态；板块渲染顺序为 开放 Issue → 灵感 → CI/CD 流水线；
// 3. 交互：输入内容提交 → POST /api/inspirations 并刷新列表；编辑 →
//    文本域回填、保存调 PUT、取消退出编辑态；删除 → DELETE 并刷新；
// 4. 边界：空白内容不发请求；接口失败显示错误且不崩溃。
import { after, mock, test } from 'node:test'

// 渲染树节点 → 纯文本（递归；Lucide 图标等元素无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
// issue #201 拆分：灵感板块独立组件 + 数据加载收敛到 useOverviewData hook，
// 源码断言按板块归属分别读取对应文件（页面组合 / hook / 灵感组件）。
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')

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
  assert.match(overview, /api\.get\('\/api\/inspirations\/overview', \{ silent: true \}\)/,
               '应调用 /api/inspirations/overview 聚合接口（轮询静默，issue #226）')
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

test('源码：灵感板块位于开放 Issue 下方、CI/CD 流水线上方，无右侧边栏（issue #293）', () => {
  // issue #201 拆分：三个板块为独立组件，板块顺序由 Overview.jsx
  // 组合顺序（组件挂载顺序）保证——开放 Issue → 灵感 → CI/CD 流水线
  const insp = overview.indexOf('<InspirationSection')
  const issues = overview.indexOf('<IssueListSection')
  const pipes = overview.indexOf('<PipelineSection')
  assert.ok(insp >= 0 && issues >= 0 && pipes >= 0,
            '三个板块组件都应挂载')
  assert.ok(issues < insp && insp < pipes,
            '灵感板块组件应位于开放 Issue 组件（IssueListSection）与 CI/CD 流水线组件（PipelineSection）之间（开放 Issue 下方）')
  assert.ok(!overview.includes('className="overview-sidebar"'),
            '不应再有右侧常驻边栏（overview-sidebar）——灵感组件保持原始位置')
  assert.ok(!overview.includes('className="overview-layout"'),
            '不应再有双栏布局容器（overview-layout）')
  assert.ok(!overview.includes('className="overview-main"'),
            '不应再有左列容器（overview-main）')
})

// ---- 渲染辅助 ----

// Overview 挂载后轮询 tasks / pipelines / issues / inspirations 四个端点，
// mock 按路径分流；灵感数据可注入。返回 renderer / getCalls / api 方法调用记录。
async function renderOverview({
  inspirationsPayload = INSPIRATIONS_PAYLOAD,
  inspirationsError = null,
  inspirationsAddResult = null,
  inspirationsAddError = null,
  inspirationPageResult = null,
  // issue #166：灵感 AI 对话 mock——chatMessages 对话历史（null=空）、
  // chatMessagesError 加载历史失败、chatSendResult 发送返回（null=默认
  // user+assistant 两消息）、chatSendError 发送失败
  chatMessages = null,
  chatMessagesError = null,
  chatSendResult = null,
  chatSendError = null,
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
    if (pathname.startsWith('/api/inspirations/pages/')) {
      if (!inspirationPageResult) throw new Error('unexpected inspiration page')
      return inspirationPageResult
    }
    if (pathname.startsWith('/api/inspirations/') && pathname.endsWith('/messages')) {
      if (chatMessagesError) throw new Error(chatMessagesError)
      return { messages: chatMessages === null ? [] : chatMessages }
    }
    throw new Error('unexpected ' + pathname)
  })
  mock.method(api, 'post', async (pathname, body) => {
    postCalls.push([pathname, body])
    // issue #143：一键提交 issue 的响应可注入（成功对象 / 抛错）
    if (String(pathname).endsWith('/add-issue')) {
      if (inspirationsAddError) throw new Error(inspirationsAddError)
      return inspirationsAddResult || {
        iid: 99, title: '灵感内容',
        web_url: 'https://gitlab.example.com/x/-/issues/99',
      }
    }
    // issue #166：灵感 AI 对话发送——返回用户消息 + AI 回复
    if (String(pathname).endsWith('/messages')) {
      if (chatSendError) throw new Error(chatSendError)
      if (chatSendResult) return chatSendResult
      return {
        messages: [
          { id: 101, role: 'user', content: body.content,
            created_at: '2026-08-17 12:00:00' },
          { id: 102, role: 'assistant', content: 'AI 的探讨回复',
            created_at: '2026-08-17 12:00:01' },
        ],
      }
    }
    return { id: 99 }
  })
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
// 编辑按钮（issue #143：编辑与「添加 Issue」共享 inspiration-action-btn
// 基类——添加按钮带专属 inspiration-add-issue-btn 类，编辑按钮无专属
// 类名，按类排除 + 文案双重匹配）
function findEditButton(renderer) {
  const btns = renderer.root.findAll((n) => n.type === 'button')
  const hit = btns.find((b) =>
    String(b.props.className || '').includes('inspiration-action-btn')
    && !String(b.props.className || '').includes('inspiration-add-issue-btn')
    && textOf(b.props.children).includes('编辑'))
  assert.ok(hit, '找不到编辑按钮')
  return hit
}

// ---- 渲染级断言 ----

test('渲染：灵感板块位于开放 Issue 下方、CI/CD 流水线上方（issue #293）', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const sidebars = findByClass(r.renderer, 'overview-sidebar')
    assert.equal(sidebars.length, 0, '不应渲染右侧常驻边栏（overview-sidebar）')
    const mains = findByClass(r.renderer, 'overview-main')
    assert.equal(mains.length, 0, '不应渲染左列容器（overview-main）')
    const text = treeText(r.renderer)
    const issueTitle = text.indexOf('开放 Issue')
    const inspTitle = text.indexOf('灵感')
    const pipeTitle = text.indexOf('CI/CD 流水线')
    assert.ok(issueTitle >= 0 && inspTitle >= 0 && pipeTitle >= 0,
              '三个板块标题都应渲染')
    assert.ok(issueTitle < inspTitle, '「开放 Issue」板块应位于「灵感」板块之前')
    assert.ok(inspTitle < pipeTitle, '「灵感」板块应位于「CI/CD 流水线」板块之前')
    assert.ok(text.includes('支持批量处理 issue'), '应渲染灵感内容')
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
    assert.ok(text.includes('灵感'), '板块标题仍应渲染')
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
    assert.ok(text.includes('灵感'), '板块骨架仍应渲染')
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
    const editBtn = findEditButton(r.renderer)
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
    const editBtn2 = findEditButton(r.renderer)
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
  const hit = btns.find((b) => textOf(b.props.children).includes(text))
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

// ---- issue #143：灵感一键提交为 GitLab issue ----

// 在已挂载的 Overview 中点击第 index 条灵感的「添加 Issue」按钮
async function clickAddIssue(renderer, index = 0) {
  const btns = renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('inspiration-add-issue-btn'))
  assert.ok(btns.length > index, `找不到第 ${index} 个「添加 Issue」按钮`)
  await TestRenderer.act(async () => {
    btns[index].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  return btns[index]
}

test('源码：「添加 Issue」按钮位于「编辑」按钮左侧', () => {
  const add = overview.indexOf("tr('overview.addIssue')")
  const edit = overview.indexOf("tr('common.edit')")
  const del = overview.indexOf("tr('common.delete')")
  assert.ok(add >= 0 && edit >= 0 && del >= 0, '三个操作按钮文案都应存在')
  assert.ok(add < edit, '「添加 Issue」应位于「编辑」左侧')
  assert.ok(edit < del, '「编辑」应位于「删除」左侧')
})

test('源码：一键提交调用 POST /api/inspirations/{id}/add-issue 并刷新灵感列表', () => {
  assert.match(overview, /api\.post\(`\/api\/inspirations\/\$\{ins\.id\}\/add-issue`\)/,
               '应调用 POST /api/inspirations/{id}/add-issue')
  // issue #162：创建成功后端已删除该灵感，前端应刷新灵感列表移除条目
  // （不等 15 秒轮询）；成功提示保留展示新 issue 链接
  const fnStart = overview.indexOf('const addIssueFromInspiration')
  const fnEnd = overview.indexOf('\n  // 对账', fnStart)
  const fnBody = overview.slice(fnStart, fnEnd > 0 ? fnEnd : overview.length)
  assert.ok(fnStart >= 0, '应存在 addIssueFromInspiration 函数')
  assert.match(fnBody, /loadIssues\(\)/, '创建成功后应刷新开放 issue 列表')
  assert.match(fnBody, /loadInspirations\(\)/,
               '创建成功后应刷新灵感列表（issue #162）')
})

test('渲染：每条灵感都有「添加 Issue」按钮且位于编辑按钮左侧', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    // 第一条灵感（id=11）的操作区按钮顺序：添加 Issue → 编辑 → 删除
    const items = r.renderer.root.findAll(
      (n) => String(n.props.className || '').includes('inspiration-item'))
    assert.ok(items.length >= 1, '应有灵感条目')
    const buttons = items[0].findAll((n) => n.type === 'button')
    const texts = buttons.map((b) => textOf(b.props.children || ''))
    const addIdx = texts.findIndex((t) => t.includes('添加 Issue'))
    const editIdx = texts.findIndex((t) => t.includes('编辑'))
    const delIdx = texts.findIndex((t) => t.includes('删除'))
    assert.ok(addIdx >= 0, '应渲染「添加 Issue」按钮')
    assert.ok(addIdx < editIdx, '「添加 Issue」应位于「编辑」左侧')
    assert.ok(editIdx < delIdx, '「编辑」应位于「删除」左侧')
  } finally {
    await r.unmount()
  }
})

test('交互：点击添加 issue → POST /api/inspirations/{id}/add-issue 并刷新开放 issue 列表', async () => {
  const r = await renderOverview({ inspirationsAddResult: {
    iid: 77, title: '灵感内容', web_url: 'https://gitlab.example.com/x/-/issues/77',
  } })
  try {
    const issuesBefore = r.getCalls.filter((p) => p === '/api/issues/overview').length
    const inspBefore = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    await clickAddIssue(r.renderer, 0)
    assert.equal(r.postCalls.length, 1, '应调用一次 POST')
    assert.equal(r.postCalls[0][0], '/api/inspirations/11/add-issue',
                 '应提交 id=11 的灵感')
    const issuesAfter = r.getCalls.filter((p) => p === '/api/issues/overview').length
    assert.ok(issuesAfter > issuesBefore, '创建成功后应重新拉取开放 issue 列表')
    const inspAfter = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    assert.ok(inspAfter > inspBefore,
              '创建成功后应重新拉取灵感列表（issue #162，条目已删除）')
    const text = treeText(r.renderer)
    assert.ok(text.includes('issue #77'), '应展示新 issue 编号')
    assert.ok(text.includes('https://gitlab.example.com/x/-/issues/77'),
              '应展示新 issue 链接')
  } finally {
    await r.unmount()
  }
})

test('交互：提交中按钮禁用，重复点击只发一次请求', async () => {
  // post 桩返回挂起 promise（不 resolve），模拟请求进行中
  let resolvePost = null
  const r = await renderOverview()
  mock.method(api, 'post', async (pathname, body) => {
    r.postCalls.push([pathname, body])
    if (String(pathname).endsWith('/add-issue')) {
      return new Promise((resolve) => { resolvePost = resolve })
    }
    return { id: 99 }
  })
  try {
    const btn = await clickAddIssue(r.renderer, 0)
    // 请求进行中：按钮应禁用（防重复提交）
    assert.equal(btn.props.disabled, true, '请求中按钮应禁用')
    // 重复点击（disabled 仅阻止浏览器事件，测试直接调用 onClick 模拟
    // 极端并发）——handler 内置 addingIssueInspIds 守卫，只发一次请求
    const btn2 = r.renderer.root.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('inspiration-add-issue-btn'))[0]
    await TestRenderer.act(async () => { btn2.props.onClick() })
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.equal(r.postCalls.length, 1, '重复点击不应发起第二次请求')
    // 释放挂起请求
    await TestRenderer.act(async () => { resolvePost({ iid: 77, title: 'x' }) })
  } finally {
    await r.unmount()
  }
})

test('交互：提交失败显示错误且不刷新灵感列表', async () => {
  const r = await renderOverview({ inspirationsAddError: 'owner token 未配置' })
  try {
    const inspBefore = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    await clickAddIssue(r.renderer, 0)
    const text = treeText(r.renderer)
    assert.ok(text.includes('owner token 未配置'), '应显示提交错误')
    assert.ok(text.includes('灵感'), '板块骨架仍应渲染')
    // issue #162：未成功推送不删除灵感，失败后不应刷新灵感列表
    // （条目保留可重试，避免界面闪烁）
    const inspAfter = r.getCalls.filter((p) => p === '/api/inspirations/overview').length
    assert.equal(inspAfter, inspBefore, '提交失败不应刷新灵感列表')
  } finally {
    await r.unmount()
  }
})

// ---- issue #166：灵感 AI 对话 ----

// 点击第 index 个灵感条目的「💬 对话」按钮（第一个仓库卡片内）
async function openInspirationChat(renderer, index = 0) {
  const btns = renderer.root.findAll((n) =>
    n.type === 'button'
    && String(n.props.className || '').includes('inspiration-chat-btn'))
  assert.ok(btns.length > index, `找不到对话按钮（共 ${btns.length} 个）`)
  await TestRenderer.act(async () => { btns[index].props.onClick() })
  await new Promise((resolve) => setTimeout(resolve, 10))
}

test('源码：灵感条目有「对话」按钮，调用 GET/POST /api/inspirations/{id}/messages', () => {
  assert.match(overview, /name="message" \/> \{tr\('overview\.chat'\)\}/, '「对话」按钮应经 t() 国际化')
  assert.equal(zhCN['overview.chat'], '对话', '中文「对话」文案应保留')
  assert.match(overview, /api\.get\(`\/api\/inspirations\/\$\{ins\.id\}\/messages`\)/,
               '打开面板应 GET /api/inspirations/{id}/messages 加载历史')
  assert.match(overview, /api\.post\(`\/api\/inspirations\/\$\{chatInspiration\.id\}\/messages`/,
               '发送消息应 POST /api/inspirations/{id}/messages')
  // issue #466：对话面板右侧边栏抽屉由 ResizableDrawer 容器统一渲染
  // （基础类名 .drawer + drawerClass 追加 chat-drawer，结构重构但右侧
  // 抽屉形态不变）
  assert.match(overview, /<ResizableDrawer\s+drawerClass="chat-drawer"/,
               '对话面板应经 ResizableDrawer 渲染为右侧边栏抽屉（chat-drawer）')
  assert.match(overview, /drawer-overlay/, '应使用右侧抽屉遮罩（drawer-overlay）')
  assert.ok(!overview.includes('chat-modal'), '对话面板不应再使用居中 modal（chat-modal）')
  assert.match(overview, /chat-msg-user/, '用户消息应有独立气泡样式')
  assert.match(overview, /chat-msg-ai/, 'AI 消息应有独立气泡样式')
})

test('交互：点击「对话」打开面板并加载历史消息', async () => {
  const history = [
    { id: 1, role: 'user', content: '之前问过的问题', created_at: '2026-08-17 10:00:00' },
    { id: 2, role: 'assistant', content: '之前的回复', created_at: '2026-08-17 10:00:05' },
  ]
  const r = await renderOverview({ chatMessages: history })
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    await openInspirationChat(r.renderer, 0)
    // 打开面板后应请求历史接口
    assert.ok(r.getCalls.includes('/api/inspirations/11/messages'),
              '打开面板应 GET 灵感对话历史（id=11）')
    const text = treeText(r.renderer)
    assert.ok(text.includes('与 AI 探讨灵感'), '应渲染对话面板标题')
    assert.ok(text.includes('之前问过的问题'), '应渲染历史用户消息')
    assert.ok(text.includes('之前的回复'), '应渲染历史 AI 消息')
    assert.ok(text.includes('支持批量处理 issue'), '面板顶部应展示灵感内容摘要')
  } finally {
    await r.unmount()
  }
})

test('交互：无历史时显示引导文案', async () => {
  const r = await renderOverview()
  try {
    await openInspirationChat(r.renderer, 0)
    const text = treeText(r.renderer)
    assert.ok(text.includes('还没有对话，向 AI 说说你对这条灵感的想法吧'),
              '空历史应显示引导文案')
  } finally {
    await r.unmount()
  }
})

// 向对话面板输入内容并提交
async function sendChat(renderer, content) {
  // 精确匹配 textarea（chat-input-row 也含 'chat-input' 子串，需排除）
  const ta = renderer.root.findAll(
    (n) => n.type === 'textarea'
      && String(n.props.className || '').includes('chat-input'))[0]
  await TestRenderer.act(async () => {
    ta.props.onChange({ target: { value: content } })
  })
  const form = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('chat-input-row'))[0]
  await TestRenderer.act(async () => {
    form.props.onSubmit({ preventDefault() {} })
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
}

test('交互：发送消息 → POST 并渲染用户消息 + AI 回复，输入清空', async () => {
  const r = await renderOverview()
  try {
    await openInspirationChat(r.renderer, 0)
    const postBefore = r.postCalls.length
    await sendChat(r.renderer, '这个灵感怎么落地？')
    assert.equal(r.postCalls.length, postBefore + 1, '应调用一次 POST')
    assert.deepEqual(r.postCalls.at(-1),
                     ['/api/inspirations/11/messages', { content: '这个灵感怎么落地？' }],
                     'POST 参数应为 {content}')
    const text = treeText(r.renderer)
    assert.ok(text.includes('这个灵感怎么落地？'), '应渲染用户消息')
    assert.ok(text.includes('AI 的探讨回复'), '应渲染 AI 回复')
    const ta = r.renderer.root.findAll(
      (n) => n.type === 'textarea'
        && String(n.props.className || '').includes('chat-input'))[0]
    assert.equal(ta.props.value, '', '发送成功后输入框应清空')
  } finally {
    await r.unmount()
  }
})

test('交互：空白消息不发起 POST', async () => {
  const r = await renderOverview()
  try {
    await openInspirationChat(r.renderer, 0)
    const postBefore = r.postCalls.length
    await sendChat(r.renderer, '   ')
    assert.equal(r.postCalls.length, postBefore, '空白消息不应调用 POST')
  } finally {
    await r.unmount()
  }
})

test('交互：发送失败显示错误且输入保留可重试', async () => {
  const r = await renderOverview({ chatSendError: '未配置 AI 对话模型' })
  try {
    await openInspirationChat(r.renderer, 0)
    await sendChat(r.renderer, '重要提问')
    const text = treeText(r.renderer)
    assert.ok(text.includes('未配置 AI 对话模型'), '应显示发送错误')
    const ta = r.renderer.root.findAll(
      (n) => n.type === 'textarea'
        && String(n.props.className || '').includes('chat-input'))[0]
    assert.equal(ta.props.value, '重要提问', '失败后输入应保留供重试')
  } finally {
    await r.unmount()
  }
})

test('交互：加载历史失败显示错误且面板可关闭', async () => {
  const r = await renderOverview({ chatMessagesError: '历史加载失败' })
  try {
    await openInspirationChat(r.renderer, 0)
    const text = treeText(r.renderer)
    assert.ok(text.includes('历史加载失败'), '应显示历史加载错误')
    // 关闭面板（× 按钮）
    const closeBtn = r.renderer.root.findAll((n) =>
      n.type === 'button' && String(n.props.className || '').includes('modal-close'))[0]
    await TestRenderer.act(async () => { closeBtn.props.onClick() })
    const after = treeText(r.renderer)
    assert.ok(!after.includes('chat-drawer'), '关闭后抽屉应卸载')
  } finally {
    await r.unmount()
  }
})

// ---- issue #219：大量灵感按仓库折叠 + 懒加载分页 ----

test('源码：灵感概览展开后使用仓库分页接口，默认页大小为 20', () => {
  assert.match(overview, /\/api\/inspirations\/pages\/\$\{repo\.repo_id\}\?offset=\$\{offset\}&limit=\$\{INSPIRATION_PAGE_SIZE\}/,
    '展开仓库应请求带 offset 的分页接口')
  assert.match(overview, /INSPIRATION_PAGE_SIZE\s*=\s*20/,
    '前端每次仅加载 20 条灵感，限制单次 DOM 增量')
  assert.match(overview, /inspiration_total/, '应使用后端返回的总数，而非仅已加载条数')
})

test('渲染：分页灵感仓库默认折叠，展开后才请求第一页', async () => {
  const largePayload = {
    repos: [{
      repo_id: 7, repo_name: 'large-repo', enabled: true, priority: 10,
      inspiration_total: 1000, inspiration_has_more: true, inspirations: [],
    }],
  }
  const r = await renderOverview({
    inspirationsPayload: largePayload,
    inspirationPageResult: {
      repo_id: 7, total: 1000, offset: 0, limit: 20, has_more: true,
      inspirations: [{ id: 701, repo_id: 7, repo_name: 'large-repo', content: '第一页灵感', updated_at: '2026-08-16 12:00:00' }],
    },
  })
  try {
    const text = treeText(r.renderer)
    assert.ok(text.includes('1000'), '折叠态应展示总条数')
    assert.ok(!text.includes('第一页灵感'), '默认折叠态不应渲染灵感条目')
    const toggle = findButton(r.renderer, 'inspiration-toggle-btn')
    assert.equal(toggle.props['aria-expanded'], false, '大量灵感默认折叠')
    await TestRenderer.act(async () => {
      toggle.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(r.getCalls.includes('/api/inspirations/pages/7?offset=0&limit=20'),
      '展开时才按第一页懒加载')
    assert.ok(treeText(r.renderer).includes('第一页灵感'), '第一页返回后再渲染条目')
  } finally {
    await r.unmount()
  }
})
