// 轻量 Markdown 渲染组件（issue #27 第六轮）：设置页内嵌展示
// docs/Synology-SSO-配置指南.md。项目无第三方 markdown 库，手写渲染器，
// 覆盖指南文档用到的语法：标题 / 段落 / 围栏代码块 / 嵌套列表 / 表格 /
// 引用块 / 行内 **粗体**、`代码`、[链接](url)。
//
// issue #181：issue 详情页「回复有 git 提交可点击跳转」——新增可选
// projectUrl prop（项目 web 基地址），传入时把纯文本中的 Git 提交
// SHA（7-40 位十六进制、词边界）渲染为跳转到 GitLab 提交页的链接；
// 未传 projectUrl 时行为与原来完全一致（设置页文档等场景不受影响）。
//
// 安全约束：全部内容走 React 文本节点（不使用 dangerouslySetInnerHTML），
// 文档中的 HTML 片段按纯文本转义显示。
import React from 'react'

// Git 提交 SHA 匹配（issue #181）：7-40 位十六进制且两侧为词边界。
// git 短 SHA 常用 7 位（bot 完成评论「提交Commit：d6adbde」），
// 完整 SHA 40 位；\b 依赖 \w，hex 均为单词字符，故混入非 hex 字母
// 的字符串（如 abc1234xyz）不会误判。
const COMMIT_SHA_RE = /\b[0-9a-f]{7,40}\b/gi

/** 纯文本中的 Git 提交 SHA → 可点击链接（issue #181，纯函数导出便于测试）。
 *
 * projectUrl 为项目 web 基地址（如 https://host/ns/repo，无结尾斜杠）；
 * 非空时把文本中的提交短 SHA / 完整 SHA 渲染为
 * `{projectUrl}/-/commit/{sha}` 链接（GitLab 支持短 SHA 提交页，
 * 短 SHA 会 302 跳转到对应提交）。projectUrl 为空时原样返回文本。
 * 返回字符串与 <a> 元素混合数组，keyPrefix 用于生成稳定 key。
 */
