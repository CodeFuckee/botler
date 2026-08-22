// 设置页左侧设置导航栏搜索框去掉搜索图标（issue #442）：
// 搜索框仅保留输入框（含 placeholder 与清除按钮）与搜索过滤功能，
// 不再渲染左侧放大镜图标。断言：
// 1. SettingsNav.jsx 搜索框容器内不再渲染 search 图标元素；
// 2. styles.css 不再保留 .settings-nav-search-icon 样式规则；
// 3. 搜索输入框、占位文案、清除按钮仍完整保留（只去图标，不砍功能）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')

test('设置导航搜索框不再渲染搜索图标（issue #442）', () => {
  assert.doesNotMatch(navSrc, /settings-nav-search-icon/,
    'SettingsNav 不应再渲染 settings-nav-search-icon 图标 span')
  // 搜索框容器（settings-nav-search 内部）不得再出现放大镜图标
  const searchBlock = navSrc.match(/className="settings-nav-search"[\s\S]*?<\/div>/)?.[0] || ''
  assert.doesNotMatch(searchBlock, /Icon name="search"/,
    '搜索框容器内不应渲染 search 图标')
})

test('styles.css 不再保留搜索图标样式规则（issue #442）', () => {
  assert.doesNotMatch(styles, /\.settings-nav-search-icon\s*\{/,
    'styles.css 不应再保留 .settings-nav-search-icon 规则')
})

test('搜索输入框、占位文案与清除按钮仍保留（仅移除图标）', () => {
  assert.match(navSrc, /type="search"/, '搜索输入框应保留')
  assert.match(navSrc, /placeholder="搜索设置项…"/, '占位文案应保留')
  assert.match(navSrc, /aria-label="搜索设置项"/, '搜索框无障碍标签应保留')
  assert.match(navSrc, /aria-label="清空搜索"/, '清除按钮应保留')
  assert.match(navSrc, /<Icon name="x" \/>/, '清除按钮图标应保留')
})
