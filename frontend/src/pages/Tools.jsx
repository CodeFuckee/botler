import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'

// 工具管理页面（issue #172）：管理给 agent 使用的 MCP 工具。
//
// 工具 = MCP server（Model Context Protocol），通过 mcpServers 配置
// （工作区 .mcp.json）暴露给 agent 调用，全局生效（issue #172 Q4）。
// 来源四类：
// - 内置市场（builtin）：平台预置工具模板，一键安装入库；
// - URL 导入（url）：粘贴 Git 仓库 / JSON 定义文件地址，平台拉取入库；
// - 远端市场索引（market）：配置 JSON 索引地址，拉取候选清单逐个安装；
// - 自定义（custom）：页面表单手工编写（名称/描述/类型/命令参数）。
//
// 数据来自 /api/tools：
//   {tools: [{id, name, description, kind, command, args, env, url,
//             source, source_url, enabled, created_at, updated_at}],
//    market: [...], market_index_url: ''}
// 交互：搜索过滤 / 新建 / 内联编辑 / 删除（确认）/ 启用停用 /
// 市场安装 / URL 导入 / 索引拉取候选安装。

// 类型与来源展示元信息（与后端 tools.py 常量一一对应）
const KIND_META = {
  stdio: { label: '本地命令', hint: '子进程方式启动（command + args + env）' },
  sse: { label: 'SSE 远程', hint: 'SSE 流式远程端点（url）' },
  http: { label: 'HTTP 远程', hint: 'HTTP 流式远程端点（url）' },
}
const SOURCE_META = {
  builtin: { label: '内置市场', cls: 'badge-default' },
  url: { label: 'URL 导入', cls: 'badge-muted' },
  market: { label: '市场索引', cls: 'badge-muted' },
  custom: { label: '自定义', cls: 'badge-primary' },
}

// 空表单（新建工具初始值）
function emptyForm() {
  return {
    name: '', description: '', kind: 'stdio',
    command: '', args: '', env: '', url: '',
  }
}

// 工具 → 编辑表单（args/env JSON 文本化）
function toForm(tool) {
  return {
    name: tool.name,
    description: tool.description || '',
    kind: tool.kind,
    command: tool.command || '',
    args: Array.isArray(tool.args) ? JSON.stringify(tool.args, null, 1) : '',
    env: (tool.env && Object.keys(tool.env).length)
      ? JSON.stringify(tool.env, null, 1) : '',
    url: tool.url || '',
  }
}

// 表单 → 提交 payload（args/env JSON 解析，非法抛错由调用方提示）
function toPayload(form) {
  const payload = {
    name: form.name.trim(),
    description: form.description.trim(),
    kind: form.kind,
    command: form.command.trim(),
    url: form.url.trim(),
  }
  if (form.kind === 'stdio') {
    const rawArgs = form.args.trim()
    if (rawArgs) {
      try {
        const parsed = JSON.parse(rawArgs)
        if (!Array.isArray(parsed) || parsed.some((x) => typeof x !== 'string')) {
          throw new Error('参数需为字符串数组')
        }
        payload.args = parsed
      } catch (e) {
        throw new Error(`参数 JSON 不合法：${e.message}`)
      }
    } else {
      payload.args = []
    }
    const rawEnv = form.env.trim()
    if (rawEnv) {
      try {
        const parsed = JSON.parse(rawEnv)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('环境变量需为键值对象')
        }
        payload.env = parsed
      } catch (e) {
        throw new Error(`环境变量 JSON 不合法：${e.message}`)
      }
    } else {
      payload.env = {}
    }
  }
  return payload
}

