// 设置页左侧导航栏测试（issue #139 架构 + issue #155 修复回归）：
//
// 架构约定（issue #155）：导航栏不再硬编码 SETTINGS_GROUPS，而是通过
// collectSettingsGroups() 读取设置页（.settings-content）中**实际渲染**的
// 设置区块（section.settings-section，锚点 id 即区块 id）与分组标题
// （h2.settings-group-title）动态生成——左边导航栏与右边设置页天然一一对应，
// 任何新设置卡片挂载到设置页后导航栏自动出现对应子选项，彻底消除两边
// 对不上的 bug（回归场景：issue #152 新增「识图模型」卡片后导航栏缺
// 「识图模型」子选项）。
//
// 本测试断言：
// 1. 设置页挂载 SettingsNav，区块/分组标题按固定约定组织（section +
//    settings-section + 锚点 id、h2.settings-group-title，含「系统设置」）；
//    「识图模型」为独立设置区块（issue #155 回归：导航栏必须包含该子选项）；
// 2. collectSettingsGroups()：从设置页 DOM 结构读取分组与子项
//    （label 取区块内首个 h2、data-nav-label 可覆盖、未分组区块进兜底组）；
// 3. 全量覆盖不变式：设置页每个 section 都出现在导航中、导航每项都能在
//    设置页找到对应区块（双向对得上，彻底解决 bug）；
// 4. 渲染：搜索框、分组折叠/展开、子项链接（href=#锚点）均基于动态分组；
// 5. 折叠/展开、搜索命中、滚动高亮等交互行为保持可用。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/components/SettingsNav.jsx')
const SettingsNav = mod.default
const { collectSettingsGroups } = mod

after(() => vite.close())

const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

/** 与设置页一致的假 DOM 结构（直接子节点按渲染顺序排列） */
function fakeSettingsContent() {
  const group = (title) => ({
    tagName: 'H2', className: 'settings-group-title', textContent: title,
  })
  const section = (id, h2, extra = {}) => ({
    tagName: 'SECTION', className: 'settings-section', id,
    querySelector: () => (h2 ? { textContent: h2 } : null),
    getAttribute: (name) => extra[name] || null,
  })
  return {
    querySelectorAll: () => [
      group('外部服务接入'),
      section('settings-sso', 'Synology SSO 登录'),
      section('settings-ai-providers', 'AI API 供应商'),
      section('settings-image-models', '生图模型'),
      section('settings-vision-models', '识图模型'),
      group('系统设置'),
      section('settings-tasks', '任务调度'),
      section('settings-ui', '界面显示'),
      section('settings-notifications', '网页通知'),
      section('settings-webhook', '消息推送 Webhook'),
      group('执行引擎'),
      section('settings-claude', 'Claude Code'),
      section('settings-dsh', 'dsh 引擎'),
      group('运维与数据'),
      section('settings-environment', '本地环境检测'),
      section('settings-backup', '数据备份'),
      group('账号与安全'),
      section('settings-owner-token', 'Owner GitLab Token（issue 编辑专用）', { 'data-nav-label': 'Owner GitLab Token' }),
      section('settings-gitlab-cred', 'GitLab 凭据（只读）'),
      group('关于'),
      section('settings-version', '版本信息'),
    ],
  }
}

/** 渲染 SettingsNav（mock document.querySelector 提供 .settings-content） */
function renderNav() {
  const realDocument = global.document
  global.document = {
    querySelector: (sel) => (sel === '.settings-content' ? fakeSettingsContent() : null),
  }
  let renderer = null
  try {
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(SettingsNav))
    })
    return { renderer, root: renderer.root, restore: () => { global.document = realDocument } }
  } catch (e) {
    global.document = realDocument
    throw e
  }
}

/** 递归取节点渲染文本（button 内是 span 组合时也能匹配） */
function deepText(node) {
  if (node == null) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  const kids = Array.isArray(node.props?.children) ? node.props.children : [node.props?.children]
  return kids.map(deepText).join('')
}

/** 查找按钮：按渲染文本包含匹配（分组头=图标+标题+计数） */
function findButton(root, text) {
  return root.findAllByType('button').find((b) => deepText(b).includes(text))
}

const linkCount = (root) =>
  root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#')).length

