// 图标统一组件测试（issue #177）：项目内所有 UI 图标改用 Lucide 系列。
// - ICONS 语义映射：每个语义名对应一个 Lucide 组件，渲染产出 <svg>；
// - issue #183 新增 Web 终端后补登 terminal 图标（terminal: TerminalIcon），
//   同步语义名清单；
// - Icon 组件：按 name 渲染对应图标；未知名回退 ×（不渲染空白）；
// - size / aria-label 等 props 透传到 svg；装饰性图标默认 aria-hidden。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { Icon, ICONS } = await vite.ssrLoadModule('/src/components/Icon.jsx')

after(() => vite.close())

function renderIcon(name, props) {
  let renderer = null
  TestRenderer.act(() => {
    renderer = TestRenderer.create(React.createElement(Icon, { name, ...props }))
  })
  return renderer
}

// 语义名清单：与 Icon.jsx 中 ICONS 映射键一一对应（防漏配）
// issue #188：新增 download（logo 下载）/ image（logo 占位）/
// sparkles（生成图标按钮）三个语义名，同步补入清单
const EXPECTED_NAMES = [
  'arrowLeft', 'arrowUp', 'bot', 'brain', 'check', 'checkCircle',
  'chevronDown', 'chevronRight', 'clipboard', 'coins', 'download',
  'externalLink', 'flag', 'folder', 'folderOpen', 'hourglass', 'image',
  'lightbulb', 'lock', 'message', 'mic', 'package', 'pencil', 'pin',
  'plus', 'refresh', 'rocket', 'search', 'settings', 'sparkles', 'square',
  'tag', 'terminal', 'trash', 'user', 'wallet', 'warning', 'wrench',
  'x', 'xCircle',
]

test('ICONS 映射完整：全部语义名齐全且渲染为 svg', () => {
  for (const name of EXPECTED_NAMES) {
    assert.ok(ICONS[name], `ICONS 应包含语义名 ${name}`)
    const renderer = renderIcon(name)
    try {
      const json = renderer.toJSON()
      assert.equal(json.type, 'svg', `${name} 应渲染为 svg`)
      assert.match(String(json.props.className || ''), /^lucide /,
                   `${name} 应带 lucide 样式类`)
    } finally {
      TestRenderer.act(() => renderer.unmount())
    }
  }
  // 映射键集合与预期清单一致（无多余、无遗漏）
  assert.deepEqual(Object.keys(ICONS).sort(), [...EXPECTED_NAMES].sort(),
                   'ICONS 键集合应与语义清单完全一致')
})

test('Icon 组件：未知 name 回退 × 图标（不渲染空白）', () => {
  const renderer = renderIcon('not-exist-icon')
  try {
    const json = renderer.toJSON()
    assert.equal(json.type, 'svg', '未知名也应渲染 svg（回退图标）')
    assert.match(String(json.props.className || ''), /lucide-x/,
                 '未知名应回退到 Lucide X 图标')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('Icon 组件：size / aria-label 等 props 透传到 svg', () => {
  const renderer = renderIcon('bot', { size: 18, 'aria-label': '机器人' })
  try {
    const json = renderer.toJSON()
    assert.equal(json.props.width, 18, 'size 应透传为 svg width')
    assert.equal(json.props.height, 18, 'size 应透传为 svg height')
    assert.equal(json.props['aria-label'], '机器人', 'aria-label 应透传')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})

test('Icon 组件：未传 size 时保持 Lucide 默认 24px', () => {
  const renderer = renderIcon('check')
  try {
    const json = renderer.toJSON()
    assert.equal(json.props.width, 24, '默认宽度应为 Lucide 默认 24')
    assert.equal(json.props.height, 24, '默认高度应为 Lucide 默认 24')
  } finally {
    TestRenderer.act(() => renderer.unmount())
  }
})
