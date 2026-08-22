// 设置页左侧导航搜索框图标与提示文字间距回归测试（issue #436）：
// 图标必须在 flex 布局中占据独立空间，并通过容器间距与输入框分隔，
// 不得绝对定位覆盖 placeholder。
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

test('设置导航搜索框让 16px 图标在 flex 布局中占据独立空间', () => {
  const search = ruleBlock('.settings-nav-search')
  const icon = ruleBlock('.settings-nav-search-icon')
  const input = ruleBlock('.settings-nav-input')

  assert.match(search, /display:\s*flex\s*;/, '搜索框容器应使用 flex 布局')
  assert.match(search, /align-items:\s*center\s*;/, '图标与输入框应垂直居中')
  assert.match(search, /gap:\s*var\(--space-2\)\s*;/, '图标与输入框应保留明确间距')
  assert.doesNotMatch(icon, /position:\s*absolute\s*;/, '图标不得绝对定位覆盖输入文字')
  assert.match(icon, /flex:\s*0\s+0\s+16px\s*;/, '图标应占用固定 16px 宽度')
  assert.match(input, /flex:\s*1\s*;/, '输入框应填满图标之后的剩余空间')
})