const navIds = (groups) => groups.flatMap((g) => g.items.map((it) => it.id))

// ---------- 源码断言：Settings.jsx 集成与固定约定 ----------

test('设置页应引入并挂载 SettingsNav（左侧导航栏）', () => {
  assert.match(settings, /import SettingsNav from '\.\.\/components\/SettingsNav\.jsx'/, '应导入 SettingsNav')
  assert.match(settings, /<div className="settings-layout">/, '页面根节点应为两栏布局 settings-layout')
  assert.match(settings, /<SettingsNav \/>/, '设置页应挂载 SettingsNav')
  assert.match(settings, /<div className="settings-content">/, '应有右侧内容区 settings-content')
})

test('设置页按分组标题整理：6 个分组标题均用 settings-group-title 约定且按序出现', () => {
  const titles = ['外部服务接入', '系统设置', '执行引擎', '运维与数据', '账号与安全', '关于']
  const positions = titles.map((t) =>
    settings.search(new RegExp(`<h2 className="settings-group-title">${t}</h2>`)),
  )
  for (let i = 0; i < titles.length; i++) {
    assert.ok(positions[i] > 0, `页面应有分组标题「${titles[i]}」`)
  }
  for (let i = 0; i < positions.length - 1; i++) {
    assert.ok(positions[i] < positions[i + 1], `分组标题「${titles[i]}」应在「${titles[i + 1]}」之前`)
  }
})

test('每个设置区块带锚点 id + settings-section 类（导航读取的固定约定）', () => {
  const ids = ['settings-sso', 'settings-ai-providers', 'settings-image-models',
    'settings-vision-models', 'settings-tasks', 'settings-ui', 'settings-notifications',
    'settings-webhook', 'settings-claude', 'settings-dsh', 'settings-environment',
    'settings-backup', 'settings-owner-token', 'settings-gitlab-cred', 'settings-version']
  for (const id of ids) {
    assert.ok(
      new RegExp(`<section id="${id}" className="settings-section"[^>]*>`).test(settings),
      `设置页应存在锚点区块 <section id="${id}" className="settings-section">`,
    )
  }
})

test('「识图模型」为独立设置区块（issue #155 回归：导航栏必须包含该子选项）', () => {
  // 生图 / 识图两个卡片各占一个区块：settings-image-models 只放生图模型卡片，
  // settings-vision-models 放识图模型卡片——导航栏才能自动生成对应子选项
  assert.match(settings, /<section id="settings-image-models" className="settings-section">[\s\S]*?<ImageModelsCard \/>[\s\S]*?<\/section>/, '生图模型区块应只包含 ImageModelsCard')
  assert.match(settings, /<section id="settings-vision-models" className="settings-section">[\s\S]*?<VisionModelsCard \/>[\s\S]*?<\/section>/, '识图模型应为独立区块并挂载 VisionModelsCard')
})

test('owner-token 区块通过 data-nav-label 提供短导航名', () => {
  assert.match(
    settings,
    /<section id="settings-owner-token" className="settings-section" data-nav-label="Owner GitLab Token">/,
    'owner-token 区块应带 data-nav-label 覆盖导航显示名',
  )
})

// ---------- collectSettingsGroups：从设置页读取结构 ----------

test('collectSettingsGroups：从设置页 DOM 结构生成 6 组 15 项（含识图模型）', () => {
  const groups = collectSettingsGroups(fakeSettingsContent())
  assert.equal(groups.length, 6, '应有 6 个分组')
  assert.deepEqual(
    groups.map((g) => g.title),
    ['外部服务接入', '系统设置', '执行引擎', '运维与数据', '账号与安全', '关于'],
  )
  const items = navIds(groups)
  assert.equal(items.length, 15, '应有 15 个设置子项')
  // issue #155 回归点：必须包含「识图模型」子选项
  const vision = groups.flatMap((g) => g.items).find((it) => it.id === 'settings-vision-models')
  assert.ok(vision, '导航应包含识图模型子选项（settings-vision-models）')
  assert.equal(vision.label, '识图模型', '识图模型子项 label 应为「识图模型」')
  assert.ok(Array.isArray(vision.keywords) && vision.keywords.length > 0, '识图模型子项应带搜索关键词')
  // 生图模型区块仍存在
  const image = groups.flatMap((g) => g.items).find((it) => it.id === 'settings-image-models')
  assert.equal(image.label, '生图模型', '生图模型子项 label 应为「生图模型」')
})

