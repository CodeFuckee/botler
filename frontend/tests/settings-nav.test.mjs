// 设置页左侧导航栏测试（issue #139）：
//
// 需求「设置页设置项太多，整理设置页面的分支，并在设置左侧添加一个导航栏，
// 导航栏可以搜索设置项，同时也可以折叠和展开分组中的子项」。
//
// 本测试断言：
// 1. 设置页引入并挂载 SettingsNav，页面按分组标题组织（外部服务接入 /
//    系统设置 / 执行引擎 / 运维与数据 / 账号与安全 / 关于），
//    每个设置区块带锚点 id（与导航项一一对应）；
// 2. SettingsNav 导出分组配置 SETTINGS_GROUPS（6 组 14 项），
//    每个子项带 id / label / keywords；
// 3. 渲染：搜索框（aria-label）、分组头（可折叠/展开，aria-expanded）、
//    子项链接（href=#锚点）；
// 4. 搜索：按名称/关键字过滤子项，未命中的分组隐藏，命中提示与
//    无结果空状态；搜索时自动展开命中分组；
// 5. 折叠/展开：点击分组头隐藏/显示子项；「全部收起/全部展开」；
// 6. 点击子项：平滑滚动到页面锚点区块并高亮（scrollIntoView）；
// 7. styles.css 提供布局与导航样式（settings-layout / sidebar / nav 等）。
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
const { default: SettingsNav, SETTINGS_GROUPS } = await vite.ssrLoadModule('/src/components/SettingsNav.jsx')

after(() => vite.close())

const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

/** 渲染 SettingsNav 并返回 { renderer, root } */
function renderNav() {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(SettingsNav))
  })
  return { renderer, root: renderer.root }
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

// ---------- 源码断言：Settings.jsx 集成 ----------

test('设置页应引入并挂载 SettingsNav（左侧导航栏）', () => {
  assert.match(settings, /import SettingsNav from '\.\.\/components\/SettingsNav\.jsx'/, '应导入 SettingsNav')
  assert.match(settings, /<div className="settings-layout">/, '页面根节点应为两栏布局 settings-layout')
  assert.match(settings, /<SettingsNav \/>/, '设置页应挂载 SettingsNav')
  assert.match(settings, /<div className="settings-content">/, '应有右侧内容区 settings-content')
})

test('设置页按分组标题整理：6 个分组标题按序出现', () => {
  const titles = ['外部服务接入', '系统设置', '执行引擎', '运维与数据', '账号与安全', '关于']
  const positions = titles.map((t) => {
    if (t === '系统设置') return settings.search(/<h1>系统设置<\/h1>/)
    return settings.search(new RegExp(`<h2 className="settings-group-title">${t}</h2>`))
  })
  for (let i = 0; i < titles.length; i++) {
    assert.ok(positions[i] > 0, `页面应有分组标题「${titles[i]}」`)
  }
  for (let i = 0; i < positions.length - 1; i++) {
    assert.ok(positions[i] < positions[i + 1], `分组标题「${titles[i]}」应在「${titles[i + 1]}」之前`)
  }
})

test('每个设置区块带锚点 id（与导航分组子项一一对应）', () => {
  const ids = SETTINGS_GROUPS.flatMap((g) => g.items.map((it) => it.id))
  assert.equal(ids.length, 14, '导航配置应覆盖 14 个设置区块')
  for (const id of ids) {
    assert.ok(
      settings.includes(`<section id="${id}"`),
      `设置页应存在锚点区块 <section id="${id}">`,
    )
  }
  // 导航配置中的锚点不重不漏：无重复 id
  assert.equal(new Set(ids).size, ids.length, '锚点 id 不应重复')
})

