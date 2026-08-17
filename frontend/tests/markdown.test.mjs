// Markdown 轻量渲染组件测试（issue #27 第六轮）：设置页内嵌展示
// docs/Synology-SSO-配置指南.md。项目无第三方 markdown 库，新增
// src/components/Markdown.jsx 手写渲染器，覆盖指南文档用到的全部语法：
// 标题 / 段落 / 围栏代码块 / 嵌套列表 / 表格 / 引用块 / 行内 code、粗体、链接。
// 安全约束：全部内容走 React 文本节点渲染（不使用 dangerouslySetInnerHTML），
// 文档中的 HTML 片段按纯文本转义显示。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Markdown } = await vite.ssrLoadModule('/src/components/Markdown.jsx')

after(() => vite.close())

/** 渲染组件为 React 元素并返回 JSON 树序列化（便于断言结构）；
 *  projectUrl 可选（issue #181：提交 SHA 链接化） */
function renderTree(content, projectUrl = '') {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(
      React.createElement(Markdown, { content, projectUrl }))
  })
  const json = JSON.stringify(renderer.toJSON())
  TestRenderer.act(() => renderer.unmount())
  return json
}

/** 渲染树 → 纯文本（与 overview-issue-notes.test.mjs 的 toText 一致） */
function toText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number' || typeof node === 'boolean') return String(node)
  if (Array.isArray(node)) return node.map(toText).join('')
  if (typeof node === 'object') {
    const children = node.children ?? node.props?.children
    return toText(children)
  }
  return ''
}

test('标题：# / ## / ### 渲染为 h1 / h2 / h3', () => {
  const tree = renderTree('# 一级\n\n## 二级\n\n### 三级')
  assert.match(tree, /"h1"/)
  assert.match(tree, /"h2"/)
  assert.match(tree, /"h3"/)
  assert.match(tree, /"一级"/)
  assert.match(tree, /"三级"/)
})

test('段落：普通文本渲染为段落节点', () => {
  const tree = renderTree('第一段文字。\n\n第二段文字。')
  assert.match(tree, /"p"/)
  assert.match(tree, /"第一段文字。"/)
  assert.match(tree, /"第二段文字。"/)
})

test('围栏代码块：``` 多行内容原样保留（含换行）', () => {
  const tree = renderTree('```yaml\nsso:\n  enabled: true\n```')
  assert.match(tree, /"pre"/)
  assert.match(tree, /"code"/)
  assert.match(tree, /"sso:\\n  enabled: true"/)
})

test('无序列表与嵌套列表：- 与缩进层级', () => {
  const tree = renderTree('- 第一项\n- 第二项\n  - 嵌套项')
  assert.match(tree, /"ul"/)
  assert.match(tree, /"li"/)
  assert.match(tree, /"第一项"/)
  assert.match(tree, /"嵌套项"/)
})

test('有序列表：1. 2. 数字列表', () => {
  const tree = renderTree('1. 步骤一\n2. 步骤二')
  assert.match(tree, /"ol"/)
  assert.match(tree, /"步骤一"/)
  assert.match(tree, /"步骤二"/)
})

test('表格：| 分隔的表头与数据行渲染为 table', () => {
  const tree = renderTree('| 现象 | 处理 |\n|---|---|\n| 登录失败 | 检查配置 |')
  assert.match(tree, /"table"/)
  assert.match(tree, /"thead"/)
  assert.match(tree, /"tbody"/)
  assert.match(tree, /"现象"/)
  assert.match(tree, /"检查配置"/)
})

test('引用块：> 行渲染为 blockquote', () => {
  const tree = renderTree('> 这是一段引用提示')
  assert.match(tree, /"blockquote"/)
  assert.match(tree, /"这是一段引用提示"/)
})

test('行内语法：**粗体**、`行内代码`、[链接](url)', () => {
  const tree = renderTree('这是 **粗体** 和 `code` 与 [文档](https://example.com)')
  assert.match(tree, /"strong"/)
  assert.match(tree, /"粗体"/)
  assert.match(tree, /"code"/)
  assert.match(tree, /"a"/)
  assert.match(tree, /"https:\/\/example\.com"/)
})

