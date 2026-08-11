#!/usr/bin/env node
// ============================================================
// 构建时版本自增脚本（issue #9）
//
// 每次构建（CI `npm run build` 或本地构建）执行：
//   1. 读取版本文件（默认 <项目>/data/version.txt），patch 位自增
//      （1.0.0 → 1.0.1），写回持久化——版本号跨构建连续递增
//   2. 生成 frontend/public/version.json（version + buildTime），
//      vite build 时自动复制进 dist/，前端 fetch 显示
//
// 版本文件位置：
//   - 环境变量 BOTLER_DATA_DIR 优先（CI 显式指向持久数据目录，
//     shell executor 构建目录可能被清理，版本号不能存在构建目录）
//   - 未设置时回退 <项目>/../data/version.txt（本地开发；本机即
//     code01，与 CI 指向同一目录，版本号共享连续）
// ============================================================
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

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

// patch 位自增（数字加法：1.0.9 → 1.0.10，major/minor 不变）
const [major, minor, patch] = version.split('.').map(Number)
const next = `${major}.${minor}.${patch + 1}`

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
