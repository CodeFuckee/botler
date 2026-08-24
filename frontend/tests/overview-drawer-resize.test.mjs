// 概览页右侧边栏拖拽调整宽度测试（issue #466）：
// issue 详情右边栏 / CI/CD 流水线右边栏 / 灵感 AI 对话右边栏，在视口宽度
// 足够（>860px，项目移动断点）的情况下，用户可拖动右边栏左侧改变宽度。
//
// 实现：ResizableDrawer 容器（复用 .drawer 右侧抽屉体系）+ useDrawerResize
// hook——抽屉左缘渲染 8px 拖拽手柄（cursor: col-resize），拖拽用 Pointer
// Events（window 级监听），宽度 clamp 到 [320, 92vw]，拖拽结束写入
// localStorage（按抽屉类型区分 key）持久化；键盘 ArrowLeft/ArrowRight
// 步进 16px 调整（role="separator" 无障碍支持）。
//
// 断言：
// 1. 纯函数：canResizeDrawer（860 不可调 / 861 可调 / 空值回退）、
//    drawerMaxWidth（floor(92vw) 且不低于 320）、clampDrawerWidth
//    （上下界钳制 / 非法输入回退 320）、parseStoredDrawerWidth（非法
//    JSON / 非数字回退 null，越界钳制）；
// 2. 三个右边栏（IssueDrawer / PipelineDrawer / InspirationSection 聊天
//    抽屉）均使用 ResizableDrawer 且带各自 storageKey；
// 3. styles.css 提供 .drawer-resize-handle（col-resize / touch-action:
//    none / 绝对定位左缘）；
// 4. 组件渲染：宽视口渲染手柄，窄视口（≤860）不渲染；
// 5. 拖拽流：pointerdown → pointermove → pointerup 更新宽度并持久化；
// 6. 键盘流：ArrowRight/ArrowLeft 步进 16px 并持久化；
// 7. localStorage 已有宽度时挂载即应用（钳制后）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与既有测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
after(() => vite.close())

const hook = await vite.ssrLoadModule('/src/hooks/useDrawerResize.js')
const { default: ResizableDrawer } =
  await vite.ssrLoadModule('/src/components/ResizableDrawer.jsx')
const issueDrawerSrc = readFileSync(
  path.join(ROOT, 'src/components/IssueDrawer.jsx'), 'utf8')
const pipelineDrawerSrc = readFileSync(
  path.join(ROOT, 'src/components/PipelineDrawer.jsx'), 'utf8')
const inspirationSrc = readFileSync(
  path.join(ROOT, 'src/components/overview/InspirationSection.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

const {
  canResizeDrawer, drawerMaxWidth, clampDrawerWidth, parseStoredDrawerWidth,
  DRAWER_RESIZE_MIN_VIEWPORT, DRAWER_RESIZE_MIN_WIDTH, DRAWER_RESIZE_STEP,
  ISSUE_DRAWER_WIDTH_KEY, PIPELINE_DRAWER_WIDTH_KEY, CHAT_DRAWER_WIDTH_KEY,
} = hook

// ---- 测试辅助：模拟 window（innerWidth + localStorage + 监听记录）----
function installFakeWindow({ innerWidth = 1440, stored = {} } = {}) {
  const listeners = new Map()
  const win = {
    innerWidth,
    addEventListener(type, cb) {
      const arr = listeners.get(type) || []
      arr.push(cb)
      listeners.set(type, arr)
    },
    removeEventListener(type, cb) {
      const arr = listeners.get(type) || []
      const i = arr.indexOf(cb)
      if (i >= 0) arr.splice(i, 1)
    },
    localStorage: {
      getItem: (k) => (k in stored ? stored[k] : null),
      setItem: (k, v) => { stored[k] = String(v) },
    },
  }
  const hadWin = 'window' in globalThis
  const orig = globalThis.window
  globalThis.window = win
  return {
    listenersOf(type) { return listeners.get(type) || [] },
    stored,
    restore() {
      if (hadWin) globalThis.window = orig
      else delete globalThis.window
    },
  }
}

async function renderDrawer({ innerWidth = 1440, stored = {} } = {}) {
  const fake = installFakeWindow({ innerWidth, stored })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(
          ResizableDrawer,
          { drawerClass: 'issue-drawer', storageKey: ISSUE_DRAWER_WIDTH_KEY },
          React.createElement('div', null, '抽屉内容')
        )
      )
    })
  } catch (e) {
    fake.restore()
    throw e
  }
  return { renderer, fake }
}

