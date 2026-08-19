// 概览页右侧边栏双滚动条修复测试（issue #348）：
// 概览页打开 issue 详情右边栏或流水线右边栏时，页面上同时出现两个
// 竖直滚动条——主页面一个、右边栏一个。要求：右边栏打开时只显示
// 右边栏自身的滚动条（主页面滚动条隐藏）。
//
// 根因：.drawer-overlay 虽以 position: fixed 覆盖全屏，但主页面 body
// 仍可滚动、滚动条仍显示；.drawer 自身 overflow-y: auto 又有滚动条，
// 于是出现「主页滚动条 + 抽屉滚动条」双滚动条。
//
// 修复方案：body:has(.drawer-overlay) { overflow: hidden }——任一右侧
// 边栏打开（存在 .drawer-overlay）即锁定主页面滚动、隐藏主页滚动条，
// 只保留抽屉自身的滚动条；选择器 :has 与项目既有用法一致
// （.add-method:has(input:checked) / .remote-option:has(input:checked)）。
//
// 断言（styles.css 源码级 + 组件渲染级，与
// overview-drawer-actions-sticky.test.mjs 同风格）：
// 1. styles.css 存在 body:has(.drawer-overlay) 规则且 overflow: hidden；
// 2. 锁定必须限定「抽屉打开」（:has(.drawer-overlay)），不能是裸
//    body { overflow: hidden }（会永久锁死页面滚动）；
// 3. .drawer 保留 overflow-y: auto（右边栏自身滚动条保留）；
// 4. .drawer-overlay 为 position: fixed + inset: 0（全屏遮罩，
//    抽屉打开时主页面被遮住、不再需要主页滚动条）；
// 5. IssueDrawer / PipelineDrawer 均渲染 .drawer-overlay（打开时
//    锁定规则生效的前提）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview 系测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
after(() => vite.close())
const { default: IssueDrawer } = await vite.ssrLoadModule('/src/components/IssueDrawer.jsx')
const { default: PipelineDrawer } = await vite.ssrLoadModule('/src/components/PipelineDrawer.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

// ---- styles.css 源码断言 ----

// 提取某选择器首个规则体（feature 特征非空时只返回含该特征的规则）
function ruleBody(css, selector, feature) {
  const re = new RegExp(`${selector}\\s*\\{([^}]*)\\}`, 'g')
  let m
  while ((m = re.exec(css))) {
    if (!feature || m[1].includes(feature)) return m[1]
  }
  assert.ok(false, `styles.css 应存在 ${selector} 规则（特征：${feature || '任意'}）`)
}

test('styles.css：body:has(.drawer-overlay) 锁定主页面滚动（隐藏主页滚动条）', () => {
  const body = ruleBody(styles, 'body:has\\(\\.drawer-overlay\\)', 'overflow')
  assert.match(body, /overflow:\s*hidden/,
               '抽屉打开时主页面应 overflow: hidden（隐藏主页滚动条）')
})

test('styles.css：锁定必须限定抽屉打开，不能是裸 body overflow: hidden', () => {
  // 永久锁死页面滚动的裸规则不允许存在
  assert.ok(!/^\s*body\s*\{\s*overflow\s*:\s*hidden/m.test(styles),
            '不应存在裸 body { overflow: hidden }（会永久锁死页面滚动）')
  // 必须通过 :has(.drawer-overlay) 限定「抽屉打开时」
  assert.ok(/body:has\(\.drawer-overlay\)/.test(styles),
            '锁定规则必须限定 .drawer-overlay 存在（抽屉打开）时才生效')
})

test('styles.css：抽屉自身保留滚动条（.drawer overflow-y: auto）', () => {
  const body = ruleBody(styles, '\\.drawer', 'overflow-y')
  assert.match(body, /overflow-y:\s*auto/,
               '右边栏自身应保留 overflow-y: auto（只显示右边栏滚动条）')
})

test('styles.css：抽屉遮罩全屏覆盖（position: fixed + inset: 0）', () => {
  const body = ruleBody(styles, '\\.drawer-overlay', 'position')
  assert.match(body, /position:\s*fixed/, '抽屉遮罩应 fixed 全屏覆盖')
  assert.match(body, /inset:\s*0/, '抽屉遮罩应 inset: 0 铺满视口')
})

// ---- 组件渲染断言 ----

// IssueDrawer 直接渲染（api.get 走 mock：detail 返回空 notes 等）
async function renderIssueDrawer(issue, onClose = () => {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.endsWith('/detail')) {
      return { notes: [], engine: 'claude', task_id: null,
               task_duration_seconds: null, task_status: null }
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(IssueDrawer, {
        issue, repoName: 'botler',
        onClose, onIssueClosed: () => {}, onLabelsUpdated: () => {},
        onAssigneeUpdated: () => {}, onPrioritized: () => {},
      }))
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

const FULL_ISSUE = {
  iid: 348, title: '概览页面打开issue详情右侧边栏或者流水线右侧边栏时，有两个滚动条',
  state: 'opened',
  updated_at: '2026-08-20 09:00:00',
  created_at: '2026-08-20 08:00:00',
  web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/work_items/348',
  description: '打开右侧边栏的时候只显示右侧边栏页面的滚动条',
  author: { name: 'Chen', username: 'chenkaidi' },
  labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
  milestone: null,
  assignees: [{ name: 'Agent', username: 'agent' }],
  user_notes_count: 0,
  project_id: 123,
}

test('IssueDrawer 渲染 .drawer-overlay（打开时锁定规则生效）', async () => {
  const { renderer, renderError } = await renderIssueDrawer(FULL_ISSUE)
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const overlays = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('drawer-overlay'))
    assert.ok(overlays.length >= 1,
              'IssueDrawer 应渲染 .drawer-overlay（抽屉打开时 body 锁定规则生效）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// PipelineDrawer 直接渲染
function renderPipelineDrawer(entry) {
  return TestRenderer.create(React.createElement(PipelineDrawer, {
    entry, onClose: () => {},
  }))
}

const PIPELINE_ENTRY = {
  repo_id: 123,
  repo_name: 'botler',
  enabled: true,
  pipeline: {
    id: 348,
    status: 'success',
    ref: 'main',
    sha: 'abcdef1234567890',
    created_at: '2026-08-20 09:00:00',
    updated_at: '2026-08-20 09:05:00',
    finished_at: '2026-08-20 09:06:00',
    duration: 300,
    web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/pipelines/348',
  },
  stages: [{ name: 'build', status: 'success', jobs: [] }],
  commit_time: '2026-08-20 09:00:00',
}

test('PipelineDrawer 渲染 .drawer-overlay（打开时锁定规则生效）', async () => {
  const renderer = renderPipelineDrawer(PIPELINE_ENTRY)
  try {
    const overlays = renderer.root.findAll(
      (n) => String(n.props.className || '').includes('drawer-overlay'))
    assert.ok(overlays.length >= 1,
              'PipelineDrawer 应渲染 .drawer-overlay（抽屉打开时 body 锁定规则生效）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})
