// 概览页「其他」分组拖动手柄图标与 issue 标题垂直对齐 E2E（issue #343）
//
// 背景：开放 issue 列表「其他」分组支持拖动排序（issue #287），手柄为装饰性
// 图标（gripVertical，li 整体可拖）。修复前手柄图标与标题行不在同一高度、
// 图标偏上——本用例测量手柄图标中心与标题行（.issue-link 单行盒）中心的
// 垂直偏差，断言在 2px 容差内，作为回归防护。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造触发拖动排序的开放 issue 数据：单仓库、repo 带 project_id、
// 「其他」分组 ≥2 条（无 bot-done/bot-failed 终态标签、无运行中任务）
function dragIssuesFixture() {
  return {
    repos: [
      {
        repo_id: 1,
        project_id: 1,
        repo_name: 'botler',
        priority: 10,
        issues: [
          {
            iid: 101,
            title: '拖动图标对齐测试 issue A',
            labels: [{ name: 'ui', color: '69D100', text_color: 'FFFFFF' }],
            milestone: null,
            updated_at: '2026-08-19 10:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/101',
            assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
            user_notes_count: 0,
          },
          {
            iid: 102,
            title: '拖动图标对齐测试 issue B',
            labels: [],
            milestone: null,
            updated_at: '2026-08-19 09:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/102',
            assignees: [],
            user_notes_count: 0,
          },
        ],
      },
    ],
    errors: [],
    total: 2,
  }
}

test('「其他」分组拖动手柄图标与 issue 标题行垂直居中对齐（issue #343）', async ({ page }) => {
  await mockGitLabApis(page, { issues: dragIssuesFixture })
  await page.goto('/overview')

  const firstItem = page.locator('.issue-item').first()
  const handle = firstItem.locator('.issue-drag-handle svg')
  await expect(handle).toBeVisible()

  const pos = await firstItem.evaluate((item) => {
    const handleSvg = item.querySelector('.issue-drag-handle svg')
    const link = item.querySelector('.issue-link')
    const handleBox = handleSvg.getBoundingClientRect()
    const linkBox = link.getBoundingClientRect()
    const handleStyle = getComputedStyle(item.querySelector('.issue-drag-handle'))
    const linkStyle = getComputedStyle(link)
    return {
      iconCenterY: handleBox.top + handleBox.height / 2,
      titleCenterY: linkBox.top + linkBox.height / 2,
      iconHeight: handleBox.height,
      linkHeight: linkBox.height,
      handleMarginTop: handleStyle.marginTop,
      linkLineHeight: linkStyle.lineHeight,
      linkFontSize: linkStyle.fontSize,
    }
  })

  const diff = Math.abs(pos.iconCenterY - pos.titleCenterY)
  // 容差 2px：手柄图标应与标题行同一高度（图标偏上即失败）
  expect(
    diff,
    `手柄图标中心(${pos.iconCenterY.toFixed(1)}px) 与标题行中心`
    + `(${pos.titleCenterY.toFixed(1)}px) 偏差 ${diff.toFixed(1)}px`
    + `（icon=${pos.iconHeight}px 行高=${pos.linkHeight}px 字号=${pos.linkFontSize}`
    + ` line-height=${pos.linkLineHeight} margin-top=${pos.handleMarginTop}）`,
  ).toBeLessThanOrEqual(2)
})
