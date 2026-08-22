// 全页面多尺寸截图专用 Playwright 配置（issue #445）
//
// 截图 spec（e2e/screenshots/page-screenshots.spec.js）不参与常规
// e2e:playwright 跑批（testDir 仍为 e2e/tests，互不干扰），由独立
// CI job e2e:screenshots 使用本配置运行：
//   npx playwright test --config=playwright.screenshots.config.js
// 继承基础配置（baseURL / projects / use 等），仅覆盖：
//   - testDir 指向截图 spec 目录；
//   - retries=0：截图任务确定性执行，失败不重试（避免重复生成与等待）；
//   - reporter 仅 list：截图 job 不产出 JUnit（避免与 e2e:playwright
//     的 junit.xml 冲突，流水线报告抽屉仍以常规 E2E 为准）。
import { defineConfig } from '@playwright/test'
import baseConfig from './playwright.config.js'

export default defineConfig({
  ...baseConfig,
  testDir: './e2e/screenshots',
  retries: 0,
  reporter: [['list']],
  outputDir: 'test-results/screenshots',
})
