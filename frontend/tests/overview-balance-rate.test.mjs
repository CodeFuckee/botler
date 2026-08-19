// 概览页 DeepSeek 余额卡片「每小时余额变化速率」测试（issue #304）：
// 如果有账户余额信息时，概览增加账户余额减少（变化）速度显示，按小时
// 为单位——前端在每次余额轮询 / 手动刷新成功时把观测样本追加到
// localStorage（键 botler.overview.dsBalanceHistory），按最早/最近观测
// 窗口计算每小时平均变化速率，展示在余额卡片每个币种条目上。
//
// 断言：
// 1. balanceRate 纯函数（frontend/src/balanceRate.js）：样本追加/清洗、
//    历史读取兜底、速率计算（减少/增加/无变化/样本不足/窗口过短/多币种/
//    过期样本/容量上限）、观测窗口格式化；
// 2. Overview 页集成：轮询成功时追加样本 + 计算速率 + 渲染速率文案；
// 3. 渲染级：预置历史样本后渲染出「每小时减少 X.XX」（减少/增加/无变化/
//    暂无数据四种文案）；
// 4. 文案经 i18n（zh-CN 为稳定来源），styles.css 提供速率样式类。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'
import {
  BALANCE_RATE_STORAGE_KEY,
  MAX_BALANCE_SAMPLES,
  MAX_SAMPLE_AGE_MS,
  MIN_RATE_WINDOW_MS,
  MS_PER_HOUR,
  appendBalanceSample,
  computeBalanceRate,
  fmtRateWindow,
  loadBalanceHistory,
  saveBalanceHistory,
} from '../src/balanceRate.js'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const enUS = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/en-US.json'), 'utf8'))
const overview = readFileSync(path.join(ROOT, 'src/hooks/useOverviewData.js'), 'utf8')
  + '\n' + readFileSync(path.join(ROOT, 'src/components/overview/DeepSeekBalanceCard.jsx'), 'utf8')
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

// ---- balanceRate 纯函数单元测试 ----

const T0 = 1_752_000_000_000 // 固定基准时间戳（2026-08 附近）
const ONE_HOUR = MS_PER_HOUR

test('appendBalanceSample：追加一条观测样本，仅保留币种与数值化总余额', () => {
  const history = []
  const next = appendBalanceSample(history, T0, [
    { currency: 'CNY', total_balance: '110.00', granted_balance: '10.00' },
    { currency: 'USD', total_balance: 5.5 },
    { currency: '  ', total_balance: '1' },      // 空币种剔除
    { currency: 'EUR', total_balance: null },     // 余额缺失剔除
    { currency: 'JPY', total_balance: 'abc' },    // 非数值剔除
    null,                                          // 异常元素剔除
  ])
  assert.equal(next.length, 1)
  assert.equal(next[0].ts, T0)
  assert.deepEqual(next[0].infos, [
    { currency: 'CNY', total_balance: 110 },
    { currency: 'USD', total_balance: 5.5 },
  ])
})

test('appendBalanceSample：非法入参兜底（非数组历史/非法时间戳/非数组 infos）', () => {
  const next = appendBalanceSample(null, T0, { not: 'array' })
  assert.equal(next.length, 1)
  assert.equal(next[0].ts, T0)
  assert.deepEqual(next[0].infos, [])
  // 非法时间戳回退当前时间（仍可追加，不抛错）
  const fallback = appendBalanceSample([], 'not-a-number', [])
  assert.equal(fallback.length, 1)
  assert.ok(Number.isFinite(fallback[0].ts))
})

test('appendBalanceSample：历史超容量上限时仅保留最近样本', () => {
  let history = []
  for (let i = 0; i < MAX_BALANCE_SAMPLES + 5; i += 1) {
    history = appendBalanceSample(history, T0 + i * 60000, [
      { currency: 'CNY', total_balance: 100 - i },
    ])
  }
  assert.equal(history.length, MAX_BALANCE_SAMPLES)
  assert.equal(history[history.length - 1].ts, T0 + (MAX_BALANCE_SAMPLES + 4) * 60000)
})

test('loadBalanceHistory：无存储/损坏 JSON/非数组一律回退空数组', () => {
  assert.deepEqual(loadBalanceHistory(null), [])
  assert.deepEqual(loadBalanceHistory({}), [])
  assert.deepEqual(loadBalanceHistory({ getItem: () => '{bad json' }), [])
  assert.deepEqual(loadBalanceHistory({ getItem: () => '{"a":1}' }), [])
  assert.deepEqual(loadBalanceHistory({ getItem: () => null }), [])
})

