// 自定义对话框测试（issue #105）：替代浏览器原生 alert/confirm 弹窗。
// 用户反馈「不要使用 alert 来弹出通知，自定义一个对话框」——页面内渲染
// 统一风格的自定义对话框（dialog.js + DialogHost），不再弹浏览器原生框。
//
// 断言：
// 1. 源码：dialog.js 导出 confirmDialog / alertDialog / installAutoAnswer；
//    DialogHost 渲染标题/消息/按钮；App 根部挂载 DialogHost；
//    全部 10 处调用点源码不再出现 window.confirm / confirm(；
// 2. confirm 形态：消息渲染；点「确定」resolve(true)；点「取消」resolve(false)；
//    点遮罩 / × 按钮 / Esc 均 resolve(false)（视为取消）；
//    点击面板内部不关闭（stopPropagation）；
// 3. alert 形态：单「确定」按钮；点确定 / 遮罩 / × / Esc 均 resolve(undefined)；
// 4. danger 参数 → 确定按钮使用 btn-danger 危险样式；自定义按钮文案生效；
// 5. 边界：空 message / 空 title（缺省「请确认」「提示」）正常渲染不崩溃；
//    多行消息保留换行（white-space: pre-line）；
// 6. 排队：连续两次调用只显示第一个，结算后才显示第二个；
// 7. 无宿主兜底：installAutoAnswer 直接应答；无注入时保守按「取消」应答
//    不悬挂；resetDialogs 清空队列（测试清理用）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件；dialog.js 与组件内
// import 的是同一模块实例（同一 vite 实例），测试注入与断言作用于真实模块。
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  // react-router-dom 的 CJS 构建不能被 vite SSR 转译（module is not
  // defined），alias 到测试用最小 mock（与 stop-all-button.test.mjs 一致）
  resolve: {
    alias: {
      'react-router-dom': path.join(ROOT, 'tests/helpers/mock-router.jsx'),
    },
  },
})
const { default: DialogHost } = await vite.ssrLoadModule('/src/components/DialogHost.jsx')
const dialog = await vite.ssrLoadModule('/src/dialog.js')

after(async () => {
  dialog.resetDialogs()
  dialog.installAutoAnswer(null)
  await vite.close()
})

// ---- 源码断言 ----

const SRC_FILES = [
  'src/components/BackupManager.jsx',
  'src/components/IssueDrawer.jsx',
  'src/components/AiProvidersCard.jsx',
  'src/pages/Labels.jsx',
  'src/pages/Repos.jsx',
  'src/pages/Tasks.jsx',
  'src/pages/Templates.jsx',
]

test('源码：dialog.js 提供确认/提示接口与测试注入点', () => {
  const dialogSrc = readFileSync(path.join(ROOT, 'src/dialog.js'), 'utf8')
  assert.match(dialogSrc, /export function confirmDialog/, '应导出 confirmDialog')
  assert.match(dialogSrc, /export function alertDialog/, '应导出 alertDialog')
  assert.match(dialogSrc, /export function installAutoAnswer/, '应导出测试注入点 installAutoAnswer')
  assert.match(dialogSrc, /export function resetDialogs/, '应导出清理函数 resetDialogs')
})

test('源码：App 根部挂载 DialogHost，全站可用', () => {
  const appSrc = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
  assert.match(appSrc, /<DialogHost/, 'App 应渲染 DialogHost')
})

test('源码：全部调用点不再使用浏览器原生 confirm', () => {
  for (const f of SRC_FILES) {
    const src = readFileSync(path.join(ROOT, f), 'utf8')
    assert.ok(
      !/window\.confirm|\bconfirm\s*\(|\balert\s*\(/.test(src),
      `${f} 不应再调用浏览器原生 confirm/alert`,
    )
    assert.match(src, /confirmDialog/, `${f} 应改用自定义对话框 confirmDialog`)
  }
})

// ---- 渲染与查找 helper ----

// 挂载 DialogHost（对话模块的宿主，生产环境挂在 App 根部）。
// 必须在 act 内 create：react-test-renderer 只有 act 会 flush passive
// effects，订阅 dialog.js 的 useEffect 不执行则 listener 未注册，
// 对话框会落入「无宿主兜底」分支被立即结算。
function renderHost() {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(DialogHost))
  })
  return renderer
}

