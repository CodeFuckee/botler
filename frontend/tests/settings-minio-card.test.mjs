// MinIO 对象存储配置卡片测试（issue #170）：设置页可配置识图图片上传
// MinIO（minio.enabled + endpoint + access_key + secret_key +
// public_base_url，另含 secure / bucket / verify_ssl），启用后用户上传
// 的图片自动上传 MinIO public 桶并经 nginx 代理地址访问。
//
// 背景：后端 settings API 自 issue #163 已支持 minio 段（GET 掩码返回、
// PUT 写入），但设置页暂未提供卡片（issue #163 CHANGELOG 明确「设置页
// 暂未提供卡片、可编辑 config.yaml 或经 API 配置」）。本次补齐设置页卡片。
//
// 本测试断言：
// 1. 设置页挂载「MinIO 对象存储」卡片（识图模型区块之后，外部服务接入分组）；
// 2. 卡片字段齐全：启用开关 / API 地址 / HTTPS / Access Key / Secret Key /
//    桶名 / 公网访问前缀 / 校验证书；
// 3. 保存走 PUT /api/settings 的 minio 段；凭据输入框留空 = 保持现有；
// 4. 导航关键词映射包含 settings-minio（导航栏自动出现「MinIO 对象存储」）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const settings = readFileSync(path.join(ROOT, 'src/pages/Settings.jsx'), 'utf8')
const nav = readFileSync(path.join(ROOT, 'src/components/SettingsNav.jsx'), 'utf8')

test('设置页挂载「MinIO 对象存储」卡片', () => {
  assert.match(settings, /<section id="settings-minio" className="settings-section">/, '应有 settings-minio 设置区块')
  assert.match(settings, /<h2>MinIO 对象存储<\/h2>/, '应有卡片标题「MinIO 对象存储」')
})

test('「MinIO 对象存储」卡片位于「识图模型」区块之后（外部服务接入分组）', () => {
  const visionPos = settings.search(/<section id="settings-vision-models"/)
  const minioPos = settings.search(/<section id="settings-minio"/)
  const sysPos = settings.search(/<h2 className="settings-group-title">系统设置<\/h2>/)
  assert.ok(visionPos > 0, '应有识图模型区块')
  assert.ok(minioPos > visionPos, 'MinIO 卡片应在识图模型区块之后')
  assert.ok(sysPos > minioPos, 'MinIO 卡片应位于「系统设置」分组之前（同属外部服务接入）')
})

test('卡片字段齐全：启用 / API 地址 / HTTPS / Access Key / Secret Key / 桶 / 公网前缀 / 校验证书', () => {
  assert.match(settings, /minio\.enabled/, '应有启用开关（minio.enabled）')
  assert.match(settings, /placeholder="127\.0\.0\.1:9000"/, '应有 API 地址输入框（endpoint 默认 127.0.0.1:9000）')
  assert.match(settings, /HTTPS <code>secure<\/code>/, '应有 HTTPS 开关（secure）')
  assert.match(settings, /Access Key <code>access_key<\/code>/, '应有 Access Key 输入')
  assert.match(settings, /Secret Key <code>secret_key<\/code>/, '应有 Secret Key 输入')
  assert.match(settings, /桶名 <code>bucket<\/code>/, '应有桶名输入')
  assert.match(settings, /placeholder="https:\/\/[^"]*\/minio-public"/, '应有公网访问前缀输入框（public_base_url，http(s) 前缀 + /minio-public）')
  assert.match(settings, /校验 endpoint 证书 <code>verify_ssl<\/code>/, '应有校验证书开关（verify_ssl）')
})

test('凭据输入框为密码框且留空 = 保持现有（后端掩码不覆盖）', () => {
  const passwordInputs = settings.match(/type="password"/g) || []
  assert.ok(passwordInputs.length >= 2, 'Access Key / Secret Key 应为密码输入框（最少 2 个）')
  assert.match(settings, /留空 = 保持现有/, '凭据输入框应提示留空保持现有')
  assert.match(settings, /access_key_masked/, 'Access Key 占位应使用后端掩码值')
  assert.match(settings, /secret_key_masked/, 'Secret Key 占位应使用后端掩码值')
  assert.match(settings, /if \(minioAccessInput\.trim\(\)\) m\.access_key/, '仅在输入非空时提交 access_key')
  assert.match(settings, /if \(minioSecretInput\.trim\(\)\) m\.secret_key/, '仅在输入非空时提交 secret_key')
})

test('保存走 PUT /api/settings 的 minio 段（卡片内独立保存 + 全局保存）', () => {
  assert.match(settings, /api\.put\('\/api\/settings', \{ minio: buildMinioPatch\(\) \}\)/, '卡片保存应提交 minio 段')
  assert.match(settings, /保存 MinIO 配置/, '应有「保存 MinIO 配置」按钮')
  assert.match(settings, /minio: buildMinioPatch\(\)/, '全局保存也应提交 minio 段（与 webhook 同模式）')
  assert.match(settings, /settings\.minio\?\.enabled/, '启用开关应读取 settings.minio.enabled')
})

test('导航关键词映射包含 settings-minio（导航栏自动出现 MinIO 对象存储子项）', () => {
  assert.match(nav, /'settings-minio':/, 'SETTING_KEYWORDS 应包含 settings-minio')
  assert.match(nav, /'minio'/, '关键词应包含 minio')
  assert.match(nav, /'对象存储'/, '关键词应包含「对象存储」')
})
