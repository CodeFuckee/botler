import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import FolderPicker from '../components/FolderPicker.jsx'
import RepoEditModal from '../components/RepoEditModal.jsx'
import { Icon } from '../components/Icon.jsx'

export default function Repos() {
  const [repos, setRepos] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // 设置弹窗编辑中的仓库（null = 关闭；issue #51）
  const [editing, setEditing] = useState(null)

  // 添加表单（method: 'url' = GitLab URL 方式，'local' = 本地文件夹方式；默认本地文件夹方式）
  const [method, setMethod] = useState('local')
  const [form, setForm] = useState({ url: '', local_path: '', remote_name: '', name: '', webhook_url: '', priority: '100' })
  const [remotes, setRemotes] = useState([])
  const [addError, setAddError] = useState('')
  // 服务器目录选择对话框
  const [pickerOpen, setPickerOpen] = useState(false)
  // 测试结果: repoId -> {token/project/webhook}
  const [testResults, setTestResults] = useState({})
  // 对账结果: repoId -> {scanned/enqueued/note/error}（issue #17）
  const [reconcileResults, setReconcileResults] = useState({})
  // 生成图标结果（issue #188）: repoId -> {loading/logo_prompt/error}
  const [logoResults, setLogoResults] = useState({})
  // 放大查看中的 logo（issue #188）: repo 对象（null = 关闭弹窗）
  const [viewLogo, setViewLogo] = useState(null)

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
    // 调度优先级（issue #161）：1~999 整数，数字越小越优先；留空按后端默认 100
    let priority
    if (form.priority.trim() !== '') {
      const num = Number(form.priority.trim())
      if (!Number.isInteger(num) || num < 1 || num > 999) {
        setAddError('优先级需为 1~999 之间的整数')
        return
      }
      priority = num
    }
    setBusy(true)
    try {
      await api.post('/api/repos', {
        url: method === 'url' ? form.url.trim() : undefined,
        local_path: method === 'local' ? form.local_path.trim() : undefined,
        remote_name: method === 'local' ? form.remote_name : undefined,
        name: form.name.trim() || undefined,
        webhook_url: form.webhook_url.trim() || undefined,
        priority,
      })
      setForm({ url: '', local_path: '', remote_name: '', name: '', webhook_url: '', priority: '100' })
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
    if (!(await confirmDialog({ message: `确认删除仓库「${repo.name}」？将注销 webhook 并从配置中移除（任务历史保留）。`, danger: true }))) return
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

  // 生成图标（issue #188）：点击调后端同步接口——agent 基于该仓库
  // README 生成 logo 提示词并调用生图模型生成 logo；成功后刷新列表
  // 展示新 logo。请求中禁用防重复点击，与「自省」「对账」同风格。
  const generateLogo = async (repo) => {
    setLogoResults((r) => ({ ...r, [repo.id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.id}/generate-logo`)
      setLogoResults((r) => ({ ...r, [repo.id]: res }))
      await load()
    } catch (e) {
      setLogoResults((r) => ({ ...r, [repo.id]: { error: e.message } }))
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
            <input type="radio" checked={method === 'local'} onChange={() => { setMethod('local'); setRemotes([]); }} />
            本地文件夹（读取 git remote）
          </label>
          <label className="add-method">
            <input type="radio" checked={method === 'url'} onChange={() => { setMethod('url'); setRemotes([]); }} />
            GitLab URL / project_id
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
        <div className="form-row">
          <input
            className="input grow"
            placeholder="调度优先级（默认 100；1~999 整数，数字越小越优先）"
            value={form.priority}
            onChange={(e) => setForm({ ...form, priority: e.target.value })}
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
        {repos.length === 0 && (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="package" /></span>
            <p className="muted">还没有仓库。添加后平台会自动注册 webhook。</p>
          </div>
        )}
        {repos.map((repo) => (
          <div key={repo.id} className="repo-item">
            {/* issue #188：每个仓库最左侧展示已生成 logo——点击放大并
                支持下载；未生成时显示占位图标，提示可点右侧「生成图标」 */}
            <div className="repo-logo">
              {repo.logo_path ? (
                <button type="button" className="repo-logo-btn"
                        onClick={() => setViewLogo(repo)}
                        title="点击放大 logo 并下载">
                  <img
                    src={`/api/repos/${repo.id}/logo?v=${encodeURIComponent(repo.logo_updated_at || '')}`}
                    alt={`${repo.name} logo`}
                  />
                </button>
              ) : (
                <span className="repo-logo-placeholder" title="尚未生成 logo，点击右侧「生成图标」">
                  <Icon name="image" />
                </span>
              )}
            </div>
            <div className="repo-main">
              <div className="repo-name">
                {repo.name}
                {!repo.enabled && <span className="badge badge-muted">已停用</span>}
                <span className="badge badge-muted" title="调度优先级：数字越小越优先">
                  优先级 {repo.priority ?? 100}
                </span>
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
              {logoResults[repo.id] && <LogoResult result={logoResults[repo.id]} />}
            </div>
            <div className="repo-actions">
              <button className="btn logo-btn" onClick={() => generateLogo(repo)}
                      disabled={logoResults[repo.id]?.loading}
                      title="agent 基于该仓库 README 生成 logo 提示词，调用生图模型生成 logo">
                {logoResults[repo.id]?.loading ? <><Icon name="refresh" /> 生成中…</> : <><Icon name="sparkles" /> 生成图标</>}
              </button>
              <button className="btn" onClick={() => test(repo)} disabled={testResults[repo.id]?.loading}>
                {testResults[repo.id]?.loading ? '测试中…' : '测试连通性'}
              </button>
              <button className="btn" onClick={() => reconcile(repo)} disabled={reconcileResults[repo.id]?.loading}>
                {reconcileResults[repo.id]?.loading ? '对账中…' : '对账'}
              </button>
              <Link className="btn" to={`/templates?repo=${repo.id}`}>模版</Link>
              <button className="btn" onClick={() => setEditing(repo)}>设置</button>
              <button className="btn" onClick={() => toggle(repo)}>
                {repo.enabled ? '停用' : '启用'}
              </button>
              <button className="btn btn-danger" onClick={() => remove(repo)}>删除</button>
            </div>
          </div>
        ))}
      </div>

      {editing && (
        <RepoEditModal
          repo={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await load()
          }}
        />
      )}

      {/* issue #188：logo 放大查看 + 下载弹窗 */}
      {viewLogo && <LogoViewModal repo={viewLogo} onClose={() => setViewLogo(null)} />}
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
        {k}: {ok ? <Icon name="check" /> : (v.error || v.message || <Icon name="x" />)}
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
        ? <span className="test-chip ok"><Icon name="check" /> {result.enqueued} 个待处理 issue 已入队</span>
        : <span className="test-chip ok"><Icon name="check" /> 无需处理</span>}
      {result.scanned > 0 && <span className="muted">扫描 {result.scanned} 个 issue</span>}
    </div>
  )
}

// 生成图标结果（issue #188）：生成中提示 / 成功（含生成提示词悬浮说明，
// 点击缩略图可放大下载）/ 失败展示后端错误信息
function LogoResult({ result }) {
  if (result.loading) return (
    <div className="small muted"><Icon name="refresh" /> logo 生成中，请稍候…</div>
  )
  if (result.error) return <div className="alert alert-error small">{result.error}</div>
  return (
    <div className="small">
      <span className="test-chip ok"
            title={result.logo_prompt ? `生成提示词：${result.logo_prompt}` : '已生成 logo'}>
        <Icon name="check" /> 已生成 logo
      </span>
    </div>
  )
}

// logo 放大查看 + 下载弹窗（issue #188）：点击仓库最左侧 logo 打开，
// 大图展示 + 「下载 logo」链接（后端 ?download=1 返回 attachment）
function LogoViewModal({ repo, onClose }) {
  const src = `/api/repos/${repo.id}/logo?v=${encodeURIComponent(repo.logo_updated_at || '')}`
  const downloadHref = `/api/repos/${repo.id}/logo?download=1`
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal repo-logo-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>{repo.name} Logo</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭弹窗"><Icon name="x" /></button>
        </div>
        <div className="repo-logo-view">
          <img src={src} alt={`${repo.name} logo`} />
        </div>
        <div className="modal-footer">
          <a className="btn btn-primary" href={downloadHref} download>
            <Icon name="download" /> 下载 logo
          </a>
          <button className="btn" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  )
}
