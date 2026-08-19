// 版本更新提示模块测试（issue #233）：页面加载后轮询 /version.json，
// 检测到与基线版本不一致（新版部署完成）→ 提示刷新。
//
// 测试层次：
// 1. detectVersionChange 纯函数：版本变化判定（缺失信息/非法版本保守不打扰）；
// 2. createVersionChecker：首次轮询只记录基线；版本变化触发 onUpdate 且只
//    触发一次；getVersion 失败/返回空静默跳过；start/stop 生命周期。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  VERSION_CHECK_INTERVAL_MS,
  detectVersionChange,
  createVersionChecker,
} from '../src/version-update.js'

test('VERSION_CHECK_INTERVAL_MS 轮询间隔约定（60s）', () => {
  assert.equal(VERSION_CHECK_INTERVAL_MS, 60000)
})

test('detectVersionChange：版本号不同 → true（新版部署）', () => {
  assert.equal(
    detectVersionChange({ version: '1.3.33' }, { version: '1.3.34', buildTime: 't' }),
    true,
  )
})

test('detectVersionChange：版本号相同 → false（含构建时间变化但版本未变）', () => {
  assert.equal(
    detectVersionChange({ version: '1.3.33', buildTime: 'a' }, { version: '1.3.33', buildTime: 'b' }),
    false,
    '构建时间变化但版本号未变不应提示（避免同一版本重复打扰）'
  )
})

test('detectVersionChange：缺失/非法信息保守返回 false', () => {
  assert.equal(detectVersionChange(null, { version: '1.3.34' }), false)
  assert.equal(detectVersionChange({ version: '1.3.33' }, null), false)
  assert.equal(detectVersionChange(undefined, undefined), false)
  assert.equal(detectVersionChange({ version: '' }, { version: '1.3.34' }), false)
  assert.equal(detectVersionChange({ version: '1.3.33' }, { version: '' }), false)
  assert.equal(detectVersionChange({ version: 123 }, { version: '1.3.34' }), false)
  assert.equal(detectVersionChange({}, {}), false)
})

test('createVersionChecker：首次轮询只记录基线，不触发 onUpdate', async () => {
  let updated = null
  const checker = createVersionChecker({
    getVersion: async () => ({ version: '1.3.33', buildTime: 't1' }),
    onUpdate: (info) => { updated = info },
  })
  await checker.check()
  assert.equal(updated, null, '首次成功只记录基线不提示')
  assert.deepEqual(checker.getBaseline(), { version: '1.3.33', buildTime: 't1' })
})

test('createVersionChecker：版本变化触发 onUpdate 并携带新版本信息', async () => {
  let current = { version: '1.3.33', buildTime: 't1' }
  let updated = null
  const checker = createVersionChecker({
    getVersion: async () => current,
    onUpdate: (info) => { updated = info },
  })
  await checker.check() // 记录基线 1.3.33
  current = { version: '1.3.34', buildTime: 't2', commit: 'abc12345' }
  await checker.check()
  assert.deepEqual(updated, { version: '1.3.34', buildTime: 't2', commit: 'abc12345' },
    'onUpdate 应收到新版本完整信息（含 commit，横幅可展示）')
})

test('createVersionChecker：版本变化只触发一次（忽略后不再重复打扰）', async () => {
  let current = { version: '1.0.0' }
  let calls = 0
  const checker = createVersionChecker({
    getVersion: async () => current,
    onUpdate: () => { calls += 1 },
  })
  await checker.check() // 基线 1.0.0
  current = { version: '1.0.1' }
  await checker.check() // 第一次变化 → 触发
  current = { version: '1.0.2' }
  await checker.check() // 再变化 → 不再触发
  current = { version: '1.0.1' }
  await checker.check()
  assert.equal(calls, 1, '版本变化应只提示一次')
})

