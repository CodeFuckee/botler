// 概览页 DeepSeek 账户余额测试（issue #138）：
// 设置里配置了 deepseek api 时，概览页展示 DeepSeek 账户余额卡片——
// 数据由后端代调 GET https://api.deepseek.com/user/balance 返回
// （GET /api/settings/deepseek-balance），API Key 明文不流转到前端。
//
// 断言：
// 1. Overview 页请求 /api/settings/deepseek-balance 并低频轮询（60 秒）；
// 2. configured=true 且有余额信息 → 渲染卡片（币种/总余额/赠送/充值/
//    账户可用/更新时间/刷新按钮）；
// 3. configured=false（未配置 deepseek api）→ 整卡不渲染，页面保持简洁；
// 4. configured=true 但余额接口报错 → 显示错误提示 + 刷新按钮；
// 5. 余额接口请求失败（网络异常）→ 不渲染卡片、页面不崩溃；
// 6. 点击「刷新」按钮 → 重新请求余额接口；
// 7. styles.css 提供余额卡片样式类。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/pages/Overview.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-*.test.mjs 一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 数据流源码断言 ----

test('概览页请求余额接口并低频轮询（60 秒）', () => {
  assert.match(overview, /\/api\/settings\/deepseek-balance/,
               '应请求 GET /api/settings/deepseek-balance')
  assert.match(overview, /DEEPSEEK_BALANCE_POLL_MS\s*=\s*60000/,
               '余额轮询间隔应为 60 秒')
  assert.match(overview, /setInterval\(loadDeepSeekBalance/,
               '余额应独立定时轮询')
})

test('未配置（configured=false）时卡片不渲染，已配置才展示', () => {
  assert.match(overview, /dsBalance\s*&&\s*dsBalance\.configured/,
               '应按 configured 控制卡片渲染')
  assert.match(overview, /DeepSeek 账户余额/, '卡片应有标题')
  assert.match(overview, /name="refresh" \/> 刷新/, '卡片应提供手动刷新按钮（Lucide RefreshCw）')
})

test('styles.css 提供余额卡片样式', () => {
  assert.match(styles, /\.deepseek-balance-section\s*\{/, '应有余额卡片容器样式')
  assert.match(styles, /\.deepseek-balance-item\s*\{/, '应有余额条目样式')
  assert.match(styles, /\.deepseek-balance-total\s*\{/, '应有总余额数字样式')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock）----

// balancePayload 覆盖余额接口返回；balanceError = true 时余额接口直接失败
async function renderOverview({
  balancePayload = { configured: false, balance: null, error: null },
  balanceFail = false,
} = {}) {
  const getCalls = []
  let balanceHits = 0
  mock.method(api, 'get', async (pathname) => {
    getCalls.push(pathname)
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
    if (pathname === '/api/inspirations/overview') return { repos: [] }
    if (pathname === '/api/settings') {
      return { gitlab: { owner_token_masked: 'test-****' } }
    }
    if (pathname === '/api/settings/deepseek-balance') {
      balanceHits += 1
      if (balanceFail) throw new Error('余额接口网络错误')
      return balancePayload
    }
    throw new Error('unexpected ' + pathname)
  })
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      // 等待首轮各数据接口的 promise flush
      await new Promise((resolve) => setTimeout(resolve, 30))
    } catch (e) {
      renderError = e
    }
  })
  return {
    renderer, renderError, getCalls,
    balanceHits: () => balanceHits,
    unmount: async () => {
      if (renderer) await TestRenderer.act(() => renderer.unmount())
      mock.restoreAll()
    },
  }
}

// 渲染整棵树为扁平文本（深度优先，与视觉顺序一致）
function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}

function findByClass(renderer, cls) {
  return renderer.root.findAll((n) => String(n.props.className || '').includes(cls))
}

// 概览页余额卡片的完整余额返回（DeepSeek user/balance 响应结构）
const BALANCE_PAYLOAD = {
  configured: true,
  balance: {
    is_available: true,
    balance_infos: [{
      currency: 'CNY',
      total_balance: '110.00',
      granted_balance: '10.00',
      topped_up_balance: '100.00',
    }],
    fetched_at: '2026-08-17 10:00:00',
  },
  error: null,
}

// ---- 渲染级断言 ----

test('渲染：已配置 deepseek api 时展示余额卡片（币种/总余额/赠送/充值/可用）', async () => {
  const r = await renderOverview({ balancePayload: BALANCE_PAYLOAD })
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    assert.ok(text.includes('DeepSeek 账户余额'), '应渲染卡片标题')
    assert.ok(text.includes('CNY'), '应渲染币种')
    assert.ok(text.includes('110.00'), '应渲染总余额')
    assert.ok(text.includes('赠送'), '应渲染赠送余额标签')
    assert.ok(text.includes('10.00'), '应渲染赠送余额数值')
    assert.ok(text.includes('充值'), '应渲染充值余额标签')
    assert.ok(text.includes('100.00'), '应渲染充值余额数值')
    assert.ok(text.includes('账户可用'), '应渲染账户可用状态')
    assert.ok(text.includes('更新于'), '应渲染更新时间')
    assert.ok(text.includes('lucide-refresh-cw'), '应渲染刷新按钮（Lucide RefreshCw）')
    // 余额接口被调用过
    assert.ok(r.getCalls.includes('/api/settings/deepseek-balance'),
              '应轮询余额接口')
  } finally {
    await r.unmount()
  }
})

test('渲染：未配置 deepseek api（configured=false）时不渲染余额卡片', async () => {
  const r = await renderOverview()
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(!text.includes('DeepSeek 账户余额'), '未配置时不应渲染余额卡片')
    assert.ok(!text.includes('lucide-refresh-cw'), '未配置时不应渲染刷新按钮')
    // 页面其他板块不受影响
    assert.ok(text.includes('开放 Issue'), '开放 Issue 板块仍应渲染')
    assert.ok(text.includes('CI/CD 流水线'), 'CI/CD 流水线板块仍应渲染')
  } finally {
    await r.unmount()
  }
})

