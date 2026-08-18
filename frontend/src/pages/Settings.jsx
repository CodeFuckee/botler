import { useEffect, useState } from 'react'
import { api, setDisplayTz, fmtTime } from '../api.js'
import { sendTestNotification } from '../notify.js'
import AiProvidersCard from '../components/AiProvidersCard.jsx'
import ImageModelsCard from '../components/ImageModelsCard.jsx'
import VisionModelsCard from '../components/VisionModelsCard.jsx'
import BackupManager from '../components/BackupManager.jsx'
import SettingsNav from '../components/SettingsNav.jsx'
import Markdown from '../components/Markdown.jsx'
import VersionBadge from '../components/VersionBadge.jsx'
import { Icon } from '../components/Icon.jsx'
import {
  THEME_MODE_LABELS,
  applyTheme,
  saveThemePreference,
} from '../theme.js'
import { useI18n, LANG_LABELS } from '../i18n.jsx'
import { loadShortcutsEnabled, saveShortcutsEnabled } from '../keymap.js'

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

// 定时暂停窗口生效星期（issue #169）：0=周一 … 6=周日，与后端 pause_weekdays
// 一致；全部不勾选 = 每天都生效
const WEEKDAY_LABELS = [
  ['周一', 0], ['周二', 1], ['周三', 2], ['周四', 3],
  ['周五', 4], ['周六', 5], ['周日', 6],
]

// 网页通知时机开关（issue #21）：与后端通知事件类型一一对应
const NOTIFY_LABELS = {
  task_needs_interaction: '任务需要交互（任务失败，需人工介入）',
  issue_completed: 'issue 完成（任务成功，issue 已关闭）',
  queue_empty: 'issue 列表为空（无待处理 issue）',
  queue_no_work: '无 issue 可处理（有 issue 但均在处理中）',
}

// 本地偏好存储（issue #217）：无 localStorage 环境（SSR/隐私模式/测试）
// 时传入 null，theme.js 内部静默忽略（与 SettingsNav issue #168 同款防护）
const themeStorage = typeof localStorage !== 'undefined' ? localStorage : null

