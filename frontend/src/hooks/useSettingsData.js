// 设置页数据与交互逻辑（issue #201 拆分）：原 Settings.jsx（959 行）的
// 状态、数据加载与全部处理函数收敛到本 hook，Settings.jsx 只做组合编排
// （主文件 ≤400 行），各设置卡片（components/settings/*）为无状态组件，
// 通过 props 接收数据与处理函数，行为与拆分前一致（前端全量测试零回归）。
import { useEffect, useState } from 'react'
import { api, setDisplayTz } from '../api.js'
import { sendTestNotification } from '../notify.js'
import { applyTheme, saveThemePreference } from '../theme.js'
import { loadShortcutsEnabled } from '../keymap.js'
import { loadTimelineEnabled } from '../lib/notesTimeline.js'  // issue #342：评论/活动合并时间线开关
import { useI18n } from '../i18n.jsx'

// 任务调度数字字段的中文标签（与后端 worker 段字段一一对应）
export const FIELD_LABELS = {
  max_concurrent_repos: '跨仓库并行上限',
  max_retries: '失败重试次数',
  reconcile_interval_seconds: '对账扫描间隔（秒）',
  gitlab_api_requests_per_second: 'GitLab API 全局限速（请求/秒）',
  reconcile_jitter_min_seconds: '全量对账抖动下限（秒）',
  reconcile_jitter_max_seconds: '全量对账抖动上限（秒）',
  // 引擎降级阈值（issue #236）：连续 N 次「引擎类」失败（命令缺失 / API key
  // 无效 / SDK 错误）后自动降级到备用引擎；任务级失败不累计
  fallback_after_failures: '降级触发失败次数',
}

// 常用显示时区（issue #14）；支持手动输入任意 IANA 时区名
export const COMMON_TZ = [
  'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Singapore', 'Asia/Seoul',
  'UTC', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles',
]

// 定时暂停窗口生效星期（issue #169）：0=周一 … 6=周日，与后端 pause_weekdays
// 一致；全部不勾选 = 每天都生效
export const WEEKDAY_LABELS = [
  ['周一', 0], ['周二', 1], ['周三', 2], ['周四', 3],
  ['周五', 4], ['周六', 5], ['周日', 6],
]

// 网页通知时机开关（issue #21）：与后端通知事件类型一一对应
export const NOTIFY_LABELS = {
  task_needs_interaction: '任务需要交互（任务失败，需人工介入）',
  issue_completed: 'issue 完成（任务成功，issue 已关闭）',
  queue_empty: 'issue 列表为空（无待处理 issue）',
  queue_no_work: '无 issue 可处理（有 issue 但均在处理中）',
}

// 本地偏好存储（issue #217）：无 localStorage 环境（SSR/隐私模式/测试）
// 时传入 null，theme.js 内部静默忽略（与 SettingsNav issue #168 同款防护）
export const themeStorage = typeof localStorage !== 'undefined' ? localStorage : null

