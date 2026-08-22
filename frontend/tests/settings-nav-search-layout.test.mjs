// 设置导航搜索框真实布局回归测试（issue #441）：
// 先前实现把图标绝对定位并仅靠 input 左内边距预留文字起点。用户反馈在
// 实际页面仍发生图标与 placeholder 重叠，因此搜索框必须改为由 flex 容器
// 管理图标、输入框和清除按钮的水平布局，不能继续让图标覆盖在输入区域上。
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

test('设置导航搜索框用 flex 显式分隔图标与输入文字', () => {
  const search = ruleBlock('.settings-nav-search')
  const icon = ruleBlock('.settings-nav-search-icon')
  const input = ruleBlock('.settings-nav-input')

  assert.match(search, /display:\s*flex\s*;/,
    '搜索框容器应使用 flex 布局，图标和输入框应是相邻项目而非覆盖关系')
  assert.match(search, /align-items:\s*center\s*;/,
    '搜索框图标和输入框应在同一行垂直居中')
  assert.match(search, /gap:\s*(?:var\(--space-[^)]+\)|\d+px)\s*;/,
    '图标与输入框之间应由明确间距控制')
  assert.doesNotMatch(icon, /position:\s*absolute\s*;/,
    '搜索图标不得绝对定位覆盖输入文字区域')
  assert.match(icon, /flex:\s*0\s+0\s+16px\s*;/,
    '搜索图标应占用固定的 16px 布局宽度')
  assert.match(input, /flex:\s*1\s*;/,
    '输入框应占用图标之后的剩余水平空间')
})
