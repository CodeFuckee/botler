import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：/api 与 /webhook 代理到 FastAPI 后端
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/webhook': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
