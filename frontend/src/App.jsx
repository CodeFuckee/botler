import { Suspense, useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
// 路由级代码分割（issue #202）：页面组件全部经 pages/lazy.jsx 的
// React.lazy 包装按路由懒加载——首屏只加载当前页面 chunk，其余页面
// 代码按需下载；页面切换期间由 <Suspense> fallback 展示轻量加载态
import {
  Overview, Repos, Templates, Labels, Tasks, Stats, TaskDetail,
  Settings, Plugins, Skills, Terminal, Login,
} from './pages/lazy.jsx'
import DialogHost from './components/DialogHost.jsx'
import ToastHost from './components/ToastHost.jsx'
import UserMenu from './components/UserMenu.jsx'
import ShortcutHelpModal from './components/ShortcutHelpModal.jsx'
import { useShortcuts } from './keymap.js'
import { api, setDisplayTz, setSsoEnabled, shortSha } from './api.js'
import { applyTheme, loadThemePreference, saveThemePreference, watchSystemTheme } from './theme.js'
import { createNotifyPoller, POLL_INTERVAL_MS } from './notify.js'
import { createVersionChecker } from './version-update.js'
import { Icon } from './components/Icon.jsx'
import { useI18n, LANG_LABELS } from './i18n.jsx'

// 路由懒加载加载态（issue #202）：页面 chunk 异步加载期间的轻量占位，
// 复用 HIG 既有 .spinner / .muted 视觉（居中 spinner + 「加载中…」），
// 避免路由切换出现空白闪烁；role=status 供屏幕阅读器播报
function PageLoading() {
  const { t } = useI18n()
  return (
    <div className="page-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p className="muted">{t('common.loading')}</p>
    </div>
  )
}

export default function App() {
  // SSO 登录状态（issue #27）：null = 检测中；{enabled, user} = 结果。
  // SSO 启用且未登录 → 只渲染登录页，不加载主界面。
  // 界面国际化（issue #268）：t 翻译 / lang 当前语言 / setLang 切换（默认中文，无 Provider 时回退）
  const { t, lang, setLang } = useI18n()
  const [auth, setAuth] = useState(null)
  const [redirect, setRedirect] = useState(null)
  // 快捷键帮助弹窗（issue #269）：右上角「快捷键帮助」按钮打开
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false)
  // 快捷键跳转（issue #269）：全局快捷键 t / g o / g s 设置跳转目标，
  // 经 <Navigate> 渲染完成路由切换后立即清除（不依赖 useNavigate——
  // 测试环境 mock Router 无导航上下文也不会崩溃；生产 BrowserRouter
  // 下正常跳转，replace 避免后退键卡在跳转环）
  useEffect(() => {
    api.get('/api/auth/status')
      .then((a) => { setSsoEnabled(a.enabled); setAuth(a) })
      .catch(() => setAuth({ enabled: false, user: null }))
  }, [])

  // 启动时加载页面显示时区（issue #14）；未设置前 fmtTime 跟随浏览器本机时区。
  // SSO 启用未登录时跳过（登录页不应发起受保护请求——401 兜底跳 /login 会
  // 整页重载，与登录页形成无限刷新循环，issue #27 第五轮）；登录成功后
  // auth 变化重新触发加载。
  // 本地偏好存储（issue #217）：无 localStorage 环境（SSR/测试）时用
  // globalThis 访问返回 undefined，theme.js 内部静默忽略，不抛错
  const themeStorage = typeof localStorage !== 'undefined' ? localStorage : null
  const [, setTzLoaded] = useState(false)
  useEffect(() => {
    if (!auth || (auth.enabled && !auth.user)) return
    api.get('/api/settings')
      .then((s) => {
        setDisplayTz(s.ui?.timezone)
        // 界面显示主题（issue #217）：后端 ui.theme 为跨设备权威配置，
        // 启动时应用并同步到本地 localStorage（首屏 inline 脚本读取，
        // 刷新不闪变）；未配置（旧版本）时保持 system 跟随系统
        const theme = s.ui?.theme || 'system'
        applyTheme(theme)
        saveThemePreference(themeStorage, theme)
        setTzLoaded(true)
      })
      .catch(() => {})
  }, [auth])

  // 系统深色偏好变化自动适配（issue #217）：仅当当前选择为「跟随系统」
  // 时重新应用——OS 切换深浅色，页面无需刷新即时跟随；手动浅色/深色
  // 不响应系统变化（用户已显式指定）。
  useEffect(() => {
    if (!auth || (auth.enabled && !auth.user)) return
    return watchSystemTheme(
      () => {
        try {
          return loadThemePreference(themeStorage) || 'system'
        } catch {
          return 'system'
        }
      },
      () => applyTheme(),
    )
  }, [auth])

  // 版本更新提示（issue #233）：页面加载后轮询 /version.json，检测到
  // 与基线版本不一致（新版部署完成）→ 显示刷新横幅。首次成功只记录基线
  // 不弹提示，版本变化只提示一次；SSO 未登录时跳过（登录页不轮询）。
  const [versionUpdate, setVersionUpdate] = useState(null)
  useEffect(() => {
    if (!auth || (auth.enabled && !auth.user)) return
    const checker = createVersionChecker({
      onUpdate: (info) => setVersionUpdate(info),
    })
    checker.start()
    return () => checker.stop()
  }, [auth])

  // 网页通知轮询（issue #21）：每 10s 拉取新事件弹系统通知。
  // 设置开关在每次轮询时实时读取 → 设置页修改立即生效，无需刷新。
  // 首次拉取只记录游标不弹，避免历史事件轰炸。
  // 注意：本 effect 必须声明在所有条件 return 之前（React Hooks 规则，
  // 否则 auth 加载前后 hook 数量不一致会触发 error #310 整树崩溃白屏）；
  // auth 未就绪或 SSO 启用未登录时跳过启动（避免登录页轮询 401 反复刷新）。
  useEffect(() => {
    if (!auth || (auth.enabled && !auth.user)) return
    const poller = createNotifyPoller({
      getEvents: (after) => api.get(`/api/notifications/events?after=${after}`, { silent: true }),
      getSettings: () => api.get('/api/settings', { silent: true }),
    })
    poller.poll()
    const timer = setInterval(poller.poll, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [auth])

  // 键盘快捷键（issue #269）：全站级绑定——t 跳转任务列表、
  // g o / g s 组合前往概览/设置页；n / r / / 为页面级绑定，由
  // 对应页面（Overview / Tasks）自行注册。输入框聚焦自动不触发、
  // 开关关闭全部失效（keymap.js 统一处理）；Esc 交由 DialogHost
  // 与各弹窗已有处理，不在此拦截
  useShortcuts({
    'go-tasks': () => setRedirect('/tasks'),
    'go-overview': () => setRedirect('/overview'),
    'go-settings': () => setRedirect('/settings'),
  }, { storage: typeof localStorage !== 'undefined' ? localStorage : null })

  // 跳转消费后清除 redirect（子组件 <Navigate> 的 effect 先于本父组件
  // effect 执行：先完成路由切换，再清空目标，<Navigate> 随即卸载，避免
  // 常驻导致用户后续手动导航时被拽回快捷键目标页）
  useEffect(() => {
    if (redirect) setRedirect(null)
  }, [redirect])

  // HIG 匠心：整页加载态用居中 spinner，非裸文本
  if (!auth) {
    return (
      <div className="app-loading">
        <span className="spinner" aria-hidden="true" />
        <p className="muted">{t('common.loading')}</p>
      </div>
    )
  }
  if (auth.enabled && !auth.user) {
    return (
      <Suspense fallback={<PageLoading />}>
        <Login />
      </Suspense>
    )
  }

  return (
    <div className="app">
      {/* 版本更新提示（issue #233）：新版部署完成后页面提示刷新，
          忽略后不再重复打扰，刷新后 VersionBadge 显示最新版本 */}
      {versionUpdate && (
        <div className="version-update-banner" role="status" aria-live="polite">
          <Icon name="refresh" aria-hidden="true" />
          <span className="version-update-banner-text">
            {t('app.versionDetected')} <strong>v{versionUpdate.version}</strong>
            {versionUpdate.buildTime && <span> · {t('app.builtAt', { time: versionUpdate.buildTime })}</span>}
            {versionUpdate.commit && <span> · {t('app.commitAt', { sha: shortSha(versionUpdate.commit) })}</span>}
            {t('app.refreshToLoad')}
          </span>
          <button className="btn btn-sm" onClick={() => window.location.reload()}>{t('app.refreshNow')}</button>
          <button className="btn btn-sm version-update-banner-dismiss" onClick={() => setVersionUpdate(null)}>{t('app.dismiss')}</button>
        </div>
      )}
      {/* HIG 灵活：导航提供 aria-label 语义（屏幕阅读器可跳过） */}
      <nav className="topnav" aria-label={t('nav.ariaMain')}>
        <div className="brand">
          <span className="brand-dot"><Icon name="bot" /></span> Botler
        </div>
        <NavLink to="/overview" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.overview')}
        </NavLink>
        {/* issue #54：默认页改为概览页，仓库页迁至 /repos */}
        <NavLink to="/repos" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.repos')}
        </NavLink>
        <NavLink to="/tasks" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.tasks')}
        </NavLink>
        {/* 统计看板页（issue #264）：成功率/引擎对比/仓库排行聚合视图 */}
        <NavLink to="/stats" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.stats')}
        </NavLink>
        <NavLink to="/templates" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.templates')}
        </NavLink>
        <NavLink to="/labels" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.labels')}
        </NavLink>
        {/* 插件管理页（issue #145）：所有插件的安装、卸载和设置都在这个界面 */}
        <NavLink to="/plugins" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.plugins')}
        </NavLink>
        {/* 技能管理页（issue #282）：展示各执行引擎拥有的技能，查看/编辑 skill.md */}
        <NavLink to="/skills" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.skills')}
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.settings')}
        </NavLink>
        {/* Web 终端（issue #183）：浏览器内多标签终端，无需再打开系统终端 */}
        <NavLink to="/terminal" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          {t('nav.terminal')}
        </NavLink>
        {/* 登录后用户名称与退出按钮位于导航栏最右（issue #9 第二轮）：
            .user-chip 的 margin-left:auto 承接原版本徽标的"推到最右"职责 */}
        {/* 界面语言快捷切换（issue #268）：右上角下拉，中/英即时切换并持久化到 localStorage */}
        <select
          className="lang-switch"
          value={lang}
          onChange={(e) => setLang(e.target.value)}
          title={t('nav.langTitle')}
          aria-label={t('nav.langTitle')}
        >
          {Object.entries(LANG_LABELS).map(([code, label]) => (
            <option key={code} value={code}>{label}</option>
          ))}
        </select>
        {/* 键盘快捷键（issue #269）：右上角「快捷键帮助」按钮——
            打开帮助面板展示全部键位 + 启用/禁用开关 */}
        <button
          type="button"
          className="btn btn-sm shortcuts-help-btn"
          onClick={() => setShortcutHelpOpen(true)}
          title={t('shortcuts.helpBtnTitle')}
          aria-label={t('shortcuts.helpBtnTitle')}
        >
          <Icon name="keyboard" /> {t('shortcuts.helpBtn')}
        </button>
        {/* 登录用户区（issue #271）：SSO 登录后右上角显示昵称/头像与「退出
            登录」按钮（头像失败回退首字母），未启用 SSO 时弱提示「未登录
            （开放模式）」；会话过期时间 tooltip 展示（与 #221 联动） */}
        <UserMenu user={auth.user} ssoEnabled={auth.enabled} />
      </nav>
      {/* 快捷键跳转（issue #269）：redirect 非空时切换路由并立即清空 */}
      {redirect && <Navigate to={redirect} replace />}
      <main className="content">
        <Suspense fallback={<PageLoading />}>
          <Routes>
            {/* issue #54：默认页面改到概览页——/ 重定向到 /overview，
                仓库页迁至 /repos（replace 避免后退键卡在重定向环） */}
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/repos" element={<Repos />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/tasks/:id" element={<TaskDetail />} />
            <Route path="/stats" element={<Stats />} />
            <Route path="/templates" element={<Templates />} />
            <Route path="/labels" element={<Labels />} />
            <Route path="/plugins" element={<Plugins />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/terminal" element={<Terminal />} />
          </Routes>
        </Suspense>
      </main>
      {/* 快捷键帮助弹窗（issue #269）：右上角按钮打开，展示键位与
          启用开关；Esc / 遮罩 / × 关闭（组件内自行监听 Esc） */}
      {shortcutHelpOpen && (
        <ShortcutHelpModal
          onClose={() => setShortcutHelpOpen(false)}
          storage={typeof localStorage !== 'undefined' ? localStorage : null}
        />
      )}
      {/* 自定义对话框宿主（issue #105）：替代浏览器原生 alert/confirm，
          挂在根部全局唯一，供 confirmDialog / alertDialog 渲染 */}
      <DialogHost />
      <ToastHost />
    </div>
  )
}
