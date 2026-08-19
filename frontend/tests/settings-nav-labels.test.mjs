// 设置页左侧导航栏「设置项名称」测试（issue #174 第二轮重写）：
//
// 需求：确保每个设置项都有对应的名称——左侧导航栏子项的名称来自设置页
// 实际渲染的名称（区块内首个 <h2> 文本，或 data-nav-label 覆盖），任何
// settings-* 设置区块都必须有名称，禁止把原始 id（如 settings-vision-models）
// 当作名称展示在导航栏上。
//
// 【第二轮根因（本轮新增）】第一轮只做静态源码解析，遗漏了真实运行时缺陷：
// SettingsNav 挂载时**只读取一次** .settings-content 的 DOM（useLayoutEffect
// 空依赖），而 AiProvidersCard / ImageModelsCard / VisionModelsCard 三个
// 卡片在数据加载中（内部 useState 初始为 null）直接 return null——区块内
// 没有 <h2>，导航名称回退成原始 id（settings-ai-providers /
// settings-image-models / settings-vision-models），且数据到达后导航不会
// 重建，异常永久保留。修复（本提交）：三个卡片与 BackupManager 同款，
// 加载中/加载失败也渲染卡片标题 <h2>，保证 SettingsNav 挂载时区块内
// 始终有名称来源。
//
// 测试层次：
// A. 源码链路（静态）：16 个设置区块都有名称来源、名称不等于原始 id、
//    名称快照、4 个卡片区块 h2、SETTING_KEYWORDS 双向一致、兜底行为文档化；
// B. 真实运行时（动态，本轮新增）：
//    - 三个卡片「数据加载中」渲染标题 h2（根因回归：杜绝 return null）；
//    - 三个卡片「加载失败」渲染标题 h2 并支持点击重试；
//    - 数据到达后卡片渲染标题 h2；
//    - 真实渲染 Settings 页：每个设置区块在渲染树中都有 h2（找出依赖
//      异步数据提供名称的三个设置项，验证修复后名称来源不缺失）；
//    - collectSettingsGroups + SettingsNav 渲染：16 项全部为可读名称。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settingsSrc = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/components/SettingsNav.jsx')
const SettingsNav = mod.default
const { collectSettingsGroups } = mod
const AiProvidersCard = (await vite.ssrLoadModule('/src/components/AiProvidersCard.jsx')).default
const ImageModelsCard = (await vite.ssrLoadModule('/src/components/ImageModelsCard.jsx')).default
const VisionModelsCard = (await vite.ssrLoadModule('/src/components/VisionModelsCard.jsx')).default
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 去标签化：提取 JSX 文本节点并压缩空白（与浏览器 textContent 对齐） */
function stripTags(s) {
  return s.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
}

/** 递归取渲染节点文本 */
function deepText(node) {
  if (node == null) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  const kids = Array.isArray(node.props?.children) ? node.props.children : [node.props?.children]
  return kids.map(deepText).join('')
}

/** 从 renderer.toJSON() 提取整棵渲染树文本（根实例无 props，不能直接用 deepText） */
function jsonText(node) {
  if (node == null) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(jsonText).join('')
  return (node.children || []).map(jsonText).join('')
}

/** 解析 Settings.jsx 全部设置区块（按文档顺序）：
 *  [{ id, navLabel, h2Text, cards }] —— h2Text 为区块内直接 h2，cards 为区块内卡片组件名 */
function sectionEntries() {
  const SECTION_RE = /<section id="([^"]+)" className="settings-section"([^>]*)>([\s\S]*?)<\/section>/g
  const entries = []
  let m
  while ((m = SECTION_RE.exec(settingsSrc)) !== null) {
    const [, id, attrs, body] = m
    const navLabel = /data-nav-label="([^"]+)"/.exec(attrs)?.[1] || null
    const h2 = /<h2[^>]*>([\s\S]*?)<\/h2>/.exec(body)
    const cards = [...body.matchAll(/<([A-Z][A-Za-z0-9]*(?:Card|Manager))[^>]*\/>/g)].map((x) => x[1])
    entries.push({ id, navLabel, h2Text: h2 ? stripTags(h2[1]) : null, cards })
  }
  return entries
}

