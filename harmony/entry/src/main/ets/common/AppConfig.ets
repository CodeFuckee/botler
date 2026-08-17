/**
 * Botler 鸿蒙端（Web 套壳）全局配置
 *
 * 鸿蒙端本质是「系统 Web 组件（WebView）套壳」，加载 Botler 的 Web 前端
 * （React/Vite 构建产物由 FastAPI 同源托管）。页面交互、任务管理、设置等
 * 全部复用 Web 端能力，鸿蒙端只负责提供原生壳与加载体验。
 *
 * 默认地址为部署机内网地址（pm2 部署，10.0.0.122:8000，ZeroTier 内网）；
 * 按实际部署环境修改 WEB_URL 即可，支持 http/https。
 */
export const WEB_URL: string = 'http://10.0.0.122:8000';
