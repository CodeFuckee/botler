// 标记库页面测试（issue #29）：用户反馈「增加一个标记库，用户可以手动添加删除，
// 建议清单作为默认选项不可删除，但页面上看不到标记库页面」。
//
// 第一轮只交付了 docs/labels.md + scripts/sync_labels.py（无 UI），
// 用户澄清要的是 botler Web UI 上的标记库管理页面。本测试断言：
// 1. 存在 /labels 路由与顶部导航入口；
// 2. 页面展示默认标签清单（标记「默认」、无删除按钮）；
// 3. 页面提供自定义标签添加表单与删除按钮；
// 4. 后端 /api/labels 提供 GET/POST/DELETE 接口。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 界面国际化（issue #268）：中文文案以 locales/zh-CN.json 为稳定来源，
// 源码断言改为「i18n key + 字典中文值」双重校验
const zhCN = JSON.parse(readFileSync(path.join(ROOT, 'src/locales/zh-CN.json'), 'utf8'))
const app = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')
const labels = readFileSync(path.join(ROOT, 'src/pages/Labels.jsx'), 'utf8')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const apiLabels = readFileSync(path.join(ROOT, '../backend/botler/api/labels.py'), 'utf8')

test('顶部导航包含「标记库」入口', () => {
  assert.match(app, /NavLink to="\/labels"/, '导航应有到 /labels 的 NavLink')
  assert.match(app, /t\('nav\.labels'\)/, '导航链接应经 t() 国际化')
  assert.equal(zhCN['nav.labels'], '标记库', '中文文案应为「标记库」')
})

test('App.jsx 注册 /labels 路由并挂载 Labels 页面', () => {
  assert.match(app, /Route path="\/labels" element={<Labels \/>}/, '应有 /labels 路由')
  assert.match(app, /import Labels from '\.\/pages\/Labels\.jsx'/, '应导入 Labels 页面组件')
})

test('标记库页展示默认清单，默认标签标记「默认」且无删除按钮', () => {
  assert.match(labels, /默认标签/, '页面应有「默认标签」区块标题')
  assert.match(labels, /不可删除/, '应注明默认标签不可删除')
  assert.match(labels, /badge-default/, '默认标签应有「默认」徽标样式类')
  // 默认标签渲染分支：removable=false → 只显示徽标，不渲染删除按钮
  assert.match(labels, /removable\s*\?\s*<button/, '删除按钮只在自定义标签分支渲染')
})

test('标记库页提供自定义标签添加表单（名称/颜色/说明/添加按钮）', () => {
  assert.match(labels, /添加自定义标签/, '应有「添加自定义标签」区块')
  assert.match(labels, /标签名（/, '应有标签名输入框')
  assert.match(labels, /#6699cc/, '应有颜色输入框（默认色占位）')
  assert.match(labels, /说明（可选）/, '应有说明输入框')
  assert.match(labels, /btn-primary/, '应有「添加」按钮')
  assert.match(labels, /api\.post\('\/api\/labels'/, '添加走 POST /api/labels')
})

test('标记库页自定义标签可删除（走 DELETE /api/labels/{name}）', () => {
  assert.match(labels, /删除自定义标签/, '应有删除确认提示')
  assert.match(labels, /api\.del\(`\/api\/labels\//, '删除走 DELETE /api/labels/{name}')
  assert.match(labels, /api\.get\('\/api\/labels'/, '列表加载走 GET /api/labels')
})

test('styles.css 提供标签条目样式（label-chip / label-color / label-list）', () => {
  for (const cls of ['label-chip', 'label-color', 'label-list', 'badge-default', 'btn-small']) {
    assert.ok(
      new RegExp(`\\.${cls}\\s*\\{`).test(styles),
      `styles.css 应包含 .${cls} 样式规则`,
    )
  }
})

test('后端提供 /api/labels 的 GET/POST/DELETE 接口', () => {
  assert.match(apiLabels, /APIRouter\(prefix="\/labels"/, 'labels API 前缀应为 /labels')
  assert.match(apiLabels, /@router\.get\(""\)/, '应有 GET /api/labels')
  assert.match(apiLabels, /@router\.post\(""\)/, '应有 POST /api/labels')
  assert.match(apiLabels, /@router\.delete\("\/\{name\}"\)/, '应有 DELETE /api/labels/{name}')
  assert.match(apiLabels, /默认标签，不可删除/, '删除默认标签应被拒绝')
})