test('collectSettingsGroups：label 默认取区块内首个 h2 文本，data-nav-label 可覆盖', () => {
  const groups = collectSettingsGroups(fakeSettingsContent())
  const byId = Object.fromEntries(groups.flatMap((g) => g.items).map((it) => [it.id, it.label]))
  assert.equal(byId['settings-sso'], 'Synology SSO 登录')
  assert.equal(byId['settings-tasks'], '任务调度')
  assert.equal(byId['settings-version'], '版本信息')
  // data-nav-label 覆盖：h2 是长标题，导航用短名
  assert.equal(byId['settings-owner-token'], 'Owner GitLab Token')
})

test('collectSettingsGroups：每个子项带搜索关键词（SETTING_KEYWORDS 映射）', () => {
  const groups = collectSettingsGroups(fakeSettingsContent())
  for (const it of groups.flatMap((g) => g.items)) {
    assert.ok(Array.isArray(it.keywords), `子项 ${it.id} 应带 keywords 数组`)
  }
  const byId = Object.fromEntries(groups.flatMap((g) => g.items).map((it) => [it.id, it]))
  assert.ok(byId['settings-sso'].keywords.includes('sso'), 'SSO 子项应带 sso 关键词')
  assert.ok(byId['settings-vision-models'].keywords.includes('识图'), '识图模型子项应带识图关键词')
  assert.ok(byId['settings-vision-models'].keywords.includes('vision'), '识图模型子项应带 vision 关键词')
})

test('collectSettingsGroups：未分组区块进「其他设置」兜底组，不会悄悄丢失', () => {
  const content = {
    querySelectorAll: () => [
      { tagName: 'SECTION', className: 'settings-section', id: 'settings-orphan',
        querySelector: () => ({ textContent: '孤儿设置' }), getAttribute: () => null },
    ],
  }
  const groups = collectSettingsGroups(content)
  assert.equal(groups.length, 1, '应生成兜底分组')
  assert.equal(groups[0].title, '其他设置')
  assert.equal(groups[0].items[0].id, 'settings-orphan')
})

test('collectSettingsGroups：空内容/无内容返回空数组，无 id 区块跳过', () => {
  assert.deepEqual(collectSettingsGroups(null), [])
  assert.deepEqual(collectSettingsGroups({ querySelectorAll: () => [] }), [])
  const groups = collectSettingsGroups({
    querySelectorAll: () => [
      { tagName: 'SECTION', className: 'settings-section', id: '',
        querySelector: () => ({ textContent: 'x' }), getAttribute: () => null },
    ],
  })
  assert.equal(groups.length, 0, '无 id 区块不应出现在导航中')
})

// ---------- 全量覆盖不变式：导航与设置页双向对得上 ----------

test('不变式：设置页每个设置区块都出现在导航中（不重不漏）', () => {
  // 从 Settings.jsx 源码提取全部设置区块 id，与 collectSettingsGroups 生成的导航对比
  const sectionIds = [...settings.matchAll(/<section id="(settings-[a-z-]+)" className="settings-section"[^>]*>/g)]
    .map((m) => m[1])
  assert.ok(sectionIds.length >= 15, `应解析到不少于 15 个设置区块，实际 ${sectionIds.length}`)
  const groups = collectSettingsGroups(fakeSettingsContent())
  const generated = navIds(groups)
  assert.deepEqual(
    [...generated].sort(),
    [...new Set(sectionIds)].sort(),
    '导航子项 id 集合应与设置页区块 id 集合完全一致',
  )
  assert.equal(new Set(generated).size, generated.length, '导航子项 id 不应重复')
})

