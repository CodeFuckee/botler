// 设置导航搜索框真实布局回归测试（issue #441 修复 + issue #442 去图标）：
// 搜索框由 flex 容器管理水平布局；issue #442 移除放大镜图标后，输入框
// 直接占满容器整行（flex:1），不再有图标占位与图标相关样式规则。
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

test('设置导航搜索框：输入框占满整行、无图标占位（issue #442）', () => {
  const search = ruleBlock('.settings-nav-search')
  const input = ruleBlock('.settings-nav-input')

  assert.match(search, /display:\s*flex\s*;/,
    '搜索框容器应使用 flex 布局，输入框占满整行')
  assert.match(search, /align-items:\s*center\s*;/,
    '输入框应在容器内垂直居中')
  assert.match(input, /flex:\s*1\s*;/,
    '输入框应占满容器剩余宽度（不再为图标让位）')
  assert.match(input, /padding-left:\s*12px\s*;/,
    '输入框应保留与 .input 一致的 12px 左内边距')
  assert.doesNotMatch(styles, /\.settings-nav-search-icon\s*\{/,
    '不应再存在搜索图标样式规则')
})
