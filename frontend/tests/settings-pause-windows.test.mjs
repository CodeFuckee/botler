// 设置页定时暂停窗口配置测试（issue #169）：任务调度卡片增加
// worker.pause_windows / pause_weekdays / pause_timezone 配置——
// 窗口内停止开始新任务，已开始任务继续执行，未开始任务等窗口结束后开始。
//
// 断言：
// 1. 「任务调度」卡片含「定时暂停窗口」区块（worker.pause_windows 标注）；
// 2. textarea 每行一个窗口串，onChange 按行拆分（trim + 过滤空行）转数组；
// 3. 星期复选框（周一~周日）切换 worker.pause_weekdays（升序去重）；
// 4. 全局「保存」提交 worker 段含 pause_windows / pause_weekdays / pause_timezone；
// 5. 服务端返回 pause_active=true 时显示「当前处于暂停窗口」提示；
// 6. 渲染时窗口数组回显为每行一个窗口串。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：任务调度卡片 JSX 移到 components/settings/TasksCard.jsx，
// setPauseWindowsText / togglePauseWeekday / save 收敛到
// hooks/useSettingsData.js——静态断言跟随新文件
const tasksCard = readFileSync(path.join(ROOT, 'src/components/settings/TasksCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsSrc = tasksCard + '\n' + hook

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-issue-priority.test.mjs 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名函数源码片段：从「const name =」到下一个顶层 const / return 之前 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} =[\\s\\S]*?(?=\\n  const |\\n  return \\()`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('任务调度卡片含「定时暂停窗口」配置区块', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /定时暂停窗口/, '卡片应含定时暂停窗口配置')
  assert.match(card, /worker\.pause_windows/, '应标注配置键 worker.pause_windows')
  assert.match(card, /worker\.pause_weekdays/, '应标注配置键 worker.pause_weekdays')
  assert.match(card, /worker\.pause_timezone/, '应标注配置键 worker.pause_timezone')
  assert.match(card, /<textarea/, '窗口应使用 textarea 逐行输入')
  assert.match(card, /09:00-12:00/, 'placeholder 应给出窗口示例')
})

test('说明文字写明暂停语义（已开始继续 / 未开始等待）', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /已经开始执行的任务可以继续执行/, '说明应写明已开始任务继续执行')
  assert.match(card, /未开始执行的任务等到窗口结束后自动开始执行/, '说明应写明未开始任务等待窗口结束')
})

test('pause_active 为真时显示当前暂停提示', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /pause_active/, '应读取服务端 pause_active 状态')
  assert.match(card, /当前处于暂停窗口/, '应显示当前处于暂停窗口提示')
})

test('setPauseWindowsText 按行拆分转数组（trim + 过滤空行）', () => {
  const body = fnBody(settingsSrc, 'setPauseWindowsText')
  assert.ok(body, '应存在 setPauseWindowsText 函数')
  assert.match(body, /split\('\\n'\)/, '应按换行拆分')
  assert.match(body, /\.trim\(\)/, '应去除每项首尾空白')
  assert.match(body, /filter\(Boolean\)/, '应过滤空行')
})

test('togglePauseWeekday 升序去重切换星期', () => {
  const body = fnBody(settingsSrc, 'togglePauseWeekday')
  assert.ok(body, '应存在 togglePauseWeekday 函数')
  assert.match(body, /includes\(day\)/, '应判断当前是否已勾选')
  assert.match(body, /\.sort\(/, '应升序存储')
})

test('全局保存提交 pause_windows / pause_weekdays / pause_timezone', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /worker\.pause_windows = settings\.worker\?\.pause_windows \|\| \[\]/,
    'save 应携带 pause_windows 数组')
  assert.match(body, /worker\.pause_weekdays = settings\.worker\?\.pause_weekdays \|\| \[\]/,
    'save 应携带 pause_weekdays 数组')
  assert.match(body, /worker\.pause_timezone = settings\.worker\?\.pause_timezone \|\| ''/,
    'save 应携带 pause_timezone')
})

/** 渲染用 fetch mock（覆盖设置页全部接口） */
function mockFetch({ windows = ['09:00-12:00'], weekdays = [], timezone = 'Asia/Shanghai', pauseActive = false } = {}) {
  const puts = []
  const originalFetch = global.fetch
  global.fetch = async (p, opts) => {
    if (opts?.method === 'PUT') {
      puts.push(JSON.parse(opts.body))
      return { ok: true, status: 200, json: async () => ({}) }
    }
    const pathname = String(p)
    if (pathname.startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          worker: {
            issue_priority: ['bug', 'test', 'feature'],
            engine: 'claude',
            pause_windows: windows,
            pause_weekdays: weekdays,
            pause_timezone: timezone,
            pause_priority_threshold: 0,
            pause_active: pauseActive,
          },
          sso: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '' }, notifications: {}, gitlab: {}, env: {},
          dsh: {}, backup: {}, browse: {}, templates: {}, ai_providers: [],
        }),
      }
    }
    if (pathname.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-13 00:00:00' }) }
    }
    if (pathname.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { puts, restore: () => { global.fetch = originalFetch } }
}

test('渲染：窗口数组回显为每行一个窗口串，保存提交解析后的数组', async () => {
  const m = mockFetch({ windows: ['09:00-12:00', '14:00-18:00'] })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const areas = renderer.root.findAll(
      (node) => node.type === 'textarea'
        && node.props.placeholder
        && node.props.placeholder.includes('09:00-12:00'))
    assert.equal(areas.length, 1, '应存在暂停窗口 textarea')
    assert.equal(areas[0].props.value, '09:00-12:00\n14:00-18:00',
      '窗口数组应回显为每行一个窗口串')

    // 修改输入（含空行）→ 点击全局「保存」
    await TestRenderer.act(async () => {
      areas[0].props.onChange({ target: { value: '09:00-12:00\n\n22:00-02:00' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find(
      (b) => Array.isArray(b.props.children)
        ? String(b.props.children.join('')).trim() === '保存'
        : String(b.props.children || '').trim() === '保存')
    assert.ok(saveBtn, '应存在全局「保存」按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.ok(m.puts.length >= 1, '点击保存应发出 PUT 请求')
    const worker = m.puts[m.puts.length - 1].worker
    assert.deepEqual(worker.pause_windows, ['09:00-12:00', '22:00-02:00'],
      '空行应被过滤，提交窗口数组应为解析后的列表')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('渲染：pause_active=true 显示暂停提示，星期勾选提交数组', async () => {
  const m = mockFetch({ windows: ['09:00-12:00'], weekdays: [0, 1], pauseActive: true })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const warns = renderer.root.findAll(
      (node) => typeof node.props.className === 'string'
        && node.props.className.includes('alert-warning'))
    assert.ok(warns.length >= 1, '处于暂停窗口时应显示警告提示')

    // 勾选周三（2）→ 保存 → 提交 pause_weekdays 为升序去重数组
    const checks = renderer.root.findAll((node) => node.type === 'input' && node.props.type === 'checkbox')
    const wed = checks.find((c) => c.props.checked === false && c.parent?.children?.includes('周三'))
    assert.ok(wed, '应存在周三复选框')
    await TestRenderer.act(async () => {
      wed.props.onChange()
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find(
      (b) => Array.isArray(b.props.children)
        ? String(b.props.children.join('')).trim() === '保存'
        : String(b.props.children || '').trim() === '保存')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const worker = m.puts[m.puts.length - 1].worker
    assert.deepEqual(worker.pause_weekdays, [0, 1, 2], '勾选后应提交升序星期数组')
    assert.equal(worker.pause_timezone, 'Asia/Shanghai', '应提交配置的时区')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})


test('任务调度卡片含「豁免优先级」配置区块（issue #299）', () => {
  const card = cardSource(settingsSrc, '任务调度')
  assert.ok(card, '设置页应存在「任务调度」卡片')
  assert.match(card, /豁免优先级/, '卡片应含豁免优先级配置')
  assert.match(card, /worker\.pause_priority_threshold/,
    '应标注配置键 worker.pause_priority_threshold')
  assert.match(card, /暂停窗口内\n\s*仍可开始新任务/, '说明应写明窗口内豁免语义')
})

test('全局保存提交 pause_priority_threshold（issue #299）', () => {
  const body = fnBody(settingsSrc, 'save')
  assert.ok(body, '应存在 save 函数')
  assert.match(body, /worker\.pause_priority_threshold = Number\(settings\.worker\?\.pause_priority_threshold \?\? 0\)/,
    'save 应携带 pause_priority_threshold 数字（缺省 0）')
})

test('渲染：豁免优先级输入框回显并随保存提交', async () => {
  const m = mockFetch({ windows: ['09:00-12:00'] })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const numInputs = renderer.root.findAll(
      (node) => node.type === 'input' && node.props.type === 'number')
    const threshold = numInputs.find((i) => String(i.props.placeholder || '').includes('关闭'))
    assert.ok(threshold, '应存在豁免优先级数字输入框（placeholder 含 关闭）')
    assert.equal(Number(threshold.props.value), 0, '缺省应回显 0（关闭）')

    // 修改为 50 → 点击全局「保存」→ 提交 pause_priority_threshold=50
    await TestRenderer.act(async () => {
      threshold.props.onChange({ target: { value: '50' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find(
      (b) => Array.isArray(b.props.children)
        ? String(b.props.children.join('')).trim() === '保存'
        : String(b.props.children || '').trim() === '保存')
    assert.ok(saveBtn, '应存在全局「保存」按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.ok(m.puts.length >= 1, '点击保存应发出 PUT 请求')
    const worker = m.puts[m.puts.length - 1].worker
    assert.equal(worker.pause_priority_threshold, 50, '应提交配置的豁免阈值')
  } finally {
    if (renderer) await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
