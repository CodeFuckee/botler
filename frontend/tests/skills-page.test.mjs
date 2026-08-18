// 技能管理页面测试（issue #282）：新增「技能」页面，展示所有配置的执行
// 引擎（executor 插件）拥有的技能，并查看 / 编辑 SKILL.md 及其他技能
// 相关 md 文件。
//
// 断言：
// 1. App.jsx 顶部导航含「技能」入口，注册 /skills 路由；
// 2. 页面按引擎分组展示技能（引擎 tab + 技能列表 + md 文件 chips）；
// 3. 编辑器：加载文件内容 → 修改 → 保存走 PUT /api/skills/{engine}/file；
// 4. 后端提供 /api/skills 的 GET / files / file 读取 / PUT file 保存接口。
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
const skills = readFileSync(path.join(ROOT, 'src/pages/Skills.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiSkills = readFileSync(path.join(ROOT, '../backend/botler/api/skills.py'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Skills } = await vite.ssrLoadModule('/src/pages/Skills.jsx')

after(() => vite.close())

test('顶部导航包含「技能」入口（i18n 中英文文案）', () => {
  assert.match(app, /NavLink to="\/skills"/, '导航应有到 /skills 的 NavLink')
  assert.match(app, /t\('nav\.skills'\)/, '导航链接应经 t() 国际化')
  assert.equal(zhCN['nav.skills'], '技能', '中文文案应为「技能」')
  assert.equal(enUS['nav.skills'], 'Skills', '英文文案应为「Skills」')
})

test('App.jsx 注册 /skills 路由并挂载 Skills 页面', () => {
  assert.match(app, /Route path="\/skills" element={<Skills \/>}/, '应有 /skills 路由')
  assert.match(app, /import Skills from '\.\/pages\/Skills\.jsx'/, '应导入 Skills 页面组件')
})

test('页面按引擎分组展示技能（引擎 tab / 技能列表 / 文件 chips / 编辑器）', () => {
  assert.match(skills, /api\.get\('\/api\/skills'\)/, '列表加载走 GET /api/skills')
  assert.match(skills, /skills-engine-tabs/, '应有引擎 tab 容器')
  assert.match(skills, /api\.put\(`\/api\/skills\/\$\{encodeURIComponent\(engine\)\}\/file`/, '保存走 PUT /api/skills/{engine}/file')
  assert.match(skills, /Markdown content=\{content\}/, '预览应渲染 Markdown')
  assert.match(skills, /技能说明/, 'SKILL.md 应有「技能说明」徽章')
  assert.match(skills, /有未保存的修改，确定放弃并切换/, '切换前应有未保存确认')
})

test('styles.css 提供技能页样式（引擎 tab / 技能卡 / 文件 chip / 编辑器）', () => {
  for (const cls of ['skills-engine-tabs', 'skills-engine-tab', 'skills-skill-btn',
                     'skills-file-chip', 'skills-textarea', 'skills-preview']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

test('后端提供 /api/skills 的 GET / files / file 读取 / PUT file 保存接口', () => {
  assert.match(apiSkills, /APIRouter\(prefix="\/skills"/, 'skills API 前缀应为 /skills')
  assert.match(apiSkills, /@router\.get\(""\)/, '应有 GET /api/skills')
  assert.match(apiSkills, /@router\.get\("\/\{engine\}\/files"\)/, '应有 GET /api/skills/{engine}/files')
  assert.match(apiSkills, /@router\.get\("\/\{engine\}\/file"\)/, '应有 GET /api/skills/{engine}/file')
  assert.match(apiSkills, /@router\.put\("\/\{engine\}\/file"\)/, '应有 PUT /api/skills/{engine}/file')
})

// ---- 渲染与交互（mock fetch，与 plugins 测试同款手法） ----

function makeSkill(name, description, extra = {}) {
  return { name, description, path: `/skills/${name}`, root: '/tmp/skills', ...extra }
}

function mockFetch() {
  const calls = []
  const originalFetch = global.fetch
  const skillsData = {
    engine: 'claude',
    engines: [
      { name: 'claude', description: 'Claude Code CLI', default: true,
        roots: [{ path: '/home/u/.claude/skills', exists: true }],
        skills: [makeSkill('animate', '做动画'), makeSkill('code-testing', '测试')] },
      { name: 'hermes', description: 'hermes-agent SDK', default: false,
        roots: [{ path: '/home/u/.hermes/skills', exists: true }],
        skills: [makeSkill('grill-me', '拷问设计')] },
      { name: 'dsh', description: 'deepseek-harness SDK', default: false,
        roots: [{ path: '/home/u/.agents/skills', exists: true }],
        skills: [makeSkill('find-skills', '查找技能')] },
    ],
  }
  const filesBySkill = {
    animate: ['SKILL.md', 'RECIPES.md'],
    'code-testing': ['SKILL.md'],
    'grill-me': ['docs/guide.md', 'SKILL.md'],
    'find-skills': ['SKILL.md'],
  }
  const contents = {
    'animate|SKILL.md': '---\nname: animate\ndescription: 做动画\n---\n# animate\n',
    'animate|RECIPES.md': '# 配方\n',
    'grill-me|SKILL.md': '# grill-me\n',
  }
  global.fetch = async (p, opts) => {
    calls.push({ p: String(p), opts })
    const method = opts?.method || 'GET'
    const url = new URL(String(p), 'http://x')
    if (url.pathname === '/api/skills' && method === 'GET') {
      return { ok: true, status: 200, json: async () => skillsData }
    }
    const m = url.pathname.match(/^\/api\/skills\/([^/]+)\/files$/)
    if (m && method === 'GET') {
      const skill = url.searchParams.get('skill')
      return { ok: true, status: 200, json: async () => ({ engine: m[1], skill, files: filesBySkill[skill] || [] }) }
    }
    const fm = url.pathname.match(/^\/api\/skills\/([^/]+)\/file$/)
    if (fm && method === 'GET') {
      const skill = url.searchParams.get('skill')
      const fpath = url.searchParams.get('path')
      const content = contents[`${skill}|${fpath}`] || '# 空\n'
      return { ok: true, status: 200, json: async () => ({ engine: fm[1], skill, path: fpath, content }) }
    }
    if (fm && method === 'PUT') {
      const body = JSON.parse(opts.body)
      contents[`${body.skill}|${body.path}`] = body.content
      return { ok: true, status: 200, json: async () => ({ ok: true, engine: fm[1], skill: body.skill, path: body.path, size: body.content.length }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { calls, contents, restore: () => { global.fetch = originalFetch } }
}

test('渲染：引擎 tab 分组展示技能与文件，加载默认技能 SKILL.md', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Skills))
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const text = JSON.stringify(renderer.toJSON())
    // 引擎 tab：三个引擎 + 技能数
    assert.match(text, /claude/, '应渲染 claude 引擎 tab')
    assert.match(text, /hermes/, '应渲染 hermes 引擎 tab')
    assert.ok(text.includes('个技能'), '应展示技能数')
    // 默认选中 animate 技能并加载 SKILL.md 内容
    assert.match(text, /animate/, '应渲染技能名')
    assert.match(text, /做动画/, '应渲染技能描述')
    assert.match(text, /SKILL\.md/, '应渲染文件 chip')
    assert.match(text, /# animate/, '编辑器应加载 SKILL.md 内容')
    const buttons = renderer.root.findAllByType('button').map((b) => String(b.props.children || '').trim())
    assert.ok(buttons.some((t) => t.includes('预览')), '应有预览按钮')
    assert.ok(buttons.some((t) => t.includes('保存')), '应有保存按钮')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：切换引擎展示该引擎技能列表', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Skills))
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const tabs = renderer.root.findAll((n) => n.type === 'button' && /hermes/.test(String(n.props.children || '')))
    assert.ok(tabs.length >= 1, '应有 hermes 引擎 tab')
    await TestRenderer.act(async () => {
      tabs[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /grill-me/, '切换后应展示 hermes 的技能')
    assert.match(text, /docs\/guide\.md/, '应展示嵌套 md 文件')
    // 保存后内容落盘：修改 → 点击保存 → PUT 调用
    const textarea = renderer.root.findByType('textarea')
    await TestRenderer.act(async () => {
      textarea.props.onChange({ target: { value: '# 修改后\n' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find((b) => String(b.props.children || '').trim().includes('保存'))
    assert.ok(saveBtn, '应存在保存按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const put = m.calls.find((c) => c.p.startsWith('/api/skills/hermes/file') && c.opts?.method === 'PUT')
    assert.ok(put, '点击保存应发出 PUT /api/skills/hermes/file')
    const body = JSON.parse(put.opts.body)
    assert.equal(body.skill, 'grill-me')
    assert.equal(body.path, 'SKILL.md')
    assert.equal(body.content, '# 修改后\n')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

// ---- 补充交互：预览 / 文件切换 / 未保存确认（提升覆盖与回归保护） ----
// 经 vite 加载 dialog.js：与组件内 import 同一模块实例，测试注入直接生效
// （与 backup-manager 测试同款手法，issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

function mountSkills(m) {
  return TestRenderer.act(async () => {
    const renderer = TestRenderer.create(React.createElement(Skills))
    await new Promise((resolve) => setTimeout(resolve, 60))
    return renderer
  })
}

test('交互：预览切换渲染 Markdown 预览，再切回编辑态', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountSkills(m)
    const buttons = renderer.root.findAllByType('button')
    const previewBtn = buttons.find((b) => String(b.props.children || '').trim().includes('预览'))
    assert.ok(previewBtn, '应存在预览按钮')
    await TestRenderer.act(async () => {
      previewBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /skills-preview/, '预览态应渲染 .skills-preview 容器')
    assert.equal(renderer.root.findAllByType('textarea').length, 0, '预览态不应有 textarea')
    assert.ok(renderer.root.findAll((n) => n.type === 'h1' && String(n.props.children || '').includes('animate')).length >= 1, '预览应渲染 h1 标题（animate）')
    // 切回编辑态
    const editBtn = renderer.root.findAllByType('button')
      .find((b) => String(b.props.children || '').includes('编辑'))
    assert.ok(editBtn, '预览态应有「编辑」按钮')
    await TestRenderer.act(async () => {
      editBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    assert.ok(renderer.root.findByType('textarea'), '编辑态应有 textarea')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：点击文件 chip 切换文件并加载内容', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    renderer = await mountSkills(m)
    const chips = renderer.root.findAll((n) => n.type === 'button' && /RECIPES\.md/.test(String(n.props.children || '')))
    assert.ok(chips.length >= 1, '应有 RECIPES.md 文件 chip')
    await TestRenderer.act(async () => {
      chips[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /# 配方/, '应加载 RECIPES.md 内容')
    const got = m.calls.find((c) => c.p.includes('path=RECIPES.md') && c.opts?.method === 'GET')
    assert.ok(got, '切换文件应发出文件读取请求')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('交互：有未保存修改时切换文件先确认（取消不切换）', async () => {
  const m = mockFetch()
  const confirms = []
  dialog.installAutoAnswer((d) => { confirms.push(d.message); return false }) // 取消
  let renderer = null
  try {
    renderer = await mountSkills(m)
    const textarea = renderer.root.findByType('textarea')
    await TestRenderer.act(async () => {
      textarea.props.onChange({ target: { value: '# 未保存的修改\n' } })
    })
    // 页面应出现「未保存」徽章
    assert.match(JSON.stringify(renderer.toJSON()), /未保存/, '修改后应显示未保存徽章')
    const chips = renderer.root.findAll((n) => n.type === 'button' && /RECIPES\.md/.test(String(n.props.children || '')))
    await TestRenderer.act(async () => {
      chips[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    assert.ok(confirms.length >= 1, '切换文件前应弹确认框')
    assert.match(confirms[0], /有未保存的修改/, '确认文案应提示未保存')
    // 取消 → 仍停留在 SKILL.md（textarea 内容未被替换）
    assert.ok(renderer.root.findByType('textarea'), '仍应有编辑器')
    assert.match(JSON.stringify(renderer.toJSON()), /# 未保存的修改/, '取消后内容应保留')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    dialog.installAutoAnswer(null)
    dialog.resetDialogs()
    m.restore()
  }
})

test('交互：确认放弃后切换文件加载新内容', async () => {
  const m = mockFetch()
  dialog.installAutoAnswer(() => true) // 确定放弃
  let renderer = null
  try {
    renderer = await mountSkills(m)
    const textarea = renderer.root.findByType('textarea')
    await TestRenderer.act(async () => {
      textarea.props.onChange({ target: { value: '# 要放弃的修改\n' } })
    })
    const chips = renderer.root.findAll((n) => n.type === 'button' && /RECIPES\.md/.test(String(n.props.children || '')))
    await TestRenderer.act(async () => {
      chips[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 60))
    })
    const text = JSON.stringify(renderer.toJSON())
    assert.match(text, /# 配方/, '确认后应加载 RECIPES.md 内容')
    assert.ok(!/# 要放弃的修改/.test(text), '放弃的修改不应保留')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    dialog.installAutoAnswer(null)
    dialog.resetDialogs()
    m.restore()
  }
})
