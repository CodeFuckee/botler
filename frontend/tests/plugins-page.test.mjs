// 插件管理页面测试（issue #145）：新增独立「插件」页面，所有插件的
// 安装、卸载和设置都在这个界面。
//
// 断言：
// 1. App.jsx 顶部导航含「插件」入口，注册 /plugins 路由；
// 2. 页面按分类分组展示全部插件（内置徽章 / 外部路径 / 供应商预设）；
// 3. 安装表单：输入模块路径 → POST /api/plugins/install，成功清空输入；
// 4. 外部插件可卸载（confirmDialog 确认后 POST /api/plugins/uninstall），
//    内置插件不渲染卸载按钮；
// 5. 默认执行引擎设置：executor 插件 radio → PUT /api/plugins/settings；
// 6. 「重新加载外部插件」按钮 → POST /api/plugins/reload；
// 7. 后端提供 /api/plugins 的 GET / POST install / POST uninstall /
//    POST reload / PUT settings 接口。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const plugins = readFileSync(path.join(ROOT, 'src/pages/Plugins.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiPlugins = readFileSync(path.join(ROOT, '../backend/botler/api/plugins.py'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Plugins } = await vite.ssrLoadModule('/src/pages/Plugins.jsx')

after(() => vite.close())

test('顶部导航包含「插件」入口', () => {
  assert.match(app, /NavLink to="\/plugins"/, '导航应有到 /plugins 的 NavLink')
  assert.match(app, /插件/, '导航链接文案应为「插件」')
})

test('App.jsx 注册 /plugins 路由并挂载 Plugins 页面', () => {
  assert.match(app, /Route path="\/plugins" element={<Plugins \/>}/, '应有 /plugins 路由')
  assert.match(app, /import Plugins from '\.\/pages\/Plugins\.jsx'/, '应导入 Plugins 页面组件')
})

test('页面按分类分组展示插件（分类标题/描述/内置徽章/外部路径）', () => {
  assert.match(plugins, /执行引擎/, '应展示「执行引擎」分类')
  assert.match(plugins, /大模型供应商/, '应展示「大模型供应商」分类')
  assert.match(plugins, /消息通知通道/, '应展示「消息通知通道」分类')
  assert.match(plugins, /内置/, '内置插件应标记「内置」')
  assert.match(plugins, /api\.get\('\/api\/plugins'\)/, '列表加载走 GET /api/plugins')
})