/** 读取卡片组件源码内首个 h2 文本（即该卡片在设置页渲染出的区块名称）；
 *  先剔除 JSX 块注释（{/* ... *\/}），避免注释里出现 <h2> 字样干扰匹配；
 *  issue #201 拆分后卡片组件在 components/ 与 components/settings/ 两处 */
function cardH2Text(componentName) {
  const candidates = [
    path.join(ROOT, 'src/components', `${componentName}.jsx`),
    path.join(ROOT, 'src/components/settings', `${componentName}.jsx`),
  ]
  let src = null
  for (const p of candidates) {
    try { src = readFileSync(p, 'utf8'); break } catch { /* 尝试下一路径 */ }
  }
  assert.ok(src, `${componentName} 组件文件应存在（src/components/ 或 src/components/settings/）`)
  src = src.replace(/\/\*[\s\S]*?\*\//g, '')
  const h2 = /<h2[^>]*>([\s\S]*?)<\/h2>/.exec(src)
  assert.ok(h2, `${componentName} 组件内应有 <h2> 作为设置项名称来源`)
  return stripTags(h2[1])
}

/** 与 collectSettingsGroups 相同的名称解析顺序：
 *  data-nav-label > 区块内首个 h2 > 卡片组件内 h2 > 原始 id（兜底，不应被触发） */
function resolveName(entry) {
  if (entry.navLabel) return entry.navLabel
  if (entry.h2Text) return entry.h2Text
  if (entry.cards.length > 0) return cardH2Text(entry.cards[0])
  return entry.id
}

/** 17 个设置项名称快照（issue #174：每个设置项都有对应的名称；
 *  issue #229 新增「聚合告警」设置区块） */
const EXPECTED_NAMES = {
  'settings-sso': 'Synology SSO 登录',
  'settings-ai-providers': 'AI API 供应商',
  'settings-image-models': '生图模型',
  'settings-vision-models': '识图模型',
  'settings-minio': 'MinIO 对象存储',
  'settings-tasks': '任务调度',
  'settings-ui': '界面显示',
  'settings-notifications': '网页通知',
  'settings-alerts': '聚合告警',
  'settings-webhook': '消息推送 Webhook',
  'settings-claude': 'Claude Code',
  'settings-dsh': 'dsh 引擎',
  'settings-environment': '本地环境检测',
  'settings-backup': '数据备份',
  'settings-owner-token': 'Owner GitLab Token',
  'settings-gitlab-cred': 'GitLab 凭据（只读）',
  'settings-version': '版本信息',
}

// ---------- A. 源码链路：每个设置区块都有名称来源 ----------

test('设置页应包含 17 个设置区块且 id 唯一（与已知设置项一致）', () => {
  const entries = sectionEntries()
  assert.equal(entries.length, 17, `应解析到 17 个设置区块，实际 ${entries.length}`)
  const ids = entries.map((e) => e.id)
  assert.equal(new Set(ids).size, ids.length, '设置区块 id 不应重复')
  assert.deepEqual(
    Object.keys(EXPECTED_NAMES).sort(),
    ids.sort(),
    '设置区块 id 集合应与已知设置项快照一致（新增设置项需同步快照）',
  )
})

test('每个设置区块都有名称来源：直接 h2 / data-nav-label / 卡片组件内 h2', () => {
  for (const entry of sectionEntries()) {
    assert.ok(
      entry.navLabel || entry.h2Text || entry.cards.length > 0,
      `${entry.id} 区块缺少名称来源：应有直接 <h2>、data-nav-label 或卡片组件内 <h2>` +
      '，否则导航栏会把原始 id 当名称展示',
    )
  }
})

test('每个设置项的名称不等于原始 id（settings-vision-models 等不允许作为导航名称）', () => {
  for (const entry of sectionEntries()) {
    const name = resolveName(entry)
    assert.ok(name, `${entry.id} 应解析出非空名称`)
    assert.notEqual(name, entry.id, `${entry.id} 的名称不应回退成原始 id（如 settings-vision-models）`)
    assert.ok(!name.includes('settings-'), `${entry.id} 的名称「${name}」不应含 settings- 前缀，必须是用户可读名称`)
  }
})

test('17 个已知设置项名称快照：每个设置项都有对应的名称', () => {
  const byId = Object.fromEntries(sectionEntries().map((e) => [e.id, resolveName(e)]))
  for (const [id, expected] of Object.entries(EXPECTED_NAMES)) {
    assert.equal(byId[id], expected, `${id} 应有对应名称「${expected}」，实际「${byId[id]}」`)
  }
})

test('卡片区块名称由卡片组件内 h2 提供（16 个卡片区块与导航名称一致）', () => {
  // issue #201 拆分后：除「版本信息」为页面内联 h2 外，其余 16 个设置区块
  // 全部由卡片组件提供区块名称（含新拆出的 SsoCard / TasksCard 等；
  // issue #229 新增 AlertsCard「聚合告警」）
  const cardSections = sectionEntries().filter((e) => e.cards.length > 0)
  assert.equal(cardSections.length, 16, '应有 16 个卡片区块（版本信息为页面内联 h2）')
  const expected = {
    'settings-sso': 'Synology SSO 登录',
    'settings-ai-providers': 'AI API 供应商',
    'settings-image-models': '生图模型',
    'settings-vision-models': '识图模型',
    'settings-minio': 'MinIO 对象存储',
    'settings-tasks': '任务调度',
    'settings-ui': '界面显示',
    'settings-notifications': '网页通知',
    'settings-alerts': '聚合告警',
    'settings-webhook': '消息推送 Webhook',
    'settings-claude': 'Claude Code',
    'settings-dsh': 'dsh 引擎',
    'settings-environment': '本地环境检测',
    'settings-backup': '数据备份',
    'settings-owner-token': 'Owner GitLab Token',
    'settings-gitlab-cred': 'GitLab 凭据（只读）',
  }
  for (const entry of cardSections) {
    assert.ok(entry.cards.length === 1, `${entry.id} 应恰好引用一个卡片组件`)
    const h2 = cardH2Text(entry.cards[0])
    assert.ok(h2, `${entry.id} 的卡片组件 ${entry.cards[0]} 内应有 <h2> 作为名称来源`)
    // 无 data-nav-label 时，导航名称必须严格等于卡片内 h2（owner-token 区块
    // 用 data-nav-label 提供短名「Owner GitLab Token」，长 h2 含徽章不参与相等断言）
    if (!entry.navLabel) {
      assert.equal(h2, expected[entry.id], `${entry.id} 的卡片组件 ${entry.cards[0]} 内 h2 应为「${expected[entry.id]}」`)
    }
    assert.equal(resolveName(entry), expected[entry.id], `${entry.id} 导航名称应为「${expected[entry.id]}」`)
  }
})

// ---------- A. collectSettingsGroups 基于真实源码结构生成 ----------

/** 基于真实源码结构构建的设置页 DOM 镜像（querySelectorAll 按文档顺序返回
 *  分组标题与设置区块；区块 querySelector('h2') 返回真实渲染的名称——
 *  卡片区块取卡片组件内 h2，与运行时一致） */
function sourceMirrorContent() {
  const GROUP_RE = /<h2 className="settings-group-title">([^<]+)<\/h2>/g
  const nodes = []
  const body = settingsSrc
  // 按文档顺序合并分组标题与设置区块
  const groupHits = [...body.matchAll(GROUP_RE)].map((x) => ({ type: 'group', at: x.index, title: x[1] }))
  const secHits = [...body.matchAll(/<section id="([^"]+)" className="settings-section"([^>]*)>([\s\S]*?)<\/section>/g)]
    .map((x) => ({ type: 'section', at: x.index, id: x[1], attrs: x[2], body: x[3] }))
  const ordered = [...groupHits, ...secHits].sort((a, b) => a.at - b.at)
  for (const hit of ordered) {
    if (hit.type === 'group') {
      nodes.push({ tagName: 'H2', className: 'settings-group-title', textContent: hit.title })
    } else {
      const h2 = /<h2[^>]*>([\s\S]*?)<\/h2>/.exec(hit.body)
      const navLabel = /data-nav-label="([^"]+)"/.exec(hit.attrs)?.[1] || null
      const cards = [...hit.body.matchAll(/<([A-Z][A-Za-z0-9]*(?:Card|Manager))[^>]*\/>/g)].map((x) => x[1])
      let labelText = h2 ? stripTags(h2[1]) : (cards.length > 0 ? cardH2Text(cards[0]) : null)
      nodes.push({
        tagName: 'SECTION',
        className: 'settings-section',
        id: hit.id,
        querySelector: (sel) => (sel === 'h2' && labelText ? { textContent: labelText } : null),
        getAttribute: (name) => (name === 'data-nav-label' ? navLabel : null),
      })
    }
  }
  return { querySelectorAll: () => nodes }
}

