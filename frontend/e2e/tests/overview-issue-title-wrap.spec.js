// 概览页「开放 Issue」板块长标题多行显示 E2E（issue #476）
//
// 需求：issue 内容一行显示不下时改为多行显示，取消现在用省略号截断的
// 展示方式。本用例在真实浏览器验证：
// 1. .issue-link 计算样式不再 nowrap / text-overflow:ellipsis / overflow
//    hidden（属性移除生效）；
// 2. 超长标题实际换行为多行（元素高度 > 单行行高）；
// 3. 全文可见无横向溢出（scrollWidth 不超出 clientWidth）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 超长中文标题：约 180 字，远超桌面视口下单行可容纳长度，保证必然换行
const LONG_TITLE = '这是一个超长 issue 标题用于验证概览页开放 issue 组件在内容一行显示不下时应当自动换行多行显示而不是用省略号截断，' +
  '本行继续补充更多文字确保在任何桌面视口宽度下都无法在一行内展示完毕，这样断言换行行为才稳定可靠，' +
  '标题越长越能体现多行展示的差异效果，同时确认右侧的更新时间与评论数不会被遮挡或者压缩。'

function wrapFixture() {
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 476,
        title: LONG_TITLE,
        labels: [{ name: 'feature', color: '69D100', text_color: 'FFFFFF' }],
        milestone: null,
        updated_at: '2026-08-24 10:00:00',
        web_url: 'https://gitlab.example.com/botler/-/issues/476',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 12,
      }],
    }],
    errors: [],
    total: 1,
  }
}

test('开放 issue 长标题多行显示、取消省略号截断（issue #476）', async ({ page }) => {
  await mockGitLabApis(page, { issues: wrapFixture() })
  await page.goto('/overview')

  const item = page.locator('.issue-item').filter({ hasText: '#476' })
  await expect(item).toBeVisible()
  const link = item.locator('.issue-link')
  await expect(link).toBeVisible()

  const layout = await link.evaluate((el) => {
    const style = getComputedStyle(el)
    const rect = el.getBoundingClientRect()
    return {
      whiteSpace: style.whiteSpace,
      textOverflow: style.textOverflow,
      overflowX: style.overflowX,
      overflowY: style.overflowY,
      lineHeight: parseFloat(style.lineHeight) || 16,
      height: rect.height,
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
      text: el.textContent,
    }
  })

  // 1. 截断属性已移除（对应 CSS 改动生效）
  expect(layout.whiteSpace).not.toBe('nowrap')
  expect(layout.textOverflow).not.toBe('ellipsis')
  // overflow 各轴都不是 hidden（避免裁掉换行后的内容）
  expect(['hidden', 'clip']).not.toContain(layout.overflowX)
  expect(['hidden', 'clip']).not.toContain(layout.overflowY)

  // 2. 实际换行多行：元素高度超过单行行高（≥ 2 行）
  expect(
    layout.height,
    `标题应换行为多行（height=${layout.height}px，lineHeight=${layout.lineHeight}px）`,
  ).toBeGreaterThan(layout.lineHeight * 1.5)

  // 3. 全文可见、无横向溢出
  expect(layout.text.replace(/\s+/g, '')).toContain(LONG_TITLE.replace(/\s+/g, '').slice(0, 20))
  expect(
    layout.scrollWidth,
    `标题不应横向溢出（scrollWidth=${layout.scrollWidth}px，clientWidth=${layout.clientWidth}px）`,
  ).toBeLessThanOrEqual(layout.clientWidth + 1)
})