test('不变式：每个分组标题下包含其全部子项区块（顺序与源码一致）', () => {
  const groups = collectSettingsGroups(fakeSettingsContent())
  const titlePos = (t) => settings.search(new RegExp(`<h2 className="settings-group-title">${t}</h2>`))
  for (let i = 0; i < groups.length; i++) {
    const g = groups[i]
    const start = titlePos(g.title)
    assert.ok(start > 0, `应有分组标题「${g.title}」`)
    const end = i + 1 < groups.length ? titlePos(groups[i + 1].title) : settings.length
    for (const it of g.items) {
      const secPos = settings.search(new RegExp(`<section id="${it.id}" className="settings-section"[^>]*>`))
      assert.ok(secPos > start, `「${g.title}」的子项「${it.label}」锚点应位于本组标题之后`)
      assert.ok(secPos < end, `「${g.title}」的子项「${it.label}」锚点应在本组区间内`)
    }
  }
})

// ---------- 源码断言：SettingsNav 不再硬编码结构 ----------

test('SettingsNav 不应再硬编码分组结构（结构来源为设置页 DOM）', () => {
  assert.ok(!navSrc.includes('SETTINGS_GROUPS'), '不应再导出硬编码的 SETTINGS_GROUPS')
  assert.match(navSrc, /collectSettingsGroups/, '应导出 collectSettingsGroups 读取设置页结构')
  assert.match(navSrc, /querySelector\('\.settings-content'\)/, '应从设置页内容区读取结构')
})

test('SettingsNav 提供搜索框、分组折叠头与子项链接', () => {
  assert.match(navSrc, /type="search"/, '应有搜索输入框')
  assert.match(navSrc, /placeholder="搜索设置项…"/, '搜索框应有占位文案')
  assert.match(navSrc, /aria-label="搜索设置项"/, '搜索框应有无障碍标签')
  assert.match(navSrc, /aria-expanded=\{!closed\}/, '分组头应暴露展开状态（aria-expanded）')
  assert.match(navSrc, /scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\)/, '点击子项应平滑滚动到锚点')
  assert.match(navSrc, /setActiveId\(id\)/, '点击子项应记录高亮 id')
})

// ---------- 渲染断言：动态分组下的交互行为 ----------

