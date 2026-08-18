#!/usr/bin/env node
// ============================================================
// 构建时版本自增脚本（issue #9）
//
// 每次构建（CI `npm run build` 或本地构建）执行：
//   1. 读取版本文件（默认 <项目>/data/version.txt），按「逢100进一」
//      规则自增（issue #179），写回持久化——版本号跨构建连续递增
//   2. 生成 frontend/public/version.json（version + buildTime + commit，
//      issue #233 起含 commit），vite build 时自动复制进 dist/，
//      前端 fetch 显示
//
// 版本自增规则（issue #179 逢100进一 + issue #283 逢百进位持续生效）：
//   - patch 位逢百进位：patch ≥ 100 时按 100 整除进位到 minor、余数保留
//     （1.0.99 → 1.1.0；已超 99 的历史值同样进位，1.0.299 → 1.3.0）
//   - minor 位逢百进位：minor ≥ 100 时按 100 整除进位到 major、余数保留
//     （1.99.99 → 2.0.0；1.150.5 → 2.50.6）
//   - major 位不设进位上限（99.99.99 → 100.0.0）
//   - 平台版本已到 300+ 时高位版本号同步加一（1.0.310 → 1.3.11），
//     版本号无限自增（issue #283）
//
// commit 取值（issue #233 前端展示 commit）：
//   - CI_COMMIT_SHA 优先（GitLab CI 注入，短号前 8 位）；
//   - 本地构建回退 `git rev-parse HEAD`；
//   - 非 git 环境（如 Docker 构建无 .git）静默省略 commit 字段，
//     前端显示降级为「版本 + 构建时间」。
//
// 版本文件位置：
//   - 环境变量 BOTLER_DATA_DIR 优先（CI 显式指向持久数据目录，
//     shell executor 构建目录可能被清理，版本号不能存在构建目录）
//   - 未设置时回退 <项目>/../data/version.txt（本地开发；本机即
//     code01，与 CI 指向同一目录，版本号共享连续）
// 输出目录：
//   - BOTLER_PUBLIC_DIR 可覆盖 version.json 输出目录（测试用临时目录，
//     默认 <项目>/public）
// ============================================================
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

// ---- 纯函数：版本自增（issue #179 逢100进一），导出供单元测试 ----

export function nextVersion(current) {
  // 读取当前版本（非法/缺失时从 1.0.0 重新开始，与历史行为一致）
  let version = '1.0.0'
  if (typeof current === 'string' && /^\d+\.\d+\.\d+$/.test(current.trim())) {
    version = current.trim()
  }

  let [major, minor, patch] = version.split('.').map(Number)
  // 逢100进一（base-100 归一化，issue #179 + issue #283）：patch/minor
  // 任一位 ≥ 100 即按 100 整除向高位进位、余数保留。patch 已超 99 的
  // 历史版本同样进位（1.0.299 → 1.3.0、1.0.310 → 1.3.11），保证版本号
  // 无限自增且高位版本号始终同步加一。
  patch += 1
  minor += Math.floor(patch / 100)
  patch %= 100
  major += Math.floor(minor / 100)
  minor %= 100
  return `${major}.${minor}.${patch}`
}

// ---- 纯函数：commit 短号（issue #233 前端展示 commit）----

// 完整 sha 截断为前 8 位；非法/空值返回 null（调用方据此省略字段）
export function shortCommit(sha) {
  if (typeof sha !== 'string') return null
  const trimmed = sha.trim()
  if (!trimmed) return null
  return trimmed.slice(0, 8)
}

// 当前构建提交短号：CI_COMMIT_SHA 优先（GitLab CI 注入），本地回退
// git rev-parse HEAD；非 git 环境（Docker 构建无 .git）返回 null
export function currentCommit() {
  const fromEnv = process.env.CI_COMMIT_SHA
  if (fromEnv) return shortCommit(fromEnv)
  try {
    const sha = execFileSync('git', ['rev-parse', 'HEAD'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    return shortCommit(sha)
  } catch {
    return null
  }
}

// ---- 主流程（仅作为脚本直接执行时运行，import 时无副作用）----

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const projectRoot = resolve(__dirname, '..')

  const dataDir = process.env.BOTLER_DATA_DIR || join(projectRoot, '..', 'data')
  const versionFile = join(dataDir, 'version.txt')
  const publicDir = process.env.BOTLER_PUBLIC_DIR || join(projectRoot, 'public')
  const outFile = join(publicDir, 'version.json')

  // 读取当前版本（文件不存在或格式非法时从 1.0.0 重新开始）
  let version = '1.0.0'
  if (existsSync(versionFile)) {
    const raw = readFileSync(versionFile, 'utf8').trim()
    if (/^\d+\.\d+\.\d+$/.test(raw)) version = raw
  }

  // 逢100进一（issue #179 + issue #283）：1.0.99 → 1.1.0，1.99.99 → 2.0.0；
  // 已超 99 的历史版本同样逢百进位（1.0.310 → 1.3.11，高位版本号加一）
  const next = nextVersion(version)

  // 写回版本文件（数据目录持久化，跨构建自增）
  mkdirSync(dataDir, { recursive: true })
  writeFileSync(versionFile, next + '\n')

  // 构建时间（本地时区，YYYY-MM-DD HH:mm:ss）
  const pad = (n) => String(n).padStart(2, '0')
  const now = new Date()
  const buildTime =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`

  // 生成构建信息（issue #233 起含 commit；非 git 环境省略该字段，
  // 前端显示降级为「版本 + 构建时间」）（vite 构建时 public/ 内容自动复制进 dist/）
  const commit = currentCommit()
  const payload = { version: next, buildTime }
  if (commit) payload.commit = commit
  mkdirSync(publicDir, { recursive: true })
  writeFileSync(outFile, JSON.stringify(payload, null, 2) + '\n')

  console.log(`✓ 版本已自增: ${version} → ${next}（构建时间 ${buildTime}${commit ? `，commit ${commit}` : ''}）`)
  console.log(`  version.json: ${outFile}`)
}
