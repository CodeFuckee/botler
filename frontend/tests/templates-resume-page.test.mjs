// 模版页「中断恢复模版」测试（issue #116）：中断恢复引导语此前硬编码
// 在 executor.py 不可编辑，现与全局默认模版同机制（config.yaml +
// settings API + Web UI 模版页）开放编辑。本测试断言模版页：
// 1. 加载时读取 settings.templates.resume 作为可编辑基线；
// 2. 提供「中断恢复模版」视图切换按钮（与全局默认/仓库级并列）；
// 3. 保存走 PUT /api/settings { templates: { resume } }；
// 4. 界面注明「留空保存即恢复内置默认」；
// 5. 后端 GET/PUT /api/settings 的 templates 段包含 resume。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const templates = readFileSync(path.join(ROOT, 'src/pages/Templates.jsx'), 'utf8')
const settingsApi = readFileSync(path.join(ROOT, '../backend/botler/api/settings.py'), 'utf8')

test('模版页加载时读取 settings.templates.resume（可编辑基线）', () => {
  assert.match(templates, /setResumeTemplate\(settings\.templates\.resume/, 'load 应读取 resume 模版')
})

test('模版页提供「中断恢复模版」视图切换按钮', () => {
  assert.match(templates, /中断恢复模版/, '应有「中断恢复模版」按钮文案')
  assert.match(templates, /selected\?\.kind === 'resume'/, '选中态按 kind === resume 高亮')
  assert.match(templates, /onClick=\{selectResume\}/, '按钮点击切换 resume 视图')
})

test('resume 视图切换加载已保存文本并复用全局占位符表', () => {
  assert.match(templates, /const selectResume = \(\) => \{/, '应有 selectResume 处理函数')
  assert.match(templates, /setText\(resumeTemplate\)/, '切换时加载 resumeTemplate 到编辑器')
  assert.match(templates, /setPlaceholders\(globalPlaceholders\)/, '占位符表复用全局占位符')
})

test('保存中断恢复模版走 PUT /api/settings { templates: { resume } }', () => {
  assert.match(
    templates,
    /api\.put\('\/api\/settings', \{ templates: \{ resume: text \} \}\)/,
    '保存应提交 templates.resume',
  )
  assert.match(templates, /setResumeTemplate\(settings\.templates\.resume/, '保存后回写最新值')
})

test('界面注明留空保存即恢复内置默认', () => {
  assert.match(templates, /留空保存即恢复内置默认/, '应有恢复内置默认的提示文案')
})

test('后端 GET /api/settings 返回 templates.resume，PUT 支持写入', () => {
  assert.match(settingsApi, /"resume": s\.resume_template/, 'GET templates 段应包含 resume')
  assert.match(settingsApi, /"resume" in tpl/, 'PUT 应处理 templates.resume 键')
  assert.match(settingsApi, /update_resume_template\(tpl\["resume"\]\)/, 'PUT 应调用 update_resume_template')
  assert.match(settingsApi, /templates\.resume 必须是字符串/, '非字符串应 400 拒绝')
})
