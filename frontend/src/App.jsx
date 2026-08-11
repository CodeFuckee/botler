import { NavLink, Route, Routes } from 'react-router-dom'
import Repos from './pages/Repos.jsx'
import Templates from './pages/Templates.jsx'
import Tasks from './pages/Tasks.jsx'
import TaskDetail from './pages/TaskDetail.jsx'
import Settings from './pages/Settings.jsx'
import VersionBadge from './components/VersionBadge.jsx'

export default function App() {
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