/** 渲染 SettingsNav（mock document.querySelector 提供源码镜像内容区） */
function renderNavWithMirror() {
  const realDocument = global.document
  global.document = { querySelector: (sel) => (sel === '.settings-content' ? sourceMirrorContent() : null) }
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

test('collectSettingsGroups：基于真实源码结构生成 17 项，每项名称非空且不等于原始 id', () => {
  const groups = collectSettingsGroups(sourceMirrorContent())
  const items = groups.flatMap((g) => g.items)
  assert.equal(items.length, 17, `应生成 17 个设置子项，实际 ${items.length}`)
  const byId = Object.fromEntries(items.map((it) => [it.id, it.label]))
  for (const [id, name] of Object.entries(byId)) {
    assert.ok(name, `${id} 子项应有非空名称`)
    assert.notEqual(name, id, `${id} 子项名称不应是原始 id`)
    assert.ok(!name.includes('settings-'), `${id} 子项名称「${name}」不应含 settings- 前缀`)
  }
  // 与已知名称快照逐一比对（覆盖 data-nav-label 覆盖场景）
  for (const [id, expected] of Object.entries(EXPECTED_NAMES)) {
    assert.equal(byId[id], expected, `${id} 导航名称应为「${expected}」，实际「${byId[id]}」`)
  }
  // issue #174 关注点：settings-vision-models 等出现时必须有对应名称
  assert.equal(byId['settings-vision-models'], '识图模型', 'settings-vision-models 应显示为「识图模型」')
  assert.equal(byId['settings-image-models'], '生图模型', 'settings-image-models 应显示为「生图模型」')
  assert.equal(byId['settings-ai-providers'], 'AI API 供应商', 'settings-ai-providers 应显示为「AI API 供应商」')
})

test('渲染：左侧边栏展示全部 17 个设置项名称，无原始 id 泄露', () => {
  const { renderer, root, restore } = renderNavWithMirror()
  try {
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 17, '应渲染 17 个子项链接')
    const linkTexts = new Set(links.map((l) => deepText(l)))
    for (const expected of Object.values(EXPECTED_NAMES)) {
      assert.ok(linkTexts.has(expected), `导航应展示设置项名称「${expected}」`)
    }
    for (const link of links) {
      const text = deepText(link)
      assert.ok(text.length > 0, '子项链接文本不应为空')
      assert.ok(!text.includes('settings-'), `子项链接文本「${text}」不应是原始 id（settings- 前缀）`)
    }
  } finally {
    TestRenderer.act(() => renderer.unmount())
    restore()
  }
})

