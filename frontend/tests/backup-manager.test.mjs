// 数据备份卡片 BackupManager 测试（issue #104 补测）：组件此前无任何测试覆盖。
//
// 断言：
// 1. 源码：备份/恢复/上传恢复端点与 RESTORE_WARNING 覆盖提示；
// 2. 初始加载「加载中…」→ 备份列表渲染（名称/大小 fmtSize/创建时间/操作按钮）；
//    空列表「暂无备份」；
// 3. 保存配置：PUT /api/settings 的 backup 段（enabled 布尔、retention_days
//    Number 转换），成功提示 + 重新加载列表；
// 4. 立即备份：POST /api/backups 并展示备份名；
// 5. 下载走 api.download；删除/恢复均需自定义确认对话框二次确认（取消不调用
//    接口），删除后重新加载列表；恢复提示自动重启；
// 6. 上传恢复：未选文件不调用；选文件需确认，确认后 api.upload + 成功提示；
// 7. 保存失败展示错误；请求进行中按钮禁用防重复（busy）；
// 8. 首次加载失败应展示错误提示而非永久「加载中…」（复现用例）。
import { after, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import React from 'react'
import TestRenderer from 'react-test-renderer'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

// node --test 原生不支持 jsx，用 vite SSR 转译加载组件（与其他测试一致）
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { default: BackupManager } = await vite.ssrLoadModule('/src/components/BackupManager.jsx')
const { api } = await vite.ssrLoadModule('/src/api.js')
// 与组件内 import 的同一 dialog.js 模块实例，测试注入直接作用于确认调用（issue #105）
const dialog = await vite.ssrLoadModule('/src/dialog.js')

const bmSrc = readFileSync(path.join(ROOT, 'src/components/BackupManager.jsx'), 'utf8')

after(() => vite.close())

// ---- 夹具与 mock ----

const BACKUP_DATA = {
  backups: [{ name: 'botler-2026-08-14.tar.gz', size: 2048, created_at: '2026-08-14 03:00:00' }],
  config: { enabled: true, retention_days: 7 },
  retention: { enabled: true, task_logs_days: 90, notification_events_days: 30, log_files_days: 90, pm2_max_log_size_mb: 10 },
}

// 各 api 方法调用记录
const calls = { get: [], post: [], put: [], del: [], download: [], upload: [] }

function resetCalls() {
  for (const k of Object.keys(calls)) calls[k] = []
}

// 默认全方法 mock：get 返回 BACKUP_DATA，post 返回备份名；可用 opts 覆盖行为
function mockAll(opts = {}) {
  resetCalls()
  mock.method(api, 'get', async (p) => {
    calls.get.push(p)
    if (opts.getImpl) return opts.getImpl(p)
    return { ...BACKUP_DATA }
  })
  mock.method(api, 'post', async (p, body) => {
    calls.post.push([p, body])
    if (opts.postImpl) return opts.postImpl(p, body)
    return { name: 'botler-2026-08-15.tar.gz' }
  })
  mock.method(api, 'put', async (p, body) => {
    calls.put.push([p, body])
    if (opts.putImpl) return opts.putImpl(p, body)
  })
  mock.method(api, 'del', async (p) => { calls.del.push(p) })
  mock.method(api, 'download', async (p, f) => { calls.download.push([p, f]) })
  mock.method(api, 'upload', async (p, f) => { calls.upload.push([p, f]) })
}

// 自定义对话框自动应答注入（issue #105，node 环境无 DialogHost 挂载）：
// 记录确认消息文案，按需返回 true/false
let confirmMessages = []
function installConfirm(ret) {
  confirmMessages = []
  dialog.installAutoAnswer((opts) => {
    confirmMessages.push(opts.message)
    return ret
  })
}

// ---- 渲染与查找 helper ----

async function renderBackup() {
  let renderer = null
  let renderError = null
  await TestRenderer.act(async () => {
    try {
      renderer = TestRenderer.create(React.createElement(BackupManager))
      await new Promise((resolve) => setTimeout(resolve, 20))
    } catch (e) {
      renderError = e
    }
  })
  return { renderer, renderError }
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

function findAlert(renderer, cls) {
  return renderer.root.findAll((node) =>
    typeof node.props?.className === 'string' && node.props.className.includes(cls))
}

// ---- 源码断言 ----

test('源码：备份管理走 /api/backups 与恢复端点，带覆盖警告', () => {
  assert.match(bmSrc, /api\.get\('\/api\/backups'\)/, '列表加载走 GET /api/backups')
  assert.match(bmSrc, /api\.post\('\/api\/backups'\)/, '立即备份走 POST /api/backups')
  assert.match(bmSrc, /api\.post\('\/api\/backups\/restore', \{ name \}\)/, '恢复走 POST /api/backups/restore')
  assert.match(bmSrc, /api\.upload\('\/api\/backups\/restore\/upload', picked\)/, '上传恢复走 api.upload')
  assert.match(bmSrc, /恢复将覆盖现有数据（config\.yaml 与 botler\.db）并自动重启服务/, '应有覆盖风险警告')
  assert.match(bmSrc, /manifest \+ 校验和/, '应有上传文件完整性校验提示')
})

// ---- 加载与列表渲染 ----

test('初始加载显示「加载中…」，数据到达后渲染备份表格', async () => {
  let release = null
  mockAll({ getImpl: () => new Promise((resolve) => { release = resolve }) })
  let renderer = null
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(BackupManager))
  })
  assert.match(textOf(renderer.root), /加载中…/, '数据未到达时应显示加载中')
  await TestRenderer.act(async () => {
    release({ ...BACKUP_DATA })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  const text = textOf(renderer.root)
  assert.match(text, /botler-2026-08-14\.tar\.gz/, '应渲染备份文件名')
  assert.match(text, /2\.0 KB/, '应渲染人类可读大小（fmtSize）')
  assert.match(text, /2026-08-14 03:00:00/, '应渲染创建时间')
  for (const op of ['下载', '恢复', '删除']) {
    assert.equal(findButtons(renderer, op).length, 1, `应有「${op}」按钮`)
  }
})

test('空备份列表显示「暂无备份」', async () => {
  mockAll({ getImpl: async () => ({ backups: [], config: { enabled: true, retention_days: 7 } }) })
  const { renderer, renderError } = await renderBackup()
  assert.equal(renderError, null, `渲染不应抛错: ${renderError}`)
  assert.match(textOf(renderer.root), /暂无备份/)
})

// ---- 保存配置 ----

test('取消勾选后保存：提交 enabled 布尔值与 retention_days 数字', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  const checkbox = renderer.root.findAllByType('input').find((i) => i.props.type === 'checkbox')
  assert.equal(checkbox.props.checked, true, '默认应为勾选状态')
  await TestRenderer.act(async () => { checkbox.props.onChange({ target: { checked: false } }) })
  assert.equal(checkbox.props.checked, false, '取消勾选后受控更新')

  findButtons(renderer, '保存配置')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.deepEqual(calls.put[0], ['/api/settings', {
    backup: { enabled: false, retention_days: 7 },
    retention: { enabled: true, task_logs_days: 90, notification_events_days: 30, log_files_days: 90, pm2_max_log_size_mb: 10 },
  }], '应提交备份与数据保留配置，enabled 为布尔 false')
  assert.match(textOf(renderer.root), /备份配置已保存（写回 config\.yaml）/, '应显示保存成功提示')
  assert.equal(calls.get.length, 2, '保存成功后应重新加载列表（初始 + 重载）')
})

