// 操作审计日志卡片（issue #260）测试。
//
// 断言：
// 1. 设置页挂载「审计日志」区块（Settings.jsx 源码 + 导航关键词）；
// 2. 卡片初始加载 → 表格渲染（时间/操作者/操作类型/目标/变更摘要/IP），
//    操作类型中文标签映射正确；
// 3. 分页：总数/页数展示、上一页/下一页边界禁用、翻页带 action 参数；
// 4. 按操作类型过滤下拉 → 重新拉取（带 action 参数）；
// 5. 删除（仅管理员）：admin=true 显示删除按钮 → 二次确认 → DELETE 接口；
//    admin=false 不显示删除按钮；
// 6. 管理员名单输入 + 保存 → PUT /api/settings 的 audit_logs 段；
// 7. 加载失败展示错误 + 点击重试；空列表「暂无审计记录」。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: AuditLogsCard } = await vite.ssrLoadModule('/src/components/settings/AuditLogsCard.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
const dialog = await vite.ssrLoadModule('/src/dialog.js')

const cardSrc = readFileSync(path.join(ROOT, 'src/components/settings/AuditLogsCard.jsx'), 'utf8')
const settingsSrc = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const navSrc = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')

after(() => vite.close())

// ---- 夹具与 mock ----

const ROWS = [
  { id: 2, actor: 'zhangsan', action: 'settings.update', target_type: 'config', target_id: null,
    detail: { sections: ['worker'], diff: { worker: { max_retries: [2, 5] } } },
    created_at: '2026-08-23 06:00:00', ip: '10.0.0.2' },
  { id: 1, actor: 'local', action: 'repo.delete', target_type: 'repo', target_id: '7',
    detail: { name: '项目A', project_id: 42 }, created_at: '2026-08-23 05:00:00', ip: '' },
]

function pageData(pg, act) {
  const items = act ? ROWS.filter((r) => r.action === act) : ROWS
  return { items, total: items.length, page: pg, per_page: 20, actions: ['repo.delete', 'settings.update'], admin: true }
}

const calls = { get: [], del: [], put: [] }
function mockApi(opts = {}) {
  calls.get = []; calls.del = []; calls.put = []
  mock.method(api, 'get', async (p) => {
    calls.get.push(p)
    if (opts.getImpl) return opts.getImpl(p)
    if (String(p).startsWith('/api/audit-logs?')) return pageData(1, '')
    if (p === '/api/settings') return { audit_logs: { admin_usernames: ['zhangsan'] } }
    return {}
  })
  mock.method(api, 'del', async (p) => { calls.del.push(p) })
  mock.method(api, 'put', async (p, body) => { calls.put.push([p, body]) })
}

let confirmMessages = []
function installConfirm(ret) {
  confirmMessages = []
  dialog.installAutoAnswer((opts) => { confirmMessages.push(opts.message); return ret })
}

async function renderCard() {
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(AuditLogsCard))
    await new Promise((resolve) => setTimeout(resolve, 30))
  })
  return renderer
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object' && node.children) return node.children.map(textOf).join('')
  if (typeof node === 'object' && node.props) return textOf(node.props.children)
  return ''
}

function findButtons(renderer) {
  return renderer.root.findAllByType('button')
}

// ---- 测试 ----