test('loadBalanceHistory：清洗非法样本（缺 ts/infos/非数值 ts/缺币种）', () => {
  const storage = {
    getItem: () => JSON.stringify([
      { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] },
      { infos: [{ currency: 'CNY', total_balance: 90 }] },   // 缺 ts
      { ts: 'bad', infos: [{ currency: 'CNY', total_balance: 80 }] },
      { ts: T0, infos: 'bad' },
      { ts: T0, infos: [{ currency: '', total_balance: 70 }] },
    ]),
  }
  const out = loadBalanceHistory(storage, T0)
  assert.equal(out.length, 1)
  assert.equal(out[0].infos[0].total_balance, 100)
})

test('loadBalanceHistory：丢弃过期样本（超过 7 天）并截断容量', () => {
  const storage = {
    getItem: () => JSON.stringify([
      { ts: T0 - MAX_SAMPLE_AGE_MS - 1000, infos: [{ currency: 'CNY', total_balance: 1 }] },
      { ts: T0, infos: [{ currency: 'CNY', total_balance: 2 }] },
    ]),
  }
  const out = loadBalanceHistory(storage, T0)
  assert.equal(out.length, 1)
  assert.equal(out[0].ts, T0)
})

test('saveBalanceHistory：写入合法 JSON；存储不可用/抛异常静默忽略', () => {
  const store = new Map()
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  }
  saveBalanceHistory(storage, [{ ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] }])
  const parsed = JSON.parse(store.get(BALANCE_RATE_STORAGE_KEY))
  assert.equal(parsed.length, 1)
  assert.equal(parsed[0].infos[0].total_balance, 100)
  // 存储不可用 / setItem 抛异常：不抛错
  saveBalanceHistory(null, [])
  const boom = { setItem: () => { throw new Error('quota') } }
  saveBalanceHistory(boom, [{ ts: T0, infos: [] }])
})

test('computeBalanceRate：余额减少 → 每小时减少速率（负数）', () => {
  const history = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 110 }] },
    { ts: T0 + 2 * ONE_HOUR, infos: [{ currency: 'CNY', total_balance: 100 }] },
  ]
  const rate = computeBalanceRate(history, T0 + 2 * ONE_HOUR)
  assert.ok(rate.CNY)
  assert.equal(rate.CNY.ratePerHour, -5)
  assert.equal(rate.CNY.windowMs, 2 * ONE_HOUR)
})

test('computeBalanceRate：余额增加 → 每小时增加速率（正数，多为充值/赠送到账）', () => {
  const history = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] },
    { ts: T0 + 2 * ONE_HOUR, infos: [{ currency: 'CNY', total_balance: 110 }] },
  ]
  const rate = computeBalanceRate(history, T0 + 2 * ONE_HOUR)
  assert.equal(rate.CNY.ratePerHour, 5)
})

test('computeBalanceRate：余额无变化 → 速率 0', () => {
  const history = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] },
    { ts: T0 + 2 * ONE_HOUR, infos: [{ currency: 'CNY', total_balance: 100 }] },
  ]
  assert.equal(computeBalanceRate(history, T0 + 2 * ONE_HOUR).CNY.ratePerHour, 0)
})

test('computeBalanceRate：样本不足（空/单样本/币种仅出现在最新样本）不计速率', () => {
  assert.deepEqual(computeBalanceRate([], T0), {})
  assert.deepEqual(
    computeBalanceRate([{ ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] }], T0),
    {})
  const onlyLatest = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] },
    { ts: T0 + ONE_HOUR, infos: [{ currency: 'CNY', total_balance: 90 }, { currency: 'USD', total_balance: 5 }] },
  ]
  const rate = computeBalanceRate(onlyLatest, T0 + ONE_HOUR)
  assert.ok(rate.CNY, 'CNY 跨两个样本应计算')
  assert.equal(rate.USD, undefined, 'USD 仅出现在最新样本，无历史基线不计速率')
})

test('computeBalanceRate：观测窗口过短（< 1 分钟）不计速率（避免噪声）', () => {
  const history = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }] },
    { ts: T0 + 30_000, infos: [{ currency: 'CNY', total_balance: 99.9 }] },
  ]
  assert.deepEqual(computeBalanceRate(history, T0 + 30_000), {})
  assert.ok(MIN_RATE_WINDOW_MS === 60_000, '最短观测窗口应为 1 分钟')
})