export default function Tools() {
  const [data, setData] = useState(null) // GET /api/tools 结果
  const [error, setError] = useState('')
  const [note, setNote] = useState(null) // {ok, text} 操作结果提示
  const [query, setQuery] = useState('') // 搜索关键字
  const [editing, setEditing] = useState(null) // null | {id, tool} 编辑中
  const [creating, setCreating] = useState(false) // 新建表单展开
  const [form, setForm] = useState(emptyForm()) // 新建/编辑表单
  const [busy, setBusy] = useState(false)
  // 导入 / 市场索引
  const [importUrl, setImportUrl] = useState('')
  const [indexUrl, setIndexUrl] = useState('')
  const [candidates, setCandidates] = useState(null) // 索引候选清单
  const [indexFetched, setIndexFetched] = useState('') // 已拉取索引地址（展示）

  const load = async () => {
    const d = await api.get('/api/tools')
    setData(d)
    if (!indexUrl && d.market_index_url) setIndexUrl(d.market_index_url)
  }

  useEffect(() => {
    // 挂载时仅加载一次：load 只使用模块级 api 与稳定 setter，每次渲染
    // 重建引用，加入 deps 会形成「加载→setState→重渲染→再加载」死循环
    load().catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 搜索过滤（名称 / 描述 / 来源 / 类型）
  const filtered = useMemo(() => {
    if (!data) return []
    const q = query.trim().toLowerCase()
    if (!q) return data.tools
    return data.tools.filter((t) =>
      [t.name, t.description, SOURCE_META[t.source]?.label,
       KIND_META[t.kind]?.label]
        .filter(Boolean).some((v) => v.toLowerCase().includes(q)))
  }, [data, query])

  // 新建：展开空表单
  const startCreate = () => {
    setCreating(true); setEditing(null)
    setForm(emptyForm())
    setNote(null)
  }
  // 编辑：回填工具数据
  const startEdit = (tool) => {
    setCreating(false); setEditing({ id: tool.id })
    setForm(toForm(tool))
    setNote(null)
  }
  const cancelForm = () => {
    setCreating(false); setEditing(null); setForm(emptyForm())
  }

  // 保存（新建 POST / 编辑 PUT）
  const save = async () => {
    setBusy(true); setNote(null); setError('')
    try {
      const payload = toPayload(form)
      if (editing) {
        const updated = await api.put(`/api/tools/${editing.id}`, payload)
        setNote({ ok: true, text: `工具「${updated.name}」已更新` })
      } else {
        const created = await api.post('/api/tools', payload)
        setNote({ ok: true, text: `工具「${created.name}」已创建` })
      }
      cancelForm()
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  // 启用 / 停用
  const toggleEnabled = async (tool) => {
    setBusy(true); setNote(null); setError('')
    try {
      const updated = await api.put(`/api/tools/${tool.id}`, {
        enabled: !tool.enabled,
      })
      setNote({ ok: true, text: `「${updated.name}」已${updated.enabled ? '启用' : '停用'}` })
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  // 删除（确认）
  const remove = async (tool) => {
    if (!(await confirmDialog({
      message: `确认删除工具「${tool.name}」？\n删除后 agent 将无法再调用该工具（启用状态同步移除）。`,
      danger: true,
    }))) return
    setNote(null); setError('')
    try {
      await api.del(`/api/tools/${tool.id}`)
      setNote({ ok: true, text: `已删除工具「${tool.name}」` })
      if (editing?.id === tool.id) cancelForm()
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    }
  }

  // 安装内置市场工具
  const installMarket = async (name) => {
    setBusy(true); setNote(null); setError('')
    try {
      const tool = await api.post('/api/tools/install', { name })
      setNote({ ok: true, text: `已从内置市场安装「${tool.name}」` })
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  // URL 导入（Git 仓库 / JSON 定义文件）
  const importFromUrl = async () => {
    const url = importUrl.trim()
    if (!url) { setNote({ ok: false, text: '请先填写导入地址（Git 仓库 / JSON 文件 URL）' }); return }
    setBusy(true); setNote(null); setError('')
    try {
      const resp = await api.post('/api/tools/import', { url })
      setNote({ ok: true, text: `已从 ${url} 导入 ${resp.count} 个工具` })
      setImportUrl('')
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  // 拉取远端市场索引（候选不落库，逐个安装）
  const fetchIndex = async () => {
    const url = indexUrl.trim()
    if (!url) { setNote({ ok: false, text: '请先填写市场索引地址' }); return }
    setBusy(true); setNote(null); setError('')
    try {
      const resp = await api.post('/api/tools/market-index', { url })
      setCandidates(resp.candidates)
      setIndexFetched(url)
      setNote({ ok: true, text: `已拉取 ${resp.count} 个候选工具（点击安装后入库）` })
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  // 安装远端市场候选工具（source=market）
  const installCandidate = async (candidate) => {
    setBusy(true); setNote(null); setError('')
    try {
      const tool = await api.post('/api/tools', { ...candidate, source: 'market' })
      setNote({ ok: true, text: `已安装市场工具「${tool.name}」` })
      await load()
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  if (!data) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

  const installedNames = new Set(data.tools.map((t) => t.name))

  return (
    <div>
      <h1>工具管理</h1>
      <p className="muted">
        管理给 agent 使用的 MCP 工具（全局生效）：可以从内置市场下载、从
        URL 导入别人编写好的工具，也可以自行编写自定义工具。启用中的工具
        会在任务执行时注入仓库工作区 <code>.mcp.json</code>，agent 通过
        MCP 协议直接调用（issue #172）。
      </p>

      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && (
        <div className={'alert ' + (note.ok ? 'alert-ok' : 'alert-error')} onClick={() => setNote(null)}>
          <Icon name={note.ok ? 'check' : 'x'} /> {note.text}
        </div>
      )}

      {/* 工具栏：搜索 + 新建 */}
      <div className="form-row tools-toolbar">
        <input
          className="input tools-search"
          type="search"
          placeholder="搜索工具名称 / 描述 / 来源…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="搜索工具"
        />
        <span className="form-row-spacer" />
        <button className="btn btn-primary" onClick={startCreate} disabled={creating || editing}>
          <Icon name="plus" /> 新建自定义工具
        </button>
      </div>

      {/* 新建 / 编辑表单 */}
      {(creating || editing) && (
        <div className="tools-form card">
          <h2>{creating ? '新建自定义工具' : `编辑工具「${form.name || editing.id}」`}</h2>
          <div className="tools-form-grid">
            <label className="field">
              <span className="field-label">名称 *</span>
              <input className="input" value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如 my-tool（字母数字_-）" />
            </label>
            <label className="field">
              <span className="field-label">描述</span>
              <input className="input" value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="工具用途说明（agent 据此选择调用）" />
            </label>
            <label className="field">
              <span className="field-label">类型 *</span>
              <select className="input" value={form.kind}
                onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                {Object.entries(KIND_META).map(([k, meta]) => (
                  <option key={k} value={k}>{meta.label}</option>
                ))}
              </select>
              <span className="muted small">{KIND_META[form.kind].hint}</span>
            </label>
            {form.kind === 'stdio' ? (
              <>
                <label className="field">
                  <span className="field-label">启动命令 *</span>
                  <input className="input" value={form.command}
                    onChange={(e) => setForm({ ...form, command: e.target.value })}
                    placeholder="如 npx / python3 / docker run…" />
                </label>
                <label className="field">
                  <span className="field-label">命令参数（JSON 数组）</span>
                  <textarea className="input textarea tools-args" rows={2}
                    value={form.args}
                    onChange={(e) => setForm({ ...form, args: e.target.value })}
                    placeholder='["-y", "@modelcontextprotocol/server-fetch"]'
                    spellCheck="false" />
                </label>
                <label className="field tools-env">
                  <span className="field-label">环境变量（JSON 对象）</span>
                  <textarea className="input textarea tools-env" rows={3}
                    value={form.env}
                    onChange={(e) => setForm({ ...form, env: e.target.value })}
                    placeholder='{"TOKEN": "xxx"}'
                    spellCheck="false" />
                </label>
              </>
            ) : (
              <label className="field">
                <span className="field-label">服务地址 *（http(s)://）</span>
                <input className="input" value={form.url}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                  placeholder="https://mcp.example.com/bridge" />
              </label>
            )}
          </div>
          <div className="form-row tools-form-actions">
            <span className="form-row-spacer" />
            <button className="btn btn-sm" onClick={cancelForm} disabled={busy}>取消</button>
            <button className="btn btn-sm btn-primary" onClick={save} disabled={busy}>
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      )}

      {/* 已安装工具列表 */}
      <h2>
        已安装工具
        <span className="badge badge-muted">{data.tools.length}</span>
      </h2>
      {data.tools.length === 0 ? (
        <p className="muted small">
          暂无工具。可从下方「工具市场」下载内置工具 / 从 URL 导入，
          或点击右上角「新建自定义工具」自行编写。
        </p>
      ) : filtered.length === 0 ? (
        <p className="muted small">没有匹配「{query}」的工具。</p>
      ) : (
        <ul className="tools-list">
          {filtered.map((tool) => (
            <li key={tool.id} className="tools-item card">
              <div className="tools-item-head">
                <span className="tools-name"><Icon name="wrench" /> {tool.name}</span>
                <span className={'badge ' + (SOURCE_META[tool.source]?.cls || 'badge-muted')}>
                  {SOURCE_META[tool.source]?.label || tool.source}
                </span>
                <span className="badge badge-muted">{KIND_META[tool.kind]?.label || tool.kind}</span>
                {tool.enabled
                  ? <span className="badge badge-ok">已启用</span>
                  : <span className="badge badge-warn">已停用</span>}
                <span className="form-row-spacer" />
                <button
                  className={'tools-toggle' + (tool.enabled ? ' on' : '')}
                  onClick={() => toggleEnabled(tool)}
                  title={tool.enabled ? '停用该工具' : '启用该工具'}
                  aria-label={tool.enabled ? `停用 ${tool.name}` : `启用 ${tool.name}`}
                >
                  <span className="tools-toggle-knob" />
                </button>
                <button className="btn btn-sm" onClick={() => startEdit(tool)}>
                  <Icon name="pencil" /> 编辑
                </button>
                <button className="btn btn-sm btn-danger" onClick={() => remove(tool)}>
                  <Icon name="trash" /> 删除
                </button>
              </div>
              <p className="muted small tools-desc">
                {tool.description || '（无描述）'}
                {tool.source_url && <span className="tools-source-url"> 来源：{tool.source_url}</span>}
              </p>
              <div className="tools-item-meta muted small">
                {tool.kind === 'stdio' ? (
                  <code>{tool.command}{tool.args?.length ? ' ' + tool.args.join(' ') : ''}</code>
                ) : (
                  <code>{tool.url}</code>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* 工具市场 */}
      <h2>工具市场</h2>
      <div className="tools-market">
        {/* 内置市场 */}
        <div className="card tools-market-section">
          <h3>内置市场</h3>
          <p className="muted small">
            平台预置的工具模板（MCP 官方参考服务器等），一键安装入库，安装后可编辑。
          </p>
          <ul className="tools-market-list">
            {data.market.map((m) => (
              <li key={m.name} className="tools-market-item">
                <div className="tools-market-info">
                  <span className="tools-name">{m.name}</span>
                  <span className="badge badge-muted">{KIND_META[m.kind]?.label || m.kind}</span>
                  <p className="muted small">{m.description}</p>
                </div>
                {installedNames.has(m.name) ? (
                  <span className="badge badge-ok">已安装</span>
                ) : (
                  <button className="btn btn-sm btn-primary" onClick={() => installMarket(m.name)} disabled={busy}>
                    <Icon name="download" /> 安装
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>

        {/* URL 导入 */}
        <div className="card tools-market-section">
          <h3>从 URL 导入</h3>
          <p className="muted small">
            粘贴 Git 仓库地址（自动读取仓库内 .mcp.json / mcp.json / tool.json）
            或 JSON 定义文件地址（mcpServers 多工具格式 / 单工具定义）。
          </p>
          <div className="form-row">
            <input className="input" type="url" value={importUrl}
              onChange={(e) => setImportUrl(e.target.value)}
              placeholder="https://github.com/user/tool-repo 或 https://x/tool.json"
              aria-label="导入地址" />
            <button className="btn btn-sm btn-primary" onClick={importFromUrl} disabled={busy}>
              <Icon name="download" /> 导入
            </button>
          </div>
        </div>

        {/* 远端市场索引 */}
        <div className="card tools-market-section">
          <h3>远端市场索引</h3>
          <p className="muted small">
            配置 JSON 索引地址（{"{tools: [...]}"} 或数组），拉取候选清单后逐个安装，
            地址会保存供下次访问自动填入。
          </p>
          <div className="form-row">
            <input className="input" type="url" value={indexUrl}
              onChange={(e) => setIndexUrl(e.target.value)}
              placeholder="https://market.example/tools.json"
              aria-label="市场索引地址" />
            <button className="btn btn-sm" onClick={fetchIndex} disabled={busy}>
              <Icon name="refresh" /> 拉取
            </button>
          </div>
          {candidates && (
            <div className="tools-candidates">
              <p className="muted small">
                来自 <code>{indexFetched}</code> 的 {candidates.length} 个候选：
              </p>
              {candidates.length === 0 ? (
                <p className="muted small">索引中没有可安装的候选工具。</p>
              ) : (
                <ul className="tools-market-list">
                  {candidates.map((c) => (
                    <li key={c.name} className="tools-market-item">
                      <div className="tools-market-info">
                        <span className="tools-name">{c.name}</span>
                        <span className="badge badge-muted">{KIND_META[c.kind]?.label || c.kind}</span>
                        <p className="muted small">{c.description || '（无描述）'}</p>
                      </div>
                      {installedNames.has(c.name) ? (
                        <span className="badge badge-ok">已安装</span>
                      ) : (
                        <button className="btn btn-sm btn-primary"
                          onClick={() => installCandidate(c)} disabled={busy}>
                          <Icon name="download" /> 安装
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