function findHandle(renderer) {
  return renderer.root.findAll(
    (n) => String(n.props.className || '').startsWith('drawer-resize-handle')
  )[0]
}

function findDrawer(renderer) {
  return renderer.root.findAll(
    (n) => String(n.props.className || '') === 'drawer issue-drawer'
  )[0]
}

// ---- 纯函数：宽度计算与合法性判定 ----

test('canResizeDrawer：860px 断点——>860 可调，≤860 与空值不可调', () => {
  assert.equal(DRAWER_RESIZE_MIN_VIEWPORT, 861, '应跟随项目 860px 移动断点')
  assert.equal(canResizeDrawer(861), true)
  assert.equal(canResizeDrawer(1440), true)
  assert.equal(canResizeDrawer(860), false, '860 属于窄视口，不渲染手柄')
  assert.equal(canResizeDrawer(0), false)
  assert.equal(canResizeDrawer(undefined), false, 'SSR/无 window 应回退不可调')
  assert.equal(canResizeDrawer(null), false)
  assert.equal(canResizeDrawer('900'), false, '非数字类型应回退不可调')
})

test('drawerMaxWidth：92vw 上限且不低于最小宽度 320', () => {
  assert.equal(drawerMaxWidth(1440), Math.floor(1440 * 0.92), '1440 → 1324')
  assert.equal(drawerMaxWidth(200), 320, '92vw 小于 320 时保底 320')
  assert.equal(drawerMaxWidth(undefined), 320, '空视口保底 320（SSR 安全）')
})

test('clampDrawerWidth：上下界钳制，非法输入回退最小宽度', () => {
  assert.equal(clampDrawerWidth(500, 1440), 500, '正常值不变')
  assert.equal(clampDrawerWidth(100, 1440), 320, '低于 320 钳到 320')
  assert.equal(clampDrawerWidth(2000, 1440), Math.floor(1440 * 0.92),
    '超过 92vw 钳到上限')
  assert.equal(clampDrawerWidth(NaN, 1440), 320, 'NaN 回退 320')
  assert.equal(clampDrawerWidth(null, 1440), 320, 'null 回退 320')
  assert.equal(clampDrawerWidth(undefined, 1440), 320, 'undefined 回退 320')
  assert.equal(clampDrawerWidth(500, 860), 500, '窄视口内正常值保持（max≥320）')
})

test('parseStoredDrawerWidth：非法存储回退 null，越界值钳制', () => {
  assert.equal(parseStoredDrawerWidth('500', 1440), 500)
  assert.equal(parseStoredDrawerWidth('100', 1440), 320, '低于下限钳制')
  assert.equal(parseStoredDrawerWidth('9999', 1440), Math.floor(1440 * 0.92),
    '超过上限钳制')
  assert.equal(parseStoredDrawerWidth('abc', 1440), null, '非法 JSON 回退 null')
  assert.equal(parseStoredDrawerWidth('"500"', 1440), null, 'JSON 字符串非数字回退 null')
  assert.equal(parseStoredDrawerWidth(null, 1440), null, '空值回退 null')
  assert.equal(parseStoredDrawerWidth('', 1440), null, '空串回退 null')
})

// ---- 源码断言：三个右边栏接入 ResizableDrawer ----

test('IssueDrawer 使用 ResizableDrawer（issue 详情右边栏）', () => {
  assert.match(issueDrawerSrc, /import ResizableDrawer/, '应导入 ResizableDrawer')
  assert.match(issueDrawerSrc,
    /<ResizableDrawer\s+drawerClass="issue-drawer"/,
    '抽屉主体应替换为 ResizableDrawer（className 保留 issue-drawer）')
  assert.match(issueDrawerSrc,
    /storageKey=\{ISSUE_DRAWER_WIDTH_KEY\}/,
    '应传入 issue 抽屉专用宽度存储 key')
})

