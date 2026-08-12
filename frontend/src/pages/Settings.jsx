import { useEffect, useState } from 'react'
import { api, setDisplayTz } from '../api.js'
import BackupManager from '../components/BackupManager.jsx'

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

  useEffect(() => {
    api.get('/api/settings').then(setSettings).catch((e) => setError(e.message))
  }, [])

  if (!settings) return <p className="muted">加载中…</p>

  const setWorkerField = (key, val) =>
    setSettings((s) => ({ ...s, worker: { ...s.worker, [key]: val } }))

  const setNotifyField = (key, val) =>
    setSettings((s) => ({ ...s, notifications: { ...s.notifications, [key]: val } }))

  const save = async () => {
    setBusy(true); setError(''); setSaved(false)
    try {
      const worker = {}
      for (const k of Object.keys(FIELD_LABELS)) {
        worker[k] = Number(settings.worker[k])
      }
      await api.put('/api/settings', {
        worker,
        claude: { command: settings.claude.command, args: settings.claude.args },
        ui: { timezone: settings.ui?.timezone || '' },
        notifications: { ...settings.notifications },
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
          </tbody>
        </table>
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
                ) : Notification.permission === 'granted' ? (
                  <span className="ok-text">✓ 已授权</span>
                ) : Notification.permission === 'denied' ? (
                  <span className="muted">已拒绝（需在浏览器地址栏重新允许通知）</span>
                ) : (
                  <button className="btn" onClick={() => Notification.requestPermission()}>
                    授权系统通知
                  </button>
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

      <BackupManager />

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
    </div>
  )
}
