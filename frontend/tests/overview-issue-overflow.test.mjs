// 概览页「开放 Issue」板块长标题展示测试（issue #81 → issue #476）。
//
// issue #81：.issue-link 原为 inline <a>，inline 元素上 overflow:hidden 与
// text-overflow:ellipsis 不生效，长标题直接横向溢出遮挡右侧评论数与更新时间，
// 修复为块级化 + 单行 ellipsis 截断。
// issue #476：产品调整——issue 内容一行显示不下时改为多行显示，取消省略号
// 截断。因此本测试改为断言：
// 1. .issue-link 仍显式声明块级显示（block），整行可点击、宽度撑满；
// 2. .issue-link 不再声明 nowrap / text-overflow:ellipsis（允许自然换行多行）；
// 3. .issue-main 声明 flex:1（占据除右侧元信息外的全部宽度）+ min-width:0；
// 4. .issue-side 保持 flex-shrink:0（评论数/时间不被压缩）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// 提取 styles.css 中指定 class 的首个规则体（跳过 :hover 等伪类）
function cssRule(css, cls) {
  const rule = css.match(new RegExp(`\\.${cls}\\s*\\{([^}]*)\\}`))
  assert.ok(rule, `styles.css 缺少 .${cls} 规则`)
  return rule[1]
}

test('issue 标题链接为块级显示且允许多行换行，不再单行省略号截断（issue #476）', () => {
  const link = cssRule(styles, 'issue-link')
  // 块级化保留：整行可点击、宽度撑满（issue #81 的布局基础）
  assert.match(link, /display\s*:\s*(block|inline-block)/,
               '.issue-link 应声明块级 display')
  // 多行显示：不再禁止换行 / 省略号 / 溢出裁切（issue #476 取消单行省略号展示）。
  // 用 ^ 行首锚定 + m 标志匹配属性声明本身，避免命中注释里的字样。
  assert.doesNotMatch(link, /^\s*white-space\s*:\s*nowrap/m,
                      '.issue-link 不应声明 white-space:nowrap——需允许多行换行')
  assert.doesNotMatch(link, /^\s*text-overflow\s*:\s*ellipsis/m,
                      '.issue-link 不应声明 text-overflow:ellipsis——取消省略号展示')
  assert.doesNotMatch(link, /^\s*overflow\s*:\s*hidden/m,
                      '.issue-link 不应声明 overflow:hidden——避免裁掉换行后的内容')
})

test('issue 主列 flex:1 占据剩余宽度，右侧元信息不被遮挡', () => {
  const main = cssRule(styles, 'issue-main')
  assert.match(main, /flex\s*:\s*1\b/, '.issue-main 应声明 flex:1 占据剩余宽度')
  assert.match(main, /min-width\s*:\s*0/, '.issue-main 应保留 min-width:0（允许收缩到内容宽度以下）')
  const side = cssRule(styles, 'issue-side')
  assert.match(side, /flex-shrink\s*:\s*0/, '.issue-side 应保持 flex-shrink:0（评论数/时间不被压缩）')
})
