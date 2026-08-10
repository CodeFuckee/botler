import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'

export default function Repos() {
  const [repos, setRepos] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // 添加表单
  const [form, setForm] = useState({ url: '', name: '', webhook_url: '' })
  const [addError, setAddError] = useState('')
  // 测试结果: repoId -> {token/project/webhook}
  const [testResults, setTestResults] = useState({})

  const load = async () => {
    try {
      const data = await api.get('/api/repos')
      setRepos(data.repos)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  const addRepo = async () => {
    setAddError('')
    if (!form.url.trim()) { setAddError('请填写 GitLab 项目 URL 或 project_id'); return }
    setBusy(true)
    try {
      await api.post('/api/repos', {
        url: form.url.trim(),
        name: form.name.trim() || undefined,
        webhook_url: form.webhook_url.trim() || undefined,
      })
      setForm({ url: '', name: '', webhook_url: '' })
      await load()
    } catch (e) {
      setAddError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const toggle = async (repo) => {
    try {
      await api.put(`/api/repos/${repo.id}`, { enabled: !repo.enabled })
      await load()
    } catch (e) { setError(e.message) }
  }

  const remove = async (repo) => {
    if (!confirm(`确认删除仓库「${repo.name}」？将注销 webhook 并从配置中移除（任务历史保留）。`)) return
    try {
      await api.del(`/api/repos/${repo.id}`)
      await load()
    } catch (e) { setError(e.message) }
  }

  const test = async (repo) => {
    setTestResults((r) => ({ ...r, [repo.id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.id}/test`)
      setTestResults((r) => ({ ...r, [repo.id]: res }))
    } catch (e) {
      setTestResults((r) => ({ ...r, [repo.id]: { error: e.message } }))
    }
  }

  return (
    <div>
      <h1>仓库管理</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}

      <div className="card">
        <h2>添加仓库</h2>
        <div className="form-row">
          <input
            className="input grow"
            placeholder="GitLab 项目 URL 或 project_id（如 https://home.chenkaidi.top:509/group/project.git）"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
          />
          <input
            className="input"
            placeholder="显示名称（可选）"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <button className="btn btn-primary" disabled={busy} onClick={addRepo}>
            {busy ? '添加中…' : '添加'}
          </button>
        </div>
        <div className="form-row">
          <input
            className="input grow"
            placeholder="webhook 回调地址（可选，默认用当前访问地址；跨网络场景可覆盖）"
            value={form.webhook_url}
            onChange={(e) => setForm({ ...form, webhook_url: e.target.value })}
          />
        </div>
        {addError && <div className="alert alert-error">{addError}</div>}
      </div>

      <div className="card">
        <h2>仓库列表（{repos.length}）</h2>
        {repos.length === 0 && <p className="muted">还没有仓库。添加后平台会自动注册 webhook。</p>}
        {repos.map((repo) => (
          <div key={repo.id} className="repo-item">
            <div className="repo-main">
              <div className="repo-name">
                {repo.name}
                {!repo.enabled && <span className="badge badge-muted">已停用</span>}
              </div>
              <div className="muted small">{repo.url} · project_id={repo.gitlab_project_id}</div>
              {testResults[repo.id] && (
                <TestResult result={testResults[repo.id]} />
              )}
            </div>
            <div className="repo-actions">
              <button className="btn" onClick={() => test(repo)} disabled={testResults[repo.id]?.loading}>
                {testResults[repo.id]?.loading ? '测试中…' : '测试连通性'}
              </button>
              <Link className="btn" to={`/templates?repo=${repo.id}`}>模版</Link>
              <button className="btn" onClick={() => toggle(repo)}>
                {repo.enabled ? '停用' : '启用'}
              </button>
              <button className="btn btn-danger" onClick={() => remove(repo)}>删除</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function TestResult({ result }) {
  if (result.error) return <div className="alert alert-error small">{result.error}</div>
  const items = ['token', 'project', 'webhook'].map((k) => {
    const v = result[k]
    if (!v) return null
    const ok = v.ok === true
    return (
      <span key={k} className={`test-chip ${ok ? 'ok' : 'bad'}`}>
        {k}: {ok ? '✓' : (v.error || v.message || '✗')}
      </span>
    )
  })
  return <div className="small">{items}</div>
}
