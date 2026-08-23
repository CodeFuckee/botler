import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import { I18nProvider } from './i18n.jsx'
import { applyTheme, loadThemePreference } from './theme.js'
import './styles.css'
// 第三方看图组件 yet-another-react-lightbox（issue #462）：大图预览组件
// 样式（portal 浮层 / 缩放按钮 / 位置计数 / 截图名称），仅在浏览器构建
// 引入（SSR 单测不加载 main.jsx，不参与 vite 处理）
import 'yet-another-react-lightbox/styles.css'
import 'yet-another-react-lightbox/plugins/captions.css'
import 'yet-another-react-lightbox/plugins/counter.css'

// 界面主题（issue #217）：index.html 首屏 inline 脚本已按本地偏好设置
// <html data-theme>，这里兜底再应用一次（inline 脚本被 CSP 拦截等场景），
// 保证 React 挂载前后主题一致，不闪变。
applyTheme(loadThemePreference(localStorage) || 'system')

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <I18nProvider storage={typeof localStorage !== 'undefined' ? localStorage : null}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </I18nProvider>
  </React.StrictMode>,
)
