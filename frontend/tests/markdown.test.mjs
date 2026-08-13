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

/** 渲染组件为 React 元素并返回 JSON 树序列化（便于断言结构） */
function renderTree(content) {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(Markdown, { content }))
  })
  const json = JSON.stringify(renderer.toJSON())
  TestRenderer.act(() => renderer.unmount())
  return json
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