test('安装表单：路径输入框 + 安装按钮走 POST /api/plugins/install', () => {
  assert.match(plugins, /安装外部插件/, '应有「安装外部插件」区块')
  assert.match(plugins, /textarea/, '应有路径输入框（每行一个模块路径）')
  assert.match(plugins, /api\.post\('\/api\/plugins\/install'/, '安装走 POST /api/plugins/install')
  assert.match(plugins, /split\(.\\n.\)/, '输入应按换行拆分为多个路径')
})

test('外部插件可卸载（确认后 POST /api/plugins/uninstall），内置不渲染卸载按钮', () => {
  assert.match(plugins, /confirmDialog/, '卸载应先弹确认框')
  assert.match(plugins, /api\.post\('\/api\/plugins\/uninstall'/, '卸载走 POST /api/plugins/uninstall')
  assert.match(plugins, /builtin\s*\?/, '内置/外部插件应有条件渲染分支')
  assert.match(plugins, /卸载/, '外部插件应有「卸载」按钮')
})

test('默认执行引擎设置：executor 插件 radio + 保存走 PUT /api/plugins/settings', () => {
  assert.match(plugins, /默认执行引擎/, '应有「默认执行引擎」设置区')
  assert.match(plugins, /type="radio"/, '引擎选择应使用 radio（executor 插件）')
  assert.match(plugins, /api\.put\('\/api\/plugins\/settings'/, '保存走 PUT /api/plugins/settings')
})

test('「重新加载外部插件」按钮走 POST /api/plugins/reload', () => {
  assert.match(plugins, /重新加载/, '应有「重新加载外部插件」按钮')
  assert.match(plugins, /api\.post\('\/api\/plugins\/reload'/, '重载走 POST /api/plugins/reload')
})

test('styles.css 提供插件页样式（plugin-card / plugin-kind / plugin-path）', () => {
  for (const cls of ['plugin-card', 'plugin-kind', 'plugin-path', 'plugin-actions']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

test('后端提供 /api/plugins 的 GET/POST install/uninstall/reload/PUT settings 接口', () => {
  assert.match(apiPlugins, /APIRouter\(prefix="\/plugins"/, 'plugins API 前缀应为 /plugins')
  assert.match(apiPlugins, /@router\.get\(""\)/, '应有 GET /api/plugins')
  assert.match(apiPlugins, /@router\.post\("\/install"\)/, '应有 POST /api/plugins/install')
  assert.match(apiPlugins, /@router\.post\("\/uninstall"\)/, '应有 POST /api/plugins/uninstall')
  assert.match(apiPlugins, /@router\.post\("\/reload"\)/, '应有 POST /api/plugins/reload')
  assert.match(apiPlugins, /@router\.put\("\/settings"\)/, '应有 PUT /api/plugins/settings')
})

// ---- 渲染与交互（mock fetch /api/plugins，与 settings 测试同款手法） ----

function mockFetch({ engine = 'claude', pluginPaths = [] } = {}) {
  const calls = []
  const originalFetch = global.fetch
  const base = {
    engine,
    plugin_paths: pluginPaths,
    plugins: {
      executor: [
        { kind: 'executor', name: 'claude', description: 'Claude Code CLI 无头模式（默认执行引擎）', version: '1.0', builtin: true, path: null, display_name: '', default_base_url: '', default_model: '' },
        { kind: 'executor', name: 'hermes', description: 'hermes-agent SDK 引擎（进程内调用，issue #171）', version: '1.0', builtin: true, path: null, display_name: '', default_base_url: '', default_model: '' },
        { kind: 'executor', name: 'dsh', description: 'deepseek-harness SDK 引擎', version: '1.0', builtin: true, path: null, display_name: '', default_base_url: '', default_model: '' },
      ],
      model_provider: [
        { kind: 'model_provider', name: 'gemini_nano_banana', description: 'Google Gemini Nano Banana Pro', version: '1.0', builtin: true, path: null, display_name: 'Gemini Nano Banana Pro', default_base_url: 'https://generativelanguage.googleapis.com/v1beta', default_model: 'gemini-3-pro-image' },
      ],
      notifier: [
        { kind: 'notifier', name: 'webhook', description: '任务完成外部 Webhook HTTP 消息推送', version: '1.0', builtin: true, path: null, display_name: '', default_base_url: '', default_model: '' },
        { kind: 'notifier', name: 'feishu_channel', description: '飞书消息通道（外部插件）', version: '1.0', builtin: false, path: '/opt/plugins/feishu.py', display_name: '', default_base_url: '', default_model: '' },
      ],
    },
  }
  global.fetch = async (p, opts) => {
    calls.push({ p: String(p), opts })
    const method = opts?.method || 'GET'
    if (p === '/api/plugins' && method === 'GET') {
      return { ok: true, status: 200, json: async () => base }
    }
    if (p === '/api/plugins/install' && method === 'POST') {
      const body = JSON.parse(opts.body)
      base.plugin_paths = [...new Set([...base.plugin_paths, body.path])]
      return { ok: true, status: 200, json: async () => base }
    }
    if (p === '/api/plugins/uninstall' && method === 'POST') {
      const body = JSON.parse(opts.body)
      base.plugin_paths = base.plugin_paths.filter((x) => x !== body.path)
      base.plugins.notifier = base.plugins.notifier.filter((x) => x.path !== body.path)
      return { ok: true, status: 200, json: async () => base }
    }
    if (p === '/api/plugins/reload' && method === 'POST') {
      return { ok: true, status: 200, json: async () => base }
    }
    if (p === '/api/plugins/settings' && method === 'PUT') {
      const body = JSON.parse(opts.body)
      base.engine = body.engine
      return { ok: true, status: 200, json: async () => base }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { calls, restore: () => { global.fetch = originalFetch } }
}

test('渲染：分类分组展示内置与外部插件', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Plugins))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /执行引擎/, '应渲染执行引擎分类')
    assert.match(text, /Claude Code CLI/, '应展示 claude 插件描述')
    assert.match(text, /飞书消息通道（外部插件）/, '应展示外部插件')
    assert.match(text, /\/opt\/plugins\/feishu\.py/, '外部插件应展示来源路径')
    assert.match(text, /Gemini Nano Banana Pro/, '模型供应商应展示默认预设名称')
    const buttons = renderer.root.findAllByType('button').map((b) => String(b.props.children || '').trim())
    assert.ok(buttons.some((t) => t === '卸载'), '外部插件应有卸载按钮')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：输入路径点击安装提交 POST /api/plugins/install', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Plugins))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const textarea = renderer.root.findByType('textarea')
    await TestRenderer.act(async () => {
      textarea.props.onChange({ target: { value: '/tmp/my_plugin.py' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const installBtn = buttons.find((b) => String(b.props.children || '').trim().includes('安装'))
    assert.ok(installBtn, '应存在安装按钮')
    await TestRenderer.act(async () => {
      installBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const post = m.calls.find((c) => c.p === '/api/plugins/install')
    assert.ok(post, '点击安装应发出 POST /api/plugins/install')
    assert.equal(JSON.parse(post.opts.body).path, '/tmp/my_plugin.py')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：切换默认执行引擎后保存提交 PUT /api/plugins/settings', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Plugins))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const radios = renderer.root.findAll((n) => n.type === 'input' && n.props.type === 'radio')
    assert.ok(radios.length >= 3, 'executor 插件应有 radio 选项')
    const dshRadio = radios.find((r) => r.props.value === 'dsh')
    await TestRenderer.act(async () => {
      dshRadio.props.onChange({ target: { value: 'dsh' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find((b) => String(b.props.children || '').trim() === '保存')
    assert.ok(saveBtn, '应存在引擎设置保存按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const put = m.calls.find((c) => c.p === '/api/plugins/settings' && c.opts?.method === 'PUT')
    assert.ok(put, '点击保存应发出 PUT /api/plugins/settings')
    assert.equal(JSON.parse(put.opts.body).engine, 'dsh')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
