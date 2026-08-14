// 提示词模版页折叠编辑器测试（issue #55）。
//
// 需求：全局模版取消内层的垂直滚动，改成折叠的方式。
// - 模版 textarea 此前固定 rows={18}，长模版（如 issue-agent 完整提示词）
//   超过 18 行后 textarea 内部出现垂直滚动条（内层滚动）
// - 修复后：默认折叠为小高度窗口（无滚动条，内容裁剪），提供
//   「展开全部（N 行）/收起」切换；展开时 textarea 高度自适应内容行数
//   完整展示、无内层滚动条，滚动交给页面最外层（与 issue #52 交互原则一致）
//
// 修复前状态（本测试应当失败）：
// - textarea rows 恒为 18，长模版下产生内层垂直滚动条
// - 无折叠/展开切换按钮
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

function click(renderer, button) {
  return TestRenderer.act(async () => {
    button.props.onClick()
  })
}

// ---- 折叠编辑器行为测试 ----

test('长模版展开时 textarea 高度自适应内容行数（无内层垂直滚动）',
  async () => {
    mockApi()
    const { renderer, renderError } = await renderTemplates()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const textareas = findTextarea(renderer)
      assert.equal(textareas.length, 1, '应有且仅有一个模版 textarea')

      // 点击「展开全部」后 rows 应等于内容行数（+1 余量），40 行内容
      // 完整展示，而不是固定 18 行导致内层滚动条
      const expandBtns = findButtons(renderer, '展开全部')
      assert.equal(expandBtns.length, 1, '折叠态应显示「展开全部」按钮')
      await click(renderer, expandBtns[0])
      assert.equal(
        findTextarea(renderer)[0].props.rows,
        LONG_TEMPLATE.split('\n').length + 1,
        '展开态 textarea rows 应自适应完整内容行数',
      )
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  })

test('模版编辑区提供「展开全部」/「收起」折叠切换，默认折叠为小高度',
  async () => {
    mockApi()
    const { renderer, renderError } = await renderTemplates()
    try {
      assert.equal(renderError, null, `渲染抛错：${renderError?.message}`)
      const textareas = findTextarea(renderer)
      assert.equal(textareas.length, 1, '应有且仅有一个模版 textarea')

      // 默认折叠：小高度窗口，无内层滚动条
      assert.equal(textareas[0].props.rows, 6, '默认折叠态 textarea 应为固定小高度')
      assert.match(
        textareas[0].props.className,
        /template-collapsed/,
        '折叠态应带 template-collapsed 样式（overflow hidden，无滚动条）',
      )
      const expandBtns = findButtons(renderer, '展开全部')
      assert.equal(expandBtns.length, 1, '折叠态应显示「展开全部」按钮')

      // 展开：rows 自适应完整内容行数
      await click(renderer, expandBtns[0])
      let ta = findTextarea(renderer)[0]
      assert.equal(ta.props.rows, 41, '展开后 textarea rows 应等于 40 行内容 + 1')
      assert.doesNotMatch(
        ta.props.className,
        /template-collapsed/,
        '展开态不应再带折叠样式',
      )
      assert.equal(findButtons(renderer, '收起').length, 1, '展开态应显示「收起」按钮')

      // 再次收起：恢复小高度，内容不丢失
      await click(renderer, findButtons(renderer, '收起')[0])
      ta = findTextarea(renderer)[0]
      assert.equal(ta.props.rows, 6, '收起后 textarea 应恢复小高度')
      assert.equal(ta.props.value, LONG_TEMPLATE, '折叠切换不应丢失模版内容')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    }
  })

test('折叠态样式声明 overflow hidden（无内层滚动条）', () => {
  const m = stylesSrc.match(/\.template-collapsed\s*\{[^}]*\}/)
  assert.ok(m, 'styles.css 应定义 .template-collapsed 样式')
  assert.match(m[0], /overflow:\s*hidden/, '折叠态应 overflow:hidden，不出现滚动条')
})
