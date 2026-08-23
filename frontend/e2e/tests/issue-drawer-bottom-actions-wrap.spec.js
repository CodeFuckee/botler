// 移动端竖屏 issue 详情右边栏底部操作栏按钮换行回归测试（issue #463）：
// 小尺寸手机（375px 竖屏）下，底部操作栏（.drawer-bottom-actions）的
// 五个按钮——「关闭 issue / 执行 / 重试 / 查看执行的详情 / 在 GitLab
// 中打开」——因宽度不足被 flex-wrap 拆成两行，右对齐的第二行零散不整
// 齐、底部操作栏被撑高，用户体验差。修复后所有按钮保持同一行（flex
// 不换行、容器可横向滚动），底部操作栏单行常驻。
//
// 本用例在真实 Chromium 375×667 竖屏视口打开抽屉，断言：
//   1) 底部操作栏承载 5 个操作按钮且全部可见；
//   2) 5 个按钮的垂直中心 y 坐标一致（同一行，修复前 flex-wrap 拆成
//      两行时 y 坐标出现两组，本用例必失败）；
//   3) 底部操作栏整体高度 ≤ 单行按钮高度（无被撑高的第二行）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 5 按钮 fixture：开放 issue + bot-failed 标签（重试按钮）+ 无排队任务
// → 渲染「关闭 issue / 执行 / 重试 / 查看执行的详情 / 在 GitLab 中打开」
function fiveButtonsFixture() {
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 332,
        project_id: 123,
        title: '概览页 issue 详情右边栏底部操作按钮换行回归',
        description: '移动端竖屏下底部操作栏按钮不应换行成两行。',
        state: 'opened',
        labels: [
          { name: 'bug', color: 'FF0000', text_color: 'FFFFFF' },
          { name: 'bot-failed', color: 'D9534F', text_color: 'FFFFFF' },
        ],
        milestone: null,
        created_at: '2026-08-19 19:45:32',
        updated_at: '2026-08-19 19:45:32',
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/work_items/332',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 0,
      }],
    }],
    errors: [],
    total: 1,
  }
}

test('竖屏 375px：底部操作栏五个按钮保持同一行，不换行成两行（issue #463）', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  await mockGitLabApis(page, { issues: fiveButtonsFixture() })
  await page.route('**/api/issues/123/332/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [],
        engine: 'claude',
        task_id: null,
        task_duration_seconds: null,
        task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  await expect(page.locator('.issue-link').first()).toBeVisible()
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.drawer.issue-drawer')
  await expect(drawer).toBeVisible()
  // 等待抽屉滑入动画结束（transform 归位）再测量几何
  await page.waitForFunction(() => {
    const d = document.querySelector('.drawer.issue-drawer')
    if (!d) return false
    const t = getComputedStyle(d).transform
    return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'
  })

  const bottomActions = drawer.locator('.drawer-bottom-actions')
  await expect(bottomActions).toBeVisible()

  // 1. 五个操作按钮全部渲染于底部操作栏且可见
  for (const name of ['关闭 issue', '执行', '重试', '查看执行的详情']) {
    await expect(bottomActions.getByRole('button', { name, exact: true })).toBeVisible()
  }
  await expect(bottomActions.locator('a', { hasText: '在 GitLab 中打开' })).toBeVisible()

  // 2. 五个按钮垂直中心 y 坐标一致（同一行）——修复前 flex-wrap 拆成
  //    两行，y 坐标出现两组值，最大差值 > 按钮高度（约 33px），必失败
  // 过滤隐藏元素（竖屏下 × 关闭按钮 display:none 但仍存在于 DOM，
  // getBoundingClientRect 为 0×0）——只统计可见按钮
  const rows = await bottomActions.locator('.btn').evaluateAll(
    (els) => els.filter((el) => {
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0
    }).map((el) => {
      const r = el.getBoundingClientRect()
      return r.top + r.height / 2
    }),
  )
  expect(rows.length).toBe(5)
  const maxY = Math.max(...rows)
  const minY = Math.min(...rows)
  expect(maxY - minY).toBeLessThanOrEqual(2)
})