test('PipelineDrawer 使用 ResizableDrawer（流水线右边栏）', () => {
  assert.match(pipelineDrawerSrc, /import ResizableDrawer/, '应导入 ResizableDrawer')
  assert.match(pipelineDrawerSrc,
    /<ResizableDrawer\s+drawerClass="pipeline-drawer"/,
    '抽屉主体应替换为 ResizableDrawer（className 保留 pipeline-drawer）')
  assert.match(pipelineDrawerSrc,
    /storageKey=\{PIPELINE_DRAWER_WIDTH_KEY\}/,
    '应传入流水线抽屉专用宽度存储 key')
})

test('灵感 AI 对话抽屉使用 ResizableDrawer（chat-drawer）', () => {
  assert.match(inspirationSrc, /import ResizableDrawer/, '应导入 ResizableDrawer')
  assert.match(inspirationSrc,
    /<ResizableDrawer\s+drawerClass="chat-drawer"/,
    '聊天抽屉主体应替换为 ResizableDrawer（className 保留 chat-drawer）')
  assert.match(inspirationSrc,
    /storageKey=\{CHAT_DRAWER_WIDTH_KEY\}/,
    '应传入聊天抽屉专用宽度存储 key')
})

test('存储 key 与步进常量符合约定', () => {
  assert.equal(ISSUE_DRAWER_WIDTH_KEY, 'botler.overview.drawerWidth.issue')
  assert.equal(PIPELINE_DRAWER_WIDTH_KEY, 'botler.overview.drawerWidth.pipeline')
  assert.equal(CHAT_DRAWER_WIDTH_KEY, 'botler.overview.drawerWidth.chat')
  assert.equal(DRAWER_RESIZE_STEP, 16, '键盘步进 16px')
  assert.equal(DRAWER_RESIZE_MIN_WIDTH, 320, '最小宽度 320')
})

test('styles.css 提供 .drawer-resize-handle（col-resize + touch-action none）', () => {
  const rule = styles.match(/^\.drawer-resize-handle\s*\{[^}]*\}/m)
  assert.ok(rule, 'styles.css 缺少 .drawer-resize-handle 规则')
  assert.match(rule[0], /position\s*:\s*absolute/, '手柄绝对定位于抽屉左缘')
  assert.match(rule[0], /cursor\s*:\s*col-resize/, '拖拽光标 col-resize')
  assert.match(rule[0], /touch-action\s*:\s*none/, 'touch-action none（指针事件）')
  assert.match(rule[0], /left\s*:\s*0/, '贴左缘')
  assert.match(rule[0], /top\s*:\s*0/, '整高贴顶')
  assert.match(rule[0], /bottom\s*:\s*0/, '整高贴底')
})

// ---- 组件渲染与交互 ----

test('宽视口（>860px）渲染拖拽手柄，窄视口（≤860px）不渲染', async () => {
  const wide = await renderDrawer({ innerWidth: 1440 })
  try {
    assert.ok(findHandle(wide.renderer), '1440px 视口应渲染拖拽手柄')
    assert.ok(findDrawer(wide.renderer), '应渲染 .drawer issue-drawer 容器')
    const h = findHandle(wide.renderer)
    assert.equal(h.props.role, 'separator', '手柄应为 separator 角色（无障碍）')
    assert.equal(h.props['aria-orientation'], 'vertical')
    assert.equal(h.props.tabIndex, 0, '手柄可聚焦支持键盘调整')
  } finally {
    wide.fake.restore()
  }
  const narrow = await renderDrawer({ innerWidth: 800 })
  try {
    assert.equal(findHandle(narrow.renderer), undefined,
      '800px 视口不应渲染拖拽手柄（宽度不足）')
    const drawer = findDrawer(narrow.renderer)
    assert.equal(drawer.props.style, undefined, '窄视口不注入内联宽度，走 CSS 默认')
  } finally {
    narrow.fake.restore()
  }
})

test('localStorage 已有宽度时挂载即应用（越界钳制）', async () => {
  const { renderer, fake } = await renderDrawer({
    innerWidth: 1440,
    stored: { [ISSUE_DRAWER_WIDTH_KEY]: '500' },
  })
  try {
    assert.equal(findDrawer(renderer).props.style.width, 500, '存储宽度应应用为内联宽度')
  } finally {
    fake.restore()
  }
})

