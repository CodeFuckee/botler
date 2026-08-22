// 结果评论模版默认内容展示（issue #438）。
// 当服务端未保存自定义 templates.comment 时，设置接口返回内置结构化模板；
// 模版页应直接以该内容作为可编辑基线，清空保存后也复用接口返回的默认内容。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const templates = readFileSync(path.join(ROOT, 'src/pages/Templates.jsx'), 'utf8')
const settingsApi = readFileSync(path.join(ROOT, '../backend/botler/api/settings.py'), 'utf8')

test('结果评论模版页读取接口提供的默认模板作为可编辑基线', () => {
  assert.match(
    templates,
    /setCommentTemplate\(settings\.templates\.comment \|\| ''\)/,
    '加载设置后应保存结果评论模板，供切换视图时编辑',
  )
  assert.match(
    templates,
    /setText\(commentTemplate\)/,
    '切换“结果评论模版”时应把默认/自定义模板放入编辑器',
  )
})

test('设置接口在 comment 未配置时返回内置结构化结果评论模板', () => {
  assert.match(
    settingsApi,
    /"comment": s\.comment_template or DEFAULT_COMMENT_TEMPLATE/,
    'GET /api/settings 应将空配置回退为内置默认模板，而非返回空字符串',
  )
})
