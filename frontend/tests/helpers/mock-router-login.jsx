// Login.jsx 测试专用 react-router-dom mock（issue #104 补测）：
// 机制与 mock-router.jsx 相同，但 useSearchParams 返回的查询参数
// 可由 globalThis.__LOGIN_SEARCH_PARAMS 注入（默认空），供登录页
// error 参数各分支（login_failed / access_denied / 未知错误透传）
// 渲染测试切换场景。
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

export function Navigate() {
  return null
}

export function Route() {
  return null
}

export function Routes({ children }) {
  return children
}

export function useParams() {
  return { id: '3' }
}

export function useLocation() {
  return { search: '' }
}

export function useSearchParams() {
  return [new URLSearchParams(globalThis.__LOGIN_SEARCH_PARAMS || ''), () => {}]
}