test('拖拽流：pointerdown → move → up 调整宽度并持久化', async () => {
  const { renderer, fake } = await renderDrawer({ innerWidth: 1440 })
  try {
    const handle = findHandle(renderer)
    // 初始无存储宽度：startWidth 回退 DRAWER_RESIZE_MIN_WIDTH（320）
    await TestRenderer.act(async () => {
      handle.props.onPointerDown({ clientX: 900, preventDefault() {} })
    })
    const moveCbs = fake.listenersOf('pointermove')
    const upCbs = fake.listenersOf('pointerup')
    assert.ok(moveCbs.length >= 1, '拖拽中应注册 window pointermove 监听')
    assert.ok(upCbs.length >= 1, '拖拽中应注册 window pointerup 监听')
    await TestRenderer.act(async () => {
      // 从 900 向左拖到 500 → 宽度 +400：320 + 400 = 720
      moveCbs[moveCbs.length - 1]({ clientX: 500 })
    })
    assert.equal(findDrawer(renderer).props.style.width, 720)
    await TestRenderer.act(async () => {
      const ups = fake.listenersOf('pointerup')
      ups[ups.length - 1]()
    })
    assert.equal(fake.stored[ISSUE_DRAWER_WIDTH_KEY], '720',
      '拖拽结束应把宽度写入 localStorage')
    assert.equal(findHandle(renderer).props.className, 'drawer-resize-handle',
      '拖拽结束 dragging 类应移除')
  } finally {
    fake.restore()
  }
})

test('拖拽超界钳制：拖出 92vw 上限不越界', async () => {
  const { renderer, fake } = await renderDrawer({ innerWidth: 1440 })
  try {
    const handle = findHandle(renderer)
    await TestRenderer.act(async () => {
      handle.props.onPointerDown({ clientX: 900, preventDefault() {} })
    })
    const moveCbs = fake.listenersOf('pointermove')
    await TestRenderer.act(async () => {
      // 拖到最左（clientX 0）→ 宽度 320 + 900 = 1220（< 1324，未超上限）
      moveCbs[moveCbs.length - 1]({ clientX: 0 })
    })
    assert.equal(findDrawer(renderer).props.style.width, 1220)
    await TestRenderer.act(async () => {
      fake.listenersOf('pointerup').slice(-1)[0]()
    })
    assert.equal(fake.stored[ISSUE_DRAWER_WIDTH_KEY], '1220')
  } finally {
    fake.restore()
  }
})

test('键盘流：ArrowRight / ArrowLeft 步进 16px 并持久化', async () => {
  const { renderer, fake } = await renderDrawer({ innerWidth: 1440 })
  try {
    let handle = findHandle(renderer)
    await TestRenderer.act(async () => {
      handle.props.onKeyDown({ key: 'ArrowRight', preventDefault() {} })
    })
    assert.equal(findDrawer(renderer).props.style.width, 320 + 16,
      'ArrowRight 应在最小宽度基础上 +16')
    handle = findHandle(renderer)
    await TestRenderer.act(async () => {
      handle.props.onKeyDown({ key: 'ArrowRight', preventDefault() {} })
    })
    assert.equal(findDrawer(renderer).props.style.width, 320 + 32)
    handle = findHandle(renderer)
    await TestRenderer.act(async () => {
      handle.props.onKeyDown({ key: 'ArrowLeft', preventDefault() {} })
    })
    assert.equal(findDrawer(renderer).props.style.width, 320 + 16)
    assert.equal(fake.stored[ISSUE_DRAWER_WIDTH_KEY], String(320 + 16),
      '键盘调整也应持久化')
  } finally {
    fake.restore()
  }
})

test('无 storageKey：不持久化但拖拽仍可用（手柄渲染）', async () => {
  // 独立渲染无 storageKey 的 ResizableDrawer（服务端下发的静态抽屉等场景）
  const fake = installFakeWindow({ innerWidth: 1440 })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(
          ResizableDrawer,
          { drawerClass: 'chat-drawer' },
          React.createElement('div', null, '内容')
        )
      )
    })
    const handle = findHandle(renderer)
    assert.ok(handle, '无 storageKey 也应渲染手柄')
    await TestRenderer.act(async () => {
      handle.props.onPointerDown({ clientX: 900, preventDefault() {} })
    })
    const moveCbs = fake.listenersOf('pointermove')
    await TestRenderer.act(async () => {
      moveCbs[moveCbs.length - 1]({ clientX: 500 })
    })
    assert.equal(
      renderer.root.findAll(
        (n) => String(n.props.className || '') === 'drawer chat-drawer'
      )[0].props.style.width, 720, '拖拽应正常调整宽度')
    assert.equal(Object.keys(fake.stored).length, 0, '无 storageKey 不应写 localStorage')
  } finally {
    fake.restore()
  }
})