test('渲染：已配置但余额接口报错 → 显示错误提示与刷新按钮，不崩溃', async () => {
  const r = await renderOverview({
    balancePayload: {
      configured: true,
      balance: null,
      error: 'DeepSeek 余额查询失败: HTTP 401 Authentication Fails',
    },
  })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('DeepSeek 账户余额'), '卡片标题仍应渲染')
    assert.ok(text.includes('HTTP 401'), '应显示余额接口错误信息')
    assert.ok(text.includes('lucide-refresh-cw'), '错误态下仍应提供刷新按钮')
  } finally {
    await r.unmount()
  }
})

test('渲染：余额接口请求失败（网络异常）→ 不渲染卡片、页面不崩溃', async () => {
  const r = await renderOverview({ balanceFail: true })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(!text.includes('DeepSeek 账户余额'), '请求失败且无缓存数据时不渲染卡片')
    assert.ok(text.includes('开放 Issue'), '页面其他板块不受影响')
  } finally {
    await r.unmount()
  }
})

// ---- 交互级断言 ----

test('交互：点击「刷新」按钮重新请求余额接口', async () => {
  const r = await renderOverview({ balancePayload: BALANCE_PAYLOAD })
  try {
    const before = r.balanceHits()
    const section = findByClass(r.renderer, 'deepseek-balance-section')
    assert.ok(section.length === 1, '应存在余额卡片容器')
    const buttons = section[0].findAll(
      (n) => n.type === 'button' && String(n.props.children || '').includes('刷新'))
    assert.ok(buttons.length === 1, '应存在刷新按钮')
    await TestRenderer.act(async () => {
      buttons[0].props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    assert.equal(r.balanceHits(), before + 1, '点击刷新后余额接口应被再次请求')
  } finally {
    await r.unmount()
  }
})

test('交互：余额信息缺失时显示空状态（balance_infos 为空）', async () => {
  const r = await renderOverview({
    balancePayload: {
      configured: true,
      balance: { is_available: false, balance_infos: [], fetched_at: null },
      error: null,
    },
  })
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('DeepSeek 账户余额'), '卡片标题仍应渲染')
    assert.ok(text.includes('账户不可用'), 'is_available=false 应显示不可用')
    assert.ok(text.includes('暂无余额信息'), '无余额条目应显示空状态')
  } finally {
    await r.unmount()
  }
})
