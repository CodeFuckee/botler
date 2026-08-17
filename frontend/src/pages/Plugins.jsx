import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'

// 插件管理页面（issue #145）：所有插件的安装、卸载和设置都在这个界面。
// 数据来自 /api/plugins（插件体系 issue #140 的注册表视图）：
// {engine, plugin_paths, plugins: {executor: [...], model_provider: [...], notifier: [...]}}
// - 安装：输入外部插件模块路径（worker.plugin_paths 扩展点），后端校验后
//   写入配置并热加载，立即生效；
// - 卸载：仅外部插件可卸载（配置 + 注册表同时移除），内置插件带「内置」徽章；
// - 设置：默认执行引擎（executor 插件，复用 worker.engine，与设置页
//   「任务调度」卡片同源）+ 外部插件重新加载。

// 插件分类元信息（与后端 PluginKind 一一对应）
const KIND_META = {
  executor: {
    label: '执行引擎',
    desc: '任务执行引擎：决定后端用哪个 agent 编写代码（默认引擎在此设置）',
  },
  model_provider: {
    label: '大模型供应商',
    desc: '生图 API 供应商：决定设置页「生图模型」的预设选项与调用接口',
  },
  vision_model_provider: {
    label: '识图模型供应商',
    desc: '识图 API 供应商：决定设置页「识图模型」的预设选项与调用接口（issue #152）',
  },
  notifier: {
    label: '消息通知通道',
    desc: '任务成功 / 失败时发送消息的通道',
  },
}

