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
