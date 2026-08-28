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

  // 添加表单（method: 'url' = GitLab URL 方式，'local' = 本地文件夹方式，
  // 'remote' = 远程服务器方式（SSH）；默认本地文件夹方式）
  const [method, setMethod] = useState('local')
  const [form, setForm] = useState({ url: '', local_path: '', remote_host: '', remote_path: '', remote_name: '', name: '', webhook_url: '', priority: '100', token_expires_at: '' })
  const [remotes, setRemotes] = useState([])
  // 远程服务器清单（config remotes，设置页维护；「远程服务器」方式下拉用）
  const [remoteHosts, setRemoteHosts] = useState([])
  const [addError, setAddError] = useState('')
  // 服务器目录选择对话框
  const [pickerOpen, setPickerOpen] = useState(false)
  // 测试结果: repoId -> {token/project/webhook}
  const [testResults, setTestResults] = useState({})
  // 对账结果: repoId -> {scanned/enqueued/note/error}（issue #17）
  const [reconcileResults, setReconcileResults] = useState({})
  // 生成图标结果（issue #188）: repoId -> {loading/logo_prompt/error}
  const [logoResults, setLogoResults] = useState({})
  // 同步到 GitLab 结果（issue #297）: repoId -> {loading/project/error}
  const [syncLogoResults, setSyncLogoResults] = useState({})
  // 从 GitLab 同步图标结果（issue #320）: repoId -> {loading/error}
  const [syncFromGitlabResults, setSyncFromGitlabResults] = useState({})
  // 放大查看中的 logo（issue #188）: repo 对象（null = 关闭弹窗）
  const [viewLogo, setViewLogo] = useState(null)
  // logo 加载失败集合（issue #338）: repoId -> true——低网速/网络错误下
  // 缩略图加载失败时显示占位图标兜底，避免渲染破图
  const [logoFailed, setLogoFailed] = useState({})
  // 健康详情弹窗中的仓库（issue #265）：null = 关闭（点击「异常」徽章打开）
  const [healthView, setHealthView] = useState(null)

  const load = async () => {
    try {
      const data = await api.get('/api/repos')
      setRepos(data.repos)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    // 远程服务器清单（「远程服务器」添加方式下拉）：加载失败静默（方式
    // 仍可选，但下拉为空时提交前会提示先到设置页配置）
    api.get('/api/settings').then((s) => setRemoteHosts(s.remotes || []))
      .catch(() => setRemoteHosts([]))
  }, [])

  const addRepo = async () => {
    setAddError('')
    if (method === 'url' && !form.url.trim()) { setAddError('请填写 GitLab 项目 URL 或 project_id'); return }
    if (method === 'local') {
      if (!form.local_path.trim()) { setAddError('请填写本地文件夹路径'); return }
      if (!form.remote_name) { setAddError('请先在本地文件夹中选择一个 remote'); return }
    }
    if (method === 'remote') {
      if (!form.remote_host) { setAddError('请选择远程服务器（无选项时先到设置页「远程服务器」配置）'); return }
      if (!form.remote_path.trim().startsWith('/')) { setAddError('请填写远程服务器上的项目绝对路径（以 / 开头）'); return }
      if (!form.remote_name) { setAddError('请先读取并选择一个 remote'); return }
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
        remote_host: method === 'remote' ? form.remote_host : undefined,
        remote_path: method === 'remote' ? form.remote_path.trim() : undefined,
        remote_name: method === 'local' || method === 'remote' ? form.remote_name : undefined,
        name: form.name.trim() || undefined,
        webhook_url: form.webhook_url.trim() || undefined,
        priority,
        token_expires_at: form.token_expires_at || undefined,
      })
      setForm({ url: '', local_path: '', remote_host: '', remote_path: '', remote_name: '', name: '', webhook_url: '', priority: '100', token_expires_at: '' })
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

  // 远程服务器方式（SSH）：经后端在远程主机上读取项目的 git remote 列表
  const discoverRemote = async () => {
    setAddError('')
    if (!form.remote_host) { setAddError('请选择远程服务器'); return }
    if (!form.remote_path.trim()) { setAddError('请填写远程项目路径'); return }
    setBusy(true)
    try {
      const res = await api.post('/api/repos/discover', {
        remote_host: form.remote_host,
        remote_path: form.remote_path.trim(),
      })
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

  // 同步 logo 到 GitLab 作为仓库图标（issue #297）：调后端把本地已生成
  // logo 上传为 GitLab 项目头像（PUT /projects/{id} 的 avatar 参数）。
  // 仅当仓库已有 logo（repo.logo_path 非空）时展示按钮；请求中禁用防
  // 重复点击，与「生成图标」同风格。
  const syncLogo = async (repo) => {
    setSyncLogoResults((r) => ({ ...r, [repo.id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.id}/sync-logo`)
      setSyncLogoResults((r) => ({ ...r, [repo.id]: res }))
    } catch (e) {
      setSyncLogoResults((r) => ({ ...r, [repo.id]: { error: e.message } }))
    }
  }

  // 从 GitLab 同步图标到本页面（issue #320）：调后端把 GitLab 项目当前
  // 图标（头像）拉回保存为本地仓库 logo——与「同步到 GitLab」方向相反，
  // 双向同步闭环；成功后刷新列表展示 GitLab 图标。请求中禁用防重复点击，
  // 与「生成图标」「同步到 GitLab」同风格。
  const syncLogoFromGitlab = async (repo) => {
    setSyncFromGitlabResults((r) => ({ ...r, [repo.id]: { loading: true } }))
    try {
      const res = await api.post(`/api/repos/${repo.id}/sync-logo-from-gitlab`)
      setSyncFromGitlabResults((r) => ({ ...r, [repo.id]: res }))
      await load()
    } catch (e) {
      setSyncFromGitlabResults((r) => ({ ...r, [repo.id]: { error: e.message } }))
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
          <label className="add-method">
            <input type="radio" checked={method === 'remote'} onChange={() => { setMethod('remote'); setRemotes([]); }} />
            远程服务器（SSH，代码在其他服务器上）
          </label>
        </div>

        {method === 'url' && (
          <div className="form-row">
            <input
              className="input grow"
              placeholder="GitLab 项目 URL 或 project_id（如 https://gitlab.example.com/group/project.git）"
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
            />
          </div>
        )}

        {method === 'remote' && (
          <>
            <div className="form-row">
              <select
                className="input"
                value={form.remote_host}
                onChange={(e) => { setForm({ ...form, remote_host: e.target.value, remote_name: '' }); setRemotes([]) }}
              >
                <option value="">{remoteHosts.length === 0 ? '（无已配置远程服务器）' : '选择远程服务器'}</option>
                {remoteHosts.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}（{r.user ? `${r.user}@` : ''}{r.host}）
                  </option>
                ))}
              </select>
              <input
                className="input grow"
                placeholder="远程服务器上的项目绝对路径（如 /srv/apps/my-repo）"
                value={form.remote_path}
                onChange={(e) => setForm({ ...form, remote_path: e.target.value })}
              />
              <button className="btn" disabled={busy || !form.remote_host} onClick={discoverRemote}>
                {busy ? '读取中…' : '读取 remote'}
              </button>
            </div>
            {remoteHosts.length === 0 && (
              <p className="muted small">尚未配置远程服务器：请先到「设置 → 执行引擎 → 远程服务器」添加并测试连接。</p>
            )}
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
            <p className="muted small">
              远程项目：任务在该服务器的工作目录上执行（建议仓库级执行引擎选
              zcode）；botler 通过 SSH 完成工作区准备与引擎调用，所有交互仍在本页完成。
            </p>
          </>
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
        <div className="form-row">
          <input className="input grow" type="date" value={form.token_expires_at}
                 onChange={(e) => setForm({ ...form, token_expires_at: e.target.value })} />
          <span className="muted small">仓库 Token 到期日（可选；有内嵌 token 时自动探测优先）</span>
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
                支持下载；未生成时显示占位图标，提示可点右侧「生成图标」。
                issue #338：列表加载小尺寸缩略图（?thumb=1，后端 Pillow
                实时缩放），低网速下快速可见；放大弹窗仍加载原图 */}
            <div className="repo-logo">
              {repo.logo_path && !logoFailed[repo.id] ? (
                <button type="button" className="repo-logo-btn"
                        onClick={() => setViewLogo(repo)}
                        title="点击放大 logo 并下载">
                  <img
                    src={`/api/repos/${repo.id}/logo?thumb=1&v=${encodeURIComponent(repo.logo_updated_at || '')}`}
                    alt={`${repo.name} logo`}
                    loading="lazy"
                    onError={() => setLogoFailed((f) => ({ ...f, [repo.id]: true }))}
                  />
                </button>
              ) : (
                <span className="repo-logo-placeholder"
                      title={repo.logo_path
                        ? 'logo 加载失败，请检查网络后刷新'
                        : '尚未生成 logo，点击右侧「生成图标」'}>
                  <Icon name="image" />
                </span>
              )}
            </div>
            <div className="repo-main">
              <div className="repo-name">
                {repo.name}
                {!repo.enabled && <span className="badge badge-muted">已停用</span>}
                <HealthBadge repo={repo} onClick={() => setHealthView(repo)} />
                <span className="badge badge-muted" title="调度优先级：数字越小越优先">
                  优先级 {repo.priority ?? 100}
                </span>
              </div>
              <div className="muted small">{repo.url} · project_id={repo.gitlab_project_id}</div>
              {repo.token_expiry?.level && repo.token_expiry.level !== 'unknown' && (
                <span className={`badge token-expiry-${repo.token_expiry.level}`}>
                  Token {repo.token_expiry.days_remaining < 0 ? '已到期' : `剩余 ${repo.token_expiry.days_remaining} 天`}
                </span>
              )}
              {repo.local_path && (
                <div className="muted small">本地工作区: {repo.local_path}</div>
              )}
              {repo.remote_host && (
                <div className="muted small">
                  远程工作区: {repo.remote_host}:{repo.remote_path}（SSH 执行）
                </div>
              )}
              {testResults[repo.id] && (
                <TestResult result={testResults[repo.id]} />
              )}
              {reconcileResults[repo.id] && (
                <ReconcileResult result={reconcileResults[repo.id]} />
              )}
              {logoResults[repo.id] && <LogoResult result={logoResults[repo.id]} />}
              {syncLogoResults[repo.id] && <SyncLogoResult result={syncLogoResults[repo.id]} />}
              {syncFromGitlabResults[repo.id] && <SyncFromGitlabResult result={syncFromGitlabResults[repo.id]} />}
            </div>
            <div className="repo-actions">
              <button className="btn logo-btn" onClick={() => generateLogo(repo)}
                      disabled={logoResults[repo.id]?.loading}
                      title="agent 基于该仓库 README 生成 logo 提示词，调用生图模型生成 logo">
                {logoResults[repo.id]?.loading ? <><Icon name="refresh" /> 生成中…</> : <><Icon name="sparkles" /> 生成图标</>}
              </button>
              {repo.logo_path && (
                <button className="btn" onClick={() => syncLogo(repo)}
                        disabled={syncLogoResults[repo.id]?.loading}
                        title="把已生成 logo 上传为 GitLab 仓库图标（项目头像）">
                  {syncLogoResults[repo.id]?.loading
                    ? <><Icon name="refresh" /> 同步中…</>
                    : <><Icon name="upload" /> 同步到 GitLab</>}
                </button>
              )}
              <button className="btn" onClick={() => syncLogoFromGitlab(repo)}
                      disabled={syncFromGitlabResults[repo.id]?.loading}
                      title="把 GitLab 项目图标（头像）同步到本页面，保存为仓库 logo">
                {syncFromGitlabResults[repo.id]?.loading
                  ? <><Icon name="refresh" /> 拉取中…</>
                  : <><Icon name="download" /> 从 GitLab 同步</>}
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

      {/* issue #265：仓库健康巡检详情弹窗（点击「异常」徽章打开）——
          展示最新巡检明细（webhook/token/项目）、自动修复标记、历史记录，
          支持「重新巡检」手动重检；重检后同步刷新仓库列表健康徽章 */}
      {healthView && (
        <HealthDetailModal
          repo={healthView}
          onClose={() => setHealthView(null)}
          onChecked={load}
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

// 同步到 GitLab 结果（issue #297）：同步中提示 / 成功（展示 GitLab
// 项目路径）/ 失败展示后端错误信息（权限不足、图片格式不支持等）
function SyncLogoResult({ result }) {
  if (result.loading) return (
    <div className="small muted"><Icon name="refresh" /> 正在同步到 GitLab…</div>
  )
  if (result.error) return <div className="alert alert-error small">{result.error}</div>
  return (
    <div className="small">
      <span className="test-chip ok"
            title={result.project ? `已同步为 ${result.project} 的项目图标` : '已同步到 GitLab'}>
        <Icon name="check" /> 已同步到 GitLab{result.project ? `（${result.project}）` : ''}
      </span>
    </div>
  )
}

// 从 GitLab 同步图标结果（issue #320）：同步中提示 / 成功（展示来源
// GitLab 项目图标）/ 失败展示后端错误信息（GitLab 未设置图标、权限
// 不足等）
function SyncFromGitlabResult({ result }) {
  if (result.loading) return (
    <div className="small muted"><Icon name="refresh" /> 正在从 GitLab 同步图标…</div>
  )
  if (result.error) return <div className="alert alert-error small">{result.error}</div>
  return (
    <div className="small">
      <span className="test-chip ok" title="已把 GitLab 项目图标同步到本页面">
        <Icon name="check" /> 已从 GitLab 同步图标
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


// 仓库健康状态徽章（issue #265）：正常=绿 / 异常=红（可点击打开详情弹窗
// 查看明细与手动重检）/ 未知=灰（从未巡检过）
function HealthBadge({ repo, onClick }) {
  const h = repo.health
  if (!h || !h.status) return null
  if (h.status === 'healthy') {
    return (
      <span className="health-badge health-healthy"
            title={`健康巡检正常（${h.check_time || ''}）`}>
        健康
      </span>
    )
  }
  if (h.status === 'abnormal') {
    return (
      <span role="button" tabIndex={0}
            className="health-badge health-abnormal"
            title={`健康巡检异常：${h.last_error || ''}（点击查看详情）`}
            onClick={onClick}
            onKeyDown={(e) => { if (e.key === 'Enter') onClick() }}>
        异常
      </span>
    )
  }
  return (
    <span className="health-badge health-unknown" title="尚未巡检过（定时巡检开启后自动检查）">
      未知
    </span>
  )
}

// 仓库健康巡检详情弹窗（issue #265）：点击「异常」徽章打开——展示最新
// 巡检状态（webhook/token/项目明细、错误描述、自动修复标记、检查时间）、
// 历史记录，并提供「重新巡检」按钮（手动重检，结果落库并刷新徽章）
function HealthDetailModal({ repo, onClose, onChecked }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      const res = await api.get(`/api/repos/${repo.id}/health`)
      setData(res)
    } catch (e) {
      setError(e.message)
    }
  }
  useEffect(() => {
    // 弹窗每次打开重新挂载（{healthView && ...} 条件渲染），仅挂载时加载
    // 一次：load 每次渲染重建引用，加入 deps 会形成「加载→setState→重渲染
    // →再加载」死循环（与 Tools/Skills 页 load 同约定）
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const recheck = async () => {
    setBusy(true)
    setError('')
    try {
      await api.post(`/api/repos/${repo.id}/health-check`)
      await load()
      onChecked?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const latest = data?.latest
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>{repo.name} 健康巡检</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭"><Icon name="x" /></button>
        </div>
        {error && <div className="alert alert-error">{error}</div>}
        {!data && !error && <div className="muted">加载中…</div>}
        {data && !latest && (
          <div className="small muted">
            该仓库尚未巡检过。点击「重新巡检」立即检查 webhook / token / 项目可达性。
          </div>
        )}
        {data && latest && (
          <>
            <div className="small muted">
              最近巡检时间：{latest.check_time}
              {latest.repaired && (
                <span className="badge health-repaired" title="巡检发现 webhook 缺失或 secret 不匹配，已自动重新注册">
                  已自动修复 webhook
                </span>
              )}
            </div>
            {latest.last_error && (
              <div className="alert alert-error small pre-wrap">{latest.last_error}</div>
            )}
            {latest.project_detail && (
              <div className="health-detail-block">
                <div className="small muted" style={{ marginBottom: 4 }}>
                  项目不可达详细诊断（issue #496）
                </div>
                {latest.project_detail}
              </div>
            )}
            <div className="health-detail-grid">
              <HealthItem label="webhook" ok={latest.webhook_ok} />
              <HealthItem label="token" ok={latest.token_ok} />
              <HealthItem label="项目可达" ok={latest.project_ok} />
            </div>
            {latest.status === 'healthy' && (
              <div className="small test-chip ok"><Icon name="check" /> 全部检查项正常</div>
            )}
          </>
        )}
        {data && data.history?.length > 0 && (
          <div className="small" style={{ marginTop: 12 }}>
            <strong>巡检历史（最近 {data.history.length} 次）</strong>
            <table className="table" style={{ marginTop: 6 }}>
              <thead>
                <tr><th>时间</th><th>状态</th><th>错误描述</th></tr>
              </thead>
              <tbody>
                {data.history.map((h) => (
                  <tr key={h.id}>
                    <td className="muted">{h.check_time}</td>
                    <td>
                      <span className={`badge ${h.status === 'healthy' ? 'health-healthy' : 'health-abnormal'}`}>
                        {h.status === 'healthy' ? '正常' : '异常'}
                      </span>
                    </td>
                    <td className="muted">{h.last_error || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="modal-footer">
          <button className="btn btn-primary" onClick={recheck} disabled={busy}>
            {busy ? <><Icon name="refresh" /> 巡检中…</> : '重新巡检'}
          </button>
          <button className="btn" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  )
}

// 巡检明细项（issue #265）：webhook / token / 项目 各自的检查结果
function HealthItem({ label, ok }) {
  if (ok === null || ok === undefined) {
    return (
      <div className="health-item">
        <strong>{label}</strong>
        <span className="muted">未检查</span>
      </div>
    )
  }
  return (
    <div className="health-item">
      <strong>{label}</strong>
      {ok
        ? <span className="test-chip ok"><Icon name="check" /> 正常</span>
        : <span className="test-chip bad"><Icon name="x" /> 异常</span>}
    </div>
  )
}
