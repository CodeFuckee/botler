// 添加 Issue 弹窗 E2E（issue #212）：概览页点「添加 Issue」打开弹窗，
// 填写标题 + 勾选标签（分配人默认 agent），提交后创建成功、弹窗关闭、
// 列表刷新展示新 issue。表单元数据与创建接口均为浏览器级 mock。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'
import { ISSUES_FIXTURE } from '../fixtures/api-fixtures.js'

test.describe('添加 Issue 弹窗提交', () => {
  test('填写标题与标签提交后创建 issue 并刷新列表', async ({ page }) => {
    // 可变负载：提交成功后往列表追加新 issue，验证刷新展示
    let issuesPayload = ISSUES_FIXTURE()
    const { createdIssues } = await mockGitLabApis(page, {
      issues: () => issuesPayload,
      onCreate: (body) => {
        issuesPayload = {
          ...issuesPayload,
          repos: issuesPayload.repos.map((r) => (
            r.repo_id === body.repo_id
              ? {
                  ...r,
                  issues: r.issues.concat({
                    iid: 999,
                    title: body.title,
                    labels: (body.labels || []).map((n) => ({
                      name: n, color: 'AAAAAA', text_color: 'FFFFFF',
                    })),
                    updated_at: '2026-08-18 00:00:00',
                    web_url: 'https://gitlab.example.com/botler/-/issues/999',
                    assignees: [],
                    user_notes_count: 0,
                  }),
                }
              : r
          )),
          total: issuesPayload.total + 1,
        }
        return {
          iid: 999,
          title: body.title,
          web_url: 'https://gitlab.example.com/botler/-/issues/999',
        }
      },
    })

    await page.goto('/overview')

    // 1. 打开第一个仓库卡片的「添加 Issue」弹窗
    await page.locator('.add-issue-btn').first().click()
    const modal = page.locator('.modal.add-issue')
    await expect(modal).toBeVisible()

    // 2. 标题必填；分配人默认 agent（fixture 成员含 agent）
    await modal.locator('.add-issue-title').fill('E2E 冒烟新增的测试 issue')

    // 3. 勾选标签 bug（必填至少一个）
    await modal
      .locator('.label-choice', { hasText: 'bug' })
      .locator('input[type=checkbox]')
      .check()

    // 4. 提交 → 弹窗关闭
    await modal.locator('.add-issue-submit').click()
    await expect(modal).toBeHidden()

    // 5. 请求体断言（创建参数正确）
    expect(createdIssues).toHaveLength(1)
    expect(createdIssues[0]).toMatchObject({
      repo_id: 1,
      title: 'E2E 冒烟新增的测试 issue',
      assignee_id: 20, // 默认选中 agent
      labels: ['bug'],
    })

    // 6. 列表刷新展示新 issue
    await expect(
      page.locator('.issue-link', { hasText: 'E2E 冒烟新增的测试 issue' }),
    ).toBeVisible()
  })

  test('标题为空时拦截提交并提示', async ({ page }) => {
    await mockGitLabApis(page)
    await page.goto('/overview')
    await page.locator('.add-issue-btn').first().click()
    const modal = page.locator('.modal.add-issue')
    await expect(modal).toBeVisible()

    // 不填标题直接提交 → 校验错误，不发请求
    await modal.locator('.add-issue-submit').click()
    await expect(modal.locator('.alert-error')).toContainText('标题不能为空')
    await expect(modal).toBeVisible() // 弹窗保持打开
  })
})
