// 概览页开放 Issue 标题与右侧元信息垂直对齐回归（issue #434）。
//
// 复现：触屏设备中标题按钮扩展为 44px 触控目标，右侧的处理人头像、
// 更新时间、评论图标和数量却仍按紧凑行顶部排列，造成明显上下错位。
// 此用例以浏览器实际几何验证这些元素与标题行处于同一水平线，并同时
// 覆盖桌面端的既有紧凑布局不回归。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

const issueFixture = {
  repos: [{
    repo_id: 1,
    repo_name: 'botler',
    priority: 10,
    issues: [{
      iid: 434,
      title: '标题与右侧信息应处于同一水平线',
      labels: [{ name: 'bug', color: 'D73A4A', text_color: 'FFFFFF' }],
      milestone: null,
      updated_at: '2026-08-22 10:00:00',
      web_url: 'https://gitlab.example.com/botler/-/issues/434',
      assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
      user_notes_count: 12,
    }],
  }],
  errors: [],
  total: 1,
}

async function expectTitleAndSideAligned(page) {
  await mockGitLabApis(page, { issues: issueFixture })
  await page.goto('/overview')

  const item = page.locator('.issue-item').filter({ hasText: '#434' })
  await expect(item).toBeVisible()
  const layout = await item.evaluate((el) => {
    const centerY = (node) => {
      const box = node.getBoundingClientRect()
      return box.top + box.height / 2
    }
    const title = el.querySelector('.issue-link')
    const avatar = el.querySelector('.assignee-avatar')
    const updated = el.querySelector('.issue-updated')
    const notes = el.querySelector('.issue-notes-count')
    return {
      titleCenterY: centerY(title),
      avatarCenterY: centerY(avatar),
      updatedCenterY: centerY(updated),
      notesCenterY: centerY(notes),
    }
  })

  for (const [name, centerY] of Object.entries(layout).filter(([name]) => name !== 'titleCenterY')) {
    expect(
      Math.abs(centerY - layout.titleCenterY),
      `${name} 的中心应与标题行中心重合（标题=${layout.titleCenterY}px，${name}=${centerY}px）`,
    ).toBeLessThanOrEqual(1)
  }
}

test.describe('桌面紧凑行', () => {
  test.use({ viewport: { width: 1280, height: 720 }, hasTouch: false, isMobile: false })

  test('标题、处理人头像、更新时间和评论数位于同一水平线（issue #434）', async ({ page }) => {
    await expectTitleAndSideAligned(page)
  })
})

test.describe('竖屏触屏 44px 触控行', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('标题、处理人头像、更新时间和评论数位于同一水平线（issue #434）', async ({ page }) => {
    await expectTitleAndSideAligned(page)
  })
})
