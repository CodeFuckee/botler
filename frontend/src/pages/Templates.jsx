import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'

export default function Templates() {
  const [params, setParams] = useSearchParams()
  const [repos, setRepos] = useState([])
  const [globalTemplate, setGlobalTemplate] = useState('')
  const [placeholders, setPlaceholders] = useState({})
  const [selected, setSelected] = useState(null) // {repoId|null, isOverride}
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    const [reposData, settings] = await Promise.all([
      api.get('/api/repos'),
      api.get('/api/settings'),
    ])
    setRepos(reposData.repos)
    setGlobalTemplate(settings.templates.default)
    const repoParam = Number(params.get('repo')) || null
    if (repoParam) {
      await selectRepo(repoParam)
    } else {
      setSelected({ repoId: null, isOverride: false })
      setText(settings.templates.default)
    }
  }

  useEffect(() => { load().catch((e) => setError(e.message)) }, [])

  const selectRepo = async (repoId) => {
    const data = await api.get(`/api/repos/${repoId}/template`)
    setSelected({ repoId, isOverride: data.is_override })
    setText(data.template)
    setPlaceholders(data.placeholders)
  }

  const save = async () => {
    setError('')
    setSaved(false)
    try {
      if (selected.repoId === null) {
        const settings = await api.get('/api/settings')
        await api.put('/api/settings', { templates: { default: text } })
        setGlobalTemplate(text)
        void settings
      } else {
        await api.put(`/api/repos/${selected.repoId}/template`, { template: text })
        await selectRepo(selected.repoId)
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setError(e.message)
    }
  }

  const clearOverride = async () => {
    if (!confirm('清空仓库级模版覆盖，回退为全局默认模版？')) return
    try {
      await api.put(`/api/repos/${selected.repoId}/template`, { template: '' })
      await selectRepo(selected.repoId)
    } catch (e) { setError(e.message) }
  }

  const selectGlobal = () => {
    setSelected({ repoId: null, isOverride: false })
    setText(globalTemplate)
    setPlaceholders({})
  }

  return (
    <div>
      <h1>提示词模版</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}

      <div className="card">
        <div className="form-row wrap">
          <button
            className={'btn ' + (selected?.repoId === null ? 'btn-primary' : '')}
            onClick={selectGlobal}
          >
            全局默认模版
          </button>
          {repos.map((r) => (
            <button
              key={r.id}
              className={'btn ' + (selected?.repoId === r.id ? 'btn-primary' : '')}
              onClick={() => selectRepo(r.id)}
            >
              {r.name}{!r.enabled ? '（停用）' : ''}
            </button>
          ))}
        </div>

        <p className="muted small">
          {selected?.repoId === null
            ? '全局默认模版：所有未配置仓库级模版的仓库使用。'
            : selected?.isOverride
              ? '仓库级模版：覆盖全局默认。'
              : '该仓库未配置覆盖，当前显示全局默认模版。编辑并保存即创建覆盖。'}
        </p>

        <textarea
          className="input textarea"
          rows={18}
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
        />

        <div className="form-row">
          <button className="btn btn-primary" onClick={save}>保存</button>
          {saved && <span className="saved-hint">✓ 已保存</span>}
          {selected?.repoId !== null && selected?.isOverride && (
            <button className="btn" onClick={clearOverride}>清空覆盖</button>
          )}
        </div>
      </div>

      <div className="card">
        <h2>可用变量占位符</h2>
        <table className="table">
          <thead><tr><th>占位符</th><th>含义</th></tr></thead>
          <tbody>
            {Object.entries(placeholders).map(([k, v]) => (
              <tr key={k}>
                <td><code>{'{' + k + '}'}</code></td>
                <td>{v}</td>
              </tr>
            ))}
            {selected?.repoId !== null && Object.keys(placeholders).length === 0 && (
              <tr><td colSpan={2}>（仓库数据未加载）</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
