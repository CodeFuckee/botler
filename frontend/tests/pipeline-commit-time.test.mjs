// 概览页流水线卡片提交时间测试（issue #43）：
// 每张流水线卡片显示最近流水线对应提交的提交时间（绝对时间，复用
// fmtTime）与距今多久（相对时间，新增 fmtAgo 纯函数）。
//
// 断言：
// 1. fmtAgo：秒/分钟/小时/天/月/年各档位换算，60 秒内显示「刚刚」；
// 2. fmtAgo 边界：空值 / 非法格式返回 null，未来时间（时钟偏差）按「刚刚」；
// 3. 卡片渲染：commit_time 存在时显示绝对+相对时间；为 null 时不渲染
//    时间节点（卡片其余部分正常，不 crash）；
// 4. 数据流：后端返回的 commit_time 为 UTC 无后缀时间串（与 fmtTime
//    解析约定一致），卡片源码使用 fmtTime + fmtAgo 展示；
// 5. styles.css 提供 pipeline-commit-time 样式。
import { after, mock, test } from 'node:test'

// 渲染树节点 → 纯文本（递归；Lucide 图标等元素无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const overview = readFileSync(path.join(ROOT, 'src/components/overview/PipelineSection.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const Overview = (await vite.ssrLoadModule('/src/pages/Overview.jsx')).default
const { fmtAgo } = await vite.ssrLoadModule('/src/api.js')
const apiMod = await vite.ssrLoadModule('/src/api.js')
const { api } = apiMod

after(() => vite.close())

// ---- 纯函数：fmtAgo ----

// 基准时刻：2026-08-13 12:00:00 UTC（毫秒时间戳）
const NOW = Date.UTC(2026, 7, 13, 12, 0, 0)

// 构造后端格式（UTC 无后缀）的时间串：基准往前推 offset 秒
function utcStr(offsetSec) {
  const d = new Date(NOW - offsetSec * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
}

test('fmtAgo：60 秒内显示「刚刚」', () => {
  assert.equal(fmtAgo(utcStr(0), NOW), '刚刚')
  assert.equal(fmtAgo(utcStr(30), NOW), '刚刚')
  assert.equal(fmtAgo(utcStr(59), NOW), '刚刚')
})

test('fmtAgo：分钟档', () => {
  assert.equal(fmtAgo(utcStr(60), NOW), '1 分钟前')
  assert.equal(fmtAgo(utcStr(5 * 60 + 30), NOW), '5 分钟前')
  assert.equal(fmtAgo(utcStr(59 * 60), NOW), '59 分钟前')
})

test('fmtAgo：小时档', () => {
  assert.equal(fmtAgo(utcStr(60 * 60), NOW), '1 小时前')
  assert.equal(fmtAgo(utcStr(23 * 60 * 60), NOW), '23 小时前')
})

test('fmtAgo：天档', () => {
  assert.equal(fmtAgo(utcStr(24 * 60 * 60), NOW), '1 天前')
  assert.equal(fmtAgo(utcStr(29 * 24 * 60 * 60), NOW), '29 天前')
})

test('fmtAgo：月档', () => {
  assert.equal(fmtAgo(utcStr(30 * 24 * 60 * 60), NOW), '1 个月前')
  assert.equal(fmtAgo(utcStr(364 * 24 * 60 * 60), NOW), '12 个月前')
})

test('fmtAgo：年档', () => {
  assert.equal(fmtAgo(utcStr(365 * 24 * 60 * 60), NOW), '1 年前')
  assert.equal(fmtAgo(utcStr(3 * 365 * 24 * 60 * 60), NOW), '3 年前')
})

test('fmtAgo 边界：空值与非法格式返回 null', () => {
  assert.equal(fmtAgo(null, NOW), null)
  assert.equal(fmtAgo('', NOW), null)
  assert.equal(fmtAgo(undefined, NOW), null)
  assert.equal(fmtAgo('not-a-date', NOW), null)
  assert.equal(fmtAgo('2026/08/13 12:00:00', NOW), null)
})

test('fmtAgo 边界：未来时间（时钟偏差）按「刚刚」', () => {
  assert.equal(fmtAgo(utcStr(-5), NOW), '刚刚')
  assert.equal(fmtAgo(utcStr(-3600), NOW), '刚刚')
})

// ---- 组件渲染 ----

async function renderAndSettle(impl, waitMs = 30) {
  mock.method(api, 'get', impl)
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(Overview))
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
}

function makeApiMock(pipelineData) {
  return async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) {
      return { tasks: [], total: 0, stats: {} }
    }
    if (pathname === '/api/pipelines/overview') {
      return pipelineData
    }
    throw new Error('unexpected ' + pathname)
  }
}

test('渲染：commit_time 存在时卡片显示绝对时间与相对时间', async () => {
  const data = {
    pipelines: [{
      repo_id: 1, repo_name: 'botler',
      pipeline: {
        id: 731, status: 'success', ref: 'main', sha: 'abc123',
        web_url: 'https://gitlab.example.com/g/botler/-/pipelines/731',
      },
      stages: [{ name: 'build', status: 'success' }],
      commit_time: '2026-08-13 04:00:00',
    }],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // commit_time 存在时应渲染时间节点（fmtTime 会按显示时区转换，
    // 具体值依赖运行环境时区，故只断言格式与相对时间文案）
    const timeNodes = root.findAll(
      (n) => n.props?.className && String(n.props.className).split(' ').includes('pipeline-commit-time'),
    )
    assert.equal(timeNodes.length, 1, 'commit_time 存在时应渲染一个时间节点')
    const nodeText = textOf(timeNodes[0].children)
    assert.match(nodeText, /\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/, '应显示绝对时间（YYYY-MM-DD HH:MM:SS 格式）')
    assert.match(nodeText, /前/, '应显示相对时间（X 秒/分钟/小时/天/月/年前）')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

test('渲染：commit_time 为 null 时不渲染时间节点，卡片其余正常', async () => {
  const data = {
    pipelines: [{
      repo_id: 1, repo_name: 'no-time-repo',
      pipeline: {
        id: 731, status: 'success', ref: 'main', sha: 'abc123',
        web_url: 'https://gitlab.example.com/g/n/-/pipelines/731',
      },
      stages: [{ name: 'build', status: 'success' }],
      commit_time: null,
    }],
    errors: [],
  }
  const { renderer, renderError } = await renderAndSettle(makeApiMock(data))
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    const root = renderer.root
    // 不渲染 pipeline-commit-time 节点
    const timeNodes = root.findAll(
      (n) => n.props?.className && String(n.props.className).split(' ').includes('pipeline-commit-time'),
    )
    assert.equal(timeNodes.length, 0, 'commit_time 为 null 不应渲染时间节点')
    const text = JSON.stringify(renderer.toJSON())
    assert.ok(text.includes('no-time-repo'), '卡片其余部分应正常渲染')
    assert.ok(text.includes('build'), 'stage 节点应正常渲染')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    mock.restoreAll()
  }
})

// ---- 源码与样式断言 ----

test('Overview 源码使用 fmtTime + fmtAgo 展示提交时间', () => {
  assert.match(overview, /fmtTime/, '应使用 fmtTime 格式化绝对时间')
  assert.match(overview, /fmtAgo/, '应使用 fmtAgo 计算相对时间')
  assert.match(overview, /commit_time/, '应读取后端的 commit_time 字段')
})

test('styles.css 提供 pipeline-commit-time 样式', () => {
  assert.match(styles, /pipeline-commit-time/, '应有提交时间节点的样式定义')
})
