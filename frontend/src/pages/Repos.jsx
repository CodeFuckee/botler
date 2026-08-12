import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import FolderPicker from '../components/FolderPicker.jsx'

export default function Repos() {
  const [repos, setRepos] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // 添加表单（method: 'url' = GitLab URL 方式，'local' = 本地文件夹方式；默认本地文件夹方式）
  const [method, setMethod] = useState('local')
  const [form, setForm] = useState({ url: '', local_path: '', remote_name: '', name: '', webhook_url: '' })
  const [remotes, setRemotes] = useState([])
  const [addError, setAddError] = useState('')
  // 服务器目录选择对话框
  const [pickerOpen, setPickerOpen] = useState(false)
  // 测试结果: repoId -> {token/project/webhook}
  const [testResults, setTestResults] = useState({})
  // 对账结果: repoId -> {scanned/enqueued/note/error}（issue #17）
  const [reconcileResults, setReconcileResults] = useState({})

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
    if (method === 'url' && !form.url.trim()) { setAddError('请填写 GitLab 项目 URL 或 project_id'); return }
    if (method === 'local') {
      if (!form.local_path.trim()) { setAddError('请填写本地文件夹路径'); return }
      if (!form.remote_name) { setAddError('请先在本地文件夹中选择一个 remote'); return }
    }
    setBusy(true)
    try {
      await api.post('/api/repos', {
        url: method === 'url' ? form.url.trim() : undefined,
        local_path: method === 'local' ? form.local_path.trim() : undefined,
        remote_name: method === 'local' ? form.remote_name : undefined,
        name: form.name.trim() || undefined,
        webhook_url: form.webhook_url.trim() || undefined,
      })
      setForm({ url: '', local_path: '', remote_name: '', name: '', webhook_url: '' })
      setRemotes([])
      await load()
    } catch (e) {
      setAddError(e.message)
    } finally {
      setBusy(false)
    }
  }

  // 本地文件夹方式：读取该文件夹 git remote -v 的 remote 列表
  const discover = async (path) => {
    setAddError('')
    const target = (path ?? form.local_path).trim()
    if (!target) { setAddError('请填写本地文件夹路径'); return }
    setBusy(true)
    try {
      const res = await api.post('/api/repos/discover', { local_path: target })
      setRemotes(res.remotes)
      setForm((f) => ({ ...f, remote_name: res.remotes.length === 1 ? res.remotes[0].name : '' }))
    } catch (e) {
      setAddError(e.message)
      setRemotes([])
    } finally {
      setBusy(false)
    }
  }

  // 目录选择对话框选中：回填路径并自动读取 remote
  const handlePickFolder = (path) => {
    setPickerOpen(false)
    setForm((f) => ({ ...f, local_path: path, remote_name: '' }))
    setRemotes([])
    discover(path)
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

  // 对账：立即扫描该仓库，把「分配给了 bot 但还没有任务」的 open issues 补入队
  const reconcile = async (repo) => {
    setReconcileResults((r) => ({ ...r, [repo.id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.id}/reconcile`)
      setReconcileResults((r) => ({ ...r, [repo.id]: res }))
    } catch (e) {
      setReconcileResults((r) => ({ ...r, [repo.id]: { error: e.message } }))
    }
  }

  return (
    <div>
      <h1>仓库管理</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}

      <div className="card">
        <h2>添加仓库</h2>
        <div className="form-row">
          <label className="add-method">
            <input type="radio" checked={method === 'url'} onChange={() => { setMethod('url'); setRemotes([]); }} />
            GitLab URL / project_id
          </label>
          <label className="add-method">
            <input type="radio" checked={method === 'local'} onChange={() => { setMethod('local'); setRemotes([]); }} />
            本地文件夹（读取 git remote）
          </label>
        </div>

        {method === 'url' && (
          <div className="form-row">
            <input
              className="input grow"
              placeholder="GitLab 项目 URL 或 project_id（如 https://home.chenkaidi.top:509/group/project.git）"
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
            />
          </div>
        )}

        {method === 'local' && (
          <>
            <div className="form-row">
              <input
                className="input grow"
                placeholder="服务器上的本地 git 仓库文件夹路径（如 /home/user/projects/my-repo）"
                value={form.local_path}
                onChange={(e) => setForm({ ...form, local_path: e.target.value })}
              />
              <button className="btn" disabled={busy} onClick={() => setPickerOpen(true)}>
                浏览…
              </button>
              <button className="btn" disabled={busy} onClick={() => discover()}>
                {busy ? '读取中…' : '读取 remote'}
              </button>
            </div>
            {remotes.length > 0 && (
              <div className="form-row remote-list">
                {remotes.map((r) => (
                  <label key={r.name} className="remote-option">
                    <input
                      type="radio"
                      name="remote"
                      checked={form.remote_name === r.name}
                      onChange={() => setForm({ ...form, remote_name: r.name })}
                    />
                    <code>{r.name}</code> {r.url}
                  </label>
                ))}
              </div>
            )}
          </>
        )}

        <div className="form-row">
          <input
            className="input grow"
            placeholder="显示名称（可选）"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <input
            className="input grow"
            placeholder="webhook 回调地址（可选，默认用当前访问地址；跨网络场景可覆盖）"
            value={form.webhook_url}
            onChange={(e) => setForm({ ...form, webhook_url: e.target.value })}
          />
        </div>
        <div className="form-row center">
          <button className="btn btn-primary btn-wide" disabled={busy} onClick={addRepo}>
            {busy ? '添加中…' : '添加'}
          </button>
        </div>
        {addError && <div className="alert alert-error">{addError}</div>}
      </div>

      <FolderPicker
        open={pickerOpen}
        initialPath={form.local_path || ''}
        onSelect={handlePickFolder}
        onClose={() => setPickerOpen(false)}
      />

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
              {repo.local_path && (
                <div className="muted small">本地工作区: {repo.local_path}</div>
              )}
              {testResults[repo.id] && (
                <TestResult result={testResults[repo.id]} />
              )}
              {reconcileResults[repo.id] && (
                <ReconcileResult result={reconcileResults[repo.id]} />
              )}
            </div>
            <div className="repo-actions">
              <button className="btn" onClick={() => test(repo)} disabled={testResults[repo.id]?.loading}>
                {testResults[repo.id]?.loading ? '测试中…' : '测试连通性'}
              </button>
              <button className="btn" onClick={() => reconcile(repo)} disabled={reconcileResults[repo.id]?.loading}>
                {reconcileResults[repo.id]?.loading ? '对账中…' : '对账'}
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

// 对账结果（issue #17）：入队 N 个 = 发现待处理；0 个 = 无需处理
function ReconcileResult({ result }) {
  if (result.error) return <div className="alert alert-error small">{result.error}</div>
  if (result.note) return <div className="small muted">{result.note}</div>
  return (
    <div className="small">
      {result.enqueued > 0
        ? <span className="test-chip ok">✓ {result.enqueued} 个待处理 issue 已入队</span>
        : <span className="test-chip ok">✓ 无需处理</span>}
      {result.scanned > 0 && <span className="muted">扫描 {result.scanned} 个 issue</span>}
    </div>
  )
}
