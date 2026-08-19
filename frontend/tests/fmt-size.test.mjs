// fmtSize 纯函数测试（issue #104 补测）：备份文件大小人类可读显示
// （数据备份卡片 BackupManager 的备份列表大小列使用）。
//
// 档位约定：<1KB 显示 B（无小数）；1KB~1MB 显示 KB（1 位小数）；
// ≥1MB 显示 MB（2 位小数）；空值返回占位符。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'


// node --test 原生不支持 jsx，用 vite SSR 转译加载模块（与其他测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { fmtSize } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

// ---- 空值与极小值 ----

test('fmtSize：null / undefined 返回占位符', () => {
  assert.equal(fmtSize(null), '—')
  assert.equal(fmtSize(undefined), '—')
})

test('fmtSize：0 字节', () => {
  assert.equal(fmtSize(0), '0 B')
})

// ---- B 档（<1KB，无小数）----

test('fmtSize：B 档边界（1023 B 仍为 B 档）', () => {
  assert.equal(fmtSize(1), '1 B')
  assert.equal(fmtSize(1023), '1023 B')
})

// ---- KB 档（1KB~1MB，1 位小数）----

test('fmtSize：恰好 1024 B → 1.0 KB（KB 档起点）', () => {
  assert.equal(fmtSize(1024), '1.0 KB')
})

test('fmtSize：KB 档四舍五入到 1 位小数', () => {
  assert.equal(fmtSize(1536), '1.5 KB')
  assert.equal(fmtSize(1024 * 1024 - 1), '1024.0 KB')
})

// ---- MB 档（≥1MB，2 位小数）----

test('fmtSize：恰好 1MB → 1.00 MB（MB 档起点）', () => {
  assert.equal(fmtSize(1024 * 1024), '1.00 MB')
})

test('fmtSize：MB 档保留 2 位小数', () => {
  assert.equal(fmtSize(1024 * 1024 * 2 + 512 * 1024), '2.50 MB')
})

test('fmtSize：GB 级大文件换算为 MB 显示', () => {
  assert.equal(fmtSize(10 * 1024 * 1024 * 1024), '10240.00 MB')
})