test('分组标题与锚点区块归属一致（每个分组标题下有其全部子项区块）', () => {
  // 以分组标题位置为界，检查该分组所有子项锚点都落在对应区间内
  const titlePos = (t) => {
    if (t === '系统设置') return settings.search(/<h1>系统设置<\/h1>/)
    return settings.search(new RegExp(`<h2 className="settings-group-title">${t}</h2>`))
  }
  for (let i = 0; i < SETTINGS_GROUPS.length; i++) {
    const g = SETTINGS_GROUPS[i]
    const start = titlePos(g.title)
    assert.ok(start > 0, `应有分组标题「${g.title}」`)
    const end = i + 1 < SETTINGS_GROUPS.length ? titlePos(SETTINGS_GROUPS[i + 1].title) : settings.length
    for (const it of g.items) {
      const secPos = settings.search(new RegExp(`<section id="${it.id}"`))
      assert.ok(secPos > start, `「${g.title}」的子项「${it.label}」锚点应位于本组标题之后`)
      assert.ok(secPos < end, `「${g.title}」的子项「${it.label}」锚点应在本组区间内`)
    }
  }
})

// ---------- 源码断言：SettingsNav 组件 ----------

test('SETTINGS_GROUPS：6 个分组、14 个子项，子项带 id/label/keywords', () => {
  assert.equal(SETTINGS_GROUPS.length, 6, '应有 6 个分组')
  const itemCount = SETTINGS_GROUPS.reduce((n, g) => n + g.items.length, 0)
  assert.equal(itemCount, 14, '应有 14 个设置子项')
  for (const g of SETTINGS_GROUPS) {
    assert.ok(g.id && g.title, `分组 ${g.title} 应带 id 与标题`)
    assert.ok(Array.isArray(g.items) && g.items.length > 0, `分组 ${g.title} 应至少 1 个子项`)
    for (const it of g.items) {
      assert.ok(it.id && it.label, `子项 ${it.label} 应带 id 与 label`)
      assert.ok(Array.isArray(it.keywords), `子项 ${it.label} 应带 keywords（搜索用）`)
    }
  }
})

test('SettingsNav 提供搜索框、分组折叠头与子项链接', () => {
  assert.match(navSrc, /type="search"/, '应有搜索输入框')
  assert.match(navSrc, /placeholder="搜索设置项…"/, '搜索框应有占位文案')
  assert.match(navSrc, /aria-label="搜索设置项"/, '搜索框应有无障碍标签')
  assert.match(navSrc, /aria-expanded=\{!closed\}/, '分组头应暴露展开状态（aria-expanded）')
  assert.match(navSrc, /scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\)/, '点击子项应平滑滚动到锚点')
  assert.match(navSrc, /setActiveId\(id\)/, '点击子项应记录高亮 id')
})

// ---------- 渲染断言：分组与搜索 ----------

