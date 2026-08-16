import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { IMAGE_MODEL_PRESETS, imageModelPresetOf, imageModelName, ProviderLogo } from '../providers.jsx'

// 识图模型配置卡片（issue #135）：设置页内可增删改识图模型列表，
// 内置 Gemini Nano Banana Pro 与 GPT Image 2 两个预设（默认 Base URL /
// 模型自动填充，均可修改），为后续 AI 功能消费做准备。
//
// 与 AI 供应商卡片（issue #46）一致的交互约定：卡片内独立保存按钮
// （只提交 image_models 段，不影响其他设置）；API Key 留空 = 保持现有
// （后端掩码回填）。
const EMPTY_FORM = {
  name: '',
  provider: 'gemini_nano_banana',
  base_url: '',
  api_key: '',
  model: '',
  enabled: true,
}

export default function ImageModelsCard() {
  const [models, setModels] = useState(null) // null = 加载中
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  // 编辑表单：null = 列表模式；{ index, form }（index 为 null 表示新增）
  const [editing, setEditing] = useState(null)
  const [apiKeyInput, setApiKeyInput] = useState('')

  const load = () => {
    setError('')
    api.get('/api/settings')
      .then((s) => setModels(s.image_models || []))
      .catch((e) => setError(e.message))
  }

  useEffect(() => { load() }, [])

  const startAdd = () => {
    setError(''); setSaved(false)
    const preset = imageModelPresetOf('gemini_nano_banana')
    setEditing({
      index: null,
      form: { ...EMPTY_FORM, base_url: preset.baseUrl, model: preset.model },
    })
    setApiKeyInput('')
  }

  const startEdit = (i) => {
    setError(''); setSaved(false)
    setEditing({ index: i, form: { ...models[i] } })
    setApiKeyInput('')
  }

  const onProviderChange = (key) => {
    const preset = imageModelPresetOf(key)
    setEditing((e) => ({
      ...e,
      form: { ...e.form, provider: key, base_url: preset.baseUrl, model: preset.model },
    }))
  }

  const setForm = (key, val) =>
    setEditing((e) => ({ ...e, form: { ...e.form, [key]: val } }))

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
  }

  const remove = async (i) => {
    setError(''); setSaved(false)
    if (!(await confirmDialog({ message: `删除识图模型「${models[i].name}」？`, danger: true }))) return
    setModels(models.filter((_, idx) => idx !== i))
  }

  const save = async () => {
    setBusy(true); setError(''); setSaved(false)
    try {
      await api.put('/api/settings', { image_models: models })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (models === null) return null // 加载失败时由卡片外 settings 的错误提示兜底

  const editingPreset = editing ? imageModelPresetOf(editing.form.provider) : null

  return (
    <div className="card">
      <h2>识图模型</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok">✓ 已保存（已写回 config.yaml）</div>}

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
              <td className="muted small">{imageModelName(m.provider)}</td>
              <td><code>{m.model || '—'}</code></td>
              <td>{m.enabled
                ? <span className="ok-text">✓ 启用</span>
                : <span className="muted">停用</span>}</td>
              <td>
                <button className="btn btn-sm" onClick={() => startEdit(i)}>编辑</button>{' '}
                <button className="btn btn-sm btn-danger" onClick={() => remove(i)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editing && (
        <div className="provider-form">
          <h3>{editing.index === null ? '添加识图模型' : `编辑识图模型：${models[editing.index].name}`}</h3>
          <div className="form-row wrap">
            <label className="provider-field">
              名称
              <input
                className="input grow"
                placeholder="如：Gemini 生产环境"
                value={editing.form.name}
                onChange={(e) => setForm('name', e.target.value)}
              />
            </label>
            <label className="provider-field">
              模型类型
              <span className="provider-select">
                <ProviderLogo provider={editingPreset.key} />
                <select
                  className="input"
                  value={editing.form.provider}
                  onChange={(e) => onProviderChange(e.target.value)}
                >
                  {IMAGE_MODEL_PRESETS.map((p) => (
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
                placeholder="如：gemini-3-pro-image"
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
            <button className="btn" onClick={() => { setEditing(null); setApiKeyInput('') }}>取消</button>
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
        配置可供图片理解/生成的识图模型（为后续 AI 功能消费做准备，本期仅存储配置并
        提供后端调用接口，不接入具体业务）。内置预设：Gemini Nano Banana Pro
        （默认模型 gemini-3-pro-image）与 OpenAI GPT Image 2（默认模型 gpt-image-2），
        选择类型会自动填充默认 Base URL 与模型（均可修改）；API Key 保存后仅显示掩码，
        编辑时留空 = 保持现有。修改后点击「保存识图模型配置」写回 config.yaml，重启后不丢失。
      </p>
    </div>
  )
}
