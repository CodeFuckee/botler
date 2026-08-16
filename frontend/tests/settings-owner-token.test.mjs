// 设置页 Owner GitLab Token 配置测试（issue #87）：设置页增加 owner
// gitlab token 设置，这个 token 专门用来编辑 issue，严禁使用它推送代码
// 以及处理流水线；同时页面显示「如何申请一个只有处理 issue 权限的
// token」的教程。
//
// 断言：
// 1. 设置页挂载「Owner GitLab Token」卡片（GitLab 凭据卡片之前）；
// 2. 卡片含密码输入框（掩码占位提示 + 留空 = 保持现有）与独立保存按钮；
// 3. 保存只提交 {gitlab: {owner_token}} 段（部分更新，不触碰其他设置）；
// 4. 卡片说明明确该 token 仅用于编辑 issue、严禁推送代码/处理流水线；
// 5. 卡片内提供「查看 token 申请教程」折叠区（GET /api/settings/owner-token-guide
//    + Markdown 渲染，与 SSO 指南同模式）；
// 6. 渲染：已配置掩码显示在 placeholder；输入 token 点保存 → PUT 请求
//    携带 {gitlab:{owner_token}}。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settingsSrc = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: Settings } = await vite.ssrLoadModule('/src/pages/Settings.jsx')

after(() => vite.close())

/** 提取指定标题卡片的源码片段（与 settings-sso-save-button.test.mjs 同款工具） */
function cardSource(src, title) {
  // issue #130：h2 内允许追加徽章等元素，匹配到 </h2> 为止
  const re = new RegExp(`<div className="card">\\s*<h2>\\s*${title}[\\s\\S]*?<\\/h2>[\\s\\S]*?(?=\\n\\s*<div className="card">|$)`)
  const m = src.match(re)
  return m ? m[0] : null
}

/** 提取具名箭头函数体源码 */
function fnBody(src, name) {
  const re = new RegExp(`const ${name} = (?:async )?\\(\\) => \\{([\\s\\S]*?)\\n  \\}`)
  const m = src.match(re)
  return m ? m[1] : null
}

test('设置页挂载「Owner GitLab Token」卡片且位于「GitLab 凭据」卡片之前', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  const ownerPos = settingsSrc.search(/Owner GitLab Token（issue 编辑专用）/)
  const credPos = settingsSrc.search(/GitLab 凭据（只读）/)
  assert.ok(ownerPos > 0 && credPos > ownerPos, '卡片应在「GitLab 凭据（只读）」卡片之前')
})

test('卡片含密码输入框与独立保存按钮，掩码占位提示 + 留空 = 保持现有', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  assert.match(card, /type="password"/, 'token 应为密码输入框')
  assert.match(card, /owner_token_masked/, '已配置时 placeholder 应显示掩码')
  assert.match(card, /留空 = 保持现有/, '输入框应提示留空 = 保持现有')
  assert.match(card, /onClick=\{saveOwnerToken\}/, '保存按钮应绑定 saveOwnerToken')
  assert.match(card, /保存 Owner Token/, '应有「保存 Owner Token」按钮')
})

test('saveOwnerToken 只提交 {gitlab:{owner_token}} 段，不触碰其他设置', () => {
  const body = fnBody(settingsSrc, 'saveOwnerToken')
  assert.ok(body, '应存在 saveOwnerToken 函数')
  assert.match(
    body,
    /api\.put\('\/api\/settings', \{ gitlab: \{ owner_token: ownerTokenInput\.trim\(\) \} \}\)/,
    '保存应只 PUT {gitlab:{owner_token}}（后端 PUT /api/settings 支持部分更新）',
  )
  assert.doesNotMatch(body, /\bworker\b/, 'saveOwnerToken 不应携带 worker 字段')
  assert.doesNotMatch(body, /\bsso\b/, 'saveOwnerToken 不应携带 sso 字段')
})

test('卡片说明明确用途边界：仅编辑 issue，严禁推送代码/处理流水线', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  assert.match(card, /编辑 issue/, '说明应写明该 token 用于编辑 issue')
  assert.match(card, /严禁|不会|绝不/, '说明应声明严禁/不会用于推送代码与流水线')
  assert.match(card, /推送/, '说明应提及推送代码')
  assert.match(card, /流水线/, '说明应提及流水线')
})

test('issue #130：卡片显示隔离徽章（已隔离 · Agent 不可用）', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  assert.match(card, /已隔离/, '标题应显示「已隔离」徽章')
  assert.match(card, /Agent 不可用/, '徽章应注明 Agent 不可用')
})

test('issue #130：说明明确隔离规则——Agent 不可使用、仅限概览页 issue 编辑、Agent 用自己仓库 token', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  assert.match(card, /所有 Agent[\s\S]{0,20}不可使用/, '应注明所有 Agent 均不可使用')
  assert.match(card, /概览页/, '应注明允许使用范围在概览页面')
  assert.match(card, /添加 issue/, '允许范围应包含添加 issue')
  assert.match(card, /关闭 issue/, '允许范围应包含关闭 issue')
  assert.match(card, /添加评论/, '允许范围应包含添加评论')
  assert.match(card, /回复 issue 评论/, '允许范围应包含回复 issue 评论')
  assert.match(card, /自己仓库的认证 token/, '应注明 Agent 只能使用自己仓库的认证 token')
  assert.match(card, /绝不/, '应声明绝不用于推送代码/流水线')
})

