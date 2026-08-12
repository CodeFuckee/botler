import { useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import Repos from './pages/Repos.jsx'
import Templates from './pages/Templates.jsx'
import Tasks from './pages/Tasks.jsx'
import TaskDetail from './pages/TaskDetail.jsx'
import Settings from './pages/Settings.jsx'
import Login from './pages/Login.jsx'
import VersionBadge from './components/VersionBadge.jsx'
import { api, setDisplayTz } from './api.js'
import { createNotifyPoller, POLL_INTERVAL_MS } from './notify.js'

export default function App() {
  // SSO 登录状态（issue #27）：null = 检测中；{enabled, user} = 结果。
  // SSO 启用且未登录 → 只渲染登录页，不加载主界面。
  const [auth, setAuth] = useState(null)
  useEffect(() => {
    api.get('/api/auth/status')
      .then(setAuth)
      .catch(() => setAuth({ enabled: false, user: null }))
  }, [])

  // 启动时加载页面显示时区（issue #14）；未设置前 fmtTime 跟随浏览器本机时区
  const [, setTzLoaded] = useState(false)
  useEffect(() => {
    api.get('/api/settings')
      .then((s) => { setDisplayTz(s.ui?.timezone); setTzLoaded(true) })
      .catch(() => {})
  }, [])

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

  if (!auth) return <p className="muted">加载中…</p>
  if (auth.enabled && !auth.user) return <Login />

  const logout = async () => {
    try { await api.post('/api/auth/logout') } catch { /* 忽略 */ }
    window.location.href = '/login'
  }

  return (
    <div className="app">
      <nav className="topnav">
        <div className="brand">
          <span className="brand-dot">🤖</span> Botler
        </div>
        <NavLink to="/" end className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          仓库
        </NavLink>
        <NavLink to="/tasks" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          任务
        </NavLink>
        <NavLink to="/templates" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          模版
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => 'navlink' + (isActive ? ' active' : '')}>
          设置
        </NavLink>
        {auth.user && (
          <span className="navlink user-chip" title="当前登录的群晖账号">
            👤 {auth.user.username || auth.user.name || auth.user.sub}
            <button className="btn btn-sm" onClick={logout}>退出</button>
          </span>
        )}
        <VersionBadge />
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Repos />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:id" element={<TaskDetail />} />
          <Route path="/templates" element={<Templates />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  )
}
