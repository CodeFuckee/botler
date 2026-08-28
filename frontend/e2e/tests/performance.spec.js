// 前端页面性能测试（issue #452：增加前端页面和后端接口性能测试）。
//
// 覆盖用户报告的两个慢场景（前端视角）：
//   1. 添加 issue 对话框（AddIssueModal）——打开时加载成员+标签
//      （GET /api/issues/form-meta/{repo_id}），上游慢时曾需十几秒；
//   2. issue 详情右边栏（IssueDrawer）——打开时加载评论与活动
//      （GET /api/issues/{project_id}/{iid}/detail），同样曾需十几秒。
//
// 测试方式：浏览器级 mock 给关键接口注入固定延迟（模拟 GitLab 上游慢
// 响应），测量「用户操作 → 内容可交互/渲染完成」的总耗时，断言在预算
// 内。预算取值考虑 CI 机器负载波动（宽松阈值只拦截「卡死/数秒级退化」，
// 不拦截毫秒级抖动）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'
import { FORM_META_FIXTURE } from '../fixtures/api-fixtures.js'

// 上游延迟注入：模拟慢 GitLab（网络波动/限流重试场景）
const UPSTREAM_DELAY_MS = 400

// 性能预算（宽松）：从点击到内容就绪的总耗时上限。
// 注入 400ms 上游延迟 + 真实浏览器渲染/网络开销，正常应在 2s 内完成；
// 若前端出现同步阻塞/无限重试/接口退化到数十秒，则超预算失败。
const BUDGET_MS = 5000

// 带 project_id/iid 的概览 fixture（issue #94：project_id 由后端聚合
// 注入，缺它时抽屉不会拉 detail——必须显式提供才能测详情加载性能）
function overviewWithDetail() {
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 452,
        project_id: 123,
        title: '增加前端页面和后端接口性能测试',
        description: '性能测试 issue 描述',
        state: 'opened',
        labels: [{ name: 'bug', color: 'FF0000', text_color: 'FFFFFF' }],
        milestone: null,
        created_at: '2026-08-19 19:45:32',
        updated_at: '2026-08-19 19:45:32',
        web_url: 'https://gitlab.example.com/chenkaidi/botler/-/work_items/452',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 2,
      }],
    }],
    errors: [],
    total: 1,
  }
}

test.describe('前端页面性能（issue #452）', () => {
  test('添加 Issue 对话框：成员与标签在预算内加载完成', async ({ page }) => {
    // form-meta 注入延迟，模拟上游慢响应；其余走默认 mock
    await mockGitLabApis(page, { issues: overviewWithDetail() })
    await page.route('**/api/issues/form-meta/*', async (route) => {
      await new Promise((r) => setTimeout(r, UPSTREAM_DELAY_MS))
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(FORM_META_FIXTURE),
      })
    })

    await page.goto('/overview')
    await expect(page.locator('.add-issue-btn').first()).toBeVisible()

    // 计时：点击「添加 Issue」→ 表单可交互（分配人下拉有选项、标签渲染）
    const t0 = Date.now()
    await page.locator('.add-issue-btn').first().click()
    const modal = page.locator('.modal.add-issue')
    await expect(modal).toBeVisible()
    // 加载完成后表单出现：分配人下拉已渲染成员选项（fixture 含 agent，
    // 默认选中 agent → select value 应为 20）；标签胶囊已渲染
    await expect(modal.locator('.add-issue-assignee')).toHaveValue('20')
    await expect(modal.locator('.label-pill').first()).toBeVisible()
    const elapsed = Date.now() - t0

    expect(elapsed, `添加 Issue 对话框加载耗时 ${elapsed}ms 超过预算 ${BUDGET_MS}ms`)
      .toBeLessThan(BUDGET_MS)
  })

  test('issue 详情右边栏：内容、评论与活动在预算内渲染完成', async ({ page }) => {
    await mockGitLabApis(page, { issues: overviewWithDetail() })
    // detail 接口注入延迟（issue 内容/评论/活动同一请求返回）
    await page.route('**/api/issues/123/452/detail', async (route) => {
      await new Promise((r) => setTimeout(r, UPSTREAM_DELAY_MS))
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          notes: [
            { id: 1, body: '性能测试评论', system: false,
              author: { name: 'agent', username: 'agent', avatar_url: '' },
              created_at: '2026-08-15 02:00:00' },
            { id: 2, body: '系统活动', system: true,
              author: null, created_at: '2026-08-15 02:00:00' },
          ],
          label_events: [],
          engine: 'claude',
          task_id: null,
          task_duration_seconds: null,
          task_status: null,
        }),
      })
    })

    await page.goto('/overview')
    await expect(page.locator('.issue-link').first()).toBeVisible()

    // 计时：点击 issue → 评论/活动渲染完成
    const t0 = Date.now()
    await page.locator('.issue-link').first().click()
    const drawer = page.locator('.issue-drawer')
    await expect(drawer).toBeVisible()
    // 评论区块渲染（性能测试评论）与活动区块渲染（系统活动）
    await expect(
      drawer.locator('.comment-item', { hasText: '性能测试评论' }),
    ).toBeVisible()
    await expect(
      drawer.locator('.activity-item', { hasText: '系统活动' }),
    ).toBeVisible()
    const elapsed = Date.now() - t0

    expect(elapsed, `issue 详情右边栏加载耗时 ${elapsed}ms 超过预算 ${BUDGET_MS}ms`)
      .toBeLessThan(BUDGET_MS)
  })
})
