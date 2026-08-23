// 概览页 CI/CD 流水线详情右边栏截图大图 → 第三方看图组件 E2E（issue #462）：
// 大图预览从自研浮层升级为 yet-another-react-lightbox（YARL）后，在真实
// Chromium 中验证用户可见行为：
//   1) 打开流水线详情抽屉 → 「查看截图」→ 缩略图网格；
//   2) 点击缩略图 → YARL 看图浮层（.yarl__portal）打开，且浮层铺满整页
//      （脱离 .drawer 的 will-change: transform 包含块——issue #459 根因，
//      浮层 boundingBox 应覆盖整个视口，而非被限定在 520px 抽屉内）；
//   3) 看图组件能力：位置计数（counter 1 / N）、缩放按钮（zoom）、
//      截图名称（captions 展示 页面 / 视口）、上一张/下一张切换；
//   4) Esc / 关闭按钮 → 浮层关闭。
// GitLab 依赖接口（pipelines 概览 / 截图列表 / 截图字节）浏览器级 mock，
// 其余接口走真实后端。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 1x1 像素 PNG（缩略图/原图字节均返回它，YARL 只关心能加载成功的图片）
const TINY_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

// 带 e2e:screenshots job（archive 产物）的流水线 fixture：
// 与单元测试 overview-pipeline-drawer.test.mjs 的入口结构一致
function screenshotsPipelineFixture() {
  return {
    pipelines: [{
      repo_id: 1,
      repo_name: 'botler',
      enabled: true,
      pipeline: {
        id: 731,
        status: 'success',
        ref: 'main',
        sha: 'abc123def456',
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/pipelines/731',
        created_at: '2026-08-19T08:00:00.000Z',
        updated_at: '2026-08-19T08:05:00.000Z',
        finished_at: '2026-08-19T08:05:00.000Z',
        duration: 300,
      },
      stages: [{
        name: 'e2e',
        status: 'success',
        jobs: [{
          id: 77,
          name: 'e2e:screenshots',
          status: 'success',
          web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/jobs/77',
          artifacts: [{
            file_type: 'archive',
            filename: 'artifacts.zip',
            size: 19579475,
            file_format: 'zip',
          }],
        }],
      }],
      commit_time: '2026-08-19 08:00:00',
    }],
    errors: [],
  }
}

const SCREENSHOTS_PAYLOAD = {
  job_id: 77,
  screenshots: [
    { path: 'frontend/screenshots/overview/desktop-1440x900.png',
      page: 'overview', viewport: 'desktop-1440x900', size: 24421 },
    { path: 'frontend/screenshots/overview/mobile-375x667.png',
      page: 'overview', viewport: 'mobile-375x667', size: 20001 },
  ],
}

test('截图大图第三方看图组件：点击缩略图打开 YARL 浮层并支持缩放/切换/关闭（issue #462）', async ({ page }) => {
  await mockGitLabApis(page, { pipelines: screenshotsPipelineFixture() })

  // 截图列表 + 预览/原图字节全部浏览器级 mock（真实后端无法访问 GitLab
  // 归档，与其余 GitLab 依赖接口同策略）
  await page.route('**/api/pipelines/1/screenshots*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(SCREENSHOTS_PAYLOAD),
    })
  })
  await page.route('**/api/pipelines/1/screenshot-preview*', async (route) => {
    await route.fulfill({ contentType: 'image/png', body: TINY_PNG })
  })
  await page.route('**/api/pipelines/1/screenshot-file*', async (route) => {
    await route.fulfill({ contentType: 'image/png', body: TINY_PNG })
  })

  await page.goto('/overview')

  // 1) 打开流水线详情抽屉（点击流水线卡片主体按钮）
  const cardBtn = page.locator('button.pipeline-link').first()
  await expect(cardBtn).toBeVisible()
  await cardBtn.click()

  // 2) e2e:screenshots 任务行 → 「查看截图」→ 缩略图网格
  const shotBtn = page.locator('.pipeline-detail-screenshot-btn')
  await expect(shotBtn).toBeVisible()
  await shotBtn.click()
  const thumbs = page.locator('.pipeline-screenshots-thumb')
  await expect(thumbs).toHaveCount(2)
  await expect(page.locator('.pipeline-screenshots-thumb-name').first()).toHaveText('desktop-1440x900')

  // 3) 点击第一张缩略图 → YARL 看图浮层打开
  await thumbs.first().click()
  const portal = page.locator('.yarl__portal')
  await expect(portal).toBeVisible()

  // 4) 浮层铺满整页（issue #459 根因验证）：不受 .drawer 520px 宽度限制
  const box = await portal.boundingBox()
  const vp = page.viewportSize()
  expect(box.width).toBeGreaterThanOrEqual(vp.width - 1)
  expect(box.height).toBeGreaterThanOrEqual(vp.height - 1)

  // 5) 看图组件能力：
  //    - counter 位置计数「1 / 2」
  await expect(page.locator('.yarl__counter')).toHaveText('1 / 2')
  //    - captions 展示「页面 / 视口」名称（.yarl__slide_title）
  await expect(page.locator('.yarl__slide_title').first()).toHaveText('overview / desktop-1440x900')
  //    - zoom 缩放按钮（中文 label 来自组件 labels）
  await expect(page.locator('.yarl__toolbar button[title="放大"]')).toBeVisible()
  await expect(page.locator('.yarl__toolbar button[title="缩小"]')).toBeVisible()
  //    - 关闭按钮 + 上一张/下一张
  await expect(page.locator('.yarl__toolbar button[title="关闭大图预览"]')).toBeVisible()
  await expect(page.locator('.yarl__navigation_next')).toBeVisible()

  // 6) 下一张切换：counter 变「2 / 2」
  await page.locator('.yarl__navigation_next').click()
  await expect(page.locator('.yarl__counter')).toHaveText('2 / 2')

  // 7) Esc 关闭浮层（抽屉保持打开）
  await page.keyboard.press('Escape')
  await expect(portal).toHaveCount(0)
  await expect(page.locator('.pipeline-drawer')).toBeVisible()
})