export function useSettingsData() {
  // 界面国际化（issue #268）：设置页「界面显示」卡片提供语言切换
  const { t, lang, setLang } = useI18n()
  // 键盘快捷键启用开关（issue #269）：localStorage botler.shortcuts
  // 持久化（与界面语言同模式），默认开启；帮助面板与设置页均可切换，
  // 分发处理器每次按键实时读取，无需刷新即全局生效
  const [shortcutsEnabled, setShortcutsEnabled] = useState(() =>
    loadShortcutsEnabled(typeof localStorage !== 'undefined' ? localStorage : null))
  // issue #342：概览页 issue 详情右边栏「评论与活动」合并时间线开关——
  // localStorage 键 botler.timeline（与快捷键开关 botler.shortcuts 同
  // 模式，默认关闭=分开显示，保持 issue #97 现状）；切换即时生效、刷新保持
  const [timelineEnabled, setTimelineEnabled] = useState(() =>
    loadTimelineEnabled(typeof localStorage !== 'undefined' ? localStorage : null))
  const [settings, setSettings] = useState(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reconcileNote, setReconcileNote] = useState('')
  const [testNote, setTestNote] = useState(null) // {ok, text}，测试通知结果提示
  const [env, setEnv] = useState(null) // 本地环境检测结果（issue #22）
  const [envError, setEnvError] = useState('')
  const [envBusy, setEnvBusy] = useState(false)
  // 工具升级（issue #465）：upgradingKey = 正在升级的工具 key（空=无）；
  // upgradeNote / upgradeError 分别为升级成功/失败提示
  const [upgradingKey, setUpgradingKey] = useState('')
  const [upgradeNote, setUpgradeNote] = useState('')
  const [upgradeError, setUpgradeError] = useState('')
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
  // 任务失败自动上报（issue #347）：卡片内独立保存（只提交 auto_issue 段，
  // 与 Webhook 卡片 issue #141 同模式）
  const [autoIssueSaveBusy, setAutoIssueSaveBusy] = useState(false)
  const [autoIssueSaved, setAutoIssueSaved] = useState(false)
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
  // 「聚合告警」卡片保存状态（issue #229）：独立保存按钮 busy / 保存成功提示
  const [alertSaveBusy, setAlertSaveBusy] = useState(false)
  const [alertSaved, setAlertSaved] = useState(false)
  // 「仓库健康巡检」卡片（issue #265）：inspection 段保存状态
  const [inspectionSaveBusy, setInspectionSaveBusy] = useState(false)
  const [inspectionSaved, setInspectionSaved] = useState(false)
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

  // 升级工具（issue #465）：点击「升级」后调用后端升级到最新版本，
  // 后端成功后延迟重启服务，前端提示用户稍后刷新页面
  const upgradeEnvTool = async (key) => {
    setUpgradingKey(key); setUpgradeNote(''); setUpgradeError('')
    try {
      await api.post('/api/environment/upgrade', { key })
      setUpgradeNote('升级成功，服务正在自动重启，请稍后刷新页面')
    } catch (e) {
      setUpgradeError(e.message || '升级失败')
    } finally {
      setUpgradingKey('')
    }
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

  // 备用引擎降级（issue #236）：文本框逗号分隔输入 ↔ 数组存储，提交时作为
  // worker.fallback_engines 数组写回（与 issue_priority 同模式）
  const setFallbackEngines = (text) =>
    setSettings((s) => ({
      ...s,
      worker: {
        ...s.worker,
        fallback_engines: text.split(',').map((x) => x.trim()).filter(Boolean),
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
  // 「聚合告警」卡片字段更新（issue #229）：alerts 段局部更新，随卡片内
  // 保存（saveAlerts）一次性提交，不影响其他设置
  const setAlertField = (key, val) =>
    setSettings((s) => ({ ...s, alerts: { ...s.alerts, [key]: val } }))
  // 「仓库健康巡检」卡片字段更新（issue #265）：inspection 段局部更新，
  // 随卡片内保存（saveInspection）一次性提交，不影响其他设置
  const setInspectionField = (key, val) =>
    setSettings((s) => ({ ...s, inspection: { ...s.inspection, [key]: val } }))
  const setOwnerTokenExpiry = (value) =>
    setSettings((s) => ({ ...s, gitlab: { ...s.gitlab, owner_token_expires_at: value } }))
  const saveOwnerTokenExpiry = async () => {
    setOwnerBusy(true); setError(''); setOwnerSaved(false)
    try {
      await api.put('/api/settings', { gitlab: {
        owner_token_expires_at: settings.gitlab?.owner_token_expires_at || '',
      } })
      setOwnerSaved(true)
      setTimeout(() => setOwnerSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setOwnerBusy(false) }
  }

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
  // 任务失败自动上报（issue #347）：字段级更新（enabled / assignee）
  const setAutoIssueField = (key, val) =>
    setSettings((s) => ({ ...s, auto_issue: { ...s.auto_issue, [key]: val } }))

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

  // 任务失败自动上报卡片内独立保存（issue #347）：只提交 auto_issue 段
  // （部分更新），后端 PUT /api/settings 支持部分更新，不影响其他设置
  const saveAutoIssue = async () => {
    setAutoIssueSaveBusy(true); setError(''); setAutoIssueSaved(false)
    try {
      await api.put('/api/settings', { auto_issue: { ...settings.auto_issue } })
      setAutoIssueSaved(true)
      setTimeout(() => setAutoIssueSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setAutoIssueSaveBusy(false) }
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

  // 「聚合告警」卡片内独立保存（issue #229）：只提交 alerts 段（部分更新），
  // 后端 PUT /api/settings 支持部分更新，不影响其他设置；阈值写回
  // config.yaml 后对账循环下次检测即按新阈值生效
  const saveAlerts = async () => {
    setAlertSaveBusy(true); setError(''); setAlertSaved(false)
    try {
      await api.put('/api/settings', { alerts: { ...settings.alerts } })
      setAlertSaved(true)
      setTimeout(() => setAlertSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setAlertSaveBusy(false) }
  }

  // 「仓库健康巡检」卡片内独立保存（issue #265）：只提交 inspection 段，
  // 后端 PUT /api/settings 支持部分更新，不影响其他设置；间隔/auto_repair
  // 写回 config.yaml 后下次定时巡检即按新配置生效
  const saveInspection = async () => {
    setInspectionSaveBusy(true); setError(''); setInspectionSaved(false)
    try {
      await api.put('/api/settings', { inspection: { ...settings.inspection } })
      setInspectionSaved(true)
      setTimeout(() => setInspectionSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setInspectionSaveBusy(false) }
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
      // 维护模式（issue #241）：人工总开关 + 新事件处理方式（默认入队保留，
      // 取消勾选 = 直接忽略新事件），跟随全局「保存」提交 worker 段
      worker.maintenance_mode = settings.worker?.maintenance_mode === true
      worker.maintenance_hold_events = settings.worker?.maintenance_hold_events !== false
      // 备用引擎降级（issue #236）：主引擎探测不可用或连续引擎类失败时
      // 按此顺序自动降级（空数组 = 不降级）
      worker.fallback_engines = settings.worker?.fallback_engines || []
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

  return {
    settings, setSettings,
    error, setError, saved, setSaved,
    busy, setBusy, reconcileNote, setReconcileNote,
    testNote, setTestNote,
    env, setEnv, envError, setEnvError, envBusy, setEnvBusy, loadEnv,
    upgradingKey, setUpgradingKey, upgradeNote, setUpgradeNote,
    upgradeError, setUpgradeError, upgradeEnvTool,
    ssoSecretInput, setSsoSecretInput, ssoBusy, setSsoBusy, ssoSaved, setSsoSaved,
    guide, guideError, guideOpen, setGuideOpen, setGuideError, setSsoField,
    buildSsoPatch, saveSso,
    webhookAuthInput, setWebhookAuthInput, webhookBusy, setWebhookBusy,
    webhookTestNote, setWebhookTestNote, webhookSaveBusy, setWebhookSaveBusy,
    webhookSaved, setWebhookSaved, setWebhookField, buildWebhookPatch,
    testWebhook, saveWebhook,
    setAutoIssueField, autoIssueSaveBusy, setAutoIssueSaveBusy,
    autoIssueSaved, setAutoIssueSaved, saveAutoIssue,
    uiSaveBusy, setUiSaveBusy, uiSaved, setUiSaved, buildUiPatch, saveUi,
    notifySaveBusy, setNotifySaveBusy, notifySaved, setNotifySaved,
    setNotifyField, handleTestNotify, saveNotify,
    setAlertField, alertSaveBusy, setAlertSaveBusy,
    alertSaved, setAlertSaved, saveAlerts,
    setInspectionField, inspectionSaveBusy, setInspectionSaveBusy,
    inspectionSaved, setInspectionSaved, saveInspection,
    minioAccessInput, setMinioAccessInput, minioSecretInput, setMinioSecretInput,
    minioSaveBusy, setMinioSaveBusy, minioSaved, setMinioSaved, setMinioField,
    buildMinioPatch, saveMinio,
    ownerTokenInput, setOwnerTokenInput, ownerBusy, setOwnerBusy, ownerSaved, setOwnerSaved,
    ownerGuide, ownerGuideError, ownerGuideOpen, setOwnerGuideOpen, setOwnerGuideError,
    setOwnerTokenExpiry, saveOwnerTokenExpiry, saveOwnerToken,
    pauseWindowsInput, setPauseWindowsInput, setWorkerField, setIssuePriority,
    setFallbackEngines, setPauseWindowsText, togglePauseWeekday, save, reconcileNow,
    t, lang, setLang, shortcutsEnabled, setShortcutsEnabled,
    timelineEnabled, setTimelineEnabled,  // issue #342：评论/活动合并时间线开关
  }
}
