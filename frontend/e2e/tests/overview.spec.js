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

// 页面可见性暂停轮询 E2E（issue #200）：概览页 3s 任务轮询在切后台标签页
// 后暂停（0 请求），恢复可见立即拉一次再恢复轮询——对应验收标准「切后台
// 标签页后 0 请求（DevTools Network 验证）/ 切回立即刷新一次并恢复轮询」。
// 用 Object.defineProperty 覆写 document.visibilityState + 手动派发
// visibilitychange 模拟真实标签页切换（headless 无法真正切后台）。
test('页面切后台暂停轮询（0 请求），切回立即刷新并恢复轮询（issue #200）', async ({ page }) => {
  let taskRequests = 0
  // 统计概览任务轮询（GET /api/tasks?...）；其余 /api/tasks*（如 SSE 事件流）
  // 放行到真实后端
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
  await mockGitLabApis(page)
  await page.goto('/overview')
  await expect(
    page.getByRole('heading', { name: '开放 Issue' }),
  ).toBeVisible()
  await page.waitForTimeout(400)
  const beforeHide = taskRequests
  expect(beforeHide).toBeGreaterThanOrEqual(1)

  // 切后台标签页：visibilitychange 后全部轮询暂停 → 0 请求
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(4000) // 超过 3s 概览轮询间隔：未暂停则必有新请求
  expect(taskRequests).toBe(beforeHide)

  // 切回前台：立即拉一次，再恢复轮询
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(600)
  expect(taskRequests).toBe(beforeHide + 1)
})
