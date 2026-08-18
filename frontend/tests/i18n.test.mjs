// 前端界面国际化测试（issue #268）：中英文切换 / 持久化 / 回退逻辑。
//
// 断言：
// 1. translate：zh-CN 返回中文、en-US 返回英文、en-US 缺失 key 回退中文、
//    key 完全缺失原样返回（不报错）、{var} 插值；
// 2. load/saveLangPreference：localStorage 读写、非法值兜底、无存储环境静默；
// 3. I18nProvider：默认中文；预置 en-US 初始即为英文；setLang 即时切换并
//    持久化到 localStorage；非法语言不生效；
// 4. 无 Provider 环境（SSR / 单组件渲染）回退中文不崩溃；
// 5. 集成：Overview / App 在 Provider(en-US) 下渲染英文文案（验收标准 1）。
import { after, mock, test } from 'node:test'
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

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
// 注意：after 必须在所有可能抛错的顶层 await 之前注册——模块加载失败时
// vite server 也要能关闭，否则 node --test 进程挂起不退出
after(() => vite.close())
const i18nMod = await vite.ssrLoadModule('/src/i18n.jsx')
const {
  LANGS, FALLBACK_LANG, LANG_STORAGE_KEY, LANG_LABELS,
  isValidLang, loadLangPreference, saveLangPreference,
  translate, applyHtmlLang, I18nProvider, useI18n, I18N_DICTS,
} = i18nMod
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { default: App } = await vite.ssrLoadModule('/src/App.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

/** 简单内存 storage 替身（localStorage 子集） */
function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    _map: map,
  }
}

/** 递归收集渲染树全部文本 */
function texts(json) {
  const out = []
  const walk = (n) => {
    if (typeof n === 'string') { out.push(n); return }
    if (n && typeof n === 'object') (n.children || []).forEach(walk)
  }
  walk(json)
  return out
}

// 探测组件：暴露 lang / t / setLang
function Probe() {
  const { lang, setLang, t } = useI18n()
  return React.createElement('div', { 'data-lang': lang },
    React.createElement('span', { className: 'probe-zh' }, t('nav.overview')),
    React.createElement('span', { className: 'probe-en' }, t('nav.settings')),
    React.createElement('button', { className: 'probe-switch', onClick: () => setLang('en-US') }))
}

async function renderProbe(storage) {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(I18nProvider, { storage }, React.createElement(Probe))
    )
  })
  return renderer
}

// ---- 纯函数 ----

test('translate：zh-CN / en-US / 回退 / 缺 key 不报错 / 插值', () => {
  assert.equal(translate('zh-CN', 'nav.overview'), '概览')
  assert.equal(translate('en-US', 'nav.overview'), 'Overview')
  // en-US 缺失 key 回退中文（验收标准 2：未翻译文案回退中文不报错）
  const savedEn = I18N_DICTS['en-US']['nav.overview']
  delete I18N_DICTS['en-US']['nav.overview']
  try {
    assert.equal(translate('en-US', 'nav.overview'), '概览', 'en-US 缺失 key 应回退中文')
  } finally {
    I18N_DICTS['en-US']['nav.overview'] = savedEn
  }
  // key 完全缺失：原样返回 key，不抛错（验收标准 2）
  assert.equal(translate('en-US', 'not.exist.key'), 'not.exist.key')
  assert.equal(translate('zh-CN', 'not.exist.key'), 'not.exist.key')
  // 非法语言：回退中文（zh 是兜底语言）；key 完全缺失才原样返回
  assert.equal(translate('fr-FR', 'nav.overview'), '概览')
  assert.equal(translate('fr-FR', 'no.such.key'), 'no.such.key')
  // {var} 插值
  assert.equal(translate('zh-CN', 'tasks.total', { n: 3 }), '共 3 条')
  assert.equal(translate('en-US', 'tasks.total', { n: 3 }), '3 total')
  // 插值变量为 undefined/null：占位符替换为空串；空 vars 不替换
  assert.equal(translate('zh-CN', 'tasks.total', { n: undefined }), '共  条') // 占位符两侧空格保留
  assert.equal(translate('zh-CN', 'tasks.total', {}), '共 {n} 条')
  // 字典内容：en 与 zh 的 key 集合一致
  assert.deepEqual(Object.keys(zhCN).sort(), Object.keys(enUS).sort())
})

test('isValidLang / LANGS / FALLBACK_LANG / LANG_LABELS', () => {
  assert.equal(FALLBACK_LANG, 'zh-CN')
  assert.deepEqual(LANGS, ['zh-CN', 'en-US'])
  assert.equal(isValidLang('zh-CN'), true)
  assert.equal(isValidLang('en-US'), true)
  assert.equal(isValidLang('fr'), false)
  assert.equal(isValidLang(null), false)
  assert.equal(isValidLang(undefined), false)
  assert.equal(LANG_LABELS['zh-CN'], '中文')
  assert.equal(LANG_LABELS['en-US'], 'English')
})

test('loadLangPreference：有效值读取 / 非法值回退 null / 无存储静默', () => {
  assert.equal(loadLangPreference(memoryStorage({ 'botler.lang': 'en-US' })), 'en-US')
  assert.equal(loadLangPreference(memoryStorage({ 'botler.lang': 'zh-CN' })), 'zh-CN')
  assert.equal(loadLangPreference(memoryStorage({ 'botler.lang': 'fr' })), null)
  assert.equal(loadLangPreference(memoryStorage({})), null)
  assert.equal(loadLangPreference(null), null)
  // getItem 抛异常（隐私模式）→ null 不抛错
  assert.equal(loadLangPreference({ getItem: () => { throw new Error('denied') } }), null)
})