// 在 act 内打开一个对话框（同步入队并触发宿主重渲染）。
// 注意：必须用同步 act——React 18 的 async act 在回调内同步 dispatch
// （useReducer 重渲染）时与 node:test 环境的调度会死循环（实测挂起），
// 同步 act 会同步 flush，行为一致且稳定。
function open(promiseFactory) {
  let p = null
  TestRenderer.act(() => { p = promiseFactory() })
  return p
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

function findButtons(renderer, text) {
  return renderer.root.findAllByType('button')
    .filter((b) => textOf(b.props.children).includes(text))
}

function findByClass(renderer, cls) {
  return renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes(cls))
}

// mock 最小 document 以捕获 Esc 监听（node 环境无 document；DialogHost
// 与现有 Modal 一致在无 document 时跳过 Esc 监听）
let keyHandler = null
function installDocumentMock() {
  keyHandler = null
  globalThis.document = {
    addEventListener: (ev, fn) => { if (ev === 'keydown') keyHandler = fn },
    removeEventListener: () => {},
  }
}

function cleanup(renderer) {
  dialog.resetDialogs()
  dialog.installAutoAnswer(null)
  delete globalThis.document
  if (renderer) TestRenderer.act(() => renderer.unmount())
}

// ---- confirm 形态 ----

test('confirm：渲染消息与取消/确定按钮，点确定 resolve(true)', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '确定删除备份 demo.tar.gz？' }))
    const text = textOf(renderer.root)
    assert.match(text, /确定删除备份 demo\.tar\.gz/, '应渲染调用方传入的消息')
    assert.match(text, /请确认/, '未传标题时应显示默认标题「请确认」')
    assert.equal(findButtons(renderer, '取消').length, 1, '应有取消按钮')
    assert.equal(findButtons(renderer, '确定').length, 1, '应有确定按钮')

    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p, true, '点确定应 resolve true')
    assert.equal(findByClass(renderer, 'modal-overlay').length, 0, '结算后对话框应消失')
  } finally {
    cleanup(renderer)
  }
})

test('confirm：点取消 resolve(false)', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '确认继续？' }))
    TestRenderer.act(() => { findButtons(renderer, '取消')[0].props.onClick() })
    assert.equal(await p, false, '点取消应 resolve false')
  } finally {
    cleanup(renderer)
  }
})

test('confirm：点遮罩视为取消 resolve(false)，点面板内部不结算', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '确认关闭？' }))
    // 先触发面板自身的 onClick（stopPropagation 处理器）——它只拦截冒泡
    // 不结算，Promise 应保持 pending（真实浏览器中点击面板内任意元素
    // 事件冒泡到面板后被 stopPropagation 截断，不会到达遮罩）
    let settled = null
    p.then((v) => { settled = v })
    const panel = findByClass(renderer, 'dialog')[0]
    TestRenderer.act(() => { panel.props.onClick({ stopPropagation() {} }) })
    await Promise.resolve() // 推进微任务，检查是否有意外结算
    assert.equal(settled, null, '触发面板 stopPropagation 不应结算对话框')
    // 点遮罩 → resolve(false)
    TestRenderer.act(() => { findByClass(renderer, 'modal-overlay')[0].props.onClick() })
    assert.equal(await p, false, '点遮罩应 resolve false')
  } finally {
    cleanup(renderer)
  }
})

