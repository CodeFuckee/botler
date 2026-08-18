// 版本徽标展示 commit + 构建时间测试（issue #233）：页面可见版本号
// （含 commit/时间）——VersionBadge 渲染 commit 短号与构建时间，版本
// 数据链路（gen-version.mjs 生成 → version.json → 前端展示）闭环；
// 后端 /api/health 与前端同源（见 backend/tests/test_version.py）。
//
// 测试层次（与仓库既有静态分析测试风格一致，读源码断言）：
// 1. VersionBadge 渲染 commit 短号与构建时间；
// 2. gen-version.mjs 生成 commit 字段（CI_COMMIT_SHA / git rev-parse）；
// 3. 设置页「版本信息」卡片文案提及提交；
// 4. App.jsx 挂载版本更新提示（新版部署后提示刷新，issue #233）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const badge = readFileSync(path.join(ROOT, 'src/components/VersionBadge.jsx'), 'utf8')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const genVersion = readFileSync(path.join(ROOT, 'scripts/gen-version.mjs'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')

test('VersionBadge 渲染 commit 短号（.version-badge-commit，issue #233）', () => {
  assert.match(badge, /version-badge-commit/, '应渲染 commit 短号 span')
  assert.match(badge, /shortSha\(info\.commit\)/, 'commit 应经 shortSha 截断展示')
})

test('VersionBadge 渲染构建时间（.version-badge-time）', () => {
  assert.match(badge, /version-badge-time/, '应渲染构建时间 span')
})

test('VersionBadge 无 commit 时静默省略（非 git 构建降级显示）', () => {
  assert.match(badge, /\{info\.commit && /, 'commit 缺失时应省略该 span 不显示占位符')
})

test('styles.css 提供 commit 短号样式', () => {
  assert.match(styles, /\.version-badge\s+\.version-badge-commit/, '应有 commit 短号样式规则')
})

test('gen-version.mjs 生成 commit 字段（CI_COMMIT_SHA 优先 + git rev-parse 回退）', () => {
  assert.match(genVersion, /commit/, '脚本应生成 commit 字段')
  assert.match(genVersion, /CI_COMMIT_SHA/, '应优先读取 CI_COMMIT_SHA（GitLab CI 注入）')
  assert.match(genVersion, /rev-parse/, '本地构建应回退 git rev-parse HEAD')
})

test('设置页版本信息文案提及提交', () => {
  assert.match(settings, /提交/, '设置页「版本信息」卡片应说明展示内容含提交')
})

test('App.jsx 挂载版本更新提示（新版部署后提示刷新）', () => {
  assert.match(app, /createVersionChecker/, 'App 应创建版本检查器轮询 /version.json')
  assert.match(app, /version-update-banner/, 'App 应渲染版本更新横幅')
  assert.match(app, /window\.location\.reload/, '横幅「立即刷新」应整页刷新加载新版本')
})
