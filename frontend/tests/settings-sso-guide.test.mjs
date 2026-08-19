// 设置页内嵌 SSO 配置指南测试（issue #27 第六轮）。
//
// 背景：用户反馈「平台的使用者看不到本地文档 docs/Synology-SSO-配置指南.md，
// 所以直接把文档内容显示在设置页面上，并优化提示文字」。SSO 卡片底部说明此前
// 指向代码仓库内的本地文档路径（部署环境中使用者无法查看），改为：设置页
// 从 GET /api/settings/sso-guide 拉取指南 Markdown（后端读 docs/ 单一来源），
// SSO 卡片内提供「查看 SSO 配置指南」折叠区直接展示渲染后的文档。
//
// 修复前状态（本测试应当失败）：
// - SSO 卡片说明文字仍写「群晖侧详细步骤见 docs/Synology-SSO-配置指南.md」
// - 无指南折叠区、无 /api/settings/sso-guide 请求
// - 登录有效期输入框 fallback 为 7（用户第三轮已确认默认 30 天）
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// issue #201 拆分：SSO 卡片 JSX 移到 components/settings/SsoCard.jsx，
// 指南拉取（useEffect）收敛到 hooks/useSettingsData.js——静态断言跟随新文件
const ssoCard = readFileSync(path.join(ROOT, 'src/components/settings/SsoCard.jsx'), 'utf8')
const hook = readFileSync(path.join(ROOT, 'src/hooks/useSettingsData.js'), 'utf8')
const settingsSrc = ssoCard + '\n' + hook

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-sso-save-button.test.mjs 同款工具） */
function cardSource(src, title) {
  const re = new RegExp(`<div className="card">\\s*<h2>${title}<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

test('SSO 卡片说明文字不得再指向本地 docs/ 文件路径（使用者看不到）', () => {
  const ssoCard = cardSource(settingsSrc, 'Synology SSO 登录')
  assert.ok(ssoCard, '设置页应存在「Synology SSO 登录」配置卡片')
  assert.doesNotMatch(
    ssoCard,
    /docs\/Synology-SSO-配置指南\.md/,
    '说明文字不应再让使用者去查看仓库本地文档（部署环境中不存在该文件）',
  )
})

test('SSO 卡片内应提供页面内「配置指南」折叠区与展开按钮', () => {
  const ssoCard = cardSource(settingsSrc, 'Synology SSO 登录')
  assert.ok(ssoCard, '设置页应存在「Synology SSO 登录」配置卡片')
  assert.match(
    ssoCard,
    /查看 SSO 配置指南/,
    'SSO 卡片内应有「查看 SSO 配置指南」按钮（文档内容直接显示在设置页）',
  )
  assert.match(
    ssoCard,
    /<Markdown /,
    '展开后应使用 Markdown 组件渲染指南文档内容',
  )
  assert.match(
    settingsSrc,
    /api\.get\('\/api\/settings\/sso-guide'\)/,
    '设置页挂载时应从后端拉取指南内容（后端读取 docs/ 单一文档来源）',
  )
})

test('登录有效期默认值应为 30 天（用户第三轮确认，历史实现误为 7）', () => {
  assert.match(
    settingsSrc,
    /session_days \?\? 30/,
    'session_days 输入框 fallback 应为 30（与后端 config 默认一致）',
  )
})

test('渲染：挂载后拉取指南，点击展开后文档内容显示在页面上', async () => {
  const requested = []
  const originalFetch = global.fetch
  global.fetch = async (path) => {
    requested.push(String(path))
    if (String(path).startsWith('/api/settings/sso-guide')) {
      return { ok: true, status: 200, json: async () => ({ content: '# Synology SSO 登录配置指南\n\n群晖 SSO Server 侧配置步骤。' }) }
    }
    if (String(path).startsWith('/api/settings')) {
      return {
        ok: true, status: 200, json: async () => ({
          sso: { enabled: false, well_known_url: '', client_id: '', scope: '', session_days: null, redirect_uri: '', verify_ssl: true },
          worker: {}, claude: { command: 'claude', args: [] }, ui: { timezone: '' },
          notifications: {}, gitlab: {}, env: {},
        }),
      }
    }
    if (String(path).startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-13 00:00:00' }) }
    }
    if (String(path).startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  try {
    let renderer = null
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    try {
      assert.ok(
        requested.some((p) => p.startsWith('/api/settings/sso-guide')),
        `挂载时应拉取 SSO 配置指南：${requested.join(', ')}`
      )
      // 默认收起：文档标题不应出现在渲染树中
      let tree = JSON.stringify(renderer.toJSON())
      assert.doesNotMatch(tree, /Synology SSO 登录配置指南/, '默认收起时不应渲染指南内容')

      // 点击「查看 SSO 配置指南」按钮 → 展开显示文档
      const buttons = renderer.root.findAllByType('button')
      const guideBtn = buttons.find((b) => b.props.children === '查看 SSO 配置指南')
      assert.ok(guideBtn, '应存在「查看 SSO 配置指南」按钮')
      await TestRenderer.act(async () => {
        guideBtn.props.onClick()
        await new Promise((resolve) => setTimeout(resolve, 10))
      })
      tree = JSON.stringify(renderer.toJSON())
      assert.match(tree, /Synology SSO 登录配置指南/, '展开后应显示渲染后的文档内容')
      assert.match(tree, /群晖 SSO Server 侧配置步骤/, '展开后应显示文档正文')
    } finally {
      await TestRenderer.act(() => renderer.unmount())
    }
  } finally {
    global.fetch = originalFetch
  }
})