test('无 window（SSR/Node 环境）：渲染不崩溃且不渲染手柄', async () => {
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(
        React.createElement(
          ResizableDrawer,
          { drawerClass: 'issue-drawer', storageKey: ISSUE_DRAWER_WIDTH_KEY },
          React.createElement('div', null, '内容')
        )
      )
    })
    assert.equal(findHandle(renderer), undefined, '无 window 视为窄视口，不渲染手柄')
    const drawer = renderer.root.findAll(
      (n) => String(n.props.className || '') === 'drawer issue-drawer'
    )[0]
    assert.equal(drawer.props.style, undefined, '无 window 不注入内联宽度')
  } finally {
    // 无 window 时无需清理（确保测试环境未污染）
    if (renderer) await TestRenderer.act(() => renderer.unmount())
  }
})

test('键盘下界钳制：ArrowLeft 到 320 后不再减小', async () => {
  const { renderer, fake } = await renderDrawer({
    innerWidth: 1440,
    stored: { [ISSUE_DRAWER_WIDTH_KEY]: '320' },
  })
  try {
    let handle = findHandle(renderer)
    await TestRenderer.act(async () => {
      handle.props.onKeyDown({ key: 'ArrowLeft', preventDefault() {} })
    })
    assert.equal(findDrawer(renderer).props.style.width, 320,
      '已达最小宽度时 ArrowLeft 不越界')
    assert.equal(fake.stored[ISSUE_DRAWER_WIDTH_KEY], '320')
  } finally {
    fake.restore()
  }
})

// ---- issue #475：拖拽手柄不得随抽屉内容滚动 ----
// 背景：.drawer 自身是滚动容器（overflow-y: auto），若手柄绝对定位在
// .drawer 内（.drawer 同时是 position: relative 定位参考系），抽屉内容
// 滚动时手柄会一起滚走（页面下方只剩一半手柄）。修复：ResizableDrawer
// 外层套 .drawer-shell（非滚动定位外壳），手柄渲染在 .drawer 之外、
// 外壳之内——滚动容器与定位参考系解耦。
test('拖拽手柄渲染在 .drawer 滚动容器之外（.drawer-shell 内）', async () => {
  const { renderer, fake } = await renderDrawer({ innerWidth: 1440 })
  try {
    const handle = findHandle(renderer)
    assert.ok(handle, '宽视口应渲染拖拽手柄')
    const drawer = findDrawer(renderer)
    assert.ok(drawer, '应渲染 .drawer issue-drawer 容器')
    // 手柄不应是 .drawer 的后代（否则会随抽屉内容滚动）
    const inDrawer = drawer.findAll(
      (n) => String(n.props.className || '').startsWith('drawer-resize-handle')
    )
    assert.equal(inDrawer.length, 0, '手柄不得位于滚动容器 .drawer 内部')
    // 手柄应在 .drawer-shell 外壳内（外壳是 position: relative 的非滚动参考系）
    const shell = renderer.root.findAll(
      (n) => String(n.props.className || '') === 'drawer-shell'
    )
    assert.ok(shell.length > 0, '应渲染 .drawer-shell 外壳')
  } finally {
    fake.restore()
  }
})

test('styles.css 提供 .drawer-shell（非滚动定位外壳，手柄定位参考系）', () => {
  const shellRule = styles.match(/^\.drawer-shell\s*\{([^}]*)\}/m)
  assert.ok(shellRule, 'styles.css 缺少 .drawer-shell 规则')
  assert.match(shellRule[1], /position\s*:\s*relative/, '外壳应 position: relative（手柄定位参考系）')
  assert.doesNotMatch(shellRule[1], /overflow/, '外壳自身不应滚动（滚动由内部 .drawer 承担）')
})