test('confirm：× 按钮与 Esc 键均视为取消 resolve(false)', async () => {
  let renderer = null
  try {
    installDocumentMock()
    renderer = renderHost()
    const p1 = open(() => dialog.confirmDialog({ message: '第一次确认' }))
    TestRenderer.act(() => { findButtons(renderer, '×')[0].props.onClick() })
    assert.equal(await p1, false, '点 × 应 resolve false')

    const p2 = open(() => dialog.confirmDialog({ message: '第二次确认' }))
    assert.ok(keyHandler, '挂载后应注册 Esc 键监听')
    TestRenderer.act(() => { keyHandler({ key: 'Escape' }) })
    assert.equal(await p2, false, '按 Esc 应 resolve false')
  } finally {
    cleanup(renderer)
  }
})

// ---- alert 形态 ----

test('alert：单「确定」按钮，点确定/遮罩/Esc 均 resolve(undefined)', async () => {
  let renderer = null
  try {
    installDocumentMock()
    renderer = renderHost()
    const p1 = open(() => dialog.alertDialog({ message: '操作成功！' }))
    const text = textOf(renderer.root)
    assert.match(text, /操作成功！/, '应渲染消息')
    assert.match(text, /提示/, '未传标题时应显示默认标题「提示」')
    assert.equal(findButtons(renderer, '取消').length, 0, '提示形态不应有取消按钮')
    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p1, undefined, '点确定应 resolve undefined')

    const p2 = open(() => dialog.alertDialog({ message: '第二条提示' }))
    TestRenderer.act(() => { findByClass(renderer, 'modal-overlay')[0].props.onClick() })
    assert.equal(await p2, undefined, '点遮罩应 resolve undefined')

    const p3 = open(() => dialog.alertDialog({ message: '第三条提示' }))
    TestRenderer.act(() => { keyHandler({ key: 'Escape' }) })
    assert.equal(await p3, undefined, '按 Esc 应 resolve undefined')
  } finally {
    cleanup(renderer)
  }
})

// ---- 样式与文案定制 ----

test('danger 参数：确定按钮使用 btn-danger 危险样式', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({
      message: '删除仓库', danger: true, confirmText: '删除', cancelText: '再想想',
    }))
    const okBtn = findButtons(renderer, '删除')[0]
    assert.ok(okBtn.props.className.includes('btn-danger'), '危险操作确定按钮应为 btn-danger')
    assert.ok(!okBtn.props.className.includes('btn-primary'), '危险按钮不应同时使用 btn-primary')
    assert.equal(findButtons(renderer, '再想想').length, 1, '应渲染自定义取消文案')
    TestRenderer.act(() => { okBtn.props.onClick() })
    assert.equal(await p, true)
  } finally {
    cleanup(renderer)
  }
})

test('非危险 confirm 确定按钮为 btn-primary 普通样式', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '重试任务？' }))
    const okBtn = findButtons(renderer, '确定')[0]
    assert.ok(okBtn.props.className.includes('btn-primary'), '普通确认应为 btn-primary')
    assert.ok(!okBtn.props.className.includes('btn-danger'), '普通确认不应使用危险样式')
    TestRenderer.act(() => { okBtn.props.onClick() })
    assert.equal(await p, true)
  } finally {
    cleanup(renderer)
  }
})

test('自定义标题渲染；消息支持多行（pre-line 样式）', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({
      title: '恢复备份',
      message: '恢复将覆盖现有数据。\n\n备份：demo.tar.gz',
    }))
    const text = textOf(renderer.root)
    assert.match(text, /恢复备份/, '应渲染自定义标题')
    assert.match(text, /恢复将覆盖现有数据/, '应渲染消息第一行')
    assert.match(text, /备份：demo\.tar\.gz/, '应渲染消息第二行')
    const msgNode = findByClass(renderer, 'dialog-message')[0]
    assert.ok(
      msgNode.props.className.includes('dialog-message'),
      '消息容器应使用 dialog-message 样式类（配合 pre-line 保留换行）',
    )
    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p, true)
  } finally {
    cleanup(renderer)
  }
})

// ---- 边界场景 ----