// ---------- A. 关键词映射与设置项一致性 ----------

test('SETTING_KEYWORDS 与设置区块 id 双向一致（每个设置项都有对应搜索关键词）', () => {
  const kwBlock = navSrc.match(/export const SETTING_KEYWORDS = \{([\s\S]*?)\n\}/)?.[1]
  assert.ok(kwBlock, 'SettingsNav 应导出 SETTING_KEYWORDS')
  const kwIds = [...kwBlock.matchAll(/'([^']+)':/g)].map((m) => m[1])
  const sectionIds = sectionEntries().map((e) => e.id)
  assert.deepEqual(
    [...kwIds].sort(),
    [...sectionIds].sort(),
    'SETTING_KEYWORDS 键集合应与设置区块 id 集合完全一致（新增设置项需补关键词）',
  )
})

// ---------- A. 兜底行为文档化（负向边界） ----------

test('无 h2 且无 data-nav-label 的区块 label 回退为原始 id（兜底，设置页源码不应触发）', () => {
  const content = {
    querySelectorAll: () => [
      { tagName: 'SECTION', className: 'settings-section', id: 'settings-orphan',
        querySelector: () => null, getAttribute: () => null },
    ],
  }
  const groups = collectSettingsGroups(content)
  assert.equal(groups[0].items[0].label, 'settings-orphan', '无名称来源时 label 回退为原始 id（安全网）')
  // 设置页真实源码中不存在需要回退的区块：全部区块都有名称来源
  for (const entry of sectionEntries()) {
    assert.ok(
      entry.navLabel || entry.h2Text || entry.cards.length > 0,
      `${entry.id} 不得依赖原始 id 兜底，必须有真实名称来源`,
    )
  }
})