test('保留天数修改后保存：字符串输入转换为数字提交', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  const numInput = renderer.root.findAllByType('input').find((i) => i.props.type === 'number')
  assert.equal(numInput.props.value, 7, '初始保留天数 7')
  await TestRenderer.act(async () => { numInput.props.onChange({ target: { value: '30' } }) })
  findButtons(renderer, '保存配置')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.equal(calls.put[0][1].backup.retention_days, 30, '应转换为数字类型提交')
  assert.equal(typeof calls.put[0][1].backup.retention_days, 'number')
})

test('保存配置失败显示错误信息', async () => {
  mockAll({ putImpl: async () => { throw new Error('写回配置失败') } })
  const { renderer } = await renderBackup()
  findButtons(renderer, '保存配置')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  const alerts = findAlert(renderer, 'alert-error')
  assert.equal(alerts.length, 1, '应渲染错误提示')
  assert.match(textOf(alerts[0]), /写回配置失败/)
})

// ---- 立即备份 ----

test('立即备份：POST /api/backups 并展示返回的备份名', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  findButtons(renderer, '立即备份')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.deepEqual(calls.post[0], ['/api/backups', undefined])
  assert.match(textOf(renderer.root), /备份完成：botler-2026-08-15\.tar\.gz/)
  assert.equal(calls.get.length, 2, '备份完成后应重新加载列表')
})

// ---- 下载 / 删除 ----

test('下载调用 api.download（文件名 URL 编码后拼路径）', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  findButtons(renderer, '下载')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.deepEqual(calls.download[0], [
    `/api/backups/${encodeURIComponent('botler-2026-08-14.tar.gz')}/download`,
    'botler-2026-08-14.tar.gz',
  ])
})