test('createVersionChecker：getVersion 失败/返回空静默跳过（不抛错不误报）', async () => {
  let updated = null
  const failing = createVersionChecker({
    getVersion: async () => { throw new Error('network') },
    onUpdate: (info) => { updated = info },
  })
  await failing.check() // 不应抛异常
  assert.equal(updated, null)

  const empty = createVersionChecker({
    getVersion: async () => null,
    onUpdate: (info) => { updated = info },
  })
  await empty.check()
  await empty.check()
  assert.equal(updated, null)
  assert.equal(empty.getBaseline(), null, '无有效版本信息不应记录基线')
})

test('createVersionChecker：start 启动轮询（立即检查 + 定时），stop 停止', async () => {
  let current = { version: '1.0.0' }
  let calls = 0
  const checker = createVersionChecker({
    getVersion: async () => current,
    onUpdate: () => { calls += 1 },
    intervalMs: 5,
  })
  checker.start()
  checker.start() // 幂等：重复 start 不重复开定时器
  // 等待轮询：基线 + 版本变化触发
  current = { version: '1.0.1' }
  await new Promise((resolve) => setTimeout(resolve, 40))
  assert.equal(calls, 1, 'start 后定时轮询应检测到版本变化')
  checker.stop()
  const callsAfterStop = calls
  await new Promise((resolve) => setTimeout(resolve, 30))
  assert.equal(calls, callsAfterStop, 'stop 后不应再轮询')
})

import { installFakeDocument } from './helpers/fake-document.mjs'

// ---- 页面可见性（issue #200）----
// 页面隐藏（后台标签页）时暂停版本检查轮询（0 请求），恢复可见立即检查
// 一次再恢复定时器；初始即隐藏不启动轮询。版本变化只提示一次（notified
// 置位，既有语义），故用 getVersion 调用次数观测「检查」行为本身。

test('createVersionChecker：页面隐藏暂停轮询，恢复可见立即检查并恢复（issue #200）', async () => {
  const doc = installFakeDocument('visible')
  let checker = null
  try {
    let current = { version: '1.0.0' }
    let getCalls = 0
    checker = createVersionChecker({
      getVersion: async () => { getCalls += 1; return current },
      onUpdate: () => {},
      intervalMs: 5,
    })
    checker.start() // 可见：立即检查一次（记录基线）+ 定时轮询
    await new Promise((resolve) => setTimeout(resolve, 20))
    const callsAtHide = getCalls
    assert.ok(callsAtHide >= 2, '页面可见时定时轮询应持续检查版本')

    // 切后台：暂停轮询 → 0 请求
    doc.setVisibility('hidden')
    await new Promise((resolve) => setTimeout(resolve, 40))
    assert.equal(getCalls, callsAtHide, '页面隐藏后不应再检查版本（0 请求）')

    // 切回：立即检查一次（同步断言，不等待——等待期间定时器已恢复会继续 tick）
    doc.setVisibility('visible')
    assert.equal(getCalls, callsAtHide + 1, '恢复可见应立即检查一次')
    const afterVisible = getCalls
    await new Promise((resolve) => setTimeout(resolve, 30))
    assert.ok(getCalls >= afterVisible + 3, '恢复可见后应恢复定时轮询')
  } finally {
    if (checker) checker.stop()
    doc.restore()
  }
})

test('createVersionChecker：初始即隐藏不轮询，恢复可见后开始检查（issue #200）', async () => {
  const doc = installFakeDocument('hidden')
  let checker = null
  try {
    let current = { version: '1.0.0' }
    let getCalls = 0
    checker = createVersionChecker({
      getVersion: async () => { getCalls += 1; return current },
      onUpdate: () => {},
      intervalMs: 5,
    })
    checker.start() // 初始隐藏：不检查不轮询
    await new Promise((resolve) => setTimeout(resolve, 30))
    assert.equal(getCalls, 0, '初始隐藏不应检查版本')

    doc.setVisibility('visible') // 恢复可见：立即检查一次（记录基线，同步断言）
    assert.equal(getCalls, 1, '恢复可见应立即检查一次')
    const afterVisible = getCalls
    await new Promise((resolve) => setTimeout(resolve, 30))
    assert.ok(getCalls >= afterVisible + 3, '恢复可见后应恢复定时轮询')
  } finally {
    if (checker) checker.stop()
    doc.restore()
  }
})
