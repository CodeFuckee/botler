import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import { I18nProvider } from './i18n.jsx'
import { applyTheme, loadThemePreference } from './theme.js'
import './styles.css'

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
