// 右侧边栏拖拽手柄不随抽屉内容滚动（issue #475）：
// 「右边栏左右拖动的拖动条，在页面滚动的时候，也会随着页面滚动，
//  导致在页面下方拖动的时候，只有一半的拖动条」——真实浏览器回归测试。
//
// 背景：issue #466 起右侧边栏（issue 详情 / 流水线 / 灵感对话 / 任务执行
// 详情）在视口 >860px 时渲染 .drawer-resize-handle 拖拽手柄（绝对定位于
// 抽屉左缘）。当抽屉内容可滚动（overflow-y: auto）时，若手柄是滚动容器
// .drawer 的后代，会随内容一起滚走——页面下方只剩一半手柄，拖拽调整宽度
// 的手感与可见性都受影响。
//
// 本用例在真实 Chromium 中打开内容足够高的 issue 详情右边栏，滚动抽屉
// 内容，断言手柄 top 坐标不随内容滚动而滚动（与 issue-drawer-sticky
// 验证 sticky 头部的思路一致）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动且视口 >860px 的开放 issue：描述足够长保证抽屉内容可滚动
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证拖拽手柄'
    + '是否随内容滚动而滚动。').repeat(40)
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 475,
        project_id: 123,
        title: '右侧边栏拖拽手柄不随内容滚动',
        description: longDesc,
        state: 'opened',
        labels: [{ name: 'bug', color: 'FF0000', color_text: 'FFFFFF' }],
        milestone: null,
        created_at: '2026-08-24 17:23:56',
        updated_at: '2026-08-24 17:23:56',
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/work_items/475',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 0,
      }],
    }],
    errors: [],
    total: 1,
  }
}

test('issue 详情右边栏拖拽手柄不随抽屉内容滚动（issue #475）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  await page.route('**/api/issues/123/475/detail', (route) => {
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
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()

  // 宽视口（默认 1280px > 860px）应渲染拖拽手柄
  const handle = page.locator('.drawer-resize-handle')
  await expect(handle).toBeVisible()

  // 抽屉内容必须可滚动（scrollHeight > clientHeight），否则断言无意义
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 记录滚动前手柄位置（从页面级查询，不依赖手柄在抽屉 DOM 内/外）
  const before = await handle.boundingBox()
  expect(before).not.toBeNull()

  // 滚动抽屉内容到底部
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)

  const afterScrollTop = await drawer.evaluate((el) => el.scrollTop)
  expect(afterScrollTop).toBeGreaterThan(100) // 确实发生了滚动

  const afterBox = await handle.boundingBox()
  expect(afterBox).not.toBeNull()
  // 手柄 top 坐标不应随内容滚动而滚动（位移 < 2px）
  expect(Math.abs(afterBox.y - before.y)).toBeLessThan(2)
  // 手柄应整高可见（高度接近抽屉可视高度，未被内容卷走只剩一半）
  expect(afterBox.height).toBeGreaterThan(400)
})