test('设置页挂载「审计日志」区块并自动进入导航栏', () => {
  assert.match(settingsSrc, /id="settings-audit-logs"/, 'Settings.jsx 应有审计日志区块锚点')
  assert.match(settingsSrc, /<AuditLogsCard \/>/, '设置页应挂载 AuditLogsCard')
  assert.match(settingsSrc, /data-nav-label="审计日志"/, '区块应有导航短名')
  assert.match(navSrc, /'settings-audit-logs': \['audit', '审计'/, '导航搜索关键词应含审计/日志')
})

test('卡片标题与说明（操作范围 + 仅管理员删除）', () => {
  assert.match(cardSrc, /<h2>审计日志<\/h2>/, '卡片应有标题')
  assert.match(cardSrc, /删除仅限管理员/, '说明应注明删除仅限管理员')
  assert.match(cardSrc, /GET \/api\/audit-logs/, '注释应说明数据端点')
})

test('初始加载渲染审计表格（操作者/时间/类型/目标/摘要/IP）', async () => {
  mockApi()
  const renderer = await renderCard()
  const all = textOf(renderer.root)
  assert.match(all, /zhangsan/, '应展示操作者')
  assert.match(all, /2026-08-23 06:00:00/, '应展示时间（UTC）')
  assert.match(all, /设置保存/, '操作类型应映射为中文标签')
  assert.match(all, /删除仓库/, 'repo.delete 应映射为「删除仓库」')
  assert.match(all, /repo#7/, '应展示目标类型与 id')
  assert.match(all, /max_retries: 2 → 5/, '应展示变更摘要 diff（旧值 → 新值）')
  assert.match(all, /10\.0\.0\.2/, '应展示 IP')
  assert.match(all, /共 2 条，第 1\/1 页/, '应展示总数与页码')
  assert.ok(calls.get.some((p) => p.startsWith('/api/audit-logs?')), '应拉取审计列表')
})

test('操作类型过滤下拉重新拉取（带 action 参数）', async () => {
  mockApi({ getImpl: (p) => {
    if (String(p).startsWith('/api/audit-logs?')) {
      const params = new URLSearchParams(String(p).split('?')[1])
      return pageData(1, params.get('action') || '')
    }
    return { audit_logs: { admin_usernames: [] } }
  } })
  const renderer = await renderCard()
  const select = renderer.root.findAllByType('select')[0]
  await TestRenderer.act(async () => {
    select.props.onChange({ target: { value: 'repo.delete' } })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  const last = calls.get.filter((p) => p.startsWith('/api/audit-logs?')).pop()
  assert.ok(last.includes('action=repo.delete'), '过滤后应带 action 参数')
  assert.match(textOf(renderer.root), /删除仓库/, '过滤后应只显示对应类型')
})

test('分页：边界禁用与翻页', async () => {
  mockApi({ getImpl: (p) => {
    if (String(p).startsWith('/api/audit-logs?')) {
      const params = new URLSearchParams(String(p).split('?')[1])
      const pg = Number(params.get('page') || 1)
      // 模拟共 25 条分 2 页
      return { items: pg === 1 ? ROWS : [{ id: 3, actor: 'bob', action: 'backup.create', target_type: 'backup', target_id: 'b.tar.gz', detail: {}, created_at: '2026-08-23 04:00:00', ip: '' }], total: 25, page: pg, per_page: 20, actions: ['repo.delete', 'settings.update', 'backup.create'], admin: true }
    }
    return { audit_logs: { admin_usernames: [] } }
  } })
  const renderer = await renderCard()
  const buttons = findButtons(renderer)
  const prev = buttons.find((b) => textOf(b) === '上一页')
  const next = buttons.find((b) => textOf(b) === '下一页')
  assert.ok(prev.props.disabled, '第一页上一页应禁用')
  assert.ok(!next.props.disabled, '第一页下一页应可用')
  await TestRenderer.act(async () => {
    next.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.match(textOf(renderer.root), /第 2\/2 页/, '翻页后应显示第 2 页')
})

test('管理员可删除：二次确认后调 DELETE 并刷新', async () => {
  mockApi()
  installConfirm(true)
  const renderer = await renderCard()
  const delBtn = findButtons(renderer).find((b) => textOf(b) === '删除')
  assert.ok(delBtn, 'admin=true 应显示删除按钮')
  await TestRenderer.act(async () => {
    delBtn.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.ok(confirmMessages.some((m) => m.includes('删除审计日志 #2')), '删除前应二次确认')
  assert.ok(calls.del.includes('/api/audit-logs/2'), '应调用 DELETE /api/audit-logs/2')
})

test('普通用户（admin=false）不显示删除按钮', async () => {
  mockApi({ getImpl: (p) => {
    if (String(p).startsWith('/api/audit-logs?')) {
      const data = pageData(1, '')
      data.admin = false
      return data
    }
    return { audit_logs: { admin_usernames: ['zhangsan'] } }
  } })
  const renderer = await renderCard()
  const delBtn = findButtons(renderer).find((b) => textOf(b) === '删除')
  assert.equal(delBtn, undefined, 'admin=false 不应显示删除按钮')
})

test('管理员名单：输入 + 保存 → PUT /api/settings 的 audit_logs 段', async () => {
  mockApi({ getImpl: (p) => {
    if (String(p).startsWith('/api/audit-logs?')) return pageData(1, '')
    if (p === '/api/settings') return { audit_logs: { admin_usernames: ['zhangsan'] } }
    return {}
  } })
  const renderer = await renderCard()
  const input = renderer.root.findAllByType('input').find((i) => i.props.placeholder && i.props.placeholder.includes('zhangsan'))
  assert.ok(input, '应有管理员名单输入框')
  await TestRenderer.act(async () => {
    input.props.onChange({ target: { value: ' alice , bob, alice ' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
  })
  const save = findButtons(renderer).find((b) => textOf(b) === '保存管理员名单')
  await TestRenderer.act(async () => {
    save.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  const put = calls.put.find(([p]) => p === '/api/settings')
  assert.ok(put, '应 PUT /api/settings')
  assert.deepEqual(put[1], { audit_logs: { admin_usernames: ['alice', 'bob'] } }, '应提交去重后的名单')
})

test('加载失败展示错误并可点击重试', async () => {
  let failFirst = true
  mockApi({ getImpl: async (p) => {
    if (String(p).startsWith('/api/audit-logs?')) {
      if (failFirst) { failFirst = false; throw new Error('列表加载失败') }
      return pageData(1, '')
    }
    return { audit_logs: { admin_usernames: [] } }
  } })
  const renderer = await renderCard()
  assert.match(textOf(renderer.root), /列表加载失败/, '应展示错误信息')
  assert.match(textOf(renderer.root), /点击重试/, '应提示点击重试')
  const retry = findButtons(renderer).find((b) => textOf(b).includes('点击重试') || textOf(b) === '刷新')
  await TestRenderer.act(async () => {
    retry.props.onClick()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.match(textOf(renderer.root), /设置保存/, '重试后应加载成功')
})

test('空列表展示「暂无审计记录」', async () => {
  mockApi({ getImpl: (p) => {
    if (String(p).startsWith('/api/audit-logs?')) {
      return { items: [], total: 0, page: 1, per_page: 20, actions: [], admin: true }
    }
    return { audit_logs: { admin_usernames: [] } }
  } })
  const renderer = await renderCard()
  assert.match(textOf(renderer.root), /暂无审计记录/, '空列表应提示')
})