test('删除：取消确认不调用接口；确认后删除并重新加载', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  installConfirm(false)
  findButtons(renderer, '删除')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.equal(calls.del.length, 0, '取消确认不应调用删除接口')
  assert.match(confirmMessages[0], /确定删除备份 botler-2026-08-14\.tar\.gz/, '确认提示应含备份名')

  installConfirm(true)
  findButtons(renderer, '删除')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.equal(calls.del[0], `/api/backups/${encodeURIComponent('botler-2026-08-14.tar.gz')}`)
  assert.equal(calls.get.length, 2, '删除后应重新加载列表')
})

// ---- 恢复 ----

test('恢复：需覆盖警告确认，确认后 POST /api/backups/restore 并提示重启', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  installConfirm(false)
  findButtons(renderer, '恢复')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.equal(calls.post.length, 0, '取消确认不应调用恢复接口')
  assert.match(confirmMessages[0], /恢复将覆盖现有数据/, '确认提示应含覆盖警告')

  installConfirm(true)
  findButtons(renderer, '恢复')[0].props.onClick()
  await TestRenderer.act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  assert.deepEqual(calls.post[0], ['/api/backups/restore', { name: 'botler-2026-08-14.tar.gz' }])
  assert.match(textOf(renderer.root), /恢复完成，服务正在自动重启，稍后请刷新页面/)
})

// ---- 上传恢复 ----

test('上传恢复：未选文件不调用接口；选文件需确认；确认后上传并提示', async () => {
  mockAll()
  const { renderer } = await renderBackup()
  const fileInput = renderer.root.findAllByType('input').find((i) => i.props.type === 'file')
  const FILE = { name: 'botler-backup.tar.gz' }

  // 未选文件（files 为空）
  installConfirm(true)
  await TestRenderer.act(async () => {
    fileInput.props.onChange({ target: { files: [] } })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.equal(calls.upload.length, 0, '未选文件不应调用上传接口')
  assert.equal(confirmMessages.length, 0, '未选文件不应弹确认框')

  // 选文件但取消确认
  installConfirm(false)
  await TestRenderer.act(async () => {
    fileInput.props.onChange({ target: { files: [FILE] } })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.equal(calls.upload.length, 0, '取消确认不应调用上传接口')
  assert.match(confirmMessages[0], /恢复将覆盖现有数据/, '确认提示应含覆盖警告')

  // 选文件并确认
  installConfirm(true)
  await TestRenderer.act(async () => {
    fileInput.props.onChange({ target: { files: [FILE] } })
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.deepEqual(calls.upload[0], ['/api/backups/restore/upload', FILE])
  assert.match(textOf(renderer.root), /上传恢复完成，服务正在自动重启，稍后请刷新页面/)
})

// ---- busy 防重复 ----

test('保存请求进行中：按钮禁用防重复点击', async () => {
  let release = null
  mockAll({ putImpl: () => new Promise((resolve) => { release = resolve }) })
  const { renderer } = await renderBackup()
  const saveBtn = findButtons(renderer, '保存配置')[0]
  await TestRenderer.act(async () => { saveBtn.props.onClick() })
  // 请求中 busy=true：按钮文案切换为「保存中…/备份中…」且全部禁用
  // （重新查询节点：状态更新后旧节点引用已失效）
  const busySaveBtn = findButtons(renderer, '保存中…')[0]
  assert.ok(busySaveBtn, '请求中保存按钮应显示保存中')
  assert.equal(busySaveBtn.props.disabled, true, '请求中保存按钮应禁用')
  assert.equal(findButtons(renderer, '备份中…')[0].props.disabled, true, '请求中立即备份应禁用')
  await TestRenderer.act(async () => {
    release()
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
  assert.equal(findButtons(renderer, '保存配置')[0].props.disabled, false, '请求完成后按钮应恢复可用')
})

// ---- 首次加载失败（复现用例：见 fix 提交修复）----

test('首次加载失败应展示错误提示而非永久「加载中…」', async () => {
  mockAll({ getImpl: async () => { throw new Error('备份列表加载失败') } })
  const { renderer } = await renderBackup()
  const alerts = findAlert(renderer, 'alert-error')
  assert.equal(alerts.length, 1, '加载失败应渲染错误提示')
  assert.match(textOf(alerts[0]), /备份列表加载失败/)
  assert.doesNotMatch(textOf(renderer.root), /加载中…/, '不应停留在加载中状态')
})

test('运行数据保留：保存配置并提供手动清理入口', () => {
  assert.match(bmSrc, /retention:\s*\{/, '保存请求应包含 retention 配置')
  assert.match(bmSrc, /api\.post\('\/api\/retention\/cleanup'\)/, '立即清理应调用保留清理 API')
  assert.match(bmSrc, /立即清理过期数据/, '应提供可见的手动清理按钮')
})
