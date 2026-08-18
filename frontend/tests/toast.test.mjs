// 全局 toast 提示模块测试（issue #226）：队列入队/出队、自动消失、
// 手动关闭与宿主订阅通知。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  TOAST_DURATION_MS,
  showToast,
  dismissToast,
  currentToasts,
  clearToasts,
  subscribeToastHost,
} from '../src/toast.js'

test('TOAST_DURATION_MS 默认 3.5 秒', () => {
  assert.equal(TOAST_DURATION_MS, 3500)
})

test('showToast：入队并返回自增 id，默认 type=error', () => {
  clearToasts()
  const id1 = showToast('接口失败')
  const id2 = showToast('保存成功', { type: 'success' })
  const list = currentToasts()
  assert.equal(list.length, 2)
  assert.equal(list[0].id, id1)
  assert.equal(list[0].message, '接口失败')
  assert.equal(list[0].type, 'error')
  assert.equal(list[1].id, id2)
  assert.equal(list[1].type, 'success')
  assert.notEqual(id1, id2)
  clearToasts()
})

test('showToast：非字符串消息转为字符串展示', () => {
  clearToasts()
  showToast(500)
  assert.equal(currentToasts()[0].message, '500')
  showToast(null)
  assert.equal(currentToasts()[1].message, 'null')
  clearToasts()
})

test('dismissToast：按 id 移除；未知 id 静默忽略', () => {
  clearToasts()
  const id = showToast('提示')
  assert.equal(currentToasts().length, 1)
  dismissToast(id)
  assert.equal(currentToasts().length, 0)
  dismissToast(id) // 再次关闭已不存在的 toast 不抛错
  assert.equal(currentToasts().length, 0)
  clearToasts()
})

test('showToast：duration 毫秒后自动消失', async () => {
  clearToasts()
  showToast('短暂提示', { duration: 30 })
  assert.equal(currentToasts().length, 1)
  await new Promise((r) => setTimeout(r, 80))
  assert.equal(currentToasts().length, 0)
  clearToasts()
})

test('showToast：duration=0 不自动消失（测试/常驻场景）', async () => {
  clearToasts()
  showToast('常驻', { duration: 0 })
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(currentToasts().length, 1)
  clearToasts()
})

test('subscribeToastHost：入队/出队时回调通知，返回取消订阅函数', () => {
  clearToasts()
  let notified = 0
  const unsub = subscribeToastHost(() => { notified++ })
  showToast('a')
  assert.equal(notified, 1)
  const id = showToast('b', { duration: 0 })
  assert.equal(notified, 2)
  dismissToast(id)
  assert.equal(notified, 3)
  unsub()
  showToast('c', { duration: 0 })
  assert.equal(notified, 3) // 取消订阅后不再通知
  clearToasts()
})

test('clearToasts：清空全部并通知宿主', () => {
  clearToasts()
  let notified = 0
  const unsub = subscribeToastHost(() => { notified++ })
  showToast('a', { duration: 0 })
  showToast('b', { duration: 0 })
  clearToasts()
  assert.equal(currentToasts().length, 0)
  assert.equal(notified, 3) // 两次入队 + 一次清空
  unsub()
})
