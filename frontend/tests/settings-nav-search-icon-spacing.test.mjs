// 设置页左侧导航搜索框图标与提示文字间距回归测试（issue #436）：
// 搜索图标绝对定位在输入框左侧时，输入区左内边距必须同时为图标及其
// 右侧留白预留空间，避免 placeholder 与图标重叠。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

function ruleBlock(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const found = styles.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`))
  assert.ok(found, `styles.css 应存在 ${selector} 样式规则`)
  return found[1]
}

test('设置导航搜索框为 16px 图标及其右侧留白预留 40px 左内边距', () => {
  const icon = ruleBlock('.settings-nav-search-icon')
  const input = ruleBlock('.settings-nav-input')

  assert.match(icon, /left:\s*12px\s*;/, '搜索图标应距输入框左边缘 12px')
  assert.match(icon, /width:\s*16px\s*;/, '搜索图标应固定为 16px 宽')
  assert.match(icon, /height:\s*16px\s*;/, '搜索图标应固定为 16px 高')
  assert.match(input, /padding-left:\s*40px\s*;/,
    '提示文字应从 40px 处开始，距图标右缘至少保留 12px 间距')
})