test('边界：空消息与空标题正常渲染不崩溃', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '' }))
    assert.match(textOf(renderer.root), /请确认/, '空消息时也应显示默认标题')
    assert.equal(findButtons(renderer, '取消').length, 1)
    assert.equal(findButtons(renderer, '确定').length, 1)
    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p, true)
  } finally {
    cleanup(renderer)
  }
})

test('边界：alert 空消息正常渲染不崩溃', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.alertDialog({}))
    assert.match(textOf(renderer.root), /提示/, '无消息时应显示默认标题「提示」')
    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p, undefined)
  } finally {
    cleanup(renderer)
  }
})

test('边界：排队——连续两次调用只显示第一个，结算后第二个出现', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    let p2 = null
    const p1 = open(() => {
      const first = dialog.confirmDialog({ message: '第一个对话框' })
      p2 = dialog.confirmDialog({ message: '第二个对话框' })
      return first
    })
    const text = textOf(renderer.root)
    assert.match(text, /第一个对话框/, '应先显示第一个对话框')
    assert.doesNotMatch(text, /第二个对话框/, '第二个对话框应排队等待')
    TestRenderer.act(() => { findButtons(renderer, '确定')[0].props.onClick() })
    assert.equal(await p1, true, '第一个结算 true')
    assert.match(textOf(renderer.root), /第二个对话框/, '第一个结算后应显示第二个')
    TestRenderer.act(() => { findButtons(renderer, '取消')[0].props.onClick() })
    assert.equal(await p2, false, '第二个结算 false')
  } finally {
    cleanup(renderer)
  }
})

test('边界：同一对话框重复点击确定只结算一次', async () => {
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '只结算一次' }))
    TestRenderer.act(() => {
      findButtons(renderer, '确定')[0].props.onClick()
      findButtons(renderer, '确定')[0].props.onClick()
    })
    assert.equal(await p, true, '重复点击后 Promise 仍只按首次结算')
    assert.equal(findByClass(renderer, 'modal-overlay').length, 0, '对话框应已关闭')
  } finally {
    cleanup(renderer)
  }
})

// ---- 无宿主兜底（单元测试渲染单个页面组件时 DialogHost 未挂载）----

test('无宿主：installAutoAnswer 直接应答并记录消息（测试注入）', async () => {
  const messages = []
  dialog.resetDialogs()
  dialog.installAutoAnswer((opts) => {
    messages.push({ kind: opts.kind, message: opts.message })
    return false
  })
  const r = await dialog.confirmDialog({ message: '注入应答' })
  assert.equal(r, false, '注入返回 false 时应 resolve false')
  assert.deepEqual(messages, [{ kind: 'confirm', message: '注入应答' }],
    '注入函数应收到对话框配置（含消息）')
  dialog.installAutoAnswer(() => true)
  assert.equal(await dialog.confirmDialog({ message: 'x' }), true, '注入返回 true 时应 resolve true')
})

test('无宿主且无注入：保守按「取消」应答，不悬挂', async () => {
  dialog.resetDialogs()
  dialog.installAutoAnswer(null)
  assert.equal(await dialog.confirmDialog({ message: 'x' }), false,
    'confirm 无宿主时应保守 resolve false（操作被取消，安全优先）')
  assert.equal(await dialog.alertDialog({ message: 'x' }), undefined,
    'alert 无宿主时应 resolve undefined')
  assert.equal(dialog.currentDialog(), null, '兜底应答后队列不应残留')
})

test('resetDialogs：清空残留队列并全部按取消结算', async () => {
  dialog.resetDialogs()
  dialog.installAutoAnswer(null)
  let renderer = null
  try {
    renderer = renderHost()
    const p = open(() => dialog.confirmDialog({ message: '将被清理' }))
    dialog.resetDialogs()
    assert.equal(await p, false, 'resetDialogs 应把未结算对话框按取消结算')
    assert.equal(dialog.currentDialog(), null, '队列应清空')
    // 清理后宿主重渲染不应崩溃
    TestRenderer.act(() => {})
  } finally {
    cleanup(renderer)
  }
})
