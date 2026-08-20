// 概览页评论图标与计数数字「同一行水平并排」E2E（issue #359）：
// 回归保护——每条开放 issue 行最右侧的评论气泡图标（Lucide message）
// 与评论计数数字必须始终保持同一行水平排列（图标在左、数字紧跟右侧），
// 无论评论数为 0、1 还是多位数，数字不得被挤到图标下方垂直堆叠。
//
// 历史 bug：概览页计数 span 与右边栏评论区块共用 `.issue-notes` 类，
// 右边栏区块的 `display:flex; flex-direction:column` 样式与概览页规则
// 同名合并，导致计数 span 变成纵向 flex 容器，图标与数字上下堆叠。
//
// 断言（真实浏览器计算几何）：
//   1. 图标与数字文本的垂直中心差 < 2px（同一行）；
//   2. 数字文本左缘 >= 图标右缘（数字在图标右侧，而非下方）；
//   3. 计数 span 的 flex 方向非纵向（column）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造含评论数 0 / 1 / 多位数（10）三种数值的开放 issue 载荷，
// 覆盖 issue #359 验收标准「无论评论数是 0、1、2 等任意数值」。
function notesFixture() {
  return {
    repos: [
      {
        repo_id: 1,
        repo_name: 'botler',
        priority: 10,
        issues: [
          {
            iid: 359,
            title: '评论数为 0 的 issue（布局回归样本）',
            labels: [],
            milestone: null,
            updated_at: '2026-08-20 10:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/359',
            assignees: [],
            user_notes_count: 0,
          },
          {
            iid: 360,
            title: '评论数为 1 的 issue',
            labels: [],
            milestone: null,
            updated_at: '2026-08-20 10:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/360',
            assignees: [],
            user_notes_count: 1,
          },
          {
            iid: 361,
            title: '评论数为 10 的 issue',
            labels: [],
            milestone: null,
            updated_at: '2026-08-20 10:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/361',
            assignees: [],
            user_notes_count: 10,
          },
        ],
      },
    ],
    errors: [],
    total: 3,
  }
}

// 读取某条 issue 行评论计数元素的布局几何：返回图标/数字文本的
// 垂直中心与水平位置（真实浏览器 getBoundingClientRect 计算）。
async function notesLayout(page, iid) {
  const row = page.locator('.issue-item').filter({ hasText: `#${iid}` })
  await expect(row).toBeVisible()
  // 计数 span：修复前为 .issue-notes，修复后为独立类 .issue-notes-count，
  // 选择器取并集保证回归测试不依赖具体类名
  const span = row.locator('.issue-notes, .issue-notes-count').first()
  await expect(span).toBeVisible()
  return span.evaluate((el) => {
    const svg = el.querySelector('svg.lucide')
    if (!svg) return { missingSvg: true }
    const textNode = Array.from(el.childNodes).find(
      (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim() !== '',
    )
    if (!textNode) return { missingText: true }
    const rectOf = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const range = document.createRange()
        range.selectNodeContents(node)
        return range.getBoundingClientRect()
      }
      return node.getBoundingClientRect()
    }
    const svgRect = rectOf(svg)
    const textRect = rectOf(textNode)
    return {
      display: getComputedStyle(el).display,
      flexDirection: getComputedStyle(el).flexDirection,
      svgCenterY: (svgRect.top + svgRect.bottom) / 2,
      textCenterY: (textRect.top + textRect.bottom) / 2,
      svgRight: svgRect.right,
      textLeft: textRect.left,
      svgTop: svgRect.top,
      textTop: textRect.top,
    }
  })
}

test.describe('概览页评论图标与数字同一行水平并排（issue #359）', () => {
  const cases = [
    { iid: 359, count: 0 },
    { iid: 360, count: 1 },
    { iid: 361, count: 10 },
  ]

  for (const { iid, count } of cases) {
    test(`评论数 ${count}：图标与数字同一行水平并排（图标左、数字右）`, async ({ page }) => {
      await mockGitLabApis(page, { issues: notesFixture() })
      await page.goto('/overview')

      const l = await notesLayout(page, iid)
      expect(l.missingSvg, '计数 span 内应渲染气泡图标').toBeUndefined()
      expect(l.missingText, '计数 span 内应渲染计数数字').toBeUndefined()
      expect(l.flexDirection, '计数 span 不得为纵向 flex（column 会导致数字垂直堆叠）')
        .not.toBe('column')

      // 同一行：图标与数字的垂直中心应基本重合（允许 2px 渲染误差）
      expect(
        Math.abs(l.svgCenterY - l.textCenterY),
        `数字应垂直居中于图标同一行（svgCenterY=${l.svgCenterY}, textCenterY=${l.textCenterY}）`,
      ).toBeLessThan(2)
      // 数字在图标右侧：数字左缘不早于图标右缘
      expect(
        l.textLeft,
        `数字应在图标右侧（svgRight=${l.svgRight}, textLeft=${l.textLeft}）`,
      ).toBeGreaterThanOrEqual(l.svgRight - 1)
    })
  }
})
