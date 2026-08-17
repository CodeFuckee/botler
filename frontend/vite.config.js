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
      '/api': 'http://localhost:8000',
      '/webhook': 'http://localhost:8000',
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/api': backendUrl,
      '/webhook': backendUrl,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
