// 概览页 issue 详情右边栏头部操作区固定在顶部（issue #332）：真实浏览器
// 验证「关闭 issue / 查看执行的详情 / 在 GitLab 中打开 / 关闭右边栏（×）」
// 四个按钮渲染于右边栏顶部头部操作区，且头部 sticky 固定——抽屉内容滚动
// 时头部不随之滚走（computed style 与滚动前后坐标双断言）。
//
// 背景：issue #331 已在 styles.css 给 .issue-drawer .modal-header 增加
// position: sticky（连同 .pipeline-drawer），本用例是 issue #332 的真实
// 浏览器回归测试——现有 overview-drawer-actions-sticky.test.mjs 只做
// styles.css 源码级断言（文本含 position: sticky 即通过），无法发现
// 「源码有规则、真实浏览器不生效」的回归（如滚动容器/祖先 overflow/
// 布局变更导致 sticky 失效）；本用例在真实 Chromium 中打开抽屉、滚动
// 内容，断言头部坐标不随内容滚动而滚动，直接验证用户可见行为。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动且四个按钮齐全的开放 issue（fixture 默认数据无 project_id，
// 「关闭 issue / 查看执行的详情」按钮会因缺 project_id 隐藏——必须显式
// 提供 project_id/iid 才能验证头部四按钮齐全；描述足够长保证抽屉内容
// 可滚动，否则「不随滚动」无从验证）
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证头部操作区'
    + '是否随内容滚动而滚动。').repeat(40)
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 332,
        project_id: 123,
        title: '概览页 issue 详情右边栏头部操作区固定在顶部',
        description: longDesc,
        state: 'opened',
        labels: [{ name: 'bug', color: 'FF0000', text_color: 'FFFFFF' }],
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

test('issue 详情右边栏头部操作区固定在顶部，不随内容滚动（issue #332）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  // 详情接口（评论/活动）：返回空评论，描述文本已足够撑高内容
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
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  const header = drawer.locator('.modal-header')

  // 1. 四个按钮全部渲染在头部操作区（.issue-drawer-actions 位于 .modal-header 内）
  const actions = drawer.locator('.issue-drawer-actions')
  for (const name of ['关闭 issue', '查看执行的详情']) {
    await expect(actions.getByRole('button', { name })).toBeVisible()
  }
  // 「在 GitLab 中打开」是链接（新窗口跳转），非 button
  await expect(actions.locator('a', { hasText: '在 GitLab 中打开' })).toBeVisible()
  // × 关闭右边栏按钮（aria-label=关闭）
  await expect(actions.getByRole('button', { name: '关闭右边栏' })).toBeVisible()

  // 2. 头部 computed style 为 sticky 且吸附顶部
  const pos = await header.evaluate((el) => getComputedStyle(el).position)
  expect(pos).toBe('sticky')

  // 3. 抽屉内容必须可滚动（scrollHeight > clientHeight），否则断言无意义
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 4. 滚动抽屉内容后，头部 top 坐标保持不动（不随内容滚走）
  const topBefore = await header.evaluate((el) => el.getBoundingClientRect().top)
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await drawer.evaluate((el) => ({
    scrollTop: el.scrollTop,
    headerTop: el.querySelector('.modal-header').getBoundingClientRect().top,
  }))
  expect(after.scrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.headerTop - topBefore)).toBeLessThan(2) // 头部固定未移动
})