export function linkifyCommits(text, projectUrl, keyPrefix = '') {
  if (!text || !projectUrl || typeof projectUrl !== 'string') return [text]
  const parts = []
  COMMIT_SHA_RE.lastIndex = 0
  let last = 0
  let i = 0
  let m
  while ((m = COMMIT_SHA_RE.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const sha = m[0]
    // GitLab 提交页 URL 统一用小写 sha（Git 提交哈希本身即小写，
    // 粘贴大写 hex 时归一化避免 404）
    const urlSha = sha.toLowerCase()
    parts.push(
      <a key={`${keyPrefix}cm${i++}`} className="commit-link"
         href={`${projectUrl}/-/commit/${urlSha}`} target="_blank"
         rel="noreferrer" title={`跳转到 GitLab 提交页面（${sha}）`}>
        {sha}
      </a>
    )
    last = m.index + sha.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

/** 行内语法渲染：**粗体** / `行内代码` / [文字](url) / 提交 SHA 链接，
 * 其余按纯文本。projectUrl 非空时纯文本段中的 Git 提交 SHA 转链接
 * （issue #181）；code 与既有链接内部不重复链接化（token 整体匹配）。 */
function renderInline(text, keyPrefix = '', projectUrl = '') {
  if (!text) return null
  const parts = []
  // 一次正则切分三种行内语法，按顺序 tokenize
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g
  let last = 0
  let m
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(...linkifyCommits(text.slice(last, m.index), projectUrl,
                                   `${keyPrefix}c${i++}-`))
    }
    const token = m[0]
    if (token.startsWith('**')) {
      parts.push(<strong key={`${keyPrefix}s${i++}`}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('`')) {
      parts.push(<code key={`${keyPrefix}c${i++}`}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('[')) {
      const sep = token.lastIndexOf('](')
      const label = token.slice(1, sep)
      const href = token.slice(sep + 2, -1)
      parts.push(
        <a key={`${keyPrefix}a${i++}`} href={href} target="_blank" rel="noreferrer">
          {label}
        </a>
      )
    }
    last = m.index + token.length
  }
  if (last < text.length) {
    parts.push(...linkifyCommits(text.slice(last), projectUrl,
                                 `${keyPrefix}c${i++}-`))
  }
  return parts
}

/** 列表块解析：连续列表行（支持两空格缩进嵌套），返回 ul/ol 元素 */
function renderList(lines, keyPrefix = '', projectUrl = '') {
  const rootItems = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const m = line.match(/^(\s*)([-*]|\d+\.)\s+(.*)$/)
    if (!m) { i++; continue }
    const indent = m[1].length
    const ordered = /^\d+\.$/.test(m[2])
    const content = m[3]
    // 收集该列表项的嵌套子行（缩进更深）
    const sub = []
    while (i + 1 < lines.length) {
      const next = lines[i + 1]
      const nm = next.match(/^(\s*)([-*]|\d+\.)\s+(.*)$/)
      if (nm && nm[1].length > indent) {
        sub.push(next.slice(indent))
        i++
      } else break
    }
    rootItems.push(
      <li key={`${keyPrefix}li${i}`}>
        {renderInline(content, `${keyPrefix}li${i}-`, projectUrl)}
        {sub.length > 0 && renderList(sub, `${keyPrefix}li${i}-`, projectUrl)}
      </li>
    )
    i++
  }
  // 根层列表类型由第一项决定（简单文档够用）
  const first = lines.find((l) => l.match(/^\s*([-*]|\d+\.)\s+/))
  const ordered = first ? /^\s*\d+\./.test(first) : false
  return ordered
    ? <ol key={`${keyPrefix}ol`}>{rootItems}</ol>
    : <ul key={`${keyPrefix}ul`}>{rootItems}</ul>
}

/** 表格块解析：| 表头 | 数据行 |，第二行为 |---| 分隔行 */
function renderTable(lines, keyPrefix = '', projectUrl = '') {
  const rows = lines.map((l) =>
    l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
  )
  const header = rows[0]
  const body = rows.slice(2) // 跳过表头与分隔行
  return (
    <table className="table" key={`${keyPrefix}tbl`}>
      <thead>
        <tr>{header.map((c, j) => <th key={`${keyPrefix}h${j}`}>{renderInline(c, `${keyPrefix}h${j}-`, projectUrl)}</th>)}</tr>
      </thead>
      <tbody>
        {body.map((r, ri) => (
          <tr key={`${keyPrefix}r${ri}`}>
            {r.map((c, j) => <td key={`${keyPrefix}r${ri}c${j}`}>{renderInline(c, `${keyPrefix}r${ri}c${j}-`, projectUrl)}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 解析为块元素列表 */
function parseBlocks(content) {
  const lines = content.split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }
    // 围栏代码块
    const fence = line.match(/^```\w*$/)
    if (fence) {
      const buf = []
      i++
      while (i < lines.length && !lines[i].match(/^```/)) { buf.push(lines[i]); i++ }
      i++ // 跳过结尾 ```
      blocks.push({ type: 'code', text: buf.join('\n') })
      continue
    }
    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) { blocks.push({ type: `h${h[1].length}`, text: h[2] }); i++; continue }
    // 引用块（连续 > 行）
    if (line.startsWith('>')) {
      const buf = []
      while (i < lines.length && lines[i].startsWith('>')) {
        buf.push(lines[i].replace(/^>\s?/, ''))
        i++
      }
      blocks.push({ type: 'quote', lines: buf })
      continue
    }
    // 表格（当前行含 | 且下一行是 |---| 分隔行）
    const isSep = (l) => /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(l) && l.includes('-')
    if (line.includes('|') && i + 1 < lines.length && isSep(lines[i + 1])) {
      const buf = [line, lines[i + 1]]
      i += 2
      while (i < lines.length && lines[i].includes('|')) { buf.push(lines[i]); i++ }
      blocks.push({ type: 'table', lines: buf })
      continue
    }
    // 列表（连续列表行，含缩进子项）
    if (line.match(/^\s*([-*]|\d+\.)\s+/)) {
      const buf = []
      while (i < lines.length && lines[i].match(/^\s*([-*]|\d+\.)\s+/)) {
        buf.push(lines[i])
        i++
      }
      // 列表行后紧跟的更深缩进文本行归入前一项？指南文档无此用法，跳过
      blocks.push({ type: 'list', lines: buf })
      continue
    }
    // 段落（连续非空行合并）
    const buf = [line]
    i++
    while (i < lines.length && lines[i].trim()
      && !lines[i].match(/^(#{1,4})\s/)
      && !lines[i].match(/^```/)
      && !lines[i].match(/^\s*([-*]|\d+\.)\s+/)
      && !lines[i].startsWith('>')) {
      buf.push(lines[i])
      i++
    }
    blocks.push({ type: 'p', text: buf.join(' ') })
  }
  return blocks
}

export default function Markdown({ content, projectUrl = '' }) {
  if (!content || typeof content !== 'string') return null
  const blocks = parseBlocks(content)
  return (
    <div>
      {blocks.map((b, i) => {
        switch (b.type) {
          case 'h1': return <h1 key={i}>{renderInline(b.text, `b${i}-`, projectUrl)}</h1>
          case 'h2': return <h2 key={i}>{renderInline(b.text, `b${i}-`, projectUrl)}</h2>
          case 'h3': return <h3 key={i}>{renderInline(b.text, `b${i}-`, projectUrl)}</h3>
          case 'h4': return <h4 key={i}>{renderInline(b.text, `b${i}-`, projectUrl)}</h4>
          case 'code': return (
            <pre key={i}><code>{b.text}</code></pre>
          )
          case 'quote': return (
            <blockquote key={i}>
              {b.lines.map((l, j) => <p key={j}>{renderInline(l, `b${i}q${j}-`, projectUrl)}</p>)}
            </blockquote>
          )
          case 'table': return renderTable(b.lines, `b${i}-`, projectUrl)
          case 'list': return renderList(b.lines, `b${i}-`, projectUrl)
          case 'p': return <p key={i}>{renderInline(b.text, `b${i}-`, projectUrl)}</p>
          default: return null
        }
      })}
    </div>
  )
}
