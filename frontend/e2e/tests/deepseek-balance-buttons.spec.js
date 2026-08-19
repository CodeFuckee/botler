// 概览页 DeepSeek 余额卡片按钮文字对齐 E2E（issue #336）：
// 竖屏界面下，「刷新」按钮（<button>）与「去充值/充值」链接按钮（<a>）
// 的文字不在同一垂直高度。
// 根因：两按钮共享 .btn / .btn-small 样式，但 .btn 未设置 font-family /
// line-height——<button> 使用浏览器 UA 默认字体（不继承 body 字体栈）且
// line-height: normal，而 <a> 继承 body 的系统字体栈与 line-height: 1.6，
// 字体度量/行高不同导致按钮内文字垂直位置不一致；竖屏（触屏）下
// min-height:44px 放大该差异。
// 复现方式：375×667 竖屏视口 mock /api/settings/deepseek-balance 渲染余额
// 卡片，用 Range API 测量两按钮内文本节点实际渲染矩形，断言文字中心
// 在同一垂直高度（差值 ≤ 1px）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 余额卡片数据源 mock（与 backend/botler/api/settings.py 返回结构一致）
const BALANCE_FIXTURE = {
  configured: true,
  balance: {
    is_available: true,
    balance_infos: [
      {
        currency: 'CNY',
        total_balance: '110.00',
        granted_balance: '10.00',
        topped_up_balance: '100.00',
      },
    ],
    fetched_at: '2026-08-20 00:00:00',
  },
  error: null,
}

// 取元素内第一个非空文本节点（按钮文字；lucide svg 无文本节点）的实际
// 渲染矩形，返回文字中心 y 坐标
async function firstTextCenterY(locator) {
  return locator.evaluate((el) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      if (node.textContent.trim()) break
    }
    if (!node) return null
    const range = document.createRange()
    range.selectNodeContents(node)
    const rect = range.getBoundingClientRect()
    return { y: rect.y + rect.height / 2, text: node.textContent.trim() }
  })
}

test.describe('概览页 DeepSeek 余额卡片按钮（issue #336）', () => {
  test.use({ viewport: { width: 375, height: 667 } })

  test('竖屏下「刷新」与「充值」按钮文字在同一垂直高度，文案为「充值」', async ({ page }) => {
    await mockGitLabApis(page)
    // 余额接口浏览器级 mock（e2e 后端未配置 deepseek key，默认 configured=false
    // 不渲染卡片；mock 后卡片展示）
    await page.route('**/api/settings/deepseek-balance', async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(BALANCE_FIXTURE),
      })
    })
    await page.goto('/overview')

    // 余额卡片渲染（标题 + 余额条目）
    await expect(page.locator('.deepseek-balance-section')).toBeVisible()
    await expect(page.locator('.deepseek-balance-currency')).toHaveText('CNY')

    // 两个操作按钮：刷新（<button>）与充值（<a> 链接）
    const refreshBtn = page.locator('.deepseek-balance-section button.btn')
    const topupBtn = page.locator('.deepseek-balance-section a.btn')
    await expect(refreshBtn).toBeVisible()
    await expect(topupBtn).toBeVisible()

    // 文案：修复同时把「去充值」改为「充值」
    await expect(topupBtn).toHaveText(/充值/)

    // 竖屏下两按钮应并排（同一水平行，不换行），否则无从对比文字高度
    const refreshBox = await refreshBtn.boundingBox()
    const topupBox = await topupBtn.boundingBox()
    expect(Math.abs(refreshBox.y - topupBox.y)).toBeLessThan(30)

    // 核心断言：两按钮内文字中心在同一垂直高度（差值 ≤ 1px）
    const refreshText = await firstTextCenterY(refreshBtn)
    const topupText = await firstTextCenterY(topupBtn)
    expect(refreshText).not.toBeNull()
    expect(topupText).not.toBeNull()
    expect(Math.abs(refreshText.y - topupText.y)).toBeLessThanOrEqual(1)
  })
})
