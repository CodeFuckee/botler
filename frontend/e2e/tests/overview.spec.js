// 概览页 E2E（issue #212）：真实浏览器加载概览页，开放 issue 数据来自
// 浏览器级 mock（GET /api/issues/overview），任务/灵感/通知等其余接口
// 走真实后端（uvicorn）。断言：页面加载、仓库卡片、issue 展示与分组。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

test.describe('概览页加载与 issue 展示', () => {
  test('概览页渲染开放 issue 列表并按 bot 终态标签分组', async ({ page }) => {
    await mockGitLabApis(page)
    await page.goto('/overview')

    // 1. 页面板块标题
    await expect(
      page.getByRole('heading', { name: '开放 Issue' }),
    ).toBeVisible()

    // 2. 仓库卡片：botler（有 issue）+ shipyard（空）
    await expect(page.locator('.issue-repo-card')).toHaveCount(2)
    await expect(
      page.locator('.issue-repo-name', { hasText: 'botler' }),
    ).toBeVisible()
    await expect(
      page.locator('.issue-repo-name', { hasText: 'shipyard' }),
    ).toBeVisible()
    await expect(
      page.locator('.issue-repo-card', { hasText: '该仓库暂无开放 issue' }),
    ).toBeVisible()

    // 3. issue #212（test 标签 → 其他分组）展示
    const issueLink = page.locator('.issue-link', {
      hasText: '无端到端测试（Playwright），关键用户流程无浏览器级保障',
    })
    await expect(issueLink).toBeVisible()
    await expect(
      issueLink.locator('.issue-iid', { hasText: '#212' }),
    ).toBeVisible()

    // 4. bot-done 分组展示（fixture 中 iid 101 带 bot-done 标签）
    const doneGroup = page.locator('.issue-group', { hasText: 'bot-done' })
    await expect(doneGroup).toBeVisible()
    await expect(
      doneGroup.locator('.issue-link', { hasText: 'E2E 示例任务：修复概览页按钮样式' }),
    ).toBeVisible()
  })
})

// 轮询改 SSE 事件驱动 E2E（issue #478）：概览页不再有固定间隔轮询——任务
// 列表仅在挂载时拉取一次，此后由 /api/events SSE 长连接事件驱动刷新。
// 前台等待超过原 3s 轮询间隔、切后台标签页、切回前台，均不应产生新的
// 任务列表请求（无事件不拉取）；SSE 事件流连接应保持建立。
// 用 Object.defineProperty 覆写 document.visibilityState + 手动派发
// visibilitychange 模拟真实标签页切换（headless 无法真正切后台）。
test('概览页无固定轮询：仅挂载拉取一次，前台/后台均无轮询请求，SSE 连接保持（issue #478）', async ({ page }) => {
  let taskRequests = 0
  let sseRequests = 0
  // 统计概览任务列表请求（GET /api/tasks?...）；其余 /api/tasks*（如任务
  // 执行事件流）与 /api/events（全局事件流）放行到真实后端
  await page.route('**/api/tasks*', (route) => {
    const url = route.request().url()
    if (route.request().method() === 'GET' && url.includes('/api/tasks?')) {
      taskRequests += 1
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ tasks: [], total: 0, stats: { queued: 0, running: 0, retrying: 0 } }),
      })
      return
    }
    route.fallback()
  })
  // 统计全局事件流连接（EventSource GET /api/events），放行到真实后端
  await page.route('**/api/events', (route) => {
    sseRequests += 1
    route.fallback()
  })
  await mockGitLabApis(page)
  await page.goto('/overview')
  await expect(
    page.getByRole('heading', { name: '开放 Issue' }),
  ).toBeVisible()
  await page.waitForTimeout(400)
  const initial = taskRequests
  expect(initial).toBeGreaterThanOrEqual(1)

  // 前台等待超过原 3s 概览轮询间隔：无固定轮询 → 任务列表请求数不变
  await page.waitForTimeout(4000)
  expect(taskRequests).toBe(initial)

  // 切后台标签页：无轮询 → 0 请求
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(4000)
  expect(taskRequests).toBe(initial)

  // 切回前台：事件驱动（无事件不拉取，非定时轮询）→ 任务列表请求数不变
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(1000)
  expect(taskRequests).toBe(initial)

  // SSE 全局事件流连接已建立（事件驱动通道）
  expect(sseRequests).toBeGreaterThanOrEqual(1)
})
