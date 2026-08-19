// 设置页「界面显示」主题三态切换测试（issue #217）：
// 夜间无人值守查看任务/日志时浅色 UI 刺眼——设置页「界面显示」卡片新增
// 「界面主题」三态下拉（跟随系统 prefers-color-scheme / 浅色 / 深色），
// 保存后写回后端 config.yaml（ui.theme）并同步浏览器 localStorage
// （botler.theme），切换即时预览、刷新不闪变。
//
// 断言：
// 1. 「界面显示」卡片提供「界面主题」三态 select（system / light / dark），
//    行标题标注配置键 ui.theme；
// 2. 切换三态即时应用主题并写入 localStorage（无需等保存）；
// 3. buildUiPatch 携带 theme（未配置时按 system 提交，兼容旧配置）；
// 4. saveUi 保存后 applyTheme + 写 localStorage（与后端双向同步）；
// 5. 渲染：select 回显当前 theme，修改后点「保存界面显示配置」提交
//    PUT ui.theme=dark；说明文字覆盖三态含义。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：界面显示卡片 JSX 移到 components/settings/UiCard.jsx，
// buildUiPatch / saveUi 收敛到 hooks/useSettingsData.js——静态断言跟随新文件
const uiCard = readFileSync(path.join(ROOT, 'src/components/settings/UiCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsSrc = uiCard + '\n' + hook

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')
const themeMod = await vite.ssrLoadModule('/src/theme.js')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-engine.test.mjs 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名函数源码片段（与 settings-engine.test.mjs 同款工具） */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} =[\\s\\S]*?(?=\\n  const |\\n  return \\()`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('「界面显示」卡片提供「界面主题」三态下拉（跟随系统/浅色/深色）', () => {
  const card = cardSource(settingsSrc, '界面显示')
  assert.ok(card, '设置页应存在「界面显示」配置卡片')
  assert.match(card, /界面主题/, '卡片应含「界面主题」行')
  assert.match(card, /ui\.theme/, '行标题应标注配置键 ui.theme')
  assert.match(card, /<select/, '主题选择应使用 select 下拉框')
  assert.match(card, /THEME_MODE_LABELS/, '选项应来自 THEME_MODE_LABELS 统一文案')
  assert.match(card, /Object\.entries\(THEME_MODE_LABELS\)\.map/, '选项应遍历三态映射渲染')
  assert.match(card, /value=\{mode\}/, 'option value 应绑定三态取值')
  assert.match(card, /\{label\}/, 'option 文案应显示中文标签')
  // 三态文案来自 theme.js 统一出口（跟随系统 / 浅色 / 深色）
  const labels = themeMod.THEME_MODE_LABELS
  assert.equal(labels.system, '跟随系统', 'system 文案应为「跟随系统」')
  assert.equal(labels.light, '浅色', 'light 文案应为「浅色」')
  assert.equal(labels.dark, '深色', 'dark 文案应为「深色」')
  assert.match(uiCard, /import \{ THEME_MODE_LABELS[^}]*\} from '..\/..\/theme\.js'/,
               'UiCard.jsx 应从 theme.js 导入 THEME_MODE_LABELS（issue #201 拆分后卡片持有）')
})

test('切换三态即时应用主题并写入 localStorage（无需等保存）', () => {
  const card = cardSource(settingsSrc, '界面显示')
  assert.ok(card, '设置页应存在「界面显示」配置卡片')
  assert.match(card, /applyTheme\(theme\)/, '切换时应即时应用主题（预览生效）')
  assert.match(card, /saveThemePreference\(themeStorage, theme\)/,
               '切换时应同步写本地 localStorage（刷新不丢偏好）')
})

test('buildUiPatch 携带 theme（未配置时按 system 提交，兼容旧配置）', () => {
  const body = fnBody(settingsSrc, 'buildUiPatch')
  assert.ok(body, '应存在 buildUiPatch（ui 段构建函数，saveUi 与全局 save 共用）')
  assert.match(body, /theme: settings\.ui\?\.theme \|\| 'system'/,
               'buildUiPatch 应提交 theme（未配置时按 system = 跟随系统，兼容旧配置）')
})

test('saveUi 保存后应用主题并写 localStorage（与后端 config.yaml 双向同步）', () => {
  const body = fnBody(settingsSrc, 'saveUi')
  assert.ok(body, '应存在 saveUi 函数')
  assert.match(body, /applyTheme\(theme\)/, '保存后应应用主题（与后端一致）')
  assert.match(body, /saveThemePreference\(themeStorage, theme\)/,
               '保存后应写本地 localStorage（与后端 ui.theme 双向同步）')
})

test('说明文字覆盖三态含义与持久化行为', () => {
  const card = cardSource(settingsSrc, '界面显示')
  assert.ok(card, '设置页应存在「界面显示」配置卡片')
  assert.match(card, /跟随系统/, '说明应写明「跟随系统」')
  assert.match(card, /浅色/, '说明应写明「浅色」')
  assert.match(card, /深色/, '说明应写明「深色」')
  assert.match(card, /prefers-color-scheme/, '说明应提及 prefers-color-scheme 自动适配')
  assert.match(card, /config\.yaml/, '说明应提及写回 config.yaml（后端同步）')
  assert.match(card, /刷新不闪变/, '说明应提及刷新不闪变（localStorage 首屏防闪烁）')
})

/** 渲染用 fetch mock（与 settings-engine.test.mjs 同款，覆盖设置页全部接口） */
function mockFetch({ theme = 'system' } = {}) {
  const puts = []
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    if (opts?.method === 'PUT') {
      puts.push(JSON.parse(opts.body))
      return { ok: true, status: 200, json: async () => ({}) }
    }
    const pathname = String(p)
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          worker: { issue_priority: ['bug'] },
          sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '', theme },
          notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {}, ai_providers: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-18 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { puts, restore: () => { global.fetch = originalFetch } }
}

test('渲染：select 回显当前 theme，修改后保存提交 ui.theme=dark', async () => {
  const m = mockFetch({ theme: 'dark' })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    // 找到「界面主题」select：值回显后端 ui.theme（dark）
    const selects = renderer.root.findAllByType('select')
    assert.ok(selects.length >= 1, '设置页应渲染出 select 控件')
    const themeSelect = selects.find((s) => {
      const p = s.props
      return p.value === 'dark' && p.onChange && /THEME_MODE_LABELS|theme/.test(String(p.onChange))
    }) || selects.find((s) => s.props.value === 'dark')
    assert.ok(themeSelect, '「界面主题」select 应回显当前 theme（dark）')

    // 切到 light 并保存
    await TestRenderer.act(async () => {
      themeSelect.props.onChange({ target: { value: 'light' } })
    })
    await TestRenderer.act(async () => {
      const saveBtn = renderer.root.findAll((n) =>
        n.type === 'button' && String(n.children?.[0] || '').includes('保存界面显示配置'))
      assert.ok(saveBtn.length === 1, '应渲染「保存界面显示配置」按钮')
      saveBtn[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const uiPut = m.puts.find((b) => b.ui)
    assert.ok(uiPut, '点击保存应提交 ui 段')
    assert.equal(uiPut.ui.theme, 'light', 'PUT ui.theme 应为切换后的 light')
  } finally {
    m.restore()
    renderer = null
  }
})
