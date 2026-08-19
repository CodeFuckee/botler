// usePolling hook 测试（issue #200）：页面可见性感知的统一轮询管理——
// 页面隐藏时暂停全部轮询（后台标签页 0 请求），恢复可见立即拉取一次再
// 恢复定时器；enabled / immediate / interval 边界与卸载清理。
//
// 测试层次：
// 1. 生命周期：挂载立即执行、按 interval 定时轮询、卸载清理定时器与监听；
// 2. 可见性（核心验收）：隐藏暂停（0 请求）、恢复可见立即拉一次 + 恢复轮询、
//    初始即隐藏不执行不轮询；
// 3. 条件轮询：enabled=false 不执行不轮询、翻转为 true 后立即执行并轮询；
//    immediate=false 不立即执行但正常轮询；fn 引用变化立即执行（过滤条件
//    变化即时生效）；interval 无效不启动定时器；
// 4. SSR/测试环境无 document 时保持既有行为（不崩溃、正常轮询）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import TestRenderer from 'react-test-renderer'
import { usePolling, isDocumentVisible } from '../src/hooks/usePolling.js'
import { installFakeDocument } from './helpers/fake-document.mjs'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// 测试组件：把 props 透传给 usePolling，不渲染任何节点
function Harness({ fn, interval, options }) {
  usePolling(fn, interval, options)
  return null
}

// ---- 生命周期 ----

test('usePolling：挂载后立即执行一次 fn（immediate 默认跟随 enabled）', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: 1000 })))
    assert.equal(calls, 1, '挂载后应立即执行一次')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：按 interval 定时执行', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: 20 })))
    assert.equal(calls, 1, '挂载后应立即执行一次')
    await sleep(70)
    assert.ok(calls >= 3, `20ms 间隔 70ms 内应至少轮询 2 次（实际 ${calls} 次）`)
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：卸载后清理定时器并移除可见性监听', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    let renderer
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Harness, { fn, interval: 20 }))
    })
    assert.equal(doc.listenerCount(), 1, '挂载后应注册 visibilitychange 监听')
    await TestRenderer.act(async () => renderer.unmount())
    assert.equal(doc.listenerCount(), 0, '卸载后应移除可见性监听')
    const afterUnmount = calls
    await sleep(60)
    assert.equal(calls, afterUnmount, '卸载后不应再轮询')
  } finally {
    doc.restore()
  }
})

// ---- 可见性（issue #200 核心验收）----

test('usePolling：页面隐藏暂停轮询（0 请求），恢复可见立即拉一次并恢复轮询', async () => {
  const doc = installFakeDocument('visible')
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: 20 })))
    await sleep(60)
    const beforeHide = calls
    assert.ok(beforeHide >= 3, '页面可见时应持续轮询')

    // 切后台标签页：暂停全部轮询 → 0 请求
    await TestRenderer.act(async () => { doc.setVisibility('hidden') })
    const atHide = calls
    await sleep(90)
    assert.equal(calls, atHide, '页面隐藏后不应再有任何请求（0 请求）')

    // 切回：立即拉一次，再恢复定时器
    await TestRenderer.act(async () => { doc.setVisibility('visible') })
    assert.equal(calls, atHide + 1, '恢复可见应立即拉取一次')
    await sleep(60)
    assert.ok(calls >= atHide + 3, '恢复可见后应按 interval 恢复轮询')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：初始即隐藏不执行不轮询，恢复可见后立即拉一次并轮询', async () => {
  const doc = installFakeDocument('hidden')
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: 20 })))
    assert.equal(calls, 0, '初始隐藏（后台打开）不应立即执行')
    await sleep(60)
    assert.equal(calls, 0, '初始隐藏不应启动轮询')

    await TestRenderer.act(async () => { doc.setVisibility('visible') })
    assert.equal(calls, 1, '恢复可见应立即拉取一次')
    await sleep(60)
    assert.ok(calls >= 3, '恢复可见后应按 interval 轮询')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

// ---- 条件轮询与边界 ----

test('usePolling：enabled=false 不执行不轮询；翻转为 true 后立即执行并轮询', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    let renderer
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(Harness, { fn, interval: 20, options: { enabled: false } }))
    })
    assert.equal(calls, 0, 'enabled=false 不应立即执行')
    await sleep(60)
    assert.equal(calls, 0, 'enabled=false 不应轮询')

    await TestRenderer.act(async () => {
      renderer.update(
        React.createElement(Harness, { fn, interval: 20, options: { enabled: true } }))
    })
    assert.equal(calls, 1, 'enabled 翻转为 true 后应立即执行一次')
    await sleep(60)
    assert.ok(calls >= 3, 'enabled=true 后应按 interval 轮询')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：immediate=false 不立即执行，但定时轮询正常', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(
        React.createElement(Harness, { fn, interval: 20, options: { immediate: false } })))
    assert.equal(calls, 0, 'immediate=false 不应立即执行')
    await sleep(60)
    assert.ok(calls >= 2, 'immediate=false 时仍应按 interval 轮询')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：fn 引用变化立即执行一次并重启定时器（过滤条件变化即时生效）', async () => {
  const doc = installFakeDocument()
  try {
    const calls = []
    const fnA = () => { calls.push('A') }
    let renderer
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Harness, { fn: fnA, interval: 1000 }))
    })
    assert.deepEqual(calls, ['A'], '挂载后应立即执行一次')

    const fnB = () => { calls.push('B') }
    await TestRenderer.act(async () => {
      renderer.update(React.createElement(Harness, { fn: fnB, interval: 1000 }))
    })
    assert.deepEqual(calls, ['A', 'B'], 'fn 引用变化应立刻用新函数拉一次')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

test('usePolling：interval 为 null 时不启动定时器（仍执行一次 immediate）', async () => {
  const doc = installFakeDocument()
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: null })))
    assert.equal(calls, 1, 'interval=null 时仍应立即执行一次')
    await sleep(60)
    assert.equal(calls, 1, 'interval=null 不应启动定时器')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    doc.restore()
  }
})

// ---- SSR / 无 document 环境 ----

test('usePolling：无 document（SSR/测试环境）不崩溃且正常轮询', async () => {
  const hadDoc = 'document' in globalThis
  const orig = globalThis.document
  delete globalThis.document
  try {
    let calls = 0
    const fn = () => { calls += 1 }
    const renderer = await TestRenderer.act(() =>
      TestRenderer.create(React.createElement(Harness, { fn, interval: 20 })))
    assert.equal(calls, 1, '无 document 时仍应立即执行一次')
    await sleep(60)
    assert.ok(calls >= 3, '无 document 时仍应正常轮询')
    await TestRenderer.act(async () => renderer.unmount())
  } finally {
    if (hadDoc) globalThis.document = orig
    else delete globalThis.document
  }
})

test('isDocumentVisible：无 document 视为可见；hidden 为不可见', () => {
  const doc = installFakeDocument('hidden')
  try {
    assert.equal(isDocumentVisible(), false, 'hidden 应判定为不可见')
    doc.setVisibility('visible')
    assert.equal(isDocumentVisible(), true, 'visible 应判定为可见')
  } finally {
    doc.restore()
  }
  const hadDoc = 'document' in globalThis
  const orig = globalThis.document
  delete globalThis.document
  try {
    assert.equal(isDocumentVisible(), true, 'SSR/测试无 document 应视为可见')
  } finally {
    if (hadDoc) globalThis.document = orig
    else delete globalThis.document
  }
})
