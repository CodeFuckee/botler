// 路由级代码分割（issue #202）：所有页面组件统一在此用 React.lazy 包装，
// App.jsx 按路由懒加载，Vite 构建时每个页面独立成 chunk，首屏只下载
// 当前页面代码；测试通过本模块引用同一 lazy 组件实例，或等待 chunk 加载
// 完成后断言底层页面组件（懒加载不影响测试渲染）。
import { lazy } from 'react'

export const Overview = lazy(() => import('./Overview.jsx'))
export const Repos = lazy(() => import('./Repos.jsx'))
export const Templates = lazy(() => import('./Templates.jsx'))
export const Labels = lazy(() => import('./Labels.jsx'))
export const Tasks = lazy(() => import('./Tasks.jsx'))
export const Stats = lazy(() => import('./Stats.jsx'))
export const TaskDetail = lazy(() => import('./TaskDetail.jsx'))
export const Settings = lazy(() => import('./Settings.jsx'))
export const Plugins = lazy(() => import('./Plugins.jsx'))
export const Skills = lazy(() => import('./Skills.jsx'))
export const Terminal = lazy(() => import('./Terminal.jsx'))
export const Login = lazy(() => import('./Login.jsx'))
