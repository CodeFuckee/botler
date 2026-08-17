// Playwright E2E 配置（issue #212）
// 目标：真实浏览器覆盖关键用户链路（概览页/添加 Issue/设置页/任务详情 SSE），
// GitLab 依赖接口走浏览器级 mock（frontend/e2e/support/mock-api.js），
// 其余接口走真实后端（uvicorn + vite preview 代理），稳定不依赖真实 GitLab。
// 服务编排见 frontend/e2e/scripts/start-servers.sh（本地与 CI 共用）。
import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.E2E_BASE_URL || 'http://127.0.0.1:4173'

export default defineConfig({
  // 测试目录：frontend/e2e/tests
  testDir: './e2e/tests',
  // 失败产物输出目录（html 报告 + 失败 trace）
  outputDir: 'test-results',

  // 单用例超时 30s；断言默认等待 10s（覆盖轮询/SSE 渲染）
  timeout: 30_000,
  expect: { timeout: 10_000 },
  // 无 flaky：重试策略——失败自动重试 2 次（共 3 次尝试），
  // 并在重试时保留 trace，便于排查偶发问题
  retries: 2,

  // 并行度：2 worker（共享同一 e2e 后端；用例均为只读或浏览器级 mock，
  // 互不写冲突）。如需完全串行可设 workers: 1
  workers: process.env.E2E_WORKERS ? Number(process.env.E2E_WORKERS) : 2,

  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
  ],

  use: {
    baseURL,
    // 真浏览器无头模式（CI/本地共用）；headed 调试可加 --headed
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
    // 本地时区固定为上海，时间断言稳定
    timezoneId: 'Asia/Shanghai',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