test('边界：空字符串 / null / undefined 不渲染任何内容也不崩溃', () => {
  for (const empty of ['', null, undefined]) {
    const tree = renderTree(empty)
    assert.ok(tree, `content=${empty} 应正常返回`)
    // 无内容时渲染为 null（JSON 序列化为 "null"）
    assert.equal(tree, 'null')
  }
})

test('安全：HTML 标签按纯文本转义渲染（不产生真实元素节点）', () => {
  const tree = renderTree('<script>alert(1)</script>')
  // 渲染树中不应出现 script 元素类型
  assert.doesNotMatch(tree, /"script"/)
  // 文本节点按原样转义保留
  assert.match(tree, /<script>alert\(1\)<\/script>/)
})

test('混合长文档：指南类文档整体渲染不崩溃且关键小节齐全', () => {
  const doc = [
    '# Synology SSO 登录配置指南',
    '',
    '接入群晖 **SSO Server**（OIDC 协议）。',
    '',
    '## 一、群晖 SSO Server 侧配置',
    '',
    '1. 在 DSM「套件中心」安装 SSO Server 套件。',
    '2. 记录 **Well-known URL**（形如 `https://<群晖地址>/.well-known/openid-configuration`）。',
    '',
    '> 群晖若使用自签名证书，Botler 侧需关闭证书校验。',
    '',
    '## 四、常见问题',
    '',
    '| 现象 | 原因与处理 |',
    '|---|---|',
    '| 无法访问 | 确认 Well-known URL 正确 |',
    '',
    '```yaml',
    'sso:',
    '  enabled: true',
    '```',
  ].join('\n')
  const tree = renderTree(doc)
  assert.match(tree, /"h1"/)
  assert.match(tree, /"h2"/)
  assert.match(tree, /"ol"/)
  assert.match(tree, /"table"/)
  assert.match(tree, /"blockquote"/)
  assert.match(tree, /"pre"/)
  assert.match(tree, /"strong"/)
  assert.match(tree, /"Synology SSO 登录配置指南"/)
})

// ---- issue #181：Git 提交 SHA 可点击跳转 GitLab 提交页 ----

test('提交链接化：短 SHA 渲染为跳转 GitLab 提交页的链接', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const parts = linkifyCommits('提交Commit：d6adbde（feat: xx）',
                               'https://gitlab.example.com/chenkaidi/botler')
  const a = parts.find((p) => typeof p === 'object' && p.type === 'a')
  assert.ok(a, '短 SHA 应渲染为 <a> 链接')
  assert.equal(a.props.href,
    'https://gitlab.example.com/chenkaidi/botler/-/commit/d6adbde')
  assert.equal(a.props.className, 'commit-link')
  assert.equal(a.props.target, '_blank')
  assert.ok(toText(a.props.children).includes('d6adbde'), '链接文案保留 SHA 原文')
})

test('提交链接化：完整 40 位 SHA 与多处 SHA 均渲染链接', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const full = 'd6adbde9d2c4074ee7634f56010a3ccb088cdd24'
  const parts = linkifyCommits(`mentioned in commit ${full} 与 a1b2c3d`,
                               'https://gitlab.example.com/chenkaidi/botler')
  const links = parts.filter((p) => typeof p === 'object' && p.type === 'a')
  assert.equal(links.length, 2, '应渲染两处提交链接')
  assert.equal(links[0].props.href,
    `https://gitlab.example.com/chenkaidi/botler/-/commit/${full}`)
  assert.equal(links[1].props.href,
    'https://gitlab.example.com/chenkaidi/botler/-/commit/a1b2c3d')
})

test('提交链接化：大写 hex 归一化为小写提交页 URL', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const parts = linkifyCommits('D6ADBDE 提交',
                               'https://gitlab.example.com/chenkaidi/botler')
  const a = parts.find((p) => typeof p === 'object' && p.type === 'a')
  assert.ok(a, '大写 hex 应渲染链接')
  assert.equal(a.props.href,
    'https://gitlab.example.com/chenkaidi/botler/-/commit/d6adbde',
    'URL 应用小写 sha')
})

