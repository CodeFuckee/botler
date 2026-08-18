import { useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import Repos from './pages/Repos.jsx'
import Overview from './pages/Overview.jsx'
import Templates from './pages/Templates.jsx'
import Labels from './pages/Labels.jsx'
import Tasks from './pages/Tasks.jsx'
import TaskDetail from './pages/TaskDetail.jsx'
import Settings from './pages/Settings.jsx'
import Plugins from './pages/Plugins.jsx'
import Terminal from './pages/Terminal.jsx'
import Login from './pages/Login.jsx'
import DialogHost from './components/DialogHost.jsx'
import { api, setDisplayTz, setSsoEnabled } from './api.js'
import { applyTheme, loadThemePreference, saveThemePreference, watchSystemTheme } from './theme.js'
import { createNotifyPoller, POLL_INTERVAL_MS } from './notify.js'
import { Icon } from './components/Icon.jsx'

export default function App() {
  // SSO 登录状态（issue #27）：null = 检测中；{enabled, user} = 结果。
  // SSO 启用且未登录 → 只渲染登录页，不加载主界面。
  const [auth, setAuth] = useState(null)
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

  // 网页通知轮询（issue #21）：每 10s 拉取新事件弹系统通知。
  // 设置开关在每次轮询时实时读取 → 设置页修改立即生效，无需刷新。
  // 首次拉取只记录游标不弹，避免历史事件轰炸。
  // 注意：本 effect 必须声明在所有条件 return 之前（React Hooks 规则，
  // 否则 auth 加载前后 hook 数量不一致会触发 error #310 整树崩溃白屏）；
  // auth 未就绪或 SSO 启用未登录时跳过启动（避免登录页轮询 401 反复刷新）。
  useEffect(() => {
    if (!auth || (auth.enabled && !auth.user)) return
    const poller = createNotifyPoller({
      getEvents: (after) => api.get(`/api/notifications/events?after=${after}`),
      getSettings: () => api.get('/api/settings'),
    })
    poller.poll()
    const timer = setInterval(poller.poll, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [auth])

  // HIG 匠心：整页加载态用居中 spinner，非裸文本
  if (!auth) {
    return (
      <div className="app-loading">
        <span className="spinner" aria-hidden="true" />
        <p className="muted">加载中…</p>
      </div>
    )
  }
  if (auth.enabled && !auth.user) return <Login />

  const logout = async () => {
    try { await api.post('/api/auth/logout') } catch { /* 忽略 */ }
    window.location.href = '/login'
  }

  return (
    <div className="app">
      {/* HIG 灵活：导航提供 aria-label 语义（屏幕阅读器可跳过） */}
      <nav className="topnav" aria-label="主导航">
        <div className="brand">
          <span className="brand-dot"><Icon name="bot" /></span> Botler
        </div>
        <NavLink to="/overview" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          概览
        </NavLink>
        {/* issue #54：默认页改为概览页，仓库页迁至 /repos */}
        <NavLink to="/repos" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          仓库
        </NavLink>
        <NavLink to="/tasks" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          任务
        </NavLink>
        <NavLink to="/templates" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          模版
        </NavLink>
        <NavLink to="/labels" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          标记库
        </NavLink>
        {/* 插件管理页（issue #145）：所有插件的安装、卸载和设置都在这个界面 */}
        <NavLink to="/plugins" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          插件
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          设置
        </NavLink>
        {/* Web 终端（issue #183）：浏览器内多标签终端，无需再打开系统终端 */}
        <NavLink to="/terminal" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          终端
        </NavLink>
        {/* 登录后用户名称与退出按钮位于导航栏最右（issue #9 第二轮）：
            .user-chip 的 margin-left:auto 承接原版本徽标的"推到最右"职责 */}
        {auth.user && (
          <span className="navlink user-chip" title="当前登录的群晖账号">
            <Icon name="user" /> {auth.user.username || auth.user.name || auth.user.sub}
            <button className="btn btn-sm" onClick={logout}>退出</button>
          </span>
        )}
      </nav>
      <main className="content">
        <Routes>
          {/* issue #54：默认页面改到概览页——/ 重定向到 /overview，
              仓库页迁至 /repos（replace 避免后退键卡在重定向环） */}
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/repos" element={<Repos />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:id" element={<TaskDetail />} />
          <Route path="/templates" element={<Templates />} />
          <Route path="/labels" element={<Labels />} />
          <Route path="/plugins" element={<Plugins />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/terminal" element={<Terminal />} />
        </Routes>
      </main>
      {/* 自定义对话框宿主（issue #105）：替代浏览器原生 alert/confirm，
          挂在根部全局唯一，供 confirmDialog / alertDialog 渲染 */}
      <DialogHost />
    </div>
  )
}
