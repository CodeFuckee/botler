// 任务失败原因分类前端工具测试（issue #274）：分类展示名 / 徽章 class。
// 与后端 botler/failure_classify.py 的 CATEGORY_LABELS 保持一致
// （env/engine/unsolvable/unknown），详情页/统计页共用本模块。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  FAILURE_CATEGORY_LABELS,
  failureCategoryClass,
  failureCategoryLabel,
} from '../src/failure-categories.js'

test('分类展示名覆盖全部四类（与后端口径一致）', () => {
  assert.equal(FAILURE_CATEGORY_LABELS.env, '环境类')
  assert.equal(FAILURE_CATEGORY_LABELS.engine, '引擎类')
  assert.equal(FAILURE_CATEGORY_LABELS.unsolvable, '无法解决类')
  assert.equal(FAILURE_CATEGORY_LABELS.unknown, '未知')
})

test('failureCategoryLabel：已知分类返回展示名', () => {
  assert.equal(failureCategoryLabel('env'), '环境类')
  assert.equal(failureCategoryLabel('engine'), '引擎类')
  assert.equal(failureCategoryLabel('unsolvable'), '无法解决类')
  assert.equal(failureCategoryLabel('unknown'), '未知')
})

test('failureCategoryLabel：未知/空分类兜底「未知」不报错（验收标准 3）', () => {
  assert.equal(failureCategoryLabel(''), '未知')
  assert.equal(failureCategoryLabel(null), '未知')
  assert.equal(failureCategoryLabel(undefined), '未知')
  assert.equal(failureCategoryLabel('badcat'), '未知')
})

test('failureCategoryClass：已知分类映射对应徽章配色 class', () => {
  assert.equal(failureCategoryClass('env'), 'failure-cat-env')
  assert.equal(failureCategoryClass('engine'), 'failure-cat-engine')
  assert.equal(failureCategoryClass('unsolvable'), 'failure-cat-unsolvable')
  assert.equal(failureCategoryClass('unknown'), 'failure-cat-unknown')
})

test('failureCategoryClass：未知分类兜底 unknown 配色', () => {
  assert.equal(failureCategoryClass('badcat'), 'failure-cat-unknown')
  assert.equal(failureCategoryClass(null), 'failure-cat-unknown')
})
