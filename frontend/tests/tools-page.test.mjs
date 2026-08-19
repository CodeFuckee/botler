// 工具管理页面测试（issue #172）：新增「工具」页面，管理给 agent 使用的
// MCP 工具——内置市场下载 / URL 导入 / 远端市场索引 / 自定义编写，
// 启用中的工具在任务执行时注入工作区 .mcp.json（Claude Code 项目级
// MCP 配置，全局生效）。
//
// 断言：
// 1. App.jsx 顶部导航含「工具」入口，注册 /tools 路由；
// 2. 页面展示已安装工具列表（名称/描述/类型/来源/启停开关）；
// 3. 新建 / 编辑表单：名称/描述/类型/命令参数/环境变量，保存走
//    POST /api/tools / PUT /api/tools/{id}；
// 4. 内置市场安装 POST /api/tools/install；URL 导入 POST /api/tools/import；
//    远端市场索引 POST /api/tools/market-index 候选逐个安装；
// 5. 后端提供 /api/tools 全套接口（GET/POST/PUT/DELETE + install/import/
//    market-index），executor 注入 .mcp.json。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const lazyPages = readFileSync(path.join(ROOT, 'src/pages/lazy.jsx'), 'utf8')
const tools = readFileSync(path.join(ROOT, 'src/pages/Tools.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiTools = readFileSync(path.join(ROOT, '../backend/botler/api/tools.py'), 'utf8')
const executorProcess = readFileSync(path.join(ROOT, '../backend/botler/executor/process.py'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Tools } = await vite.ssrLoadModule('/src/pages/Tools.jsx')
const dialog = await vite.ssrLoadModule('/src/dialog.js')

after(() => vite.close())

test('顶部导航包含「工具」入口（i18n 中英文文案）', () => {
  assert.match(app, /NavLink to="\/tools"/, '导航应有到 /tools 的 NavLink')
  assert.match(app, /t\('nav\.tools'\)/, '导航链接应经 t() 国际化')
  assert.equal(zhCN['nav.tools'], '工具', '中文文案应为「工具」')
  assert.equal(enUS['nav.tools'], 'Tools', '英文文案应为「Tools」')
})

test('App.jsx 注册 /tools 路由并挂载 Tools 页面', () => {
  assert.match(app, /Route path="\/tools" element={<Tools \/>}/, '应有 /tools 路由')
  assert.match(lazyPages, /export const Tools = lazy\(\(\) => import\('\.\/Tools\.jsx'\)\)/, 'lazy.jsx 应包装 Tools 页面')
})

test('页面结构：工具列表 / 搜索 / 新建 / 启停 / 市场 / 导入 / 索引', () => {
  assert.match(tools, /api\.get\('\/api\/tools'\)/, '列表加载走 GET /api/tools')
  assert.match(tools, /api\.post\('\/api\/tools'/, '新建走 POST /api/tools')
  assert.match(tools, /api\.put\(`\/api\/tools\/\$\{editing\.id\}`/, '编辑走 PUT /api/tools/{id}')
  assert.match(tools, /api\.del\(`\/api\/tools\/\$\{tool\.id\}`/, '删除走 DELETE /api/tools/{id}')
  assert.match(tools, /api\.post\('\/api\/tools\/install'/, '市场安装走 POST /api/tools/install')
  assert.match(tools, /api\.post\('\/api\/tools\/import'/, 'URL 导入走 POST /api/tools/import')
  assert.match(tools, /api\.post\('\/api\/tools\/market-index'/, '市场索引走 POST /api/tools/market-index')
  assert.match(tools, /工具市场/, '应有工具市场板块')
  assert.match(tools, /新建自定义工具/, '应有新建按钮')
  assert.match(tools, /搜索工具/, '应有搜索框')
})

test('styles.css 提供工具页样式（列表 / 表单 / 市场 / 启停开关）', () => {
  for (const cls of ['tools-toolbar', 'tools-list', 'tools-item', 'tools-form',
                     'tools-market', 'tools-market-section', 'tools-toggle']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

test('后端提供 /api/tools 全套接口与 executor 注入', () => {
  assert.match(apiTools, /APIRouter\(prefix="\/tools"/, 'tools API 前缀应为 /tools')
  assert.match(apiTools, /@router\.get\(""\)/, '应有 GET /api/tools')
  assert.match(apiTools, /@router\.post\(""\)/, '应有 POST /api/tools')
  assert.match(apiTools, /@router\.put\("\/\{tool_id\}"\)/, '应有 PUT /api/tools/{id}')
  assert.match(apiTools, /@router\.delete\("\/\{tool_id\}"\)/, '应有 DELETE /api/tools/{id}')
  assert.match(apiTools, /@router\.post\("\/install"\)/, '应有 POST /api/tools/install')
  assert.match(apiTools, /@router\.post\("\/import"\)/, '应有 POST /api/tools/import')
  assert.match(apiTools, /@router\.post\("\/market-index"\)/, '应有 POST /api/tools/market-index')
  // executor 注入 .mcp.json
  assert.match(executorProcess, /_inject_mcp_tools/, 'executor 应有 MCP 工具注入方法')
  assert.match(executorProcess, /\.mcp\.json/, '注入应写工作区 .mcp.json')
})

// ---- 渲染与交互（mock fetch，与 skills 页测试同款手法） ----

function makeTool(overrides = {}) {
  return {
    id: 1, name: 'web-fetch', description: '网页抓取',
    kind: 'stdio', command: 'npx', args: ['-y', 'server-fetch'], env: {},
    url: '', source: 'builtin', source_url: '', enabled: true,
    created_at: '2026-08-19 00:00:00', updated_at: '2026-08-19 00:00:00',
    ...overrides,
  }
}

function mockFetch() {
  const calls = []
  const originalFetch = global.fetch
  const toolsData = {
    tools: [
      makeTool(),
      makeTool({ id: 2, name: 'http-bridge', kind: 'http',
                 url: 'https://mcp.example.com/bridge', source: 'custom',
                 description: '远程端点', enabled: false, command: '' }),
    ],
    market: [
      { name: 'web-fetch', description: '网页抓取', kind: 'stdio', command: 'npx', args: ['-y', 'server-fetch'], env: {}, url: '' },
      { name: 'filesystem', description: '文件系统', kind: 'stdio', command: 'npx', args: ['-y', 'server-filesystem', '/tmp'], env: {}, url: '' },
    ],
    market_index_url: '',
  }
  let nextId = 10
  const store = [...toolsData.tools]

  global.fetch = async (p, opts) => {
    calls.push({ p: String(p), opts })
    const method = opts?.method || 'GET'
    const url = new URL(String(p), 'http://x')
    if (url.pathname === '/api/tools' && method === 'GET') {
      return { ok: true, status: 200, json: async () => ({ ...toolsData, tools: store }) }
    }
    if (url.pathname === '/api/tools' && method === 'POST') {
      const body = JSON.parse(opts.body)
      const tool = makeTool({ id: nextId++, name: body.name, description: body.description,
                              kind: body.kind, source: body.source || 'custom', ...body })
      store.push(tool)
      return { ok: true, status: 200, json: async () => tool }
    }
    const m = url.pathname.match(/^\/api\/tools\/(\d+)$/)
    if (m && method === 'PUT') {
      const id = Number(m[1])
      const body = JSON.parse(opts.body)
      const idx = store.findIndex((t) => t.id === id)
      const updated = { ...store[idx], ...body }
      store[idx] = updated
      return { ok: true, status: 200, json: async () => updated }
    }
    if (m && method === 'DELETE') {
      const id = Number(m[1])
      const idx = store.findIndex((t) => t.id === id)
      if (idx >= 0) store.splice(idx, 1)
      return { ok: true, status: 200, json: async () => ({ ok: true, id }) }
    }
    if (url.pathname === '/api/tools/install' && method === 'POST') {
      const { name } = JSON.parse(opts.body)
      if (store.some((t) => t.name === name)) {
        return { ok: false, status: 400, json: async () => ({ detail: `工具已安装: ${name}` }) }
      }
      const tool = makeTool({ id: nextId++, name, source: 'builtin' })
      store.push(tool)
      return { ok: true, status: 200, json: async () => tool }
    }
    if (url.pathname === '/api/tools/import' && method === 'POST') {
      const { url: importUrl } = JSON.parse(opts.body)
      const tool = makeTool({ id: nextId++, name: 'imported-tool', source: 'url', source_url: importUrl })
      store.push(tool)
      return { ok: true, status: 200, json: async () => ({ imported: [tool], count: 1 }) }
    }
    if (url.pathname === '/api/tools/market-index' && method === 'POST') {
      return { ok: true, status: 200, json: async () => ({
        candidates: [{ name: 'idx-tool', description: '索引工具', kind: 'stdio', command: 'python3', args: [], env: {}, url: '' }],
        count: 1, market_index_url: String(p),
      }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { calls, store, restore: () => { global.fetch = originalFetch } }
}

async function mountTools(m) {
  return TestRenderer.act(async () => {
    const renderer = TestRenderer.create(React.createElement(Tools))
    await new Promise((resolve) => setTimeout(resolve, 80))
    return renderer
  })
}

test('渲染：展示已安装工具列表与市场', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountTools(m)
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /web-fetch/, '应渲染工具名')
    assert.match(text, /网页抓取/, '应渲染工具描述')
    assert.match(text, /http-bridge/, '应渲染第二个工具')
    assert.match(text, /已启用/, '启用工具应显示已启用')
    assert.match(text, /已停用/, '停用工具应显示已停用')
    assert.match(text, /内置市场/, '应渲染内置市场板块')
    assert.match(text, /filesystem/, '应渲染市场工具')
    assert.match(text, /从 URL 导入/, '应渲染导入板块')
    assert.match(text, /远端市场索引/, '应渲染索引板块')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：新建自定义工具（表单 → POST /api/tools）', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountTools(m)
    const buttons = renderer.root.findAllByType('button')
    const createBtn = buttons.find((b) => String(b.props.children || '').includes('新建自定义工具'))
    assert.ok(createBtn, '应有新建按钮')
    await TestRenderer.act(async () => {
      createBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const inputs = renderer.root.findAllByType('input')
    const setName = (value) => {
      const nameInput = renderer.root.findAll((n) => n.type === 'input'
        && n.props.value === '' && n.props.placeholder && /^如 my-tool/.test(n.props.placeholder))
      assert.ok(nameInput.length, '应有名称输入框')
      nameInput[0].props.onChange({ target: { value } })
    }
    // 填名称（placeholder 含「my-tool」）
    const nameField = renderer.root.findAll((n) => n.type === 'input'
      && /my-tool/.test(String(n.props.placeholder || '')))
    assert.ok(nameField.length >= 1, '应有名称输入框')
    await TestRenderer.act(async () => {
      nameField[0].props.onChange({ target: { value: 'my-new-tool' } })
    })
    // 填描述（placeholder 含「工具用途」）
    const descField = renderer.root.findAll((n) => n.type === 'input'
      && /工具用途/.test(String(n.props.placeholder || '')))
    assert.ok(descField.length >= 1, '应有描述输入框')
    await TestRenderer.act(async () => {
      descField[0].props.onChange({ target: { value: '新工具' } })
    })
    // 填命令（placeholder 含「npx / python3」）
    const cmdField = renderer.root.findAll((n) => n.type === 'input'
      && /npx \/ python3/.test(String(n.props.placeholder || '')))
    assert.ok(cmdField.length >= 1, '应有命令输入框')
    await TestRenderer.act(async () => {
      cmdField[0].props.onChange({ target: { value: 'python3' } })
    })
    const saveBtn = renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').trim() === '保存')
    assert.ok(saveBtn, '应有保存按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const post = m.calls.find((c) => String(c.p) === '/api/tools' && c.opts?.method === 'POST')
    assert.ok(post, '保存应发出 POST /api/tools')
    const body = JSON.parse(post.opts.body)
    assert.equal(body.name, 'my-new-tool')
    assert.equal(body.command, 'python3')
    assert.deepEqual(body.args, [])
    // 列表刷新后包含新工具
    assert.ok(m.store.some((t) => t.name === 'my-new-tool'), '新工具应入库')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：启停开关（PUT /api/tools/{id}）', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountTools(m)
    const toggles = renderer.root.findAllByType('button')
      .filter((b) => /tools-toggle/.test(b.props.className || ''))
    assert.ok(toggles.length >= 1, '应有启停开关')
    await TestRenderer.act(async () => {
      toggles[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const put = m.calls.find((c) => /^\/api\/tools\/\d+$/.test(String(c.p)) && c.opts?.method === 'PUT')
    assert.ok(put, '切换应发出 PUT /api/tools/{id}')
    assert.equal(JSON.parse(put.opts.body).enabled, false, '首次切换应停用')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：删除需确认（取消不删除 / 确认后 DELETE）', async () => {
  const m = mockFetch()
  const confirms = []
  dialog.installAutoAnswer((d) => { confirms.push(d.message); return false }) // 取消
  let renderer = null
  try {
    renderer = await mountTools(m)
    const delBtns = renderer.root.findAllByType('button')
      .filter((b) => String(b.props.children || '').includes('删除'))
    assert.ok(delBtns.length >= 1, '应有删除按钮')
    await TestRenderer.act(async () => {
      delBtns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    assert.ok(confirms.length >= 1, '删除前应弹确认框')
    assert.match(confirms[0], /确认删除工具/, '确认文案应包含工具名提示')
    const del = m.calls.find((c) => c.opts?.method === 'DELETE')
    assert.ok(!del, '取消删除不应发出 DELETE')
    // 确认删除
    dialog.installAutoAnswer(() => true)
    await TestRenderer.act(async () => {
      delBtns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const del2 = m.calls.find((c) => c.opts?.method === 'DELETE')
    assert.ok(del2, '确认后应发出 DELETE /api/tools/{id}')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：内置市场安装 / URL 导入', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountTools(m)
    // 市场安装（filesystem 未安装）
    const installBtns = renderer.root.findAllByType('button')
      .filter((b) => String(b.props.children || '').includes('安装'))
    assert.ok(installBtns.length >= 1, '应有安装按钮')
    await TestRenderer.act(async () => {
      installBtns[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const install = m.calls.find((c) => String(c.p) === '/api/tools/install' && c.opts?.method === 'POST')
    assert.ok(install, '应发出 POST /api/tools/install')
    // URL 导入
    const urlInput = renderer.root.findAll((n) => n.type === 'input'
      && /tool-repo/.test(String(n.props.placeholder || '')))
    assert.ok(urlInput.length >= 1, '应有导入地址输入框')
    await TestRenderer.act(async () => {
      urlInput[0].props.onChange({ target: { value: 'https://github.com/u/repo' } })
    })
    const importBtn = renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes('导入'))
    await TestRenderer.act(async () => {
      importBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const imp = m.calls.find((c) => String(c.p) === '/api/tools/import' && c.opts?.method === 'POST')
    assert.ok(imp, '应发出 POST /api/tools/import')
    assert.equal(JSON.parse(imp.opts.body).url, 'https://github.com/u/repo')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：远端市场索引拉取候选并安装', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountTools(m)
    const idxInput = renderer.root.findAll((n) => n.type === 'input'
      && /market\.example/.test(String(n.props.placeholder || '')))
    assert.ok(idxInput.length >= 1, '应有索引地址输入框')
    await TestRenderer.act(async () => {
      idxInput[0].props.onChange({ target: { value: 'https://market.example/tools.json' } })
    })
    const fetchBtn = renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes('拉取'))
    await TestRenderer.act(async () => {
      fetchBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const idx = m.calls.find((c) => String(c.p) === '/api/tools/market-index' && c.opts?.method === 'POST')
    assert.ok(idx, '应发出 POST /api/tools/market-index')
    // 候选展示 + 安装（在 .tools-candidates 区域内找安装按钮）
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /idx-tool/, '应展示候选工具')
    const candidatesArea = renderer.root.find((n) => n.props.className
      && String(n.props.className).includes('tools-candidates'))
    const candInstall = candidatesArea.findAllByType('button')
      .find((b) => String(b.props.children || '').includes('安装'))
    assert.ok(candInstall, '候选区应有安装按钮')
    await TestRenderer.act(async () => {
      candInstall.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 80))
    })
    const create = m.calls.find((c) => String(c.p) === '/api/tools' && c.opts?.method === 'POST')
    assert.ok(create, '候选安装应发出 POST /api/tools')
    assert.equal(JSON.parse(create.opts.body).source, 'market')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
