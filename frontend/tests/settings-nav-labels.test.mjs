// 设置页左侧导航栏「设置项名称」测试（issue #174）：
//
// 需求：确保每个设置项都有对应的名称——左侧导航栏子项的名称来自设置页
// 实际渲染的名称（区块内首个 <h2> 文本，或 data-nav-label 覆盖），任何
// settings-* 设置区块都必须有名称，禁止把原始 id（如 settings-vision-models）
// 当作名称展示在导航栏上。
//
// 背景（issue #139/#155 架构）：导航栏通过 collectSettingsGroups() 读取
// 设置页 .settings-content 中的 section.settings-section 动态生成子项，
// label 默认取区块内首个 h2。但 4 个区块（settings-ai-providers /
// settings-image-models / settings-vision-models / settings-backup）的 h2
// 在卡片组件内部（AiProvidersCard / ImageModelsCard / VisionModelsCard /
// BackupManager），Settings.jsx 源码里看不到——手工维护的 fake DOM 测试
// 无法发现「卡片丢了 h2」导致导航名称回退成原始 id 的回归。
//
// 本测试从**真实源码链路**解析名称：
// 1. Settings.jsx 每个设置区块都必须有名称来源（直接 h2 / data-nav-label /
//    卡片组件内 h2），解析出的名称不能等于原始 id；
// 2. 15 个已知设置项的名称快照（含 data-nav-label 覆盖）；
// 3. 4 个卡片区块的名称由卡片组件内 h2 提供，与导航名称一致；
// 4. collectSettingsGroups 基于真实源码结构生成导航时，每个子项名称都
//    非空且不等于原始 id；
// 5. SETTING_KEYWORDS 与设置区块 id 双向一致（每个设置项都有搜索关键词）。
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

after(() => vite.close())

/** 去标签化：提取 JSX 文本节点并压缩空白（与浏览器 textContent 对齐） */
function stripTags(s) {
  return s.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
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
    const cards = [...body.matchAll(/<([A-Z][A-Za-z0-9]*(?:Card|Manager))\s*\/>/g)].map((x) => x[1])
    entries.push({ id, navLabel, h2Text: h2 ? stripTags(h2[1]) : null, cards })
  }
  return entries
}

/** 读取卡片组件源码内首个 h2 文本（即该卡片在设置页渲染出的区块名称） */
function cardH2Text(componentName) {
  const src = readFileSync(path.join(ROOT, 'src/components', `${componentName}.jsx`), 'utf8')
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

/** 15 个设置项名称快照（issue #174：每个设置项都有对应的名称） */
const EXPECTED_NAMES = {
  'settings-sso': 'Synology SSO 登录',
  'settings-ai-providers': 'AI API 供应商',
  'settings-image-models': '生图模型',
  'settings-vision-models': '识图模型',
  'settings-tasks': '任务调度',
  'settings-ui': '界面显示',
  'settings-notifications': '网页通知',
  'settings-webhook': '消息推送 Webhook',
  'settings-claude': 'Claude Code',
  'settings-dsh': 'dsh 引擎',
  'settings-environment': '本地环境检测',
  'settings-backup': '数据备份',
  'settings-owner-token': 'Owner GitLab Token',
  'settings-gitlab-cred': 'GitLab 凭据（只读）',
  'settings-version': '版本信息',
}

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
      const cards = [...hit.body.matchAll(/<([A-Z][A-Za-z0-9]*(?:Card|Manager))\s*\/>/g)].map((x) => x[1])
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

/** 递归取节点渲染文本 */
function deepText(node) {
  if (node == null) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  const kids = Array.isArray(node.props?.children) ? node.props.children : [node.props?.children]
  return kids.map(deepText).join('')
}

// ---------- 源码链路：每个设置区块都有名称来源 ----------

test('设置页应包含 15 个设置区块且 id 唯一（与已知设置项一致）', () => {
  const entries = sectionEntries()
  assert.equal(entries.length, 15, `应解析到 15 个设置区块，实际 ${entries.length}`)
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

test('15 个已知设置项名称快照：每个设置项都有对应的名称', () => {
  const byId = Object.fromEntries(sectionEntries().map((e) => [e.id, resolveName(e)]))
  for (const [id, expected] of Object.entries(EXPECTED_NAMES)) {
    assert.equal(byId[id], expected, `${id} 应有对应名称「${expected}」，实际「${byId[id]}」`)
  }
})

test('卡片区块名称由卡片组件内 h2 提供（4 个卡片区块与导航名称一致）', () => {
  const cardSections = sectionEntries().filter((e) => e.cards.length > 0)
  assert.equal(cardSections.length, 4, '应有 4 个卡片区块（ai-providers/image-models/vision-models/backup）')
  const expected = {
    'settings-ai-providers': 'AI API 供应商',
    'settings-image-models': '生图模型',
    'settings-vision-models': '识图模型',
    'settings-backup': '数据备份',
  }
  for (const entry of cardSections) {
    assert.ok(entry.cards.length === 1, `${entry.id} 应恰好引用一个卡片组件`)
    const h2 = cardH2Text(entry.cards[0])
    assert.equal(h2, expected[entry.id], `${entry.id} 的卡片组件 ${entry.cards[0]} 内 h2 应为「${expected[entry.id]}」`)
    assert.equal(resolveName(entry), expected[entry.id], `${entry.id} 导航名称应来自卡片组件内 h2`)
  }
})

// ---------- 运行时：collectSettingsGroups 基于真实源码结构生成 ----------

test('collectSettingsGroups：基于真实源码结构生成 15 项，每项名称非空且不等于原始 id', () => {
  const groups = collectSettingsGroups(sourceMirrorContent())
  const items = groups.flatMap((g) => g.items)
  assert.equal(items.length, 15, `应生成 15 个设置子项，实际 ${items.length}`)
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
})

// ---------- 运行时：左侧边栏渲染展示每个设置项名称 ----------

test('渲染：左侧边栏展示全部 15 个设置项名称，无原始 id 泄露', () => {
  const { renderer, root, restore } = renderNavWithMirror()
  try {
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 15, '应渲染 15 个子项链接')
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

// ---------- 关键词映射与设置项一致性 ----------

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

// ---------- 兜底行为文档化（负向边界） ----------

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