test('computeBalanceRate：多币种独立计算', () => {
  const history = [
    { ts: T0, infos: [{ currency: 'CNY', total_balance: 100 }, { currency: 'USD', total_balance: 10 }] },
    { ts: T0 + ONE_HOUR, infos: [{ currency: 'CNY', total_balance: 90 }, { currency: 'USD', total_balance: 9.5 }] },
  ]
  const rate = computeBalanceRate(history, T0 + ONE_HOUR)
  assert.equal(rate.CNY.ratePerHour, -10)
  assert.equal(rate.USD.ratePerHour, -0.5)
})

test('fmtRateWindow：按观测窗口时长格式化为小时/分钟', () => {
  assert.deepEqual(fmtRateWindow(2 * ONE_HOUR), { kind: 'hour', value: 2 })
  assert.deepEqual(fmtRateWindow(1.5 * ONE_HOUR), { kind: 'hour', value: 1.5 })
  assert.deepEqual(fmtRateWindow(30 * 60_000), { kind: 'minute', value: 30 })
  // 不足 1 分钟按 1 分钟兜底展示
  assert.deepEqual(fmtRateWindow(45_000), { kind: 'minute', value: 1 })
  assert.deepEqual(fmtRateWindow(0), { kind: 'minute', value: 1 })
})

// ---- 数据流源码断言 ----