test('渲染：默认展示全部分组与子项，分组头可见', () => {
  const { renderer, root } = renderNav()
  try {
    // 分组标题按钮全部存在
    for (const g of SETTINGS_GROUPS) {
      const head = findButton(root, g.title)
      assert.ok(head, `应渲染分组头「${g.title}」`)
      assert.equal(head.props['aria-expanded'], true, `分组「${g.title}」默认应展开`)
    }
    // 子项链接全部存在且指向锚点
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 14, '应渲染 14 个子项链接')
    for (const g of SETTINGS_GROUPS) {
      for (const it of g.items) {
        const link = root.findAll(
          (n) => n.type === 'a' && n.props.href === '#' + it.id,
        )
        assert.equal(link.length, 1, `应有子项链接「${it.label}」`)
      }
    }
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：搜索按名称过滤子项，未命中分组隐藏，命中提示可见', () => {
  const { renderer, root } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    // 搜索「备份」→ 只命中「数据备份」（运维与数据组）
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '备份' } })
    })
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 1, '搜索「备份」应只命中 1 个子项')
    assert.ok(root.findAll((n) => n.type === 'a' && n.props.href === '#settings-backup').length === 1)
    // 命中提示
    const hits = root.findAll((n) => n.type === 'p' && String(n.props.children || '').includes('共命中'))
    assert.equal(hits.length, 1, '搜索时应有命中提示')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：搜索按关键字命中（如 sso），命中分组自动展开', () => {
  const { renderer, root } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    // 先折叠「外部服务接入」分组，再搜索关键字 sso —— 应自动展开并命中
    const extHead = findButton(root, '外部服务接入')
    TestRenderer.act(() => { extHead.props.onClick() })
    assert.equal(extHead.props['aria-expanded'], false, '折叠后分组应收起')

    TestRenderer.act(() => {
      input.props.onChange({ target: { value: 'sso' } })
    })
    const link = root.findAll((n) => n.type === 'a' && n.props.href === '#settings-sso')
    assert.equal(link.length, 1, '搜索关键字 sso 应命中 Synology SSO 登录（keywords 命中）')
    // 搜索时命中分组应自动展开（重新查询分组头，折叠状态被搜索覆盖）
    const extHeadNow = findButton(root, '外部服务接入')
    assert.equal(extHeadNow.props['aria-expanded'], true, '搜索时命中分组应自动展开露出匹配项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：搜索无结果时展示空状态', () => {
  const { renderer, root } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '不存在的设置项xyz' } })
    })
    const empty = root.findAll((n) => n.type === 'p' && String(n.props.children || '').includes('未找到'))
    assert.equal(empty.length, 1, '无结果时应展示「未找到」空状态')
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 0, '无结果时不应渲染任何子项链接')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：清空搜索恢复全部分组', () => {
  const { renderer, root } = renderNav()
  try {
    const input = root.find((n) => n.type === 'input' && n.props.type === 'search')
    TestRenderer.act(() => {
      input.props.onChange({ target: { value: '备份' } })
    })
    // 出现清空按钮 → 点击 → 恢复 14 项
    const clear = root.find((n) => n.type === 'button' && n.props['aria-label'] === '清空搜索')
    TestRenderer.act(() => { clear.props.onClick() })
    const links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 14, '清空搜索后应恢复全部分组子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

// ---------- 渲染断言：折叠/展开 ----------

test('渲染：点击分组头折叠子项，再次点击展开', () => {
  const { renderer, root } = renderNav()
  try {
    const head = findButton(root, '外部服务接入')
    // 折叠
    TestRenderer.act(() => { head.props.onClick() })
    assert.equal(head.props['aria-expanded'], false, '点击后分组应收起')
    let links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 14 - 3, '收起「外部服务接入」后应少 3 个子项')
    // 再点展开
    TestRenderer.act(() => { head.props.onClick() })
    assert.equal(head.props['aria-expanded'], true, '再次点击后分组应展开')
    links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 14, '展开后应恢复 14 个子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('渲染：「全部收起」后所有分组折叠，「全部展开」恢复', () => {
  const { renderer, root } = renderNav()
  try {
    const collapseAll = findButton(root, '全部收起')
    assert.ok(collapseAll, '默认应有「全部收起」按钮')
    TestRenderer.act(() => { collapseAll.props.onClick() })
    let links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 0, '全部收起后应无子项链接')
    const expandAll = findButton(root, '全部展开')
    assert.ok(expandAll, '全部收起后按钮应变为「全部展开」')
    TestRenderer.act(() => { expandAll.props.onClick() })
    links = root.findAll((n) => n.type === 'a' && String(n.props.href || '').startsWith('#'))
    assert.equal(links.length, 14, '全部展开后应恢复 14 个子项')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

// ---------- 渲染断言：点击滚动 ----------

test('渲染：点击子项调用 scrollIntoView 滚动到锚点并高亮', () => {
  const scrolled = []
  const realDocument = global.document
  global.document = {
    getElementById: (id) => ({ scrollIntoView: (opts) => scrolled.push({ id, opts }) }),
  }
  try {
    const { renderer, root } = renderNav()
    try {
      const link = root.find((n) => n.type === 'a' && n.props.href === '#settings-webhook')
      TestRenderer.act(() => {
        link.props.onClick({ preventDefault: () => {} })
      })
      assert.equal(scrolled.length, 1, '点击子项应触发一次 scrollIntoView')
      assert.equal(scrolled[0].id, 'settings-webhook', '应滚动到对应锚点区块')
      assert.deepEqual(scrolled[0].opts, { behavior: 'smooth', block: 'start' }, '应为平滑滚动到区块顶部')
      // 点击后该子项高亮
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
