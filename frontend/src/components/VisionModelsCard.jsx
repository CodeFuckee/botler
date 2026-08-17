import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { VISION_MODEL_PRESETS, visionModelPresetOf, visionModelName, ProviderLogo } from '../providers.jsx'

// 识图模型配置卡片（issue #152）：设置页内可增删改识图模型列表，
// 内置 Gemini 视觉 / OpenAI 视觉 两个预设 + 自定义（OpenAI 兼容
// chat/completions 接口），默认 Base URL / 模型自动填充（均可修改）。
//
// 与生图模型卡片（issue #135/#137）一致的交互约定：卡片内独立保存
// 按钮（只提交 vision_models 段，不影响其他设置）；API Key 留空 =
// 保持现有（后端掩码回填）。
//
// 测试按钮：用户配置好 Base URL / API Key / 识图模型类型后可点
// 「测试」验证配置是否可用——点击后弹出图片选择框，用户上传一张图片，
// 后端调用配置的识图模型描述图片内容，前端展示描述文本；编辑表单内
// 测试用当前表单值（未保存也可测），列表行测试用已保存配置；
// 后端 POST /api/settings/vision-model-test（multipart）返回
// ok / description / 错误原因。
function TestResult({ result }) {
  if (!result) return null
  return (
    <div className={`test-result ${result.ok ? 'ok' : 'err'}`}>
      <span className={result.ok ? 'saved-hint' : 'err-hint'}>{result.text}</span>
      {result.image && (
        <div className="vision-test-image">
          <img className="test-image" src={result.image} alt="测试图片" />
          {result.description && (
            <p className="vision-desc">模型描述：{result.description}</p>
          )}
        </div>
      )}
    </div>
  )
}

const EMPTY_FORM = {
  name: '',
  provider: 'gemini_vision',
  base_url: '',
  api_key: '',
  model: '',
  enabled: true,
}