test('saveLangPreference：有效值写入 / 非法值忽略 / setItem 抛异常静默', () => {
  const st = memoryStorage()
  saveLangPreference(st, 'en-US')
  assert.equal(st._map.get('botler.lang'), 'en-US')
  saveLangPreference(st, 'fr')
  assert.equal(st._map.get('botler.lang'), 'en-US')
  saveLangPreference(null, 'en-US') // 无存储环境不抛错
  saveLangPreference({ setItem: () => { throw new Error('denied') } }, 'en-US') // 抛异常静默
})

test('applyHtmlLang：设置 html lang；无 DOM / 异常静默', () => {
  const fakeDoc = { documentElement: {} }
  applyHtmlLang('en-US', fakeDoc)
  assert.equal(fakeDoc.documentElement.lang, 'en-US')
  applyHtmlLang(null, null) // 无 doc 不抛错
  applyHtmlLang('zh-CN', { documentElement: { set lang(_v) { throw new Error('x') } } }) // setter 异常静默
})

// ---- Provider 渲染 ----

test('I18nProvider：默认中文；预置 en-US 初始即为英文', async () => {
  let r1 = await renderProbe(memoryStorage())
  try {
    assert.equal(r1.root.findByProps({ 'data-lang': 'zh-CN' }), r1.root.findByType('div'))
    assert.ok(texts(r1.toJSON()).includes('概览'))
    assert.ok(!texts(r1.toJSON()).includes('Overview'))
  } finally { await TestRenderer.act(() => r1.unmount()) }

  let r2 = await renderProbe(memoryStorage({ 'botler.lang': 'en-US' }))
  try {
    assert.ok(texts(r2.toJSON()).includes('Overview'))
    assert.ok(texts(r2.toJSON()).includes('Settings'))
    assert.ok(!texts(r2.toJSON()).includes('概览'))
  } finally { await TestRenderer.act(() => r2.unmount()) }
})

test('I18nProvider：setLang 即时切换并持久化到 localStorage（验收标准 1）', async () => {
  const storage = memoryStorage()
  const renderer = await renderProbe(storage)
  try {
    // 点击切换 → 英文
    await TestRenderer.act(() => {
      renderer.root.findByType('button').props.onClick()
    })
    assert.ok(texts(renderer.toJSON()).includes('Overview'), '切换后应即时显示英文')
    assert.equal(storage._map.get('botler.lang'), 'en-US', '选择应持久化到 localStorage')
  } finally { await TestRenderer.act(() => renderer.unmount()) }
})

test('无 Provider 环境：默认上下文回退中文且 setLang 为空操作（不崩溃）', async () => {
  // 直接渲染 Overview（无 Provider，模拟现有单组件测试/SSR）
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, 20))
    } catch (e) {
      renderError = e
    }
  })
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.ok(renderer.toJSON(), '渲染结果为 null')
    // 默认中文文案仍然渲染（关键测试用稳定文案）
    assert.ok(texts(renderer.toJSON()).some((t) => t.includes('概览')), '无 Provider 时应渲染中文「概览」')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

// ---- 集成：真实组件在 en-US 下渲染英文 ----

test('集成：Overview 在 I18nProvider(en-US) 下渲染英文板块标题', async () => {
  // mock 概览页全部轮询接口（空数据，避免真实请求）
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [] }
    if (pathname.startsWith('/api/inspirations')) return { repos: [] }
    if (pathname === '/api/usage/stats') return { summary: null, by_engine: [], by_repo: [] }
    if (pathname.startsWith('/api/issues/completion-stats')) return { completed_count: 0, trend: [] }
    if (pathname.startsWith('/api/deepseek/balance')) return { configured: false }
    if (pathname.startsWith('/api/notifications')) return { events: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(I18nProvider, { storage: memoryStorage({ 'botler.lang': 'en-US' }) },
        React.createElement(Overview))
    )
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const flat = texts(renderer.toJSON())
    assert.ok(flat.includes('Overview'), 'h1 应为英文「Overview」')
    assert.ok(flat.includes('Open Issues'), '开放 Issue 板块标题应为英文')
    assert.ok(flat.includes('Ideas'), '灵感板块标题应为英文')
    assert.ok(flat.includes('CI/CD Pipelines'), '流水线板块标题应为英文')
    assert.ok(!flat.includes('开放 Issue'), '不应再渲染中文「开放 Issue」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('集成：App 导航在 I18nProvider(en-US) 下渲染英文导航项', async () => {
  const { MemoryRouter } = await import('react-router-dom')
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/auth/status') return { enabled: false, user: { username: 'tester' } }
    if (pathname === '/api/settings') return {}
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, null,
        React.createElement(I18nProvider, { storage: memoryStorage({ 'botler.lang': 'en-US' }) },
          React.createElement(App)))
    )
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  try {
    const flat = texts(renderer.toJSON())
    assert.ok(flat.includes('Overview'), '导航应有英文「Overview」')
    assert.ok(flat.includes('Tasks'), '导航应有英文「Tasks」')
    assert.ok(flat.includes('Settings'), '导航应有英文「Settings」')
    assert.ok(flat.includes('Sign out'), '退出按钮应为英文「Sign out」')
    assert.ok(!flat.includes('概览'), '导航不应再渲染中文「概览」')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