export default function Settings() {
  // 界面国际化（issue #268）：设置页「界面显示」卡片提供语言切换
  const { t, lang, setLang } = useI18n()
  // 键盘快捷键启用开关（issue #269）：localStorage botler.shortcuts
  // 持久化（与界面语言同模式），默认开启；帮助面板与设置页均可切换，
  // 分发处理器每次按键实时读取，无需刷新即全局生效
  const [shortcutsEnabled, setShortcutsEnabled] = useState(() =>
    loadShortcutsEnabled(typeof localStorage !== 'undefined' ? localStorage : null))
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

  // Webhook 消息推送（issue #136）：Authorization 输入框留空 = 保持现有
  // 凭据（后端掩码不覆盖，与 SSO client_secret 同模式）；测试推送结果提示
  const [webhookAuthInput, setWebhookAuthInput] = useState('')
  const [webhookBusy, setWebhookBusy] = useState(false)
  const [webhookTestNote, setWebhookTestNote] = useState(null) // {ok, text}
  // Webhook 卡片内独立保存（issue #141）：用户反馈「消息推送 Webhook设置
  // 没有保存按钮」——全局「保存」按钮在上方「任务调度」卡片，Webhook 卡片
  // 在页面下方，需在卡片内可独立保存（与 SSO 卡片 issue #27 同模式）
  const [webhookSaveBusy, setWebhookSaveBusy] = useState(false)
  const [webhookSaved, setWebhookSaved] = useState(false)
  // MinIO 对象存储（issue #170）：识图图片上传配置——Access Key / Secret Key
  // 输入框留空 = 保持现有凭据（后端掩码不覆盖，与 webhook.authorization /
  // SSO client_secret 同模式）；卡片内独立保存（与 Webhook 卡片 issue #141
  // 同模式）
  const [minioAccessInput, setMinioAccessInput] = useState('')
  const [minioSecretInput, setMinioSecretInput] = useState('')
  const [minioSaveBusy, setMinioSaveBusy] = useState(false)
  const [minioSaved, setMinioSaved] = useState(false)
  // 「界面显示」卡片内独立保存（issue #142 反馈轮）：用户反馈「取消勾选后
  // 没有保存按钮，无法保存设置」——全局「保存」按钮在上方「任务调度」卡片，
  // 「界面显示」卡片在其下方，需在卡片内可独立保存（与 SSO 卡片 issue #27 /
  // Webhook 卡片 issue #141 同模式）
  const [uiSaveBusy, setUiSaveBusy] = useState(false)
  const [uiSaved, setUiSaved] = useState(false)
  // 「网页通知」卡片内独立保存（issue #292）：用户反馈「设置里的网页通知
  // 增加一个保存按钮，现在无法保存设置」——全局「保存」按钮在「任务调度」
  // 卡片，「网页通知」卡片在其下方，需在卡片内可独立保存（与 SSO 卡片
  // issue #27 / Webhook 卡片 issue #141 / 界面显示卡片 issue #142 同模式）
  const [notifySaveBusy, setNotifySaveBusy] = useState(false)
  const [notifySaved, setNotifySaved] = useState(false)
  // 定时暂停窗口（issue #169）：textarea 每行一个窗口串 ↔ 数组存储；
  // 初始值在 settings 加载后回填（与 issue_priority 同模式）
  const [pauseWindowsInput, setPauseWindowsInput] = useState('')

  // 设置页「弹出测试通知」按钮（issue #21 增量）：直接弹一条浏览器
  // 系统通知验证功能；权限未决时 sendTestNotification 会先请求授权。
  const handleTestNotify = async () => {
    setTestNote(null)
    const res = await sendTestNotification()
    setTestNote(
      res.ok
        ? { ok: true, text: '已弹出测试通知，请查看系统通知' }
        : res.reason === 'insecure-context'
          ? { ok: false, text: '当前页面非安全上下文（需 HTTPS 且证书受信任），浏览器通知不可用' }
          : res.reason === 'denied'
            ? { ok: false, text: '浏览器已拒绝通知授权：点击地址栏左侧图标将通知权限改为「允许」后再试' }
            : { ok: false, text: '当前浏览器不支持系统通知' }
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
      ? <span className="ok-text"><Icon name="check" /> 已是最新</span>
      : <span className="err-hint"><Icon name="warning" /> 可升级</span>
  }

  useEffect(() => {
    api.get('/api/settings').then((s) => {
      setSettings(s)
      // 定时暂停窗口（issue #169）：数组回显为每行一个窗口串
      setPauseWindowsInput((s.worker?.pause_windows || []).join('\n'))
    }).catch((e) => setError(e.message))
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

  // 定时暂停窗口（issue #169）：textarea 每行一个窗口串 → 数组存储
  // （trim + 过滤空行，与 issue_priority 的逗号分隔同模式）
  const setPauseWindowsText = (text) => {
    setPauseWindowsInput(text)
    setWorkerField(
      'pause_windows',
      text.split('\n').map((x) => x.trim()).filter(Boolean),
    )
  }

  // 定时暂停窗口生效星期（issue #169）：勾选切换 0-6，升序去重存储
  const togglePauseWeekday = (day) => {
    const cur = settings.worker?.pause_weekdays || []
    const next = cur.includes(day)
      ? cur.filter((d) => d !== day)
      : [...cur, day]
    next.sort((a, b) => a - b)
    setWorkerField('pause_weekdays', next)
  }

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

  const setWebhookField = (key, val) =>
    setSettings((s) => ({ ...s, webhook: { ...s.webhook, [key]: val } }))

  // webhook 段构建（issue #136）：authorization 输入框留空 = 保持现有
  // 凭据（后端掩码不覆盖，与 sso.client_secret 同模式）；全局 save 与
  // 卡片内测试推送共用
  const buildWebhookPatch = () => {
    const wh = {
      enabled: settings.webhook?.enabled === true,
      url: settings.webhook?.url || '',
      content_type: settings.webhook?.content_type || 'application/json',
      body_template: settings.webhook?.body_template || '',
    }
    if (webhookAuthInput.trim()) wh.authorization = webhookAuthInput.trim()
    return wh
  }

  // ui 段构建（issue #142）：全局 save 与卡片内 saveUi 共用，保证两处保存
  // 行为一致；show_disabled_repos 未配置时按 true（显示）处理
  const buildUiPatch = () => {
    return {
      timezone: settings.ui?.timezone || '',
      show_disabled_repos: settings.ui?.show_disabled_repos !== false,
      // 界面显示主题（issue #217）：system / light / dark 三态，未配置时
      // 按 system（跟随系统 prefers-color-scheme）提交，兼容旧配置
      theme: settings.ui?.theme || 'system',
    }
  }

  // 设置页「发送测试推送」按钮（issue #136）：发送一条测试消息验证
  // webhook 配置可用（与任务完成推送共用同一发送链路）
  const testWebhook = async () => {
    setWebhookBusy(true); setWebhookTestNote(null)
    try {
      const res = await api.post('/api/settings/webhook-test')
      setWebhookTestNote(res.ok
        ? { ok: true, text: `测试推送成功（HTTP ${res.status_code}），请检查目标服务是否收到` }
        : { ok: false, text: (res.error || '发送失败') })
    } catch (e) { setWebhookTestNote({ ok: false, text: e.message }) }
    finally { setWebhookBusy(false) }
  }

  // Webhook 卡片内独立保存（issue #141）：只提交 webhook 段（部分更新），
  // 后端 PUT /api/settings 支持部分更新，不影响 worker/claude 等其他设置；
  // authorization 留空 = 保持现有凭据（buildWebhookPatch 处理）
  const saveWebhook = async () => {
    setWebhookSaveBusy(true); setError(''); setWebhookSaved(false)
    try {
      await api.put('/api/settings', { webhook: buildWebhookPatch() })
      setWebhookSaved(true)
      setTimeout(() => setWebhookSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setWebhookSaveBusy(false) }
  }

  // 「界面显示」卡片内独立保存（issue #142 反馈轮）：只提交 ui 段（部分
  // 更新），后端 PUT /api/settings 支持部分更新，不影响 worker/claude 等
  // 其他设置；保存后清空流水线概览缓存（后端处理），开关立即生效
  const saveUi = async () => {
    setUiSaveBusy(true); setError(''); setUiSaved(false)
    try {
      await api.put('/api/settings', { ui: buildUiPatch() })
      setDisplayTz(settings.ui?.timezone) // 立即生效，无需刷新页面
      // 界面显示主题（issue #217）：保存后立即应用 + 写入本地 localStorage，
      // 与后端 config.yaml（ui.theme）双向同步，刷新/重进页面不闪变
      const theme = settings.ui?.theme || 'system'
      applyTheme(theme)
      saveThemePreference(themeStorage, theme)
      setUiSaved(true)
      setTimeout(() => setUiSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setUiSaveBusy(false) }
  }

  // 「网页通知」卡片内独立保存（issue #292）：只提交 notifications 段
  // （部分更新），后端 PUT /api/settings 支持部分更新，不影响其他设置
  const saveNotify = async () => {
    setNotifySaveBusy(true); setError(''); setNotifySaved(false)
    try {
      await api.put('/api/settings', { notifications: { ...settings.notifications } })
      setNotifySaved(true)
      setTimeout(() => setNotifySaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setNotifySaveBusy(false) }
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

  const setMinioField = (key, val) =>
    setSettings((s) => ({ ...s, minio: { ...s.minio, [key]: val } }))

  // minio 段构建（issue #170）：access_key / secret_key 输入框留空 = 保持
  // 现有凭据（后端掩码不覆盖，与 webhook.authorization 同模式）；全局 save
  // 与卡片内 saveMinio 共用，保证两处保存行为一致
  const buildMinioPatch = () => {
    const m = {
      enabled: settings.minio?.enabled === true,
      endpoint: settings.minio?.endpoint || '',
      secure: settings.minio?.secure === true,
      bucket: settings.minio?.bucket || 'public',
      public_base_url: settings.minio?.public_base_url || '',
      verify_ssl: settings.minio?.verify_ssl !== false,
    }
    if (minioAccessInput.trim()) m.access_key = minioAccessInput.trim()
    if (minioSecretInput.trim()) m.secret_key = minioSecretInput.trim()
    return m
  }

  // MinIO 卡片内独立保存（issue #170）：只提交 minio 段（部分更新），后端
  // PUT /api/settings 支持部分更新，不影响其他设置；凭据留空 = 保持现有
  const saveMinio = async () => {
    setMinioSaveBusy(true); setError(''); setMinioSaved(false)
    try {
      await api.put('/api/settings', { minio: buildMinioPatch() })
      setMinioSaved(true)
      setMinioAccessInput(''); setMinioSecretInput('') // 保存成功清空凭据输入框
      setTimeout(() => setMinioSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setMinioSaveBusy(false) }
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
      // 任务执行引擎（issue #113）：切换后端编写代码的 agent
      worker.engine = settings.worker?.engine || 'claude'
      // 定时暂停窗口（issue #169）：窗口串 / 生效星期 / 时区跟随全局保存提交
      worker.pause_windows = settings.worker?.pause_windows || []
      worker.pause_weekdays = settings.worker?.pause_weekdays || []
      worker.pause_timezone = settings.worker?.pause_timezone || ''
      // 暂停窗口豁免优先级阈值（issue #299）：0=关闭；1~999=仓库调度
      // 优先级（数字越小越优先）不差于该值的仓库在窗口内仍可开始新任务
      worker.pause_priority_threshold = Number(settings.worker?.pause_priority_threshold ?? 0)
      await api.put('/api/settings', {
        worker,
        claude: { command: settings.claude.command, args: settings.claude.args },
        // dsh 引擎推理等级（issue #123）：跟随全局「保存」提交
        dsh: { reasoning_effort: settings.dsh?.reasoning_effort || '' },
        // 灵感 / CI/CD 页面是否显示未启用项目（issue #142）：关闭 = 只展示已启用仓库
        ui: buildUiPatch(),
        notifications: { ...settings.notifications },
        ...(settings.webhook ? { webhook: buildWebhookPatch() } : {}),
        // MinIO 对象存储（issue #170）：跟随全局「保存」提交（与 webhook 同模式）
        ...(settings.minio ? { minio: buildMinioPatch() } : {}),
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
      setReconcileNote({ ok: true, text: '对账已在后台触发，可稍后查看任务列表' })
    } catch (e) { setReconcileNote({ ok: false, text: e.message }) }
  }

  return (
    <div className="settings-layout">
      <SettingsNav />
      <div className="settings-content">
      {/* 设置页分组标题（issue #139）：与左侧导航栏分组一一对应 */}
      <h2 className="settings-group-title">外部服务接入</h2>
      <section id="settings-sso" className="settings-section">
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
          {ssoSaved && <span className="saved-hint"><Icon name="check" /> SSO 配置已保存（已写回 config.yaml）</span>}
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
      </section>

      {/* AI API 供应商（issue #46）：SSO 卡片后第二位，外部服务接入类配置聚合 */}
      <section id="settings-ai-providers" className="settings-section">
      <AiProvidersCard />
      </section>

      {/* 生图模型（issue #135）：AI 供应商卡片之后，同为外部服务接入类配置 */}
      <section id="settings-image-models" className="settings-section">
      <ImageModelsCard />
      </section>

      {/* 识图模型（issue #152）：独立区块（issue #155）——导航栏通过读取设置页
          区块动态生成，自动出现「识图模型」子选项，不再需要手工同步 */}
      <section id="settings-vision-models" className="settings-section">
      <VisionModelsCard />
      </section>

      {/* MinIO 对象存储（issue #170）：识图图片上传配置——启用后用户上传的
          图片先计算 SHA-256 哈希、以哈希值为对象名上传 MinIO public 桶
          （桶不存在自动创建并设为公开只读），识图请求传 http URL 而非
          base64（OpenAI 兼容网关拒绝 data: URL）；public_base_url 建议填
          nginx 代理地址（deploy/nginx-minio-public.conf），无需暴露 9000
          端口 */}
      <section id="settings-minio" className="settings-section">
      <div className="card">
        <h2>MinIO 对象存储</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>启用 MinIO <code>minio.enabled</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.minio?.enabled === true}
                  onChange={(e) => setMinioField('enabled', e.target.checked)}
                />
                <span className="muted small">
                  {settings.minio?.enabled
                    ? '启用后识图模型调用时图片自动上传 MinIO public 桶'
                    : '关闭时 OpenAI 兼容识图模型将报错引导启用 MinIO（issue #164）'}
                </span>
              </td>
            </tr>
            <tr>
              <th>API 地址 <code>endpoint</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="127.0.0.1:9000"
                  value={settings.minio?.endpoint || ''}
                  onChange={(e) => setMinioField('endpoint', e.target.value.trim())}
                />
              </td>
            </tr>
            <tr>
              <th>HTTPS <code>secure</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.minio?.secure === true}
                  onChange={(e) => setMinioField('secure', e.target.checked)}
                />
                <span className="muted small">endpoint 是否为 https（本机内网一般关闭）</span>
              </td>
            </tr>
            <tr>
              <th>Access Key <code>access_key</code></th>
              <td>
                <input
                  type="password"
                  className="input grow"
                  placeholder={settings.minio?.access_key_masked
                    ? `已配置（${settings.minio.access_key_masked}），留空 = 保持现有`
                    : 'MinIO 根凭据（MINIO_ROOT_USER）'}
                  value={minioAccessInput}
                  onChange={(e) => setMinioAccessInput(e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>Secret Key <code>secret_key</code></th>
              <td>
                <input
                  type="password"
                  className="input grow"
                  placeholder={settings.minio?.secret_key_masked
                    ? `已配置（${settings.minio.secret_key_masked}），留空 = 保持现有`
                    : 'MinIO 根凭据（MINIO_ROOT_PASSWORD）'}
                  value={minioSecretInput}
                  onChange={(e) => setMinioSecretInput(e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>桶名 <code>bucket</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="public"
                  value={settings.minio?.bucket || 'public'}
                  onChange={(e) => setMinioField('bucket', e.target.value.trim())}
                />
                <div className="muted small">不存在时自动创建并设为公开只读（匿名 s3:GetObject）</div>
              </td>
            </tr>
            <tr>
              <th>公网访问前缀 <code>public_base_url</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="https://home.chenkaidi.top:509/minio-public"
                  value={settings.minio?.public_base_url || ''}
                  onChange={(e) => setMinioField('public_base_url', e.target.value.trim())}
                />
                <div className="muted small">
                  识图模型取图的 http(s) 前缀，对象 URL = public_base_url/bucket/哈希；
                  建议填 nginx 代理地址（deploy/nginx-minio-public.conf），无需暴露 9000 端口
                </div>
              </td>
            </tr>
            <tr>
              <th>校验 endpoint 证书 <code>verify_ssl</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.minio?.verify_ssl !== false}
                  onChange={(e) => setMinioField('verify_ssl', e.target.checked)}
                />
                <span className="muted small">endpoint 为自签 https 证书时取消勾选</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div className="form-row">
          <button className="btn btn-primary" disabled={minioSaveBusy} onClick={saveMinio}>
            {minioSaveBusy ? '保存中…' : '保存 MinIO 配置'}
          </button>
          {minioSaved && <span className="saved-hint"><Icon name="check" /> MinIO 配置已保存（已写回 config.yaml）</span>}
        </div>
        <p className="muted small">
          识图模型图片上传（issue #163/#164）：启用后图片先计算 SHA-256 哈希、
          以哈希值为对象名上传 MinIO public 桶，识图请求传 http URL 而非 base64；
          凭据支持 ${'{'}ENV{'}'} 引用、留空回退环境变量 MINIO_ROOT_USER /
          MINIO_ROOT_PASSWORD（与部署写入 data/backend/.env 的凭据同源）。
          Gemini 官方接口仅支持 base64 inline_data，保持 base64 内联输入。
        </p>
      </div>
      </section>

      <h2 className="settings-group-title">系统设置</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok"><Icon name="check" /> 已保存（已写回 config.yaml）</div>}

      <section id="settings-tasks" className="settings-section">
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
            <tr>
              <th>
                定时暂停窗口
                <br /><code>worker.pause_windows</code>
              </th>
              <td>
                {settings.worker?.pause_active && (
                  <div className="alert alert-warning small">
                    <Icon name="warning" /> 当前处于暂停窗口：新任务暂缓开始，运行中任务不受影响，
                    未开始任务将在窗口结束后自动执行。
                  </div>
                )}
                <textarea
                  className="input grow"
                  rows={3}
                  placeholder={'09:00-12:00\n14:00-18:00'}
                  value={pauseWindowsInput}
                  onChange={(e) => setPauseWindowsText(e.target.value)}
                />
                <div className="weekday-row">
                  <span className="muted">生效星期 <code>worker.pause_weekdays</code>：</span>
                  {WEEKDAY_LABELS.map(([label, day]) => (
                    <label key={day} className="weekday-check">
                      <input
                        type="checkbox"
                        className="check-input"
                        checked={(settings.worker?.pause_weekdays || []).includes(day)}
                        onChange={() => togglePauseWeekday(day)}
                      />
                      {label}
                    </label>
                  ))}
                  <span className="muted">（不勾选 = 每天都生效）</span>
                </div>
                <div className="weekday-row">
                  <span className="muted">判断时区 <code>worker.pause_timezone</code></span>
                </div>
                <input
                  className="input grow"
                  list="pause-timezone-options"
                  placeholder="时区（IANA 名，留空 = 服务器本地时区）"
                  value={settings.worker?.pause_timezone || ''}
                  onChange={(e) => setWorkerField('pause_timezone', e.target.value.trim())}
                />
                <datalist id="pause-timezone-options">
                  {COMMON_TZ.map((tz) => <option key={tz} value={tz} />)}
                </datalist>
                <div className="weekday-row">
                  <span className="muted">豁免优先级 <code>worker.pause_priority_threshold</code>：</span>
                </div>
                <input
                  className="input num-input"
                  type="number"
                  min={0}
                  max={999}
                  placeholder="0（关闭）"
                  value={settings.worker?.pause_priority_threshold ?? 0}
                  onChange={(e) => setWorkerField('pause_priority_threshold', e.target.value)}
                />
                <span className="muted small">
                  仓库调度优先级（数字越小越优先）不差于该值的仓库，在暂停窗口内
                  仍可开始新任务；0 = 关闭（所有仓库都受暂停窗口约束）。
                </span>
              </td>
            </tr>
            <tr>
              <th>任务执行引擎 <code>worker.engine</code></th>
              <td>
                <select
                  className="input"
                  value={settings.worker?.engine || 'claude'}
                  onChange={(e) => setWorkerField('engine', e.target.value)}
                >
                  <option value="claude">claude — Claude Code CLI（默认）</option>
                  <option value="hermes">hermes — hermes-agent SDK</option>
                  <option value="dsh">dsh — deepseek-harness SDK</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          issue 标签优先级：同仓库有多个排队任务时，按此顺序优先派发标签命中靠前的
          issue（默认 bug 最优先）；未列出的标签排在最后，同优先级按 issue 更新时间
          升序处理。逗号分隔、可增删调整顺序，修改后点击「保存」对已排队任务立即生效。
        </p>
        <p className="muted small">
          定时暂停窗口：窗口内停止开始新任务，已经开始执行的任务可以继续执行，
          未开始执行的任务等到窗口结束后自动开始执行。每行一个窗口（
          HH:MM-HH:MM，24 小时制，支持跨天如 22:00-02:00）；星期勾选生效
          范围（不勾选 = 每天都生效）；时区留空 = 服务器本地时区。修改后
          点击「保存」立即生效，对运行中任务无影响。
        </p>
        <p className="muted small">
          暂停窗口豁免优先级（issue #299）：填写仓库调度优先级阈值（1~999，
          数字越小越优先）后，优先级不差于该值（priority ≤ 阈值）的仓库在
          暂停窗口内仍可开始新任务，不受窗口影响；填 0 或留空 = 关闭豁免，
          所有仓库都受暂停窗口约束。
        </p>
        <p className="muted small">
          任务执行引擎：切换后端编写代码的 agent，默认 claude（Claude Code CLI）；
          hermes 为 hermes-agent SDK（进程内调用，源码经
          deploy/install-hermes-agent.sh editable 安装进 botler venv），dsh 为
          deepseek-harness SDK（DeepSeek API Key 走部署机环境变量或「dsh 引擎」
          配置段）。切换后点击「保存」立即生效，对新领取的任务使用新引擎，
          运行中任务不受影响。
        </p>
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? '保存中…' : '保存'}
          </button>
          <button className="btn" onClick={reconcileNow}>立即对账一次</button>
          {reconcileNote && <span className={reconcileNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={reconcileNote.ok ? 'check' : 'x'} /> {reconcileNote.text}</span>}
        </div>
      </div>

      </section>
      <section id="settings-ui" className="settings-section">
      <div className="card">
        <h2>界面显示</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>显示未启用项目 <code>ui.show_disabled_repos</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.ui?.show_disabled_repos !== false}
                  onChange={(e) => setSettings((s) => ({
                    ...s,
                    ui: { ...(s.ui || {}), show_disabled_repos: e.target.checked },
                  }))}
                />
              </td>
            </tr>
            <tr>
              <th>界面主题 <code>ui.theme</code></th>
              <td>
                <select
                  className="input"
                  value={settings.ui?.theme || 'system'}
                  onChange={(e) => {
                    const theme = e.target.value
                    setSettings((s) => ({ ...s, ui: { ...(s.ui || {}), theme } }))
                    // 立即预览（issue #217）：切换三态即时生效，无需等保存；
                    // 保存按钮负责持久化到后端 config.yaml 与本地 localStorage
                    applyTheme(theme)
                    saveThemePreference(themeStorage, theme)
                  }}
                >
                  {Object.entries(THEME_MODE_LABELS).map(([mode, label]) => (
                    <option key={mode} value={mode}>{label}</option>
                  ))}
                </select>
              </td>
            </tr>
            <tr>
              {/* 界面语言（issue #268）：切换即时生效并持久化到 localStorage
                  botler.lang（刷新后保持）；语言名以母语展示 */}
              <th>{t('settings.ui.language')} <code>botler.lang</code></th>
              <td>
                <select
                  className="input"
                  value={lang}
                  onChange={(e) => setLang(e.target.value)}
                >
                  {Object.entries(LANG_LABELS).map(([code, label]) => (
                    <option key={code} value={code}>{label}</option>
                  ))}
                </select>
              </td>
            </tr>
            <tr>
              {/* 键盘快捷键（issue #269）：全站快捷键启用开关，持久化
                  localStorage（键 botler.shortcuts，默认开启）；帮助
                  面板同步展示同一开关，任意一处切换即时全局生效 */}
              <th>启用键盘快捷键 <code>botler.shortcuts</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input shortcuts-toggle-input"
                  checked={shortcutsEnabled}
                  onChange={(e) => {
                    setShortcutsEnabled(e.target.checked)
                    saveShortcutsEnabled(
                      typeof localStorage !== 'undefined' ? localStorage : null,
                      e.target.checked)
                  }}
                />
              </td>
            </tr>
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
        <div className="form-row">
          <button className="btn btn-primary" disabled={uiSaveBusy} onClick={saveUi}>
            {uiSaveBusy ? '保存中…' : '保存界面显示配置'}
          </button>
          {uiSaved && <span className="saved-hint"><Icon name="check" /> 界面显示配置已保存（已写回 config.yaml）</span>}
        </div>
        <p className="muted small">
          灵感板块与 CI/CD 流水线板块是否显示未启用项目：勾选 = 显示（未启用仓库带
          「未启用」徽章，默认）；取消 = 两个板块只展示已启用仓库。
          界面主题三态：跟随系统（prefers-color-scheme 自动适配）/ 浅色 / 深色，
          切换即时预览，保存后写回 config.yaml 并同步浏览器本地偏好，刷新不闪变。
          任务创建/开始/完成时间与执行日志时间戳按显示时区展示；留空则跟随本机浏览器时区
          （默认与访问者本机一致），修改后点击下方「保存界面显示配置」立即生效，无需刷新。
          键盘快捷键（n 新建 issue / r 刷新 / t 跳转任务 / g o 概览 / g s 设置 / / 聚焦搜索）：
          勾选 = 启用（默认），取消 = 全站快捷键立即失效；输入框聚焦时快捷键不触发，避免误操作。
          {t('settings.ui.languageHint')}
        </p>
      </div>

      </section>
      <section id="settings-notifications" className="settings-section">
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
                  <span className="ok-text"><Icon name="check" /> 已授权</span>
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
                  <span className={testNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={testNote.ok ? 'check' : 'x'} /> {testNote.text}</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
        <div className="form-row">
          <button className="btn btn-primary" disabled={notifySaveBusy} onClick={saveNotify}>
            {notifySaveBusy ? '保存中…' : '保存网页通知配置'}
          </button>
          {notifySaved && <span className="saved-hint"><Icon name="check" /> 网页通知配置已保存（已写回 config.yaml）</span>}
        </div>
        <p className="muted small">
          通过浏览器在电脑上弹出系统通知：任务需要交互（失败）、issue 完成、队列清空、无新任务可处理。
          修改后点击下方「保存网页通知配置」立即生效；需保持本页面打开（浏览器限制），首次启用时请授权系统通知。
        </p>
      </div>

      </section>
      <section id="settings-webhook" className="settings-section">
      <div className="card">
        <h2>消息推送 Webhook</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>启用推送 <code>webhook.enabled</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.webhook?.enabled === true}
                  onChange={(e) => setWebhookField('enabled', e.target.checked)}
                />
              </td>
            </tr>
            <tr>
              <th>Webhook 地址 <code>url</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="https://example.com/webhook/botler"
                  value={settings.webhook?.url || ''}
                  onChange={(e) => setWebhookField('url', e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>Content-Type <code>content_type</code></th>
              <td>
                <input
                  className="input grow"
                  placeholder="application/json"
                  value={settings.webhook?.content_type || 'application/json'}
                  onChange={(e) => setWebhookField('content_type', e.target.value)}
                />
              </td>
            </tr>
            <tr>
              <th>Authorization <code>authorization</code></th>
              <td>
                <input
                  type="password"
                  className="input grow"
                  placeholder={settings.webhook?.authorization_masked
                    ? settings.webhook.authorization_masked
                    : '可选，如 Bearer xxxxx'}
                  value={webhookAuthInput}
                  onChange={(e) => setWebhookAuthInput(e.target.value)}
                />
                <div className="muted small">
                  可选，如 <code>Bearer xxxxx</code>；留空 = 保持现有凭据
                </div>
              </td>
            </tr>
            <tr>
              <th>POST 结构体 <code>body_template</code></th>
              <td>
                <textarea
                  className="input textarea"
                  rows="8"
                  value={settings.webhook?.body_template || ''}
                  onChange={(e) => setWebhookField('body_template', e.target.value)}
                />
                <div className="muted small">
                  可使用全局模板占位符：
                  {Object.keys(settings.templates?.placeholders || {}).map((k) => (
                    <span key={k}> <code>{'{' + k + '}'}</code></span>
                  ))}
                  ，请求时自动填充；留空 = 内置默认 JSON 模板。
                </div>
              </td>
            </tr>
            <tr>
              <th>测试推送</th>
              <td>
                <button className="btn" onClick={testWebhook} disabled={webhookBusy}>
                  {webhookBusy ? '发送中…' : '发送测试推送'}
                </button>
                {webhookTestNote && (
                  <span className={webhookTestNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={webhookTestNote.ok ? 'check' : 'x'} /> {webhookTestNote.text}</span>
                )}
              </td>
            </tr>
          </tbody>
        </table>
        <div className="form-row">
          <button className="btn btn-primary" disabled={webhookSaveBusy} onClick={saveWebhook}>
            {webhookSaveBusy ? '保存中…' : '保存 Webhook 配置'}
          </button>
          {webhookSaved && <span className="saved-hint"><Icon name="check" /> Webhook 配置已保存（已写回 config.yaml）</span>}
        </div>
        <p className="muted small">
          任务完成（成功收尾）时调用 webhook 进行消息推送。修改后点击下方「保存 Webhook 配置」立即生效；
          可先点「发送测试推送」验证配置是否可用（推送失败不会影响任务收尾）。
        </p>
      </div>

      </section>
      <h2 className="settings-group-title">执行引擎</h2>
      <section id="settings-claude" className="settings-section">
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

      {/* dsh 引擎（issue #84）：deepseek-harness SDK 推理等级设置（issue #123）。
           SDK 运行时 llm-deepseek adapter 支持 reasoningEffort（off / high / max），
           botler 在设置后自动派生 Cordis 注入，无需手工维护 cordis 文件 */}
      </section>
      <section id="settings-dsh" className="settings-section">
      <div className="card">
        <h2>dsh 引擎</h2>
        <table className="table kv">
          <tbody>
            <tr>
              <th>推理等级 <code>reasoning_effort</code></th>
              <td>
                <select
                  className="input"
                  value={settings.dsh?.reasoning_effort || ''}
                  onChange={(e) => setSettings((s) => ({
                    ...s,
                    dsh: { ...s.dsh, reasoning_effort: e.target.value },
                  }))}
                >
                  <option value="">默认（不设置，SDK 默认 high）</option>
                  <option value="off">off — 关闭推理（更快更省）</option>
                  <option value="high">high — 高</option>
                  <option value="max">max — 最高（更严谨，更慢更贵）</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <p className="muted small">
          dsh 引擎（deepseek-harness SDK）的推理等级（reasoningEffort）：控制模型思考深度，
          off = 关闭推理、high = 高、max = 最高。修改后点击上方「保存」生效，对新领取的
          dsh 引擎任务生效，运行中任务不受影响。留空 = 不设置（SDK 默认）。
        </p>
      </div>

      </section>
      <h2 className="settings-group-title">运维与数据</h2>
      <section id="settings-environment" className="settings-section">
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
                    ? <span className="ok-text"><Icon name="check" /> 已安装</span>
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
      </section>

      <section id="settings-backup" className="settings-section">
      <BackupManager />
      </section>

      {/* Owner GitLab Token（issue #87）：专用于编辑 issue（评论/标签）的
          个人访问令牌，严禁用于推送代码与处理流水线。
          issue #130：系统架构层已隔离——所有 Agent 均不可使用，只允许
          在概览页面编辑 issue、添加 issue、关闭 issue、添加评论与回复
          评论时由平台使用；Agent 只能使用自己仓库的认证 token 编辑 issue */}
      <h2 className="settings-group-title">账号与安全</h2>
      <section id="settings-owner-token" className="settings-section" data-nav-label="Owner GitLab Token">
      <div className="card">
        <h2>
          Owner GitLab Token（issue 编辑专用）
          <span className="badge badge-muted owner-token-isolated">已隔离 · Agent 不可用</span>
        </h2>
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
            <span className="saved-hint"><Icon name="check" /> Owner token 已保存（已写回 config.yaml）</span>
          )}
        </div>
        <p className="muted small">
          <Icon name="lock" /> <strong>隔离状态</strong>：该 token 已由系统架构隔离，<strong>所有 Agent
          均不可使用</strong>——Agent 处理 issue 时只能使用自己仓库的认证 token 进行
          issue 编辑，会话环境中绝不会注入此 token。
        </p>
        <p className="muted small">
          <strong>允许使用范围</strong>：仅限在概览页面上编辑 issue、添加 issue、关闭 issue、
          在 issue 添加评论以及回复 issue 评论时由平台使用；其他场景（推送代码、处理流水线
          等）一律不得使用，botler 绝不会用它推送代码或处理流水线——推送与流水线操作仍
          使用 bot token。推荐用仓库 Reporter 角色的低权限账号申请（账号权限层面杜绝越权
          使用），申请步骤见下方「查看 token 申请教程」。留空保存 = 保持现有 token。
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

      </section>
      <section id="settings-gitlab-cred" className="settings-section">
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
      </section>
      <h2 className="settings-group-title">关于</h2>
      <section id="settings-version" className="settings-section">
      <div className="card">
        <h2>版本信息</h2>
        <p className="muted small">当前版本、构建时间与提交（每次 CI/CD 构建自动更新，构建信息见 /version.json 与 /api/health）：</p>
        <div className="settings-version">
          <VersionBadge />
        </div>
      </div>
      </section>
      </div>
    </div>
  )
}
