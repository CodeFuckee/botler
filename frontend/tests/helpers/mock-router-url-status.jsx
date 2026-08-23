// Tasks.jsx URL status 参数测试专用 react-router-dom mock（issue #257）：
// 机制与 mock-router.jsx 相同，但 useSearchParams 返回的查询参数可由
// globalThis.__URL_STATUS_PARAMS 注入（默认空串），供「导航栏水位徽章
// 点击跳转 /tasks?status=xxx → 任务列表按状态过滤」测试切换场景。
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
  return [new URLSearchParams(globalThis.__URL_STATUS_PARAMS || ''), () => {}]
}

export function useNavigate() {
  return (_to) => {}
}

export function useInRouterContext() {
  return false
}
