// 概览页「其他」分组拖动手柄图标与 issue 标题垂直对齐——竖屏/触屏回归
// 测试（issue #350）
//
// 背景：issue #343 已修复桌面端（fine pointer）手柄图标与标题行不对齐
// 的问题（margin-top: calc((1.6em - 1em) / 2)）；但竖屏页面（触屏设备，
// pointer: coarse）下 `.issue-link` 命中触控目标规则 min-height: 44px，
// 按钮盒被撑高、内部文本垂直居中，标题文本中心随之下降到 22px 处，而
// 手柄图标仍停留在 11.2px 处——图标偏上。
//
// 本用例在竖屏触屏视口（390×844，hasTouch+isMobile→pointer: coarse）
// 测量手柄图标中心与标题行中心的垂直偏差，断言在 2px 容差内；同时保留
// 桌面端（fine pointer）用例防止回归（issue #343）。
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

// 测量手柄图标中心与标题行（.issue-link）中心的垂直偏差，返回测量详情
async function measureAlignDiff(page) {
  const firstItem = page.locator('.issue-item').first()
  const handle = firstItem.locator('.issue-drag-handle svg')
  await expect(handle).toBeVisible()
  return firstItem.evaluate((item) => {
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
      linkMinHeight: linkStyle.minHeight,
      coarse: matchMedia('(pointer: coarse)').matches,
    }
  })
}

test.describe('桌面视口（fine pointer）——issue #343 不回归', () => {
  test.use({ viewport: { width: 1280, height: 720 }, hasTouch: false, isMobile: false })

  test('拖动手柄图标与 issue 标题行垂直居中对齐', async ({ page }) => {
    await mockGitLabApis(page, { issues: dragIssuesFixture })
    await page.goto('/overview')
    const pos = await measureAlignDiff(page)
    const diff = Math.abs(pos.iconCenterY - pos.titleCenterY)
    expect(
      diff,
      `桌面下手柄图标中心(${pos.iconCenterY.toFixed(1)}px) 与标题行中心`
      + `(${pos.titleCenterY.toFixed(1)}px) 偏差 ${diff.toFixed(1)}px`
      + `（icon=${pos.iconHeight}px 行高=${pos.linkHeight}px 字号=${pos.linkFontSize}`
      + ` line-height=${pos.linkLineHeight} margin-top=${pos.handleMarginTop}）`,
    ).toBeLessThanOrEqual(2)
  })
})

test.describe('竖屏触屏视口（coarse pointer）——issue #350', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('拖动手柄图标与 issue 标题行垂直居中对齐', async ({ page }) => {
    await mockGitLabApis(page, { issues: dragIssuesFixture })
    await page.goto('/overview')
    const pos = await measureAlignDiff(page)
    const diff = Math.abs(pos.iconCenterY - pos.titleCenterY)
    expect(
      diff,
      `竖屏下手柄图标中心(${pos.iconCenterY.toFixed(1)}px) 与标题行中心`
      + `(${pos.titleCenterY.toFixed(1)}px) 偏差 ${diff.toFixed(1)}px`
      + `（icon=${pos.iconHeight}px 行高=${pos.linkHeight}px 字号=${pos.linkFontSize}`
      + ` line-height=${pos.linkLineHeight} min-height=${pos.linkMinHeight}`
      + ` margin-top=${pos.handleMarginTop} coarse=${pos.coarse}）`,
    ).toBeLessThanOrEqual(2)
  })
})
