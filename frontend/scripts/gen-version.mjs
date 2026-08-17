#!/usr/bin/env node
// ============================================================
// 构建时版本自增脚本（issue #9）
//
// 每次构建（CI `npm run build` 或本地构建）执行：
//   1. 读取版本文件（默认 <项目>/data/version.txt），按「逢100进一」
//      规则自增（issue #179），写回持久化——版本号跨构建连续递增
//   2. 生成 frontend/public/version.json（version + buildTime），
//      vite build 时自动复制进 dist/，前端 fetch 显示
//
// 版本自增规则（issue #179：逢100进一）：
//   - patch 位自增到 100 时向 minor 进位（patch 归零、minor +1）：
//     1.0.99 → 1.1.0
//   - minor 位随之到 100 时再向 major 进位（minor 归零、major +1）：
//     1.99.99 → 2.0.0
//   - major 位不设进位上限（99.99.99 → 100.0.0）
//   - 已超过 99 的历史版本号仅按数字自增、不做回写修正
//     （1.0.299 → 1.0.300）
//
// 版本文件位置：
//   - 环境变量 BOTLER_DATA_DIR 优先（CI 显式指向持久数据目录，
//     shell executor 构建目录可能被清理，版本号不能存在构建目录）
//   - 未设置时回退 <项目>/../data/version.txt（本地开发；本机即
//     code01，与 CI 指向同一目录，版本号共享连续）
// ============================================================
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
  patch += 1
  if (patch === 100) {
    // 逢100进一：patch 归零、向 minor 进位
    patch = 0
    minor += 1
    if (minor === 100) {
      // minor 随之到 100：归零、再向 major 进位
      minor = 0
      major += 1
    }
  }
  return `${major}.${minor}.${patch}`
}

// ---- 主流程（仅作为脚本直接执行时运行，import 时无副作用）----

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const projectRoot = resolve(__dirname, '..')

  const dataDir = process.env.BOTLER_DATA_DIR || join(projectRoot, '..', 'data')
  const versionFile = join(dataDir, 'version.txt')
  const publicDir = join(projectRoot, 'public')
  const outFile = join(publicDir, 'version.json')

  // 读取当前版本（文件不存在或格式非法时从 1.0.0 重新开始）
  let version = '1.0.0'
  if (existsSync(versionFile)) {
    const raw = readFileSync(versionFile, 'utf8').trim()
    if (/^\d+\.\d+\.\d+$/.test(raw)) version = raw
  }

  // 逢100进一（issue #179）：1.0.99 → 1.1.0，1.99.99 → 2.0.0
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

  // 生成构建信息（vite 构建时 public/ 内容自动复制进 dist/）
  mkdirSync(publicDir, { recursive: true })
  writeFileSync(outFile, JSON.stringify({ version: next, buildTime }, null, 2) + '\n')

  console.log(`✓ 版本已自增: ${version} → ${next}（构建时间 ${buildTime}）`)
  console.log(`  version.json: ${outFile}`)
}
