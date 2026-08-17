// 提示词模版页折叠编辑器测试（issue #56）。
//
// 需求：模版页面默认全部展开，折叠方式改成类似任务详情页面里的
// 聊天记录的方式（issue #52 SectionToggle）。
// - 此前（issue #55）默认折叠为小高度窗口（rows=6 + 截断样式），
//   每次进入页面都要点「展开全部」才能看到完整模版
// - 本次：默认全部展开，textarea 高度自适应内容行数完整展示、无内层
//   滚动条（滚动交给页面最外层）；折叠交互改为与任务详情页聊天记录
//   一致的标题行切换（chevron ▾/▸），折叠时编辑器与操作按钮整体隐藏
//
// 修复前状态（本测试应当失败）：
// - 默认折叠 rows=6 带 template-collapsed 截断样式
// - 折叠交互是独立「展开全部/收起」按钮，折叠时 textarea 仍渲染
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const stylesSrc = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// react-router-dom mock（与 task-detail-collapsible-sections.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { api } = await vite.ssrLoadModule('/src/api.js')
const { default: Templates } = await vite.ssrLoadModule('/src/pages/Templates.jsx')

after(() => vite.close())

// ---- 模版数据与 API mock ----

// 40 行长模版，模拟 issue-agent 完整提示词超长场景
const LONG_TEMPLATE = Array.from({ length: 40 }, (_, i) => `第${i + 1}行模版内容`).join('\n')

function mockApi(template = LONG_TEMPLATE) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname === '/api/repos') return { repos: [] }
    if (pathname === '/api/settings') {
      return { templates: { default: template, placeholders: {} } }
    }
    throw new Error('unexpected ' + pathname)
  })
}

async function renderTemplates() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(
        React.createElement(Templates),
      )
      // 等待 load() 的 Promise.all 完成
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function findTextarea(renderer) {
  return renderer.root.findAllByType('textarea')
}

function findButtons(renderer, text) {
  return renderer.root
    .findAllByType('button')
    .filter((b) => String(b.props.children).includes(text))
}

function findToggle(renderer) {
  // 折叠标题行（SectionToggle 风格，与任务详情页聊天记录一致）
  return renderer.root.findAll((node) => node.type === 'button'
    && typeof node.props.className === 'string'
    && node.props.className.includes('section-toggle'))
}

// 递归提取 React children 的纯文本（chevron span + 文本 + 行数 span）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

function click(renderer, button) {
  return TestRenderer.act(async () => {
    button.props.onClick()
  })
}

// ---- 折叠编辑器行为测试（issue #56：默认展开 + 聊天记录式折叠） ----

test('模版编辑器默认全部展开：rows 自适应完整内容行数，无截断样式',
  async () => {
    mockApi()
    const { renderer, renderError } = await renderTemplates()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const textareas = findTextarea(renderer)
      assert.equal(textareas.length, 1, '默认展开态应有且仅有一个模版 textarea')
      assert.equal(
        textareas[0].props.rows,
        LONG_TEMPLATE.split('\n').length + 1,
        '默认展开态 textarea rows 应自适应完整内容行数（无内层滚动条）',
      )
      assert.doesNotMatch(
        textareas[0].props.className,
        /template-collapsed/,
        '默认展开态不应带截断样式',
      )
      assert.equal(
        findButtons(renderer, '展开全部').length,
        0,
        '不再有「展开全部」按钮（旧折叠交互已移除）',
      )
      // 折叠标题行：默认展开，chevron 朝下
      const toggles = findToggle(renderer)
      assert.equal(toggles.length, 1, '应有且仅有一个折叠标题行')
      assert.equal(toggles[0].props['aria-expanded'], true, '默认 aria-expanded 应为 true')
      assert.match(
        JSON.stringify(renderer.toJSON()),
        /lucide-chevron-down/,
        '展开态标题行 chevron 应为 Lucide ChevronDown',
      )
      assert.match(
        textOf(toggles[0].props.children),
        /40 行/,
        '标题行应提示模版行数',
      )
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  })

test('标题行点击折叠/展开切换（聊天记录式）：折叠时编辑器整体隐藏',
  async () => {
    mockApi()
    const { renderer, renderError } = await renderTemplates()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const toggle = findToggle(renderer)[0]

      // 折叠：textarea 与操作按钮整体隐藏（同任务详情页聊天记录折叠）
      await click(renderer, toggle)
      assert.equal(
        findTextarea(renderer).length,
        0,
        '折叠态不应渲染 textarea（内容整体隐藏，非截断小窗口）',
      )
      assert.equal(
        findButtons(renderer, '保存').length,
        0,
        '折叠态不应渲染保存按钮（操作区随编辑器一起隐藏）',
      )
      assert.equal(toggle.props['aria-expanded'], false, '折叠态 aria-expanded 应为 false')
      assert.match(JSON.stringify(renderer.toJSON()), /lucide-chevron-right/, '折叠态标题行 chevron 应为 Lucide ChevronRight')
      assert.match(textOf(toggle.props.children), /40 行/, '折叠态标题行仍提示行数')

      // 再次展开：编辑器恢复、内容不丢失、行数提示不变
      await click(renderer, toggle)
      const ta = findTextarea(renderer)[0]
      assert.equal(ta.props.value, LONG_TEMPLATE, '折叠切换不应丢失模版内容')
      assert.equal(ta.props.rows, 41, '重新展开后 rows 仍自适应完整内容行数')
      assert.equal(findButtons(renderer, '保存').length, 1, '展开态恢复保存按钮')
      assert.equal(toggle.props['aria-expanded'], true, '重新展开后 aria-expanded 应为 true')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  })

test('边界情况：空模版与单行模版默认展开不抛错、行数提示正确',
  async () => {
    for (const [tmpl, lines] of [['', 1], ['单行模版内容', 1]]) {
      mockApi(tmpl)
      const { renderer, renderError } = await renderTemplates()
      try {
        assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
        const ta = findTextarea(renderer)[0]
        assert.equal(ta.props.rows, lines + 1, '空/单行模版 rows 应为行数 + 1')
        assert.match(
          textOf(findToggle(renderer)[0].props.children),
          new RegExp(`${lines} 行`),
          `空/单行模版行数提示应为 ${lines} 行`,
        )
      } finally {
        await TestRenderer.act(() => renderer.unmount())
        mock.restoreAll()
      }
    }
  })

test('styles.css 不再定义 template-collapsed（折叠为整体隐藏，无需截断样式）',
  () => {
    assert.doesNotMatch(
      stylesSrc,
      /template-collapsed/,
      '折叠改为整体隐藏后，截断样式 .template-collapsed 应已移除',
    )
  })