test('渲染：默认展示全部分组与子项（含识图模型），分组头可见', () => {
  const { renderer, root, restore } = renderNav()
  try {
    for (const title of ['外部服务接入', '系统设置', '执行引擎', '运维与数据', '账号与安全', '关于']) {
      const head = findButton(root, title)
      assert.ok(head, `应渲染分组头「${title}」`)
      assert.equal(head.props['aria-expanded'], true, `分组「${title}」默认应展开`)
    }
    assert.equal(linkCount(root), 15, '应渲染 15 个子项链接')
    const visionLink = root.findAll((n) => n.type === 'a' && n.props.href === '#settings-vision-models')
    assert.equal(visionLink.length, 1, '应有识图模型子项链接')
    assert.equal(deepText(visionLink[0]), '识图模型', '识图模型链接文本应为「识图模型」')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：搜索按名称过滤子项，未命中分组隐藏，命中提示可见', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '备份' } })
    })
    assert.equal(linkCount(root), 1, '搜索「备份」应只命中 1 个子项')
    assert.ok(root.findAll((n) => n.type === 'a' && n.props.href === '#settings-backup').length === 1)
    const hits = root.findAll((n) => n.type === 'p' && String(n.props.children || '').includes('共命中'))
    assert.equal(hits.length, 1, '搜索时应有命中提示')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：搜索按关键字命中（如 sso），命中分组自动展开', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    const extHead = findButton(root, '外部服务接入')
    TestRenderer.act(() => { extHead.props.onClick() })
    assert.equal(extHead.props['aria-expanded'], false, '折叠后分组应收起')

    TestRenderer.act(() => {
      input.props.onChange({ target: { value: 'sso' } })
    })
    const link = root.findAll((n) => n.type === 'a' && n.props.href === '#settings-sso')
    assert.equal(link.length, 1, '搜索关键字 sso 应命中 Synology SSO 登录（keywords 命中）')
    const extHeadNow = findButton(root, '外部服务接入')
    assert.equal(extHeadNow.props['aria-expanded'], true, '搜索时命中分组应自动展开露出匹配项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：搜索识图相关关键词可命中识图模型', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '识图' } })
    })
    assert.equal(linkCount(root), 1, '搜索「识图」应只命中识图模型子项')
    assert.ok(root.findAll((n) => n.type === 'a' && n.props.href === '#settings-vision-models').length === 1)
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：搜索无结果时展示空状态', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '不存在的设置项xyz' } })
    })
    const empty = root.findAll((n) => n.type === 'p' && String(n.props.children || '').includes('未找到'))
    assert.equal(empty.length, 1, '无结果时应展示「未找到」空状态')
    assert.equal(linkCount(root), 0, '无结果时不应渲染任何子项链接')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：清空搜索恢复全部分组', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '备份' } })
    })
    const clear = root.find((n) => n.type === 'button' && n.props['aria-label'] === '清空搜索')
    TestRenderer.act(() => { clear.props.onClick() })
    assert.equal(linkCount(root), 15, '清空搜索后应恢复全部分组子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

// ---------- 渲染断言：折叠/展开 ----------

test('渲染：点击分组头折叠子项，再次点击展开', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const head = findButton(root, '外部服务接入')
    TestRenderer.act(() => { head.props.onClick() })
    assert.equal(head.props['aria-expanded'], false, '点击后分组应收起')
    assert.equal(linkCount(root), 15 - 4, '收起「外部服务接入」后应少 4 个子项')
    TestRenderer.act(() => { head.props.onClick() })
    assert.equal(head.props['aria-expanded'], true, '再次点击后分组应展开')
    assert.equal(linkCount(root), 15, '展开后应恢复 15 个子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

test('渲染：「全部收起」后所有分组折叠，「全部展开」恢复', () => {
  const { renderer, root, restore } = renderNav()
  try {
    const collapseAll = findButton(root, '全部收起')
    assert.ok(collapseAll, '默认应有「全部收起」按钮')
    TestRenderer.act(() => { collapseAll.props.onClick() })
    assert.equal(linkCount(root), 0, '全部收起后应无子项链接')
    const expandAll = findButton(root, '全部展开')
    assert.ok(expandAll, '全部收起后按钮应变为「全部展开」')
    TestRenderer.act(() => { expandAll.props.onClick() })
    assert.equal(linkCount(root), 15, '全部展开后应恢复 15 个子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

// ---------- 渲染断言：点击滚动 ----------

test('渲染：点击子项调用 scrollIntoView 滚动到锚点并高亮', () => {
  const scrolled = []
  const realDocument = global.document
  global.document = {
    querySelector: (sel) => (sel === '.settings-content' ? fakeSettingsContent() : null),
    getElementById: (id) => ({ scrollIntoView: (opts) => scrolled.push({ id, opts }) }),
  }
  try {
    let renderer = null
    TestRenderer.act(() => {
      renderer = TestRenderer.create(React.createElement(SettingsNav))
    })
    const root = renderer.root
    try {
      const link = root.find((n) => n.type === 'a' && n.props.href === '#settings-webhook')
      TestRenderer.act(() => {
        link.props.onClick({ preventDefault: () => {} })
      })
      assert.equal(scrolled.length, 1, '点击子项应触发一次 scrollIntoView')
      assert.equal(scrolled[0].id, 'settings-webhook', '应滚动到对应锚点区块')
      assert.deepEqual(scrolled[0].opts, { behavior: 'smooth', block: 'start' }, '应为平滑滚动到区块顶部')
      const active = root.findAll(
        (n) => n.type === 'a' && String(n.props.className || '').includes('active'),
      )
      assert.equal(active.length, 1, '点击后应有一个子项处于高亮态')
      assert.equal(active[0].props.href, '#settings-webhook', '高亮项应为刚点击的子项')
    } finally {
      TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    global.document = realDocument
  }
})

// ---------- CSS 断言 ----------

test('styles.css 提供设置页两栏布局与导航样式', () => {
  for (const cls of [
    'settings-layout', 'settings-sidebar', 'settings-nav',
    'settings-nav-search', 'settings-nav-group-head',
    'settings-nav-chevron', 'settings-nav-item', 'settings-section',
    'settings-group-title',
  ]) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
  assert.match(styles, /\.settings-layout\s*\{\s*display: grid/, '设置页应为 grid 两栏布局')
  assert.match(styles, /\.settings-sidebar\s*\{\s*position: sticky/, '导航栏宽视口应吸顶')
  assert.match(styles, /@media \(max-width: 860px\)/, '窄视口应有单栏回落断点')
})
