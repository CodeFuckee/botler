import { useEffect, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { AI_PROVIDER_PRESETS, presetOf, providerName, ProviderLogo } from '../providers.jsx'

// AI API 供应商配置卡片（issue #46）：设置页内可增删改供应商列表，
// 为后续 AI 功能消费做准备（本期纯配置存储，不接入实际调用）。
//
// 与 SSO 卡片一致的交互约定：卡片内独立保存按钮（只提交 ai_providers 段，
// 不影响其他设置）；API Key 留空 = 保持现有（后端掩码回填）。
const EMPTY_FORM = { name: '', provider: 'deepseek', base_url: '', api_key: '', model: '', enabled: true, priority: 100 }

export default function AiProvidersCard() {
  const [providers, setProviders] = useState(null) // null = 加载中
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  // 编辑表单：null = 列表模式；{ index, form }（index 为 null 表示新增）
  const [editing, setEditing] = useState(null)
  const [apiKeyInput, setApiKeyInput] = useState('')
  // 获取模型（issue #499）：modelOptions = null 未加载 / [] 无模型 / [ids] 已加载
  const [modelOptions, setModelOptions] = useState(null)
  const [fetchingModels, setFetchingModels] = useState(false)

  const load = () => {
    setError('')
    api.get('/api/settings')
      .then((s) => setProviders(s.ai_providers || []))
      .catch((e) => setError(e.message))
  }

  useEffect(() => { load() }, [])

  const startAdd = () => {
    setError(''); setSaved(false)
    const preset = presetOf('deepseek')
    setEditing({
      index: null,
      form: { ...EMPTY_FORM, base_url: preset.baseUrl, model: preset.model },
    })
    setApiKeyInput(''); setModelOptions(null)
  }

  const startEdit = (i) => {
    setError(''); setSaved(false)
    setEditing({ index: i, form: { ...providers[i] } })
    setApiKeyInput(''); setModelOptions(null)
  }

  const onProviderChange = (key) => {
    const preset = presetOf(key)
    setEditing((e) => ({
      ...e,
      form: { ...e.form, provider: key, base_url: preset.baseUrl, model: preset.model },
    }))
  }

  const setForm = (key, val) =>
    setEditing((e) => ({ ...e, form: { ...e.form, [key]: val } }))

  // 获取模型（issue #499）：经后端代理 POST /api/ai/list-models 调
  // OpenAI 兼容 {base_url}/models，拿回模型列表供用户选择。
  // apiKeyInput 留空 / 掩码时后端按 name 匹配已保存 Key（与保存同语义）。
  const fetchModels = async () => {
    const url = (editing.form.base_url || '').trim()
    if (!url) { setError('请先填写 Base URL 再获取模型'); return }
    setFetchingModels(true); setError(''); setModelOptions(null)
    try {
      const data = await api.post('/api/ai/list-models', {
        base_url: url,
        api_key: apiKeyInput.trim(),
        name: (editing.form.name || '').trim(),
      })
      const models = data.models || []
      setModelOptions(models)
      if (models.length === 0) setError('未获取到可用模型，请检查 Base URL / API Key')
    } catch (e) {
      setError(e.message)
    } finally {
      setFetchingModels(false)
    }
  }

  const pickModel = (m) => {
    setForm('model', m)
    setModelOptions(null)
  }

  // 表单确认：写入本地列表（最终保存统一 PUT 到后端）
  const commitForm = () => {
    const form = { ...editing.form, name: editing.form.name.trim() }
    if (!form.name) { setError('供应商名称不能为空'); return }
    if (providers.some((p, idx) => p.name === form.name && idx !== editing.index)) {
      setError(`供应商名称重复: ${form.name}`); return
    }
    // issue #495：优先级 1~999 整数（数字小优先级高，缺省 100，
    // 与仓库调度优先级 repos[].priority 同语义）
    const pri = Number(form.priority)
    if (!Number.isInteger(pri) || pri < 1 || pri > 999) {
      setError('优先级必须是 1~999 的整数（数字越小优先级越高）'); return
    }
    form.priority = pri
    const list = [...providers]
    const entry = { ...form, api_key: apiKeyInput.trim() }
    if (editing.index === null) list.push(entry)
    else list[editing.index] = entry
    setProviders(list)
    setEditing(null)
    setApiKeyInput(''); setModelOptions(null)
  }

  const remove = async (i) => {
    setError(''); setSaved(false)
    if (!(await confirmDialog({ message: `删除供应商「${providers[i].name}」？`, danger: true }))) return
    setProviders(providers.filter((_, idx) => idx !== i))
  }

  const save = async () => {
    setBusy(true); setError(''); setSaved(false)
    try {
      await api.put('/api/settings', { ai_providers: providers })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (providers === null) {
    // 加载中/加载失败也渲染卡片标题 h2（issue #174）：SettingsNav 挂载时
    // 只读取一次 DOM，若这里 return null 区块内没有标题元素，左侧导航会把
    // 原始 id（settings-ai-providers）当名称展示；与 BackupManager 加载态同款。
    return (
      <div className="card">
        <h2>AI API 供应商</h2>
        {error
          ? <div className="alert alert-error" onClick={load}>{error}（点击重试）</div>
          : <p className="muted">加载中…</p>}
      </div>
    )
  }

  const editingPreset = editing ? presetOf(editing.form.provider) : null

  return (
    <div className="card">
      <h2>AI API 供应商</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok"><Icon name="check" /> 已保存（已写回 config.yaml）</div>}

      <table className="table provider-table">
        <thead>
          <tr><th>供应商</th><th>类型</th><th>默认模型</th><th>优先级</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          {providers.length === 0 && (
            <tr><td colSpan={6} className="muted center">尚未配置供应商，点击下方「添加供应商」开始配置</td></tr>
          )}
          {providers.map((p, i) => (
            <tr key={p.name}>
              <td>
                <span className="provider-cell">
                  <ProviderLogo provider={p.provider} />
                  <span>{p.name}</span>
                </span>
              </td>
              <td className="muted small">{providerName(p.provider)}</td>
              <td><code>{p.model || '—'}</code></td>
              <td><code>{p.priority ?? 100}</code></td>
              <td>{p.enabled
                ? <span className="ok-text"><Icon name="check" /> 启用</span>
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
          <h3>{editing.index === null ? '添加供应商' : `编辑供应商：${providers[editing.index].name}`}</h3>
          <div className="form-row wrap">
            <label className="provider-field">
              名称
              <input
                className="input grow"
                placeholder="如：DeepSeek 生产环境"
                value={editing.form.name}
                onChange={(e) => setForm('name', e.target.value)}
              />
            </label>
            <label className="provider-field">
              供应商类型
              <span className="provider-select">
                <ProviderLogo provider={editingPreset.key} />
                <select
                  className="input"
                  value={editing.form.provider}
                  onChange={(e) => onProviderChange(e.target.value)}
                >
                  {AI_PROVIDER_PRESETS.map((p) => (
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
              <span className="provider-model-row">
                <input
                  className="input grow"
                  placeholder="如：deepseek-chat"
                  value={editing.form.model}
                  onChange={(e) => setForm('model', e.target.value.trim())}
                />
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={fetchingModels}
                  onClick={fetchModels}
                >
                  {fetchingModels ? '获取中…' : '获取模型'}
                </button>
              </span>
              {modelOptions && modelOptions.length > 0 && (
                <span className="model-picker">
                  {modelOptions.map((m) => (
                    <button
                      key={m}
                      type="button"
                      className="btn btn-sm"
                      onClick={() => pickModel(m)}
                    >
                      {m}
                    </button>
                  ))}
                </span>
              )}
            </label>
            <label className="provider-field">
              优先级
              <input
                className="input grow"
                type="number"
                min="1"
                max="999"
                placeholder="1~999，数字越小优先级越高"
                value={editing.form.priority ?? 100}
                onChange={(e) => setForm('priority', e.target.value)}
              />
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                className="check-input"
                checked={editing.form.enabled}
                onChange={(e) => setForm('enabled', e.target.checked)}
              />
              启用该供应商
            </label>
          </div>
          <div className="form-row">
            <button className="btn btn-primary" onClick={commitForm}>
              {editing.index === null ? '添加到列表' : '确认修改'}
            </button>
            <button className="btn" onClick={() => { setEditing(null); setApiKeyInput(''); setModelOptions(null) }}>取消</button>
          </div>
        </div>
      )}

      {!editing && (
        <div className="form-row">
          <button className="btn" onClick={startAdd}>添加供应商</button>
        </div>
      )}

      <div className="form-row">
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? '保存中…' : '保存 AI 供应商配置'}
        </button>
        {saved && <span className="saved-hint"><Icon name="check" /> 已保存</span>}
      </div>
      <p className="muted small">
        为 AI 功能配置可用的 API 供应商。选择预设类型会自动填充默认 Base URL 与模型
        （均可修改）；点击「获取模型」经 OpenAI 兼容接口（GET {'{base_url}'}/models）
        拉取该供应商可用模型供选择；API Key 保存后仅显示掩码，编辑时留空 = 保持现有。
        优先级为 1~999 整数（数字越小优先级越高，缺省 100）：调用 AI 时优先使用
        启用的高优先级供应商，该供应商额度不足 / 调用失败后自动切换下一个启用的
        供应商。修改后点击「保存 AI 供应商配置」写回 config.yaml，重启后不丢失。
      </p>
    </div>
  )
}