test('提交链接化：无 projectUrl 时不渲染链接（原样文本）', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const parts = linkifyCommits('提交Commit：d6adbde', '')
  assert.equal(parts.length, 1, '应原样返回文本')
  assert.equal(typeof parts[0], 'string')
  assert.equal(parts[0], '提交Commit：d6adbde')
  assert.ok(!parts.some((p) => typeof p === 'object'), '不应出现 <a> 元素')
})

test('提交链接化：非 hex 词 / 混入非 hex 字母 / 不足 7 位 / 超长不误判', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const url = 'https://gitlab.example.com/chenkaidi/botler'
  // commit 含非 hex 字母 o/m/t → 不匹配；abc1234x 末尾 x 非 hex → 不匹配；
  // feed 仅 4 位 → 不匹配；41 位超长 hex 不截断匹配（>40 无词边界）→ 不匹配；
  // 完全中文 → 不匹配
  const long41 = 'a'.repeat(41)
  const parts = linkifyCommits(`commit 记录 abc1234x feed ${long41} 提交完成`,
                               url)
  assert.ok(!parts.some((p) => typeof p === 'object'),
            '非提交 SHA 文本不应渲染链接')
})

test('提交链接化：空文本 / null 安全返回', async () => {
  const mod = await vite.ssrLoadModule('/src/components/Markdown.jsx')
  const { linkifyCommits } = mod
  const url = 'https://gitlab.example.com/chenkaidi/botler'
  assert.deepEqual(linkifyCommits('', url), [''])
  assert.deepEqual(linkifyCommits(null, url), [null])
})

// Markdown 组件集成：projectUrl 传入时提交 SHA 转链接
test('Markdown：projectUrl 传入时段落内提交 SHA 渲染为链接', () => {
  const tree = renderTree(
    '提交Commit：d6adbde（feat: xx）',
    'https://gitlab.example.com/chenkaidi/botler')
  assert.match(tree, /commit-link/)
  assert.match(tree, /https:\/\/gitlab\.example\.com\/chenkaidi\/botler\/-\/commit\/d6adbde/)
  assert.match(tree, /d6adbde/)
})

test('Markdown：未传 projectUrl 时提交 SHA 保持纯文本', () => {
  const tree = renderTree('提交Commit：d6adbde')
  assert.doesNotMatch(tree, /commit-link/)
  assert.doesNotMatch(tree, /\/-\/commit\//)
  assert.match(tree, /d6adbde/)
})

test('Markdown：代码块/行内 code 内的 SHA 不链接化', () => {
  // 行内 code：`` `d6adbde` `` 不应出现链接
  const treeCode = renderTree('SHA 为 `d6adbde`',
                              'https://gitlab.example.com/chenkaidi/botler')
  assert.match(treeCode, /"code"/)
  assert.doesNotMatch(treeCode, /commit-link/, 'code 内 SHA 不应转链接')
  // 围栏代码块内 SHA 同样不链接化
  const treeFence = renderTree('```\nd6adbde\n```',
                               'https://gitlab.example.com/chenkaidi/botler')
  assert.doesNotMatch(treeFence, /commit-link/, '围栏代码块内 SHA 不应转链接')
})

test('Markdown：既有 [链接] 内 SHA 不重复链接化', () => {
  const tree = renderTree(
    '查看 [d6adbde](https://gitlab.example.com/other/-/commit/d6adbde)',
    'https://gitlab.example.com/chenkaidi/botler')
  assert.match(tree, /"a"/)
  assert.equal((tree.match(/commit-link/g) || []).length, 0,
               '既有链接标签内的 SHA 不应再套一层链接')
})

test('Markdown：列表/引用块中的 SHA 同样渲染提交链接', () => {
  const treeList = renderTree('- 提交 d6adbde 完成',
                              'https://gitlab.example.com/chenkaidi/botler')
  assert.match(treeList, /commit-link/)
  const treeQuote = renderTree('> 提到 d6adbde9d2c4074ee7634f56010a3ccb088cdd24',
                               'https://gitlab.example.com/chenkaidi/botler')
  assert.match(treeQuote, /commit-link/)
})