test('概览页集成余额速率：轮询成功追加样本并计算每小时速率', () => {
  assert.match(overview, /from '\.\.\/balanceRate\.js'/,
               '应引入 balanceRate 模块')
  assert.match(overview, /appendBalanceSample\(/, '应追加余额观测样本')
  assert.match(overview, /computeBalanceRate\(/, '应计算每小时变化速率')
  assert.match(overview, /loadBalanceHistory\(/, '应读取历史样本（存储键由 balanceRate 模块封装）')
  assert.match(overview, /saveBalanceHistory\(/, '应保存历史样本')
  assert.match(overview, /tr\('overview\.rateDecrease'/, '减少速率文案应经 t() 国际化')
  assert.match(overview, /tr\('overview\.rateIncrease'/, '增加速率文案应经 t() 国际化')
  assert.match(overview, /tr\('overview\.rateStable'/, '无变化文案应经 t() 国际化')
  assert.match(overview, /tr\('overview\.rateNone'/, '暂无数据文案应经 t() 国际化')
})

test('i18n：速率文案中英文键齐全，中文为稳定来源', () => {
  assert.equal(zhCN['overview.rateDecrease'], '每小时减少 {amount}', '中文减少文案应保留')
  assert.equal(zhCN['overview.rateIncrease'], '每小时增加 {amount}', '中文增加文案应保留')
  assert.equal(zhCN['overview.rateStable'], '每小时无变化', '中文无变化文案应保留')
  assert.equal(zhCN['overview.rateNone'], '暂无速率数据', '中文暂无数据文案应保留')
  assert.ok(zhCN['overview.rateHint'], '应有观测窗口说明文案')
  assert.ok(zhCN['overview.rateWindowHour'], '应有小时单位文案')
  assert.ok(zhCN['overview.rateWindowMinute'], '应有分钟单位文案')
  // 英文键齐全（值非空字符串）
  for (const k of ['overview.rateDecrease', 'overview.rateIncrease',
                   'overview.rateStable', 'overview.rateNone',
                   'overview.rateHint', 'overview.rateWindowHour',
                   'overview.rateWindowMinute']) {
    assert.ok(typeof enUS[k] === 'string' && enUS[k].length > 0, `英文键 ${k} 应已翻译`)
  }
})

test('styles.css 提供余额速率样式类', () => {
  assert.match(styles, /\.deepseek-balance-rate\s*\{/, '应有余额速率样式类')
})

// ---- 组件渲染（api.get 通过 vite 模块实例 mock + localStorage 预置历史）----

// 概览页余额卡片的完整余额返回（DeepSeek user/balance 响应结构）
const BALANCE_PAYLOAD = {
  configured: true,
  balance: {
    is_available: true,
    balance_infos: [{
      currency: 'CNY',
      total_balance: '100.00',
      granted_balance: '10.00',
      topped_up_balance: '90.00',
    }],
    fetched_at: '2026-08-19 10:00:00',
  },
  error: null,
}

// 内存版 localStorage 桩：可预置余额历史样本（issue #304 速率数据源）
function makeMemoryStorage(seed = {}) {
  const store = new Map(Object.entries(seed))
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    _store: store,
  }
}

// 预置一条 2 小时前的历史样本（总余额 balance），供速率计算使用
function seedHistory(store, { ts = Date.now() - 2 * MS_PER_HOUR, balance = 110 } = {}) {
  store.setItem(BALANCE_RATE_STORAGE_KEY, JSON.stringify([{
    ts,
    infos: [{ currency: 'CNY', total_balance: balance }],
  }]))
}

async function renderOverviewWithRate({ storage, balancePayload = BALANCE_PAYLOAD } = {}) {
  // storage：localStorage 桩（可预置余额历史样本）；缺省时新建空桩
  const ls = storage || makeMemoryStorage()
  // 让组件代码能访问 localStorage 桩（node 原生环境无 localStorage）
  const prevLS = globalThis.localStorage
  globalThis.localStorage = ls
  try {
    mock.method(api, 'get', async (pathname) => {
      if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
      if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
      if (pathname === '/api/issues/overview') return { repos: [], errors: [] }
      if (pathname === '/api/inspirations/overview') return { repos: [] }
      if (pathname === '/api/settings') {
        return { gitlab: { owner_token_masked: 'test-****' } }
      }
      if (pathname === '/api/settings/deepseek-balance') return balancePayload
      throw new Error('unexpected ' + pathname)
    })
    let renderer = null
    let renderError = null
    await TestRenderer.act(async () => {
      try {
        renderer = TestRenderer.create(React.createElement(Overview))
        await new Promise((resolve) => setTimeout(resolve, 30))
      } catch (e) {
        renderError = e
      }
    })
    return {
      renderer, renderError, storage: ls,
      unmount: async () => {
        if (renderer) await TestRenderer.act(() => renderer.unmount())
        mock.restoreAll()
        if (prevLS === undefined) delete globalThis.localStorage
        else globalThis.localStorage = prevLS
      },
    }
  } catch (e) {
    mock.restoreAll()
    if (prevLS === undefined) delete globalThis.localStorage
    else globalThis.localStorage = prevLS
    throw e
  }
}

function treeText(renderer) {
  return JSON.stringify(renderer.toJSON())
}

test('渲染：预置历史样本且余额减少 → 展示「每小时减少 X.XX」（按小时）', async () => {
  const store = makeMemoryStorage()
  seedHistory(store, { ts: Date.now() - 2 * MS_PER_HOUR, balance: 110 })
  const r = await renderOverviewWithRate({ storage: store })
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    assert.ok(text.includes('DeepSeek 账户余额'), '余额卡片应渲染')
    assert.ok(text.includes('每小时减少'), '应展示每小时减少速率')
    assert.ok(text.includes('5.00'), '速率应为 (110-100)/2h = 5.00/小时')
  } finally {
    await r.unmount()
  }
})

test('渲染：预置历史样本且余额增加（充值/赠送到账）→ 展示「每小时增加 X.XX」', async () => {
  const store = makeMemoryStorage()
  seedHistory(store, { ts: Date.now() - 2 * MS_PER_HOUR, balance: 90 })
  const r = await renderOverviewWithRate({ storage: store })
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    assert.ok(text.includes('每小时增加'), '应展示每小时增加速率')
    assert.ok(text.includes('5.00'), '速率应为 (100-90)/2h = 5.00/小时')
  } finally {
    await r.unmount()
  }
})

test('渲染：预置历史样本且余额无变化 → 展示「每小时无变化」', async () => {
  const store = makeMemoryStorage()
  seedHistory(store, { ts: Date.now() - 2 * MS_PER_HOUR, balance: 100 })
  const r = await renderOverviewWithRate({ storage: store })
  try {
    assert.equal(r.renderError, null, `渲染抛错：${r.renderError?.message || r.renderError}`)
    const text = treeText(r.renderer)
    assert.ok(text.includes('每小时无变化'), '余额不变应展示无变化')
  } finally {
    await r.unmount()
  }
})

test('渲染：无历史样本（首次观测）→ 展示「暂无速率数据」且不崩溃', async () => {
  const r = await renderOverviewWithRate()
  try {
    assert.equal(r.renderError, null, '渲染不应崩溃')
    const text = treeText(r.renderer)
    assert.ok(text.includes('DeepSeek 账户余额'), '余额卡片仍应渲染')
    assert.ok(text.includes('暂无速率数据'), '样本不足时应展示暂无速率数据')
    // 历史样本应已追加（首次观测落库一条）
    const raw = r.storage.getItem(BALANCE_RATE_STORAGE_KEY)
    assert.ok(raw, '首次观测应写入历史样本存储键')
    const stored = JSON.parse(raw)
    assert.ok(Array.isArray(stored) && stored.length >= 1, '首次观测应写入历史样本')
  } finally {
    await r.unmount()
  }
})