// ---------- B. 真实运行时：三个卡片加载中/失败/就绪均渲染标题 h2 ----------
// 根因回归（issue #174 第二轮）：SettingsNav 挂载时只读取一次 DOM，卡片在
// 数据加载中若 return null，区块内没有 <h2>，导航名称回退成原始 id 且永久
// 不恢复。以下用真实组件 + 不同 fetch 时序验证三种状态都有标题 h2。

/** 依赖异步数据提供名称的三个设置区块对应的卡片（issue #174 要找的） */
const ASYNC_NAME_CARDS = [
  ['AiProvidersCard', AiProvidersCard, 'settings-ai-providers', 'AI API 供应商'],
  ['ImageModelsCard', ImageModelsCard, 'settings-image-models', '生图模型'],
  ['VisionModelsCard', VisionModelsCard, 'settings-vision-models', '识图模型'],
]

/** fetch 永不 resolve：模拟挂载瞬间数据尚未加载完成的真实时序 */
function pendingFetch() {
  const originalFetch = global.fetch
  global.fetch = () => new Promise(() => {})
  return { restore: () => { global.fetch = originalFetch } }
}

/** fetch 立即失败：模拟加载失败（卡片应展示错误 + 可点击重试，而不是消失）。
 *  返回 4xx 而非网络层 reject——GET 自动重试（issue #226）只对网络错误/5xx
 *  生效，4xx 业务错误不重试，测试才能确定性地快速进入失败态 */
function rejectFetch() {
  const originalFetch = global.fetch
  global.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: 'network down' }) })
  return { restore: () => { global.fetch = originalFetch } }
}

/** fetch 立即成功：模拟数据到达后的渲染 */
function resolveFetch() {
  const originalFetch = global.fetch
  global.fetch = async (p) => {
    if (String(p).startsWith('/api/settings')) {
      return {
        ok: true, status: 200,
        json: async () => ({
          ai_providers: [{ name: 'deepseek', provider: 'deepseek', enabled: true }],
          image_models: [{ name: 'gemini', provider: 'gemini_nano_banana', enabled: true }],
          vision_models: [{ name: 'vision', provider: 'gemini_vision', enabled: true }],
        }),
      }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { restore: () => { global.fetch = originalFetch } }
}

/** 挂载真实卡片组件（fetch 时序由 mock 决定），返回 renderer 与还原函数 */
async function mountCard(Component, fetchMock) {
  const m = fetchMock()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Component))
    })
    return { renderer, restore: m.restore }
  } catch (e) {
    m.restore()
    throw e
  }
}

