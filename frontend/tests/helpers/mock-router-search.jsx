// SearchOverlay 测试专用 react-router-dom mock：useNavigate 记录跳转
// 目标（SearchOverlay 选中结果后调用 navigate(to)），测试断言跳转路径。
// 其余导出与 mock-router.jsx 一致（vite optimizeDeps 预扫描兼容）。
import React from 'react'

// 跳转记录：每次 useNavigate 返回的 navigate(to) 会 push 到这里；
// 每个用例开头需重置（navCalls.length = 0）
export const navCalls = []

export function MemoryRouter({ children }) {
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

export function useNavigate() {
  return (to) => navCalls.push(to)
}

export function useInRouterContext() {
  return false
}
