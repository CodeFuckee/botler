// 添加 issue 弹窗「标题语音输入」测试（issue #165）：标题输入框右侧新增
// 语音输入按钮，点击后通过浏览器 Web Speech API（SpeechRecognition）把
// 语音实时转文字填入标题；识别中再点按钮停止；浏览器不支持 / 权限拒绝 /
// 未检测到语音等异常场景给出中文错误提示；语音填入标题同样走 issue #103
// 的「描述为空自动复制标题」联动逻辑（键盘输入与语音输入行为一致）。
//
// 断言：
// 1. 渲染：标题输入框右侧渲染语音按钮（与输入框同处一行）；
// 2. 识别：点击按钮创建 SpeechRecognition 并 start；interim 结果实时
//    填入标题、final 结果确认标题；
// 3. 联动：语音填入标题时描述为空自动跟随（issue #103 语义），描述为
//    用户手写内容时不覆盖；
// 4. 停止：识别中再点按钮 → rec.stop() 被调用、按钮退出识别态；
// 5. 异常：浏览器不支持 → 点击提示「不支持」；权限拒绝 / 无语音 → 中文
//    错误提示；
// 6. 卸载：识别中关闭弹窗 → rec.abort() 被调用（清理识别实例）。
import { after, mock, test } from 'node:test'

// 渲染树节点 → 纯文本（递归；Lucide 图标等元素无文本内容，自动忽略）
function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children)
}

import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'


// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与 overview-add-issue.test.mjs 一致）。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Overview } = await vite.ssrLoadModule('/src/pages/Overview.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')

after(() => vite.close())

const FORM_META = {
  members: [
    { id: 20, username: 'agent', name: 'Agent' },
    { id: 21, username: 'dev', name: 'Dev' },
  ],
  labels: [{ name: 'bug', color: 'FF0000', text_color: 'FFFFFF' }],
}

const ISSUES_PAYLOAD = {
  repos: [
    { repo_id: 1, repo_name: 'botler', priority: 10, issues: [
      { iid: 11, title: '已有 issue',
        updated_at: '2026-08-15 01:00:00',
        web_url: 'https://gitlab.example.com/x/-/issues/11' },
    ] },
  ],
  errors: [], total: 1,
}

// 模拟浏览器 SpeechRecognition（Web Speech API）：
// 记录每次创建的实例与 start/stop/abort 调用，测试中手动触发
// onresult / onerror / onend 模拟识别过程。
class MockSpeechRecognition {
  static instances = []
  static reset() { MockSpeechRecognition.instances = [] }
  constructor() {
    this.lang = ''
    this.interimResults = null
    this.continuous = null
    this.started = false
    this.stopped = false
    this.aborted = false
    this.onresult = null
    this.onerror = null
    this.onend = null
    MockSpeechRecognition.instances.push(this)
  }
  start() { this.started = true }
  stop() { this.stopped = true }
  abort() { this.aborted = true }
}

// 构造识别事件：results 为 [{ transcript, isFinal }, ...]，还原真实
// SpeechRecognitionEvent.results（数字下标 + isFinal 属性）。
function resultEvent(results) {
  return {
    results: results.map((r) => {
      const item = [{ transcript: r.transcript }]
      item.isFinal = Boolean(r.isFinal)
      return item
    }),
  }
}