export default function VisionModelsCard() {
  const [models, setModels] = useState(null) // null = 加载中
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  // 编辑表单：null = 列表模式；{ index, form }（index 为 null 表示新增）
  const [editing, setEditing] = useState(null)
  const [apiKeyInput, setApiKeyInput] = useState('')
  // 测试按钮状态（issue #152）：{ok, text, image, description}；
  // testBusy 防止重复点击；fileRef 隐藏文件选择框 + 待提交的测试载荷
  const [testBusy, setTestBusy] = useState(false)
  const [testResult, setTestResult] = useState(null)
  const [testPayload, setTestPayload] = useState(null)
  const fileRef = useRef(null)

  const load = () => {
    setError('')
    api.get('/api/settings')
      .then((s) => setModels(s.vision_models || []))
      .catch((e) => setError(e.message))
  }

  useEffect(() => { load() }, [])

  const startAdd = () => {
    setError(''); setSaved(false); setTestResult(null)
    const preset = visionModelPresetOf('gemini_vision')
    setEditing({
      index: null,
      form: { ...EMPTY_FORM, base_url: preset.baseUrl, model: preset.model },
    })
    setApiKeyInput('')
  }

  const startEdit = (i) => {
    setError(''); setSaved(false); setTestResult(null)
    setEditing({ index: i, form: { ...models[i] } })
    setApiKeyInput('')
  }

  const onProviderChange = (key) => {
    const preset = visionModelPresetOf(key)
    setEditing((e) => ({
      ...e,
      form: { ...e.form, provider: key, base_url: preset.baseUrl, model: preset.model },
    }))
  }

  const setForm = (key, val) =>
    setEditing((e) => ({ ...e, form: { ...e.form, [key]: val } }))

  // 识图模型测试（issue #152）：先让用户选择一张图片，再连同当前
  // 表单/已保存配置（multipart）提交后端，后端真实调用识图模型
  // 描述图片，返回 ok=true + description 或错误原因
  const testModel = async (payload) => {
    setTestPayload(payload)
    fileRef.current?.click() // 弹出图片选择框
  }

  // 用户选定图片后自动提交识别（空文件/未选 = 提示先上传图片）
  const onFileChange = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // 允许连续选择同一文件
    if (!file) return
    if (!testPayload) return
    setTestBusy(true); setTestResult(null)
    try {
      const fd = new FormData()
      fd.append('image', file)
      fd.append('name', testPayload.name)
      fd.append('provider', testPayload.provider)
      fd.append('base_url', testPayload.base_url)
      fd.append('api_key', testPayload.api_key)
      fd.append('model', testPayload.model)
      fd.append('prompt', testPayload.prompt || '')
      const res = await api.post('/api/settings/vision-model-test', fd)
      setTestResult(res.ok
        ? {
            ok: true,
            text: '✓ 识别成功，模型描述：',
            description: res.description,
            image: URL.createObjectURL(file),
          }
        : { ok: false, text: '✗ ' + (res.error || '识图测试失败') })
    } catch (err) { setTestResult({ ok: false, text: '✗ ' + err.message }) }
    finally {
      setTestBusy(false)
      setTestPayload(null)
    }
  }

  // 编辑表单内测试：用当前表单值（url / api key / 识图模型类型）
  const testCurrentForm = () => {
    testModel({
      name: editing.form.name.trim(),
      provider: editing.form.provider,
      base_url: editing.form.base_url,
      api_key: apiKeyInput.trim(),
      model: editing.form.model,
      prompt: '请详细描述这张图片的内容',
    })
  }

  // 表单确认：写入本地列表（最终保存统一 PUT 到后端）
  const commitForm = () => {
    const form = { ...editing.form, name: editing.form.name.trim() }
    if (!form.name) { setError('模型名称不能为空'); return }
    if (models.some((m, idx) => m.name === form.name && idx !== editing.index)) {
      setError(`模型名称重复: ${form.name}`); return
    }
    const list = [...models]
    const entry = { ...form, api_key: apiKeyInput.trim() }
    if (editing.index === null) list.push(entry)
    else list[editing.index] = entry
    setModels(list)
    setEditing(null)
    setApiKeyInput('')
    setTestResult(null)
  }

  const remove = async (i) => {
    setError(''); setSaved(false); setTestResult(null)
    if (!(await confirmDialog({ message: `删除识图模型「${models[i].name}」？`, danger: true }))) return
    setModels(models.filter((_, idx) => idx !== i))
  }

  const save = async () => {
    setBusy(true); setError(''); setSaved(false)
    try {
      await api.put('/api/settings', { vision_models: models })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (models === null) return null // 加载失败时由卡片外 settings 的错误提示兜底

  const editingPreset = editing ? visionModelPresetOf(editing.form.provider) : null

  return (
    <div className="card">
      <h2>识图模型</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok">✓ 已保存（已写回 config.yaml）</div>}

      {/* 隐藏文件选择框：点「测试」→ 选图 → 自动提交识别 */}
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        className="hidden-file-input"
        onChange={onFileChange}
      />

      <table className="table provider-table">
        <thead>
          <tr><th>模型</th><th>类型</th><th>默认模型</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          {models.length === 0 && (
            <tr><td colSpan={5} className="muted center">尚未配置识图模型，点击下方「添加模型」开始配置</td></tr>
          )}
          {models.map((m, i) => (
            <tr key={m.name}>
              <td>
                <span className="provider-cell">
                  <ProviderLogo provider={m.provider} />
                  <span>{m.name}</span>
                </span>
              </td>
              <td className="muted small">{visionModelName(m.provider)}</td>
              <td><code>{m.model || '—'}</code></td>
              <td>{m.enabled
                ? <span className="ok-text">✓ 启用</span>
                : <span className="muted">停用</span>}</td>
              <td>
                <button className="btn btn-sm" disabled={testBusy}
                  onClick={() => testModel({ name: m.name, provider: m.provider })}>
                  {testBusy ? '测试中…' : '测试'}
                </button>{' '}
                <button className="btn btn-sm" onClick={() => startEdit(i)}>编辑</button>{' '}
                <button className="btn btn-sm btn-danger" onClick={() => remove(i)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* 测试结果展示：成功显示所选图片 + 模型描述，失败显示错误原因 */}
      {!editing && <TestResult result={testResult} />}

      {editing && (
        <div className="provider-form">
          <h3>{editing.index === null ? '添加识图模型' : `编辑识图模型：${models[editing.index].name}`}</h3>
          <div className="form-row wrap">
            <label className="provider-field">
              名称
              <input
                className="input grow"
                placeholder="如：Gemini 视觉生产环境"
                value={editing.form.name}
                onChange={(e) => setForm('name', e.target.value)}
              />
            </label>
            <label className="provider-field">
              识图模型类型
              <span className="provider-select">
                <ProviderLogo provider={editingPreset.key} />
                <select
                  className="input"
                  value={editing.form.provider}
                  onChange={(e) => onProviderChange(e.target.value)}
                >
                  {VISION_MODEL_PRESETS.map((p) => (
                    <option key={p.key} value={p.key}>{p.name}</option>
                  ))}
                </select>
              </span>
            </label>
          </div>
          <div className="form-row wrap">
            <label className="provider-field">
              Base URL
              <input
                className="input grow"
                placeholder="https://api.example.com/v1"
                value={editing.form.base_url}
                onChange={(e) => setForm('base_url', e.target.value.trim())}
              />
            </label>
            <label className="provider-field">
              API Key
              <input
                className="input grow"
                type="password"
                placeholder={editing.index === null
                  ? '留空 = 暂不配置'
                  : '已配置（仅显示掩码），留空 = 保持现有'}
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
              />
            </label>
          </div>
          <div className="form-row wrap">
            <label className="provider-field">
              默认模型
              <input
                className="input grow"
                placeholder="如：gemini-2.5-flash"
                value={editing.form.model}
                onChange={(e) => setForm('model', e.target.value.trim())}
              />
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                className="check-input"
                checked={editing.form.enabled}
                onChange={(e) => setForm('enabled', e.target.checked)}
              />
              启用该模型
            </label>
          </div>
          <div className="form-row">
            <button className="btn btn-primary" onClick={commitForm}>
              {editing.index === null ? '添加到列表' : '确认修改'}
            </button>
            <button className="btn" disabled={testBusy} onClick={testCurrentForm}>
              {testBusy ? '识别中…' : '测试配置'}
            </button>
            <TestResult result={testResult} />
            <button className="btn" onClick={() => { setEditing(null); setApiKeyInput(''); setTestResult(null) }}>取消</button>
          </div>
        </div>
      )}

      {!editing && (
        <div className="form-row">
          <button className="btn" onClick={startAdd}>添加模型</button>
        </div>
      )}

      <div className="form-row">
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? '保存中…' : '保存识图模型配置'}
        </button>
        {saved && <span className="saved-hint">✓ 已保存</span>}
      </div>
      <p className="muted small">
        配置具有视觉理解能力的识图模型（本期仅存储配置并提供测试能力：
        点「测试」上传一张图片，后端调用模型描述图片内容）。内置预设：
        Gemini 视觉（默认模型 gemini-2.5-flash）与 OpenAI 视觉（默认模型
        gpt-4o），选择识图模型类型会自动填充默认 Base URL 与模型（均可
        修改）；自定义类型走 OpenAI 兼容 chat/completions 接口，需自填
        Base URL / 模型 / API Key。API Key 保存后仅显示掩码，编辑时留空 =
        保持现有。测试按钮：点击后请上传一张图片（png / jpg 等），后端用
        当前配置真实调用一次识图接口，返回模型对图片的描述（列表行测试
        用已保存配置，表单内测试用当前填写值，均不落盘）。自定义 Base URL
        （不等于官方预设默认值）将作为完整请求地址直接使用，不再自动拼接
        接口路径（如配置 https://api.example.com/v1/chat/completions 就
        直接请求该地址）；留空或使用预设默认值则按官方接口拼接
        （/chat/completions、:generateContent）。修改后点击「保存识图模型
        配置」写回 config.yaml，重启后不丢失。
      </p>
    </div>
  )
}
