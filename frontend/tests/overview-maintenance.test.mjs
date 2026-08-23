// 概览页维护模式横幅测试（issue #241）：
//
// 需求：维护模式开启时概览页顶部显示醒目「维护中」横幅（与导航栏徽章
// 同数据源），提示新任务暂停派发、运行中任务继续执行；关闭后横幅消失。
//
// 断言：
// 1. /api/settings 返回 worker.maintenance_mode=true → 渲染横幅
//    （.maintenance-banner，含「维护中/暂停派发」文案，role=status）；
// 2. 返回 false → 不渲染横幅；
// 3. 接口失败 → 不渲染不崩溃。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const overviewSrc = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const ISSUES_PAYLOAD = { repos: [], errors: [], total: 0 }

/** 挂载 Overview：api.get 按路径分流，/api/settings 返回可配置的维护模式 */
async function renderOverview({ maintenanceMode = false, settingsOk = true } = {}) {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return ISSUES_PAYLOAD
    if (pathname === '/api/settings') {
      if (!settingsOk) throw new Error('settings 500')
      return {
        gitlab: { owner_token_masked: 'glpat-***' },
        worker: { maintenance_mode: maintenanceMode },
      }
    }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(Overview))
    await new Promise((resolve) => setTimeout(resolve, 40))
  })
  return renderer
}

function findBanner(renderer) {
  const nodes = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('maintenance-banner'))
  return nodes.length > 0 ? nodes[0] : null
}

function collectText(node) {
  let out = ''
  for (const c of node.children || []) {
    if (typeof c === 'string') out += c
    else out += collectText(c)
  }
  return out
}

test('维护模式开启：概览页顶部渲染「维护中」横幅', async () => {
  const renderer = await renderOverview({ maintenanceMode: true })
  try {
    const banner = findBanner(renderer)
    assert.ok(banner, '维护模式开启时应渲染横幅')
    assert.equal(banner.props.role, 'status', '横幅应带 role=status 供读屏播报')
    const text = collectText(banner)
    assert.ok(text.includes(zhCN['overview.maintenanceBanner'].slice(0, 8)),
      `横幅应显示维护中提示文案：${text}`)
    assert.match(text, /暂停派发/, '文案应说明新任务暂停派发')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('维护模式关闭：不渲染横幅', async () => {
  const renderer = await renderOverview({ maintenanceMode: false })
  try {
    assert.equal(findBanner(renderer), null, '维护模式关闭时不应渲染横幅')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('settings 接口失败：不渲染横幅不崩溃', async () => {
  const renderer = await renderOverview({ settingsOk: false })
  try {
    assert.equal(findBanner(renderer), null, '接口失败时应静默降级不渲染横幅')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
  }
})

test('源码：Overview.jsx 渲染横幅且样式存在', () => {
  assert.match(overviewSrc, /maintenance-banner/, '应渲染维护中横幅容器')
  assert.match(overviewSrc, /overview\.maintenanceBanner/, '横幅文案应走 i18n')
  assert.ok(styles.includes('.maintenance-banner {'), '应定义 .maintenance-banner 样式')
})