export default function Plugins() {
  const [data, setData] = useState(null) // GET /api/plugins 结果
  const [error, setError] = useState('')
  const [note, setNote] = useState(null) // {ok, text} 操作结果提示
  const [installPath, setInstallPath] = useState('') // 安装表单（每行一个路径）
  const [busy, setBusy] = useState(false)
  // 默认执行引擎（executor 插件设置）：加载后按后端当前值初始化，
  // 用户切换后保存时提交
  const [engine, setEngine] = useState('')

  const load = async () => setData(await api.get('/api/plugins'))

  useEffect(() => { load().catch((e) => setError(e.message)) }, [])

  // 数据加载后初始化默认引擎选择（仅首次/未手动选择时同步，避免覆盖
  // 用户尚未保存的切换）
  useEffect(() => {
    if (data && !engine) setEngine(data.engine || 'claude')
  }, [data, engine])

  // HIG 匠心：加载态用 spinner，非裸文本
  if (!data) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

  // 安装：每行一个模块路径，逐个提交（后端逐项校验：文件存在 / 模块可
  // 加载 / 至少注册一个插件 / 与已安装插件无冲突，失败不落盘）
  const install = async () => {
    const paths = installPath.split('\n').map((x) => x.trim()).filter(Boolean)
    if (!paths.length) {
      setNote({ ok: false, text: '✗ 请先输入插件模块路径（每行一个）' })
      return
    }
    setBusy(true); setNote(null); setError('')
    try {
      let latest = null
      for (const p of paths) {
        latest = await api.post('/api/plugins/install', { path: p })
      }
      setData(latest)
      setInstallPath('')
      setNote({ ok: true, text: `✓ 已安装 ${paths.length} 个外部插件模块（已写入 worker.plugin_paths 并生效）` })
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    } finally { setBusy(false) }
  }

  // 卸载：外部插件才显示卸载按钮；配置与注册表同时移除
  const uninstall = async (plugin) => {
    if (!(await confirmDialog({
      message: `确认卸载外部插件「${plugin.name}」？\n配置移除后重启不再加载，已注册能力立即失效。`,
      danger: true,
    }))) return
    setNote(null)
    try {
      setData(await api.post('/api/plugins/uninstall', { path: plugin.path }))
      setNote({ ok: true, text: `✓ 已卸载外部插件「${plugin.name}」` })
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    }
  }

  // 重新加载外部插件（新增/修改的模块即时生效）
  const reload = async () => {
    setBusy(true); setNote(null)
    try {
      setData(await api.post('/api/plugins/reload'))
      setNote({ ok: true, text: '✓ 外部插件已按 worker.plugin_paths 重新加载' })
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    } finally { setBusy(false) }
  }

  // 保存默认执行引擎（executor 插件设置）
  const saveEngine = async () => {
    setBusy(true); setNote(null); setError('')
    try {
      setData(await api.put('/api/plugins/settings', { engine }))
      setNote({ ok: true, text: `✓ 默认执行引擎已切换为 ${engine}（对新领取的任务生效）` })
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    } finally { setBusy(false) }
  }

  const executorItems = data.plugins.executor || []

  return (
    <div>
      <h1>插件管理</h1>
      <p className="muted">
        集中管理平台插件：安装外部插件模块、卸载外部插件、设置默认执行引擎。
        插件分为执行引擎 / 大模型供应商 / 识图模型供应商 / 消息通知通道四类（插件体系 issue #140）。
      </p>

      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && (
        <div className={'alert ' + (note.ok ? 'alert-ok' : 'alert-error')} onClick={() => setNote(null)}>
          {note.text}
        </div>
      )}

      {/* 安装 / 重载 */}
      <div className="card">
        <h2>安装外部插件</h2>
        <p className="muted small">
          输入插件模块的 Python 文件路径（每行一个，即 worker.plugin_paths 扩展点）。
          后端会校验：文件存在、模块可加载且至少注册一个插件、与已安装插件无同名同分类冲突；
          校验通过后写入配置并立即生效（无需重启）。
        </p>
        <textarea
          className="input plugin-install-input"
          rows={4}
          placeholder={'/opt/botler-plugins/my_engine.py\n/opt/botler-plugins/feishu_channel.py'}
          value={installPath}
          onChange={(e) => setInstallPath(e.target.value)}
          aria-label="插件模块路径"
        />
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy} onClick={install}>
            {busy ? '安装中…' : '安装插件'}
          </button>
          <button className="btn" disabled={busy} onClick={reload}>重新加载外部插件</button>
        </div>
      </div>

      {/* 默认执行引擎设置（executor 插件设置） */}
      <div className="card">
        <h2>默认执行引擎</h2>
        <p className="muted small">
          任务执行引擎（executor 插件）设置：决定后端用哪个 agent 编写代码，
          切换后对新领取的任务生效（与设置页「任务调度」卡片同源，两处修改互相可见）。
        </p>
        <div className="plugin-engine-options">
          {executorItems.map((p) => (
            <label className="plugin-engine-radio" key={p.name}>
              <input
                type="radio"
                name="plugin-engine"
                value={p.name}
                checked={engine === p.name}
                onChange={(e) => setEngine(e.target.value)}
              />
              {p.display_name || p.name}
              <span className="muted small">v{p.version}</span>
            </label>
          ))}
        </div>
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy} onClick={saveEngine}>
            {busy ? '保存中…' : '保存'}
          </button>
        </div>
      </div>

      {/* 插件列表（按分类分组） */}
      {Object.entries(KIND_META).map(([kind, meta]) => {
        const items = data.plugins[kind] || []
        return (
          <section className="plugins-group" key={kind}>
            <h2>{meta.label} <span className="badge badge-muted">{items.length}</span></h2>
            <p className="muted small">{meta.desc}</p>
            {items.length === 0 ? (
              <p className="muted small">该分类暂无插件</p>
            ) : (
              <ul className="plugin-list">
                {items.map((item) => (
                  <li className="plugin-card" key={`${item.kind}-${item.name}`}>
                    <div className="plugin-head">
                      <span className="plugin-name">{item.display_name || item.name}</span>
                      {item.builtin ? (
                        <span className="badge badge-default">内置</span>
                      ) : (
                        <span className="badge badge-external">外部</span>
                      )}
                      <span className="muted small">v{item.version}</span>
                      <span className="muted small plugin-kind">{meta.label}</span>
                    </div>
                    <p className="muted small plugin-desc">{item.description}</p>
                    {item.default_base_url && (
                      <p className="muted small plugin-path">
                        默认接口 {item.default_base_url} · 默认模型 {item.default_model}
                      </p>
                    )}
                    {item.path && (
                      <p className="muted small plugin-path">模块 {item.path}</p>
                    )}
                    <div className="plugin-actions">
                      {!item.builtin && (
                        <button className="btn btn-sm btn-danger" onClick={() => uninstall(item)}>
                          卸载
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )
      })}
    </div>
  )
}
