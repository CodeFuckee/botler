// 设置页左侧导航栏四周不留边距测试（issue #344）：
// 设置页左侧设置导航栏此前是「悬浮卡片」形态（.settings-nav 带 16px 四边
// 内边距 + 背景 + 阴影 + 圆角），四周有明显留边；issue #339 将设置页改为
// 「左侧列表 + 右侧详情」的手机设置页观感后，导航栏应贴近手机设置页左侧
// 列表——四周不再留边距，去掉卡片容器样式（内边距/背景/阴影/圆角），
// 导航内容直接铺在页面上。
//
// 断言（styles.css 源码级）：
// 1. .settings-nav 基础规则不留内边距（padding: 0 或不声明 padding）——
//    四周不留边距；
// 2. .settings-nav 基础规则不再是卡片容器（不声明 background /
//    box-shadow / border-radius）——导航不悬浮于页面之上；
// 3. .settings-nav-rail（整体折叠态窄栏）同样去掉卡片容器样式，
//    折叠/展开两种状态观感一致；
// 4. 不误伤设置页两栏布局：.settings-layout 仍为两栏网格 + 列间距；
// 5. 导航面板内部滚动能力保留（max-height + overflow-y: auto），
//    导航项多时不截断。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

/** 提取 styles.css 中首个匹配选择器的规则块（取基础规则：断点外首个声明） */
function ruleBlock(css, selector) {
  // 锚定行首：只匹配「行首直接声明」的基础规则，避免误取
  // .settings-sidebar.collapsed .settings-nav 等复合选择器规则
  const re = new RegExp('(?:^|\\n)\\s*' + selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')
  const m = css.match(re)
  assert.ok(m, `styles.css 应存在 ${selector} 规则`)
  return m[1]
}

test('基础规则：.settings-nav 四周不留内边距（padding: 0）', () => {
  const body = ruleBlock(styles, '.settings-nav')
  // 允许两种写法：显式 padding: 0，或完全不再声明 padding（继承默认 0）
  const hasPadding = /padding\s*:/.test(body)
  if (hasPadding) {
    assert.match(body, /padding\s*:\s*0\b/, '.settings-nav 若声明 padding 必须为 0（四周不留边距）')
  }
})

test('基础规则：.settings-nav 不再是卡片容器（无背景/阴影/圆角）', () => {
  const body = ruleBlock(styles, '.settings-nav')
  assert.doesNotMatch(body, /background\s*:/, '.settings-nav 不应再声明背景（不再是悬浮卡片）')
  assert.doesNotMatch(body, /box-shadow\s*:/, '.settings-nav 不应再声明阴影（不再是悬浮卡片）')
  assert.doesNotMatch(body, /border-radius\s*:/, '.settings-nav 不应再声明圆角（不再是悬浮卡片）')
})

test('基础规则：.settings-nav-rail 折叠态同样去掉卡片容器样式', () => {
  const body = ruleBlock(styles, '.settings-nav-rail')
  assert.doesNotMatch(body, /background\s*:/, '.settings-nav-rail 不应再声明背景（折叠态与展开态观感一致）')
  assert.doesNotMatch(body, /box-shadow\s*:/, '.settings-nav-rail 不应再声明阴影')
  assert.doesNotMatch(body, /border-radius\s*:/, '.settings-nav-rail 不应再声明圆角')
  assert.doesNotMatch(body, /padding\s*:\s*(?!0\b)[^;]+/, '.settings-nav-rail 不应保留非零内边距')
})

test('不误伤两栏布局：.settings-layout 仍为两栏网格 + 列间距', () => {
  const body = ruleBlock(styles, '.settings-layout')
  assert.match(body, /grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)/,
    '设置页应保持「侧栏 auto + 面板 1fr」两栏网格')
  assert.match(body, /gap:\s*var\(--space-5\)/, '桌面设置页两栏列间距应保持 24px（--space-5）')
})

test('导航面板内部滚动保留：max-height + overflow-y: auto', () => {
  const body = ruleBlock(styles, '.settings-nav')
  assert.match(body, /max-height:\s*calc\(100vh\s*-\s*92px\)/, '导航面板应限高（吸顶内部滚动）')
  assert.match(body, /overflow-y:\s*auto/, '导航面板应内部滚动（导航项多不截断）')
})
