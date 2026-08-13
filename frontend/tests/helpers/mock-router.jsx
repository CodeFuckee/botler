// react-router-dom 的最小 mock（node --test 渲染组件用，见 stop-all-button.test.mjs）。
//
// react-router-dom 的 CJS 构建无法被 vite SSR 转译（module is not defined），
// 而任务相关组件（Tasks.jsx）渲染时需要 Link；测试环境不关心真实路由行为，
// 用 vite resolve.alias 把 react-router-dom 指向本文件：MemoryRouter 透传
// children，Link 渲染为 <a>（to 为对象时取 pathname）。
import React from 'react'

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