// 挂载 Overview 并打开第一个仓库的「添加 Issue」弹窗
async function renderAddIssueModal() {
  mock.method(api, 'get', async (pathname) => {
    if (pathname.startsWith('/api/tasks?')) return { tasks: [], total: 0, stats: {} }
    if (pathname === '/api/pipelines/overview') return { pipelines: [], errors: [] }
    if (pathname === '/api/issues/overview') return ISSUES_PAYLOAD
    if (pathname.startsWith('/api/issues/form-meta/')) return FORM_META
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
  const btns = renderer.root.findAll(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('add-issue-btn'))
  await TestRenderer.act(async () => {
    btns[0].props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return { renderer, renderError }
}

// 在「支持语音识别」的 window 环境下挂载弹窗并返回 window 恢复函数
async function renderWithSpeech() {
  const saved = globalThis.window
  globalThis.window = { SpeechRecognition: MockSpeechRecognition }
  return { ...(await renderAddIssueModal()), savedWindow: saved }
}

// 在「不支持语音识别」的 window 环境下挂载弹窗
async function renderWithoutSpeech() {
  const saved = globalThis.window
  globalThis.window = {}
  return { ...(await renderAddIssueModal()), savedWindow: saved }
}

function restoreWindow(saved) {
  if (saved === undefined) delete globalThis.window
  else globalThis.window = saved
}

// 弹窗内表单元素定位辅助
function titleInput(renderer) {
  return renderer.root.find(
    (n) => n.props.className === 'input add-issue-title')
}
function descInput(renderer) {
  return renderer.root.find(
    (n) => n.props.className === 'input add-issue-desc')
}
function voiceBtn(renderer) {
  return renderer.root.find(
    (n) => n.type === 'button'
      && String(n.props.className || '').includes('add-issue-voice'))
}
function titleRow(renderer) {
  return renderer.root.find(
    (n) => String(n.props.className || '').includes('add-issue-title-row'))
}
function voiceError(renderer) {
  const el = renderer.root.findAll(
    (n) => String(n.props.className || '').includes('add-issue-voice-error'))
  return el.map((a) => textOf(a.props.children)).join('|')
}

async function cleanup(renderer) {
  await TestRenderer.act(() => renderer.unmount())
  mock.restoreAll()
}

// ---- 渲染 ----

test('渲染：标题输入框右侧渲染语音按钮（与输入框同一行）', async () => {
  const { renderer, renderError } = await renderAddIssueModal()
  try {
    assert.equal(renderError, null, `渲染抛错：${renderError?.message || renderError}`)
    assert.ok(voiceBtn(renderer), '应渲染语音输入按钮')
    const row = titleRow(renderer)
    const hasInput = row.findAll((n) => n.props.className === 'input add-issue-title').length === 1
    const hasBtn = row.findAll(
      (n) => n.type === 'button'
        && String(n.props.className || '').includes('add-issue-voice')).length === 1
    assert.ok(hasInput && hasBtn, '语音按钮应与标题输入框同处一行')
    assert.equal(voiceBtn(renderer).props.title, '语音输入标题')
  } finally { await cleanup(renderer) }
})

// ---- 识别流程 ----

test('识别：点击按钮创建 SpeechRecognition 并 start，interim 实时填入、final 确认', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null,
      `渲染抛错：${renderError?.message || renderError}`)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    assert.ok(rec, '点击后应创建 SpeechRecognition 实例')
    assert.ok(rec.started, '应调用 rec.start()')
    assert.equal(rec.lang, 'zh-CN', '识别语言应为中文')
    assert.ok(String(voiceBtn(renderer).props.className).includes('listening'),
              '识别中按钮应处于 listening 态')
    assert.equal(voiceBtn(renderer).props.title, '点击停止语音输入')
    // interim：实时显示识别中的文字
    await TestRenderer.act(async () => {
      rec.onresult(resultEvent([{ transcript: '语音', isFinal: false }]))
    })
    assert.equal(titleInput(renderer).props.value, '语音',
                 'interim 结果应实时填入标题')
    // final：确认最终结果（真实浏览器同一结果槽由 interim 转 final，
    // 直接替换标题中的临时文字）
    await TestRenderer.act(async () => {
      rec.onresult(resultEvent([
        { transcript: '语音输入标题', isFinal: true },
      ]))
      rec.onend()
    })
    assert.equal(titleInput(renderer).props.value, '语音输入标题',
                 'final 结果应确认标题')
    // 多段语音：final 段 + 末尾 interim 段按顺序拼接（真实事件语义）
    MockSpeechRecognition.reset()
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec2 = MockSpeechRecognition.instances[0]
    await TestRenderer.act(async () => {
      rec2.onresult(resultEvent([
        { transcript: '你好', isFinal: true },
        { transcript: '世界', isFinal: false },
      ]))
      rec2.onend()
    })
    assert.equal(titleInput(renderer).props.value, '你好世界',
                 'final 段与 interim 段应按顺序拼接')
    assert.ok(!String(voiceBtn(renderer).props.className).includes('listening'),
              '识别结束按钮应退出 listening 态')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('联动：语音填入标题时描述为空自动跟随（issue #103 语义）', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    await TestRenderer.act(async () => {
      rec.onresult(resultEvent([{ transcript: '语音标题', isFinal: true }]))
      rec.onend()
    })
    assert.equal(titleInput(renderer).props.value, '语音标题')
    assert.equal(descInput(renderer).props.value, '语音标题',
                 '描述为空时语音标题应自动复制到描述')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('联动：描述为手写内容时语音标题不覆盖描述', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => {
      descInput(renderer).props.onChange({ target: { value: '用户手写的描述' } })
    })
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    await TestRenderer.act(async () => {
      rec.onresult(resultEvent([{ transcript: '语音标题', isFinal: true }]))
      rec.onend()
    })
    assert.equal(titleInput(renderer).props.value, '语音标题')
    assert.equal(descInput(renderer).props.value, '用户手写的描述',
                 '描述非空时不应被语音标题覆盖')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('停止：识别中再点按钮 → rec.stop() 被调用、按钮退出识别态', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    assert.ok(String(voiceBtn(renderer).props.className).includes('listening'))
    assert.equal(voiceBtn(renderer).props.title, '点击停止语音输入')
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    assert.ok(rec.stopped, '再点按钮应调用 rec.stop()')
    assert.ok(!String(voiceBtn(renderer).props.className).includes('listening'),
              '停止后按钮应退出 listening 态')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

// ---- 异常场景 ----

test('异常：浏览器不支持语音识别 → 点击提示不支持', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithoutSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    assert.equal(MockSpeechRecognition.instances.length, 0,
                 '不支持时不应创建识别实例')
    assert.ok(voiceError(renderer).includes('不支持'),
              '应提示浏览器不支持语音输入')
    assert.equal(titleInput(renderer).props.value, '', '标题不应被改动')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('异常：麦克风权限被拒绝 → 中文错误提示', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    await TestRenderer.act(async () => {
      rec.onerror({ error: 'not-allowed' })
      rec.onend()
    })
    assert.ok(voiceError(renderer).includes('麦克风权限被拒绝'),
              '权限拒绝应给出中文错误提示')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('异常：未检测到语音 → 中文错误提示', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    const rec = MockSpeechRecognition.instances[0]
    await TestRenderer.act(async () => {
      rec.onerror({ error: 'no-speech' })
      rec.onend()
    })
    assert.ok(voiceError(renderer).includes('未检测到语音'),
              '无语音应给出中文错误提示')
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
})

test('卸载：识别中关闭弹窗 → rec.abort() 被调用', async () => {
  MockSpeechRecognition.reset()
  const { renderer, renderError, savedWindow } = await renderWithSpeech()
  let rec = null
  try {
    assert.equal(renderError, null)
    await TestRenderer.act(async () => { voiceBtn(renderer).props.onClick() })
    rec = MockSpeechRecognition.instances[0]
  } finally {
    await cleanup(renderer)
    restoreWindow(savedWindow)
  }
  assert.ok(rec && rec.aborted, '卸载时应中止进行中的语音识别')
})
