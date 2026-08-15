import { useEffect, useState } from 'react'
import { api, setDisplayTz, fmtTime } from '../api.js'
import { sendTestNotification } from '../notify.js'
import AiProvidersCard from '../components/AiProvidersCard.jsx'
import BackupManager from '../components/BackupManager.jsx'
import Markdown from '../components/Markdown.jsx'
import VersionBadge from '../components/VersionBadge.jsx'

const FIELD_LABELS = {
  max_concurrent_repos: '跨仓库并行上限',
  task_timeout_seconds: '单任务超时（秒）',
  max_retries: '失败重试次数',
  reconcile_interval_seconds: '对账扫描间隔（秒）',
}

// 常用显示时区（issue #14）；支持手动输入任意 IANA 时区名
const COMMON_TZ = [
  'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Singapore', 'Asia/Seoul',
  'UTC', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles',
]

// 网页通知时机开关（issue #21）：与后端通知事件类型一一对应
const NOTIFY_LABELS = {
  task_needs_interaction: '任务需要交互（任务失败，需人工介入）',
  issue_completed: 'issue 完成（任务成功，issue 已关闭）',
  queue_empty: 'issue 列表为空（无待处理 issue）',
  queue_no_work: '无 issue 可处理（有 issue 但均在处理中）',
}