for (const [file, Component, sectionId, title] of ASYNC_NAME_CARDS) {
  test(`【运行时】${file} 数据加载中即渲染标题 h2「${title}」（导航名称来源不缺失）`, async () => {
    const { renderer, restore } = await mountCard(Component, pendingFetch)
    try {
      const h2s = renderer.root.findAllByType('h2')
      assert.equal(h2s.length, 1, `${file} 加载中应渲染 1 个 h2（区块 ${sectionId} 需要名称来源）`)
      assert.equal(deepText(h2s[0]).trim(), title, `加载中标题应为「${title}」`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      restore()
    }
  })

  test(`【运行时】${file} 加载失败渲染标题 h2「${title}」并支持点击重试`, async () => {
    const { renderer, restore } = await mountCard(Component, rejectFetch)
    try {
      const h2s = renderer.root.findAllByType('h2')
      assert.equal(h2s.length, 1, `${file} 加载失败也不应消失：区块 ${sectionId} 仍需标题 h2`)
      assert.equal(deepText(h2s[0]).trim(), title)
      const text = jsonText(renderer.toJSON())
      assert.match(text, /network down/, '应展示加载失败原因')
      assert.match(text, /点击重试/, '错误提示应支持点击重试')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      restore()
    }
  })

  test(`【运行时】${file} 数据到达后渲染标题 h2「${title}」`, async () => {
    const { renderer, restore } = await mountCard(Component, resolveFetch)
    try {
      const h2s = renderer.root.findAllByType('h2')
      assert.ok(h2s.length >= 1, `${file} 数据到达后应渲染标题 h2`)
      assert.equal(deepText(h2s[0]).trim(), title, `数据到达后标题应为「${title}」`)
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      restore()
    }
  })
}

// ---------- B. 真实运行时：Settings 页真实渲染树，每个区块都有 h2 ----------

/** 覆盖设置页全部接口的 fetch mock（与 settings-issue-priority.test.mjs 同款） */
function mockFetchAll() {
  const originalFetch = global.fetch
  global.fetch = async (p) => {
    const pathname = String(p)
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200,
        json: async () => ({
          worker: { issue_priority: ['bug', 'test', 'feature'] },
          sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '' }, notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {},
          ai_providers: [], image_models: [], vision_models: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-13 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    if (pathname.startsWith('/api/settings/sso-guide') || pathname.startsWith('/api/settings/owner-token-guide')) {
      return { ok: true, status: 200, json: async () => ({ content: '' }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { restore: () => { global.fetch = originalFetch } }
}

test('【运行时】真实渲染 Settings：每个设置区块在渲染树中都有 h2，无原始 id 名称', async () => {
  const m = mockFetchAll()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const sections = renderer.root.findAll(
      (n) => n.type === 'section' && String(n.props.className || '').split(/\s+/).includes('settings-section'))
    assert.equal(sections.length, 17, `设置页应渲染 17 个设置区块，实际 ${sections.length}`)
    const byId = {}
    for (const s of sections) {
      const h2 = s.findAll((n) => n.type === 'h2')[0]
      assert.ok(h2, `${s.props.id} 区块在真实渲染树中应有 <h2>（导航名称来源），否则左侧导航会回退成原始 id`)
      byId[s.props.id] = deepText(h2).trim()
    }
    // issue #174：找出依赖异步数据提供名称的三个设置项，修复后名称来源不缺失
    assert.equal(byId['settings-ai-providers'], 'AI API 供应商', 'settings-ai-providers 名称来源应存在')
    assert.equal(byId['settings-image-models'], '生图模型', 'settings-image-models 名称来源应存在')
    assert.equal(byId['settings-vision-models'], '识图模型', 'settings-vision-models 名称来源应存在')
    assert.equal(byId['settings-backup'], '数据备份', 'settings-backup 名称来源应存在')
    // 全部 17 项渲染名都不是原始 id
    for (const [id, name] of Object.entries(byId)) {
      assert.ok(name && !name.includes('settings-'), `${id} 渲染名「${name}」不应含 settings- 前缀（必须是用户可读名称）`)
    }
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
