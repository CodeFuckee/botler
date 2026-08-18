import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：/api 与 /webhook 代理到 FastAPI 后端
// 预览模式（vite preview）同样代理，保证本地/CI 用「vite preview + uvicorn」
// 起真实前后端跑 E2E 时接口同源（issue #212）。E2E 后端地址可用
// E2E_BACKEND_URL 覆盖（CI 用独立端口起的 uvicorn），默认沿用 8000。
const backendUrl = process.env.E2E_BACKEND_URL || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // ws: true —— 终端 WebSocket（/api/terminal/ws/*）在开发模式同样代理到后端
      '/api': { target: 'http://localhost:8000', ws: true },
      '/webhook': { target: 'http://localhost:8000', ws: true },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/api': { target: backendUrl, ws: true },
      '/webhook': { target: backendUrl, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 路由级代码分割（issue #202）：react/react-dom 等全站共享依赖
        // 独立 vendor chunk（缓存稳定，页面改动不影响其 hash）；其余
        // node_modules 依赖按需并入对应页面 chunk，避免把终端等重依赖
        // 塞进首屏
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          // 注意顺序：react-router* 先于 react 匹配（其 id 含 'react'）
          if (id.includes('react-router')) return 'router-vendor'
          if (id.includes('/react/') || id.includes('/react-dom/') ||
              id.includes('/scheduler/') || id.includes('/react-is/')) {
            return 'react-vendor'
          }
          if (id.includes('@xterm')) return 'xterm-vendor'
          if (id.includes('lucide-react')) return 'icons-vendor'
        },
      },
    },
  },
})