test('卡片内提供申请教程折叠区（GET /api/settings/owner-token-guide + Markdown 渲染）', () => {
  const card = cardSource(settingsSrc, 'Owner GitLab Token（issue 编辑专用）')
  assert.ok(card, '设置页应存在 Owner GitLab Token 配置卡片')
  assert.match(card, /查看 token 申请教程/, '应有「查看 token 申请教程」折叠按钮')
  assert.match(card, /<Markdown /, '展开后应使用 Markdown 组件渲染教程')
  assert.match(
    settingsSrc,
    /api\.get\('\/api\/settings\/owner-token-guide'\)/,
    '设置页挂载时应拉取教程（后端读 docs/ 单一文档来源，与 SSO 指南同模式）',
  )
})

// ---- 渲染与交互（mock global.fetch，与 settings-sso-guide.test.mjs 同款） ----

function mockFetch({ ownerMasked = '', guideContent = '# GitLab Owner Token 申请教程\n\n教程正文。' } = {}) {
  const requested = []
  const puts = []
  const originalFetch = global.fetch
  global.fetch = async (path, opts) => {
    requested.push(String(path))
    const p = String(path)
    if (p.startsWith('/api/settings/owner-token-guide')) {
      return { ok: true, status: 200, json: async () => ({ content: guideContent }) }
    }
    if (p.startsWith('/api/settings')) {
      if (opts?.method === 'PUT') {
        puts.push(JSON.parse(opts.body))
        return {
          ok: true, status: 200, json: async () => ({
            sso: {}, worker: {}, claude: { command: 'claude', args: [] },
            ui: { timezone: '' }, notifications: {}, gitlab: {}, env: {},
          }),
        }
      }
      return {
        ok: true, status: 200, json: async () => ({
          sso: {}, worker: {}, claude: { command: 'claude', args: [] },
          ui: { timezone: '' }, notifications: {},
          gitlab: { owner_token_masked: ownerMasked }, env: {},
        }),
      }
    }
    if (p.startsWith('/api/environment')) {
      return { ok: true, status: 200, json: async () => ({ tools: [], hostname: 'h', platform: 'p', detected_at: '2026-08-13 00:00:00' }) }
    }
    if (p.startsWith('/api/backups')) {
      return { ok: true, status: 200, json: async () => ({ backups: [], config: { enabled: false, retention_days: 7 } }) }
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found' }) }
  }
  return { requested, puts, restore: () => { global.fetch = originalFetch } }
}

test('渲染：已配置掩码显示在 placeholder，未配置时显示申请提示', async () => {
  const m = mockFetch({ ownerMasked: 'glpa****7890' })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const inputs = renderer.root.findAllByType('input')
    const ownerInput = inputs.find((i) => i.props.type === 'password'
      && String(i.props.placeholder || '').includes('glpa'))
    assert.ok(ownerInput, `应存在带掩码占位的 owner token 密码输入框`)
    assert.match(String(ownerInput.props.placeholder), /留空 = 保持现有/)
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('渲染：输入 token 点击保存 → PUT /api/settings 携带 {gitlab:{owner_token}}', async () => {
  const m = mockFetch()
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    const inputs = renderer.root.findAllByType('input')
    const ownerInput = inputs.find((i) => i.props.type === 'password'
      && String(i.props.placeholder || '').includes('glpat'))
    assert.ok(ownerInput, '应存在 owner token 密码输入框')
    await TestRenderer.act(async () => {
      ownerInput.props.onChange({ target: { value: ' glpat-my-owner-token ' } })
    })
    const buttons = renderer.root.findAllByType('button')
    const saveBtn = buttons.find((b) => b.props.children === '保存 Owner Token')
    assert.ok(saveBtn, '应存在「保存 Owner Token」按钮')
    await TestRenderer.act(async () => {
      saveBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.deepEqual(m.puts, [{ gitlab: { owner_token: 'glpat-my-owner-token' } }],
      '保存应提交去空格后的 owner token 且只含 gitlab 段')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})

test('渲染：点击「查看 token 申请教程」展开显示教程内容', async () => {
  const m = mockFetch({ guideContent: '# GitLab Owner Token 申请教程\n\n推荐用 Reporter 角色账号申请。' })
  let renderer = null
  try {
    await TestRenderer.act(async () => {
      renderer = TestRenderer.create(React.createElement(Settings))
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    assert.ok(m.requested.some((p) => p.startsWith('/api/settings/owner-token-guide')),
      `挂载时应拉取 owner token 教程：${m.requested.join(', ')}`)
    let tree = JSON.stringify(renderer.toJSON())
    assert.doesNotMatch(tree, /推荐用 Reporter 角色账号申请/, '默认收起时不应渲染教程内容')
    const buttons = renderer.root.findAllByType('button')
    const guideBtn = buttons.find((b) => b.props.children === '查看 token 申请教程')
    assert.ok(guideBtn, '应存在「查看 token 申请教程」按钮')
    await TestRenderer.act(async () => {
      guideBtn.props.onClick()
      await new Promise((resolve) => setTimeout(resolve, 10))
    })
    tree = JSON.stringify(renderer.toJSON())
    assert.match(tree, /GitLab Owner Token 申请教程/, '展开后应显示教程标题')
    assert.match(tree, /推荐用 Reporter 角色账号申请/, '展开后应显示教程正文')
  } finally {
    await TestRenderer.act(() => renderer.unmount())
    m.restore()
  }
})
