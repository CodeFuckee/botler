// 概览页「开放 Issue」单列分组布局组头折叠按钮换行测试：单列分组的仓库
// 子分组组头 .issue-repo-head 首行应为「折叠开关 + 仓库名」并排，但实际
// 渲染中折叠开关单独占一行、仓库名另起一行（用户截图）。
//
// 根因：styles.css 中 .issue-repo-group-name（flex-basis: auto，让单列
// 布局仓库名与折叠开关同行）定义在基础规则 .issue-repo-name
// （flex-basis: 100%，卡片布局让仓库名独占首行，issue #102）之前；
// 两个单类选择器同优先级，层叠按源码顺序后者胜出，覆盖规则成为死代码，
// 单列布局仓库名仍独占整行，把折叠开关挤到单独一行。
//
// 断言（模拟 CSS 层叠：同优先级规则按源码顺序后者覆盖前者）：
// 1. 同时带 issue-repo-name + issue-repo-group-name 两个类的元素，生效的
//    flex-basis 应为 auto（折叠开关+仓库名同行），不得为 100%；
// 2. .issue-repo-head 仍 flex-wrap: wrap——修复不得回归窄卡片换行能力
//    （issue #102 契约，仓库名在窄视口下仍可换行不被压没）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 去注释后再匹配规则：注释里会提到类名/属性（如 #102 注释引用
// .issue-repo-name），不去掉会干扰规则定位
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

// 收集选择器列表包含 className 的规则体（按源码顺序）
function rulesFor(css, className) {
  const out = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m
  while ((m = re.exec(css)) !== null) {
    const selectors = m[1].split(',').map((s) => s.trim())
    if (selectors.includes(className)) out.push(m[2])
  }
  return out
}

// 模拟同优先级（单类选择器）层叠：源码顺序后者覆盖前者，返回属性生效值。
// classNames 为元素同时携带的类，任一规则命中即参与层叠。
function effectiveDecl(css, classNames, prop) {
  let value = null
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m
  while ((m = re.exec(css)) !== null) {
    const selectors = m[1].split(',').map((s) => s.trim())
    if (!classNames.some((c) => selectors.includes(c))) continue
    const decl = m[2].split(';').map((d) => d.trim())
      .find((d) => d.startsWith(`${prop}:`))
    if (decl) value = decl.slice(prop.length + 1).trim()
  }
  return value
}

test('层叠模拟：issue-repo-name + issue-repo-group-name 并存时生效 flex-basis 应为 auto（折叠开关+仓库名同行）', () => {
  // 单列分组组头仓库名 JSX：className="issue-repo-name issue-repo-group-name"
  const v = effectiveDecl(styles,
                          ['.issue-repo-name', '.issue-repo-group-name'],
                          'flex-basis')
  assert.equal(
    v, 'auto',
    `仓库名在单列分组组头的生效 flex-basis 应为 auto（实际 ${v}）——` +
    '为 100% 时仓库名独占整行，折叠开关被挤到单独一行',
  )
})

test('styles.css：.issue-repo-group-name 覆盖规则须位于 .issue-repo-name 基础规则之后（同优先级靠源码顺序取胜）', () => {
  const base = rulesFor(styles, '.issue-repo-name')
  const override = rulesFor(styles, '.issue-repo-group-name')
  assert.ok(base.length >= 1, 'styles.css 应存在 .issue-repo-name 基础规则')
  assert.ok(override.length >= 1, 'styles.css 应存在 .issue-repo-group-name 覆盖规则')
  const basePos = styles.indexOf(base[0])
  const overridePos = styles.indexOf(override[0])
  assert.ok(
    overridePos > basePos,
    `.issue-repo-group-name 覆盖规则（位置 ${overridePos}）必须出现在 ` +
    `.issue-repo-name 基础规则（位置 ${basePos}）之后——` +
    '两者同为单类选择器，覆盖规则在前会被基础规则的 flex-basis: 100% 反超',
  )
})

test('styles.css：.issue-repo-head 仍 flex-wrap: wrap（不回归 issue #102 窄卡片换行）', () => {
  const bodies = rulesFor(styles, '.issue-repo-head')
  assert.ok(bodies.length >= 1, 'styles.css 应存在 .issue-repo-head 规则')
  assert.match(
    bodies.join(';'),
    /flex-wrap:\s*wrap/,
    '组头应保留 flex-wrap: wrap，窄视口下仓库名仍可换行不被固定项压没',
  )
})