export default function Settings() {
  const [settings, setSettings] = useState(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reconcileNote, setReconcileNote] = useState('')
  const [testNote, setTestNote] = useState(null) // {ok, text}，测试通知结果提示
  const [env, setEnv] = useState(null) // 本地环境检测结果（issue #22）
  const [envError, setEnvError] = useState('')
  const [envBusy, setEnvBusy] = useState(false)
  // SSO client_secret 输入（issue #27）：留空 = 保持现有凭据（后端掩码不覆盖）
  const [ssoSecretInput, setSsoSecretInput] = useState('')
  // SSO 卡片内独立保存（issue #27 第四轮）：用户反馈「SSO 配置没有保存按钮」——
  // 全局「保存」按钮在下方「任务调度」卡片，SSO 卡片（第一位）内需可独立保存
  const [ssoBusy, setSsoBusy] = useState(false)
  const [ssoSaved, setSsoSaved] = useState(false)
  // SSO 配置指南（issue #27 第六轮）：使用者看不到仓库本地文档，改为
  // 从后端拉取 docs/ 指南 Markdown 在设置页直接展示（默认收起，点击展开）
  const [guide, setGuide] = useState(null)
  const [guideError, setGuideError] = useState('')
  const [guideOpen, setGuideOpen] = useState(false)
  // Owner GitLab Token（issue #87）：专用于编辑 issue（评论/标签）的
  // 个人访问令牌。留空 = 保持现有（后端掩码不覆盖，与 SSO secret 同模式）
  const [ownerTokenInput, setOwnerTokenInput] = useState('')
  const [ownerBusy, setOwnerBusy] = useState(false)
  const [ownerSaved, setOwnerSaved] = useState(false)
  // 申请教程（issue #87）：与 SSO 指南同模式，后端读 docs/ 单一文档来源
  const [ownerGuide, setOwnerGuide] = useState(null)
  const [ownerGuideError, setOwnerGuideError] = useState('')
  const [ownerGuideOpen, setOwnerGuideOpen] = useState(false)

  // 设置页「弹出测试通知」按钮（issue #21 增量）：直接弹一条浏览器
  // 系统通知验证功能；权限未决时 sendTestNotification 会先请求授权。
  const handleTestNotify = async () => {
    setTestNote(null)
    const res = await sendTestNotification()
    setTestNote(
      res.ok
        ? { ok: true, text: '✓ 已弹出测试通知，请查看系统通知' }
        : res.reason === 'insecure-context'
          ? { ok: false, text: '✗ 当前页面非安全上下文（需 HTTPS 且证书受信任），浏览器通知不可用' }
          : res.reason === 'denied'
            ? { ok: false, text: '✗ 浏览器已拒绝通知授权：点击地址栏左侧图标将通知权限改为「允许」后再试' }
            : { ok: false, text: '✗ 当前浏览器不支持系统通知' }
    )
  }

  // 本地环境检测（issue #22）：进入设置页自动检测一次，可点「重新检测」刷新
  const loadEnv = async () => {
    setEnvBusy(true); setEnvError('')
    try {
      setEnv(await api.get('/api/environment'))
    } catch (e) { setEnvError(e.message) } finally { setEnvBusy(false) }
  }

  // 单工具状态提示：未安装 / 无法获取最新版本 / 已是最新 / 可升级
  const envStatus = (t) => {
    if (!t.installed) return <span className="muted">未安装</span>
    if (!t.latest) return <span className="muted">无法获取最新版本</span>
    return t.up_to_date
      ? <span className="ok-text">✓ 已是最新</span>
      : <span className="err-hint">⚠ 可升级</span>
  }

  useEffect(() => {
    api.get('/api/settings').then(setSettings).catch((e) => setError(e.message))
    api.get('/api/settings/sso-guide').then((d) => setGuide(d.content))
      .catch((e) => setGuideError(e.message))
    api.get('/api/settings/owner-token-guide').then((d) => setOwnerGuide(d.content))
      .catch((e) => setOwnerGuideError(e.message))
    loadEnv()
  }, [])

  // HIG 匠心：加载态用 spinner，非裸文本
  if (!settings) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

  const setWorkerField = (key, val) =>
    setSettings((s) => ({ ...s, worker: { ...s.worker, [key]: val } }))

  // issue 标签优先级（issue #76）：文本框逗号分隔输入 ↔ 数组存储，
  // 提交时作为 worker.issue_priority 数组写回
  const setIssuePriority = (text) =>
    setSettings((s) => ({
      ...s,
      worker: {
        ...s.worker,
        issue_priority: text.split(',').map((x) => x.trim()).filter(Boolean),
      },
    }))

  const setNotifyField = (key, val) =>
    setSettings((s) => ({ ...s, notifications: { ...s.notifications, [key]: val } }))

  const setSsoField = (key, val) =>
    setSettings((s) => ({ ...s, sso: { ...s.sso, [key]: val } }))

  // sso 段构建（issue #27）：client_secret 留空 = 保持现有凭据；全局 save 与
  // 卡片内 saveSso 共用，保证两处保存行为一致
  const buildSsoPatch = () => {
    const sso = {
      enabled: settings.sso.enabled,
      well_known_url: settings.sso.well_known_url,
      client_id: settings.sso.client_id,
      scope: settings.sso.scope,
      session_days: Number(settings.sso.session_days),
      redirect_uri: settings.sso.redirect_uri,
      verify_ssl: settings.sso.verify_ssl,
    }
    if (ssoSecretInput.trim()) sso.client_secret = ssoSecretInput.trim()
    return sso
  }

  // SSO 卡片内独立保存（issue #27 第四轮）：只提交 sso 段，
  // 后端 PUT /api/settings 支持部分更新，不影响其他设置
  const saveSso = async () => {
    setSsoBusy(true); setError(''); setSsoSaved(false)
    try {
      await api.put('/api/settings', { sso: buildSsoPatch() })
      setSsoSaved(true)
      setTimeout(() => setSsoSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setSsoBusy(false) }
  }

  // Owner token 独立保存（issue #87）：只提交 gitlab 段（部分更新）。
  // 留空 = 保持现有凭据（后端掩码不覆盖）；成功后清空输入框
  const saveOwnerToken = async () => {
    setOwnerBusy(true); setError(''); setOwnerSaved(false)
    try {
      await api.put('/api/settings', { gitlab: { owner_token: ownerTokenInput.trim() } })
      setOwnerSaved(true)
      setOwnerTokenInput('')
      setTimeout(() => setOwnerSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setOwnerBusy(false) }
  }

  const save = async () => {
    setBusy(true); setError(''); setSaved(false)
    try {
      const worker = {}
      for (const k of Object.keys(FIELD_LABELS)) {
        worker[k] = Number(settings.worker[k])
      }
      // issue 标签优先级（issue #76）：跟随全局「保存」提交 worker 段
      worker.issue_priority = settings.worker.issue_priority || ['bug', 'test', 'feature']
      await api.put('/api/settings', {
        worker,
        claude: { command: settings.claude.command, args: settings.claude.args },
        ui: { timezone: settings.ui?.timezone || '' },
        notifications: { ...settings.notifications },
        sso: buildSsoPatch(),
      })
      setDisplayTz(settings.ui?.timezone) // 立即生效，无需刷新页面
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const reconcileNow = async () => {
    setReconcileNote('')
    try {
      await api.post('/api/settings/reconcile-now')
      setReconcileNote('✓ 对账已在后台触发，可稍后查看任务列表')
    } catch (e) { setReconcileNote('✗ ' + e.message) }
  }

  return (
    <div>
      <div className="card">
        <h2>Synology SSO 登录</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>启用 SSO <code>sso.enabled</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.sso?.enabled === true}
                  onChange={(e) => setSsoField('enabled', e.target.checked)}
                />
                <span className="muted small">
                  {settings.sso?.enabled
                    ? '启用后访问本界面需用群晖账号登录'
                    : '未启用时保持开放访问（无需登录）'}
                </span>
              </td>
            </tr>
            <tr>
              <th>Well-known URL <code>well_known_url</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="https://群晖地址/.well-known/openid-configuration"
                  value={settings.sso?.well_known_url || ''}
                  onChange={(e) => setSsoField('well_known_url', e.target.value.trim())}
                />
              </td>
            </tr>
            <tr>
              <th>Application ID（Client ID）<code>client_id</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="群晖 SSO Server 应用配置中的 Application ID"
                  value={settings.sso?.client_id || ''}
                  onChange={(e) => setSsoField('client_id', e.target.value.trim())}
                />
              </td>
            </tr>
            <tr>
              <th>Application Secret <code>client_secret</code></th>
              <td>
                <input
                  className="input grow"
                  type="password"
                  placeholder={settings.sso?.client_secret_masked
                    ? `已配置（${settings.sso.client_secret_masked}），留空 = 保持现有`
                    : '群晖 SSO Server 应用配置中的 Application Secret'}
                  value={ssoSecretInput}
                  onChange={(e) => setSsoSecretInput(e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>Scope <code>scope</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="openid profile email"
                  value={settings.sso?.scope || ''}
                  onChange={(e) => setSsoField('scope', e.target.value.trim())}
                />
              </td>
            </tr>
            <tr>
              <th>登录有效期（天）<code>session_days</code></th>
              <td>
                <input
                  className="input num-input"
                  type="number"
                  min={1}
                  max={365}
                  value={settings.sso?.session_days ?? 30}
                  onChange={(e) => setSsoField('session_days', e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>回调地址 <code>redirect_uri</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="留空 = 按浏览器访问地址自动生成（https://主机/api/auth/callback）"
                  value={settings.sso?.redirect_uri || ''}
                  onChange={(e) => setSsoField('redirect_uri', e.target.value.trim())}
                />
              </td>
            </tr>
            <tr>
              <th>校验群晖证书 <code>verify_ssl</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.sso?.verify_ssl !== false}
                  onChange={(e) => setSsoField('verify_ssl', e.target.checked)}
                />
                <span className="muted small">群晖为自签名证书时取消勾选</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div className="form-row">
          <button className="btn btn-primary" disabled={ssoBusy} onClick={saveSso}>
            {ssoBusy ? '保存中…' : '保存 SSO 配置'}
          </button>
          {ssoSaved && <span className="saved-hint">✓ SSO 配置已保存（已写回 config.yaml）</span>}
        </div>
        <p className="muted small">
          接入群晖 SSO Server（OIDC 协议）：先在群晖「SSO Server → 应用程序」新增 OIDC 应用，
          填写回调地址并记下 Application ID / Secret，再回此页填写并保存。
          修改后点击下方「保存 SSO 配置」生效；启用后当前会话不受影响，下次访问需登录。
          完整的配置步骤（含群晖侧设置与常见问题）见下方「查看 SSO 配置指南」。
        </p>
        <div className="guide-box">
          <button className="btn" onClick={() => setGuideOpen((v) => !v)}>
            {guideOpen ? '收起 SSO 配置指南' : '查看 SSO 配置指南'}
          </button>
          {guideOpen && (
            <div className="guide-content">
              {guideError && (
                <div className="alert alert-error" onClick={() => setGuideError('')}>
                  指南文档不可用：{guideError}
                </div>
              )}
              {!guide && !guideError && <p className="muted">指南加载中…</p>}
              {guide && <Markdown content={guide} />}
            </div>
          )}
        </div>
      </div>

      {/* AI API 供应商（issue #46）：SSO 卡片后第二位，外部服务接入类配置聚合 */}
      <AiProvidersCard />

      <h1>系统设置</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok">✓ 已保存（已写回 config.yaml）</div>}

      <div className="card">
        <h2>任务调度</h2>
        <table className="table kv">
          <tbody>
            {Object.entries(FIELD_LABELS).map(([key, label]) => (
              <tr key={key}>
                <th>{label} <code>{key}</code></th>
                <td>
                  <input
                    className="input num-input"
                    type="number"
                    min={1}
                    value={settings.worker[key]}
                    onChange={(e) => setWorkerField(key, e.target.value)}
                  />
                </td>
              </tr>
            ))}
            <tr>
              <th>issue 标签优先级 <code>worker.issue_priority</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="bug, test, feature"
                  value={(settings.worker.issue_priority || []).join(', ')}
                  onChange={(e) => setIssuePriority(e.target.value)}
                />
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          issue 标签优先级：同仓库有多个排队任务时，按此顺序优先派发标签命中靠前的
          issue（默认 bug 最优先）；未列出的标签排在最后，同优先级按 issue 更新时间
          升序处理。逗号分隔、可增删调整顺序，修改后点击「保存」对已排队任务立即生效。
        </p>
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? '保存中…' : '保存'}
          </button>
          <button className="btn" onClick={reconcileNow}>立即对账一次</button>
          {reconcileNote && <span className="saved-hint">{reconcileNote}</span>}
        </div>
      </div>

      <div className="card">
        <h2>界面显示</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>显示时区 <code>ui.timezone</code></th>
              <td>
                <input
                  className="input grow"
                  list="timezone-options"
                  placeholder="留空 = 跟随本机（浏览器时区）"
                  value={settings.ui?.timezone || ''}
                  onChange={(e) => setSettings((s) => ({ ...s, ui: { timezone: e.target.value.trim() } }))}
                />
                <datalist id="timezone-options">
                  {COMMON_TZ.map((tz) => <option key={tz} value={tz} />)}
                </datalist>
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          任务创建/开始/完成时间与执行日志时间戳按此时区显示；留空则跟随本机浏览器时区（默认与访问者本机一致），
          修改后点击上方「保存」立即生效，无需刷新。
        </p>
      </div>

      <div className="card">
        <h2>网页通知</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>启用通知 <code>notifications.enabled</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.notifications?.enabled !== false}
                  onChange={(e) => setNotifyField('enabled', e.target.checked)}
                />
              </td>
            </tr>
            {Object.entries(NOTIFY_LABELS).map(([key, label]) => (
              <tr key={key}>
                <th>{label} <code>{key}</code></th>
                <td>
                  <input
                    type="checkbox"
                    className="check-input"
                    checked={settings.notifications?.[key] !== false}
                    onChange={(e) => setNotifyField(key, e.target.checked)}
                  />
                </td>
              </tr>
            ))}
            <tr>
              <th>浏览器授权</th>
              <td>
                {typeof Notification === 'undefined' ? (
                  <span className="muted">当前浏览器不支持通知</span>
                ) : window.isSecureContext === false ? (
                  <span className="muted">当前页面非安全上下文（需 HTTPS 且证书受信任），通知不可用</span>
                ) : Notification.permission === 'granted' ? (
                  <span className="ok-text">✓ 已授权</span>
                ) : Notification.permission === 'denied' ? (
                  <span className="muted">已拒绝（点击地址栏左侧图标将通知权限改为「允许」）</span>
                ) : (
                  <button className="btn" onClick={() => Notification.requestPermission()}>
                    授权系统通知
                  </button>
                )}
              </td>
            </tr>
            <tr>
              <th>测试通知</th>
              <td>
                <button className="btn" onClick={handleTestNotify}>弹出测试通知</button>
                {testNote && (
                  <span className={testNote.ok ? 'saved-hint' : 'err-hint'}>{testNote.text}</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          通过浏览器在电脑上弹出系统通知：任务需要交互（失败）、issue 完成、队列清空、无新任务可处理。
          修改后点击上方「保存」立即生效；需保持本页面打开（浏览器限制），首次启用时请授权系统通知。
        </p>
      </div>

      <div className="card">
        <h2>Claude Code</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>命令 <code>command</code></th>
              <td>
                <input
                  className="input grow"
                  value={settings.claude.command}
                  onChange={(e) => setSettings((s) => ({ ...s, claude: { ...s.claude, command: e.target.value } }))}
                />
              </td>
            </tr>
            <tr>
              <th>参数 <code>args</code></th>
              <td>
                <input
                  className="input grow"
                  value={settings.claude.args.join(' ')}
                  onChange={(e) => setSettings((s) => ({
                    ...s,
                    claude: { ...s.claude, args: e.target.value.split(/\s+/) },
                  }))}
                />
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          认证继承自服务器环境变量：
          {settings.env.anthropic_base_url
            ? <> <code>ANTHROPIC_BASE_URL={settings.env.anthropic_base_url}</code> · <code>ANTHROPIC_MODEL={settings.env.anthropic_model}</code></>
            : '（未配置 ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY，请检查 .env）'}
        </p>
      </div>

      <div className="card">
        <h2>本地环境检测</h2>
        <p className="muted small">
          检测 botler 服务器上常见 AI agent 与基础工具是否安装及其版本；
          最新版本来自 npm registry / GitHub API（网络不可达时显示 "—"）。
        </p>
        {envError && <div className="alert alert-error" onClick={() => setEnvError('')}>{envError}</div>}
        {!env && !envError && <p className="muted">检测中…</p>}
        {env && (
          <table className="table">
            <thead>
              <tr><th>工具</th><th>状态</th><th>已装版本</th><th>最新版本</th><th>提示</th></tr>
            </thead>
            <tbody>
              {env.tools.map((t) => (
                <tr key={t.key}>
                  <td>{t.name} <code>{t.key}</code></td>
                  <td>{t.installed
                    ? <span className="ok-text">✓ 已安装</span>
                    : <span className="muted">未安装</span>}</td>
                  <td>{t.version || <span className="muted">未知</span>}</td>
                  <td>{t.latest || <span className="muted">—</span>}</td>
                  <td>{envStatus(t)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="form-row">
          <button className="btn" disabled={envBusy} onClick={loadEnv}>
            {envBusy ? '检测中…' : '重新检测'}
          </button>
          {env && (
            <span className="muted small">
              {env.hostname} · {env.platform} · {fmtTime(env.detected_at)} 检测
            </span>
          )}
        </div>
      </div>

      <BackupManager />

      {/* Owner GitLab Token（issue #87）：专用于编辑 issue（评论/标签）的
          个人访问令牌，严禁用于推送代码与处理流水线 */}
      <div className="card">
        <h2>Owner GitLab Token（issue 编辑专用）</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>Token <code>gitlab.owner_token</code></th>
              <td>
                <input
                  className="input grow"
                  type="password"
                  placeholder={settings.gitlab?.owner_token_masked
                    ? `已配置（${settings.gitlab.owner_token_masked}），留空 = 保持现有`
                    : '粘贴 GitLab Personal Access Token（glpat-xxxx）'}
                  value={ownerTokenInput}
                  onChange={(e) => setOwnerTokenInput(e.target.value)}
                />
              </td>
            </tr>
          </tbody>
        </table>
        <div className="form-row">
          <button className="btn btn-primary" disabled={ownerBusy} onClick={saveOwnerToken}>
            {ownerBusy ? '保存中…' : '保存 Owner Token'}
          </button>
          {ownerSaved && (
            <span className="saved-hint">✓ Owner token 已保存（已写回 config.yaml）</span>
          )}
        </div>
        <p className="muted small">
          该 token 专门用来编辑 issue（写评论、打标签），botler 绝不会用它推送代码或处理
          流水线——推送与流水线操作仍使用 bot token。推荐用仓库 Reporter 角色的低权限账号
          申请（账号权限层面杜绝越权使用），申请步骤见下方「查看 token 申请教程」。
          留空保存 = 保持现有 token。
        </p>
        <div className="guide-box">
          <button className="btn" onClick={() => setOwnerGuideOpen((v) => !v)}>
            {ownerGuideOpen ? '收起 token 申请教程' : '查看 token 申请教程'}
          </button>
          {ownerGuideOpen && (
            <div className="guide-content">
              {ownerGuideError && (
                <div className="alert alert-error" onClick={() => setOwnerGuideError('')}>
                  教程文档不可用：{ownerGuideError}
                </div>
              )}
              {!ownerGuide && !ownerGuideError && <p className="muted">教程加载中…</p>}
              {ownerGuide && <Markdown content={ownerGuide} />}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h2>GitLab 凭据（只读）</h2>
        <table className="table kv">
          <tbody>
            <tr><th>地址</th><td>{settings.gitlab.url}</td></tr>
            <tr><th>bot 账号</th><td>{settings.gitlab.bot_username || '（自动识别）'}</td></tr>
            <tr><th>bot token</th><td><code>{settings.gitlab.bot_token_masked || '未配置'}</code></td></tr>
            <tr><th>webhook secret</th><td><code>{settings.gitlab.webhook_secret_masked || '未配置'}</code></td></tr>
          </tbody>
        </table>
        <p className="muted small">
          凭据修改请直接编辑 <code>backend/config.yaml</code> 与 <code>.env</code>，然后重启服务。
        </p>
      </div>

      {/* 版本信息（issue #9 第二轮）：从导航栏移入设置页面底部，
          每次 CI/CD 构建自动更新版本号与构建时间 */}
      <div className="card">
        <h2>版本信息</h2>
        <p className="muted small">当前版本与构建时间（每次 CI/CD 构建自动更新）：</p>
        <div className="settings-version">
          <VersionBadge />
        </div>
      </div>
    </div>
  )
}
