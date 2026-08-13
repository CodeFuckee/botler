// react-router-dom 的最小 mock（node --test 渲染组件用，见 stop-all-button.test.mjs）。
//
// react-router-dom 的 CJS 构建无法被 vite SSR 转译（module is not defined），
// 而任务相关组件（Tasks.jsx）渲染时需要 Link；测试环境不关心真实路由行为，
// 用 vite resolve.alias 把 react-router-dom 指向本文件：MemoryRouter 透传
// children，Link 渲染为 <a>（to 为对象时取 pathname）。
//
// 其余导出（NavLink/Route/Routes/BrowserRouter/useParams/useLocation/
// useSearchParams）供 vite optimizeDeps 扫描 index.html 入口链时匹配，
// 避免预扫描报 missing export 噪音（不影响组件渲染断言）。
import React from 'react'

export function MemoryRouter({ children }) {
  return children
}

export function BrowserRouter({ children }) {
  return children
}

export function Link({ to, className, title, children }) {
  return React.createElement(
    'a',
    { href: typeof to === 'string' ? to : to?.pathname || '#', className, title },
    children,
  )
}

export const NavLink = Link

export function Route() {
  return null
}

export function Routes({ children }) {
  return children
}

// 详情页（TaskDetail.jsx）用到的路由 hook（issue #49 用时测试）：
// 测试环境固定返回 id=3 与空 location，不关心真实路由行为
export function useParams() {
  return { id: '3' }
}

export function useLocation() {
  return { search: '' }
}

export function useSearchParams() {
  return [new URLSearchParams(), () => {}]
}
