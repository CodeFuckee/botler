// 概览页「开放 Issue」板块长标题溢出测试（issue #81）：issue 标题文字
// 超出开放 issue 的范围，遮挡右侧评论数与更新时间。
//
// 根因：.issue-link 是 inline <a>，inline 元素上 overflow:hidden 与
// text-overflow:ellipsis 不生效，长标题直接横向溢出；.issue-main 缺
// flex:1，宽度计算不可靠。
//
// 断言（styles.css 源码断言，与 tasks-responsive-cols.test.mjs 风格一致）：
// 1. .issue-link 显式声明块级显示（block/inline-block），使 ellipsis 生效；
// 2. .issue-link 保留 overflow:hidden + text-overflow:ellipsis + nowrap；
// 3. .issue-main 声明 flex:1（占据除右侧元信息外的全部宽度）；
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

test('issue 标题链接为块级显示，长标题 ellipsis 生效（复现 issue #81）', () => {
  const link = cssRule(styles, 'issue-link')
  // inline 元素上 text-overflow:ellipsis 不生效——必须显式块级化
  assert.match(link, /display\s*:\s*(block|inline-block)/,
               '.issue-link 应声明块级 display，否则 ellipsis 对 inline 元素无效')
  assert.match(link, /overflow\s*:\s*hidden/, '.issue-link 应保留 overflow:hidden')
  assert.match(link, /text-overflow\s*:\s*ellipsis/, '.issue-link 应保留 text-overflow:ellipsis')
  assert.match(link, /white-space\s*:\s*nowrap/, '.issue-link 应保留 white-space:nowrap')
})

test('issue 主列 flex:1 占据剩余宽度，右侧元信息不被遮挡', () => {
  const main = cssRule(styles, 'issue-main')
  assert.match(main, /flex\s*:\s*1\b/, '.issue-main 应声明 flex:1 占据剩余宽度')
  assert.match(main, /min-width\s*:\s*0/, '.issue-main 应保留 min-width:0（允许收缩到内容宽度以下）')
  const side = cssRule(styles, 'issue-side')
  assert.match(side, /flex-shrink\s*:\s*0/, '.issue-side 应保持 flex-shrink:0（评论数/时间不被压缩）')
})
