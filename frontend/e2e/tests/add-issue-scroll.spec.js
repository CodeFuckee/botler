// 概览页「添加 Issue」弹窗滚动布局 E2E（issue #458）：真实浏览器验证
// 弹窗内容超高（超过 .modal max-height 80vh）时——头部标题固定在顶部、
// 取消/创建 Issue 按钮固定在底部、中间表单区（标题/描述/图片附件/分配人/
// 标签）独立上下滚动。
//
// 背景：单元测试 overview-add-issue-scroll.test.mjs 只做 styles.css
// 源码级（overflow-y: auto / min-height: 0）+ 组件结构级断言，无法发现
// 「源码有规则、真实浏览器不生效」的回归（如 flex 布局/祖先 overflow/
// 滚动容器变化导致不滚动、footer 被滚走）；本用例在真实 Chromium 中
// 打开弹窗、用大量标签撑高内容、滚动中间表单区，断言滚动前后头部/尾部
// 坐标不随内容移动、滚动区真实发生滚动、底部按钮始终在视口内，直接
// 验证用户可见行为。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动的添加 Issue 弹窗 fixture：80 个长名称标签撑高中间表单区，
// 使 .add-issue-body 内容高度超过弹窗可视高（否则「可滚动/按钮固定」
// 无从验证）
function tallFormMeta() {
  const labels = Array.from({ length: 80 }, (_, i) => ({
    name: `label-${String(i).padStart(2, '0')} 这是一个用于撑高添加 Issue 弹窗内容的标签名称`,
    color: '428BCA',
    text_color: 'FFFFFF',
  }))
  return {
    members: [
      { id: 20, username: 'agent', name: 'Agent' },
      { id: 21, username: 'dev', name: 'Dev' },
    ],
    labels,
  }
}

test('添加 Issue 弹窗：中间表单区可滚动，取消/创建按钮固定底部（issue #458）', async ({ page }) => {
  // 700px 视口 → .modal max-height 80vh = 560px；80 个标签内容远超此值
  await page.setViewportSize({ width: 1280, height: 700 })
  await mockGitLabApis(page, { formMeta: tallFormMeta() })

  await page.goto('/overview')
  await page.locator('.add-issue-btn').first().click()
  const modal = page.locator('.modal.add-issue')
  await expect(modal).toBeVisible()
  const body = modal.locator('.add-issue-body')
  await expect(body).toBeVisible()

  // 1. 结构：头部 / 尾部（取消/创建按钮）在滚动区外，是弹窗直接子节点
  expect(await modal.locator('.add-issue-body .modal-header').count()).toBe(0)
  expect(await modal.locator('.add-issue-body .modal-footer').count()).toBe(0)

  // 2. 中间表单区必须真实可滚动（scrollHeight > clientHeight），
  //    否则「头部/尾部固定」断言无意义
  const before = await modal.evaluate((el) => {
    const b = el.querySelector('.add-issue-body')
    return {
      headerTop: el.querySelector('.modal-header').getBoundingClientRect().top,
      footerTop: el.querySelector('.modal-footer').getBoundingClientRect().top,
      bodyScrollHeight: b.scrollHeight,
      bodyClientHeight: b.clientHeight,
      bodyScrollTop: b.scrollTop,
    }
  })
  expect(before.bodyScrollHeight).toBeGreaterThan(before.bodyClientHeight)

  // 3. 滚动中间表单区到底后：滚动确实发生、头部/尾部坐标不随内容移动
  await body.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await modal.evaluate((el) => {
    const h = el.querySelector('.modal-header').getBoundingClientRect()
    const f = el.querySelector('.modal-footer').getBoundingClientRect()
    return {
      headerTop: h.top,
      footerTop: f.top,
      bodyScrollTop: el.querySelector('.add-issue-body').scrollTop,
      footerVisible: f.bottom <= window.innerHeight && f.top >= 0,
      headerVisible: h.bottom > 0 && h.top >= 0,
    }
  })
  expect(after.bodyScrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.headerTop - before.headerTop)).toBeLessThan(2)
  expect(Math.abs(after.footerTop - before.footerTop)).toBeLessThan(2)
  expect(after.footerVisible).toBe(true) // 取消/创建 Issue 按钮仍在视口内
  expect(after.headerVisible).toBe(true)
})
