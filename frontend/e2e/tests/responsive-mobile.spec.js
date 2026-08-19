// 移动端响应式冒烟 E2E（issue #270）：375×667 手机竖屏视口（等效 Chrome
// DevTools 移动模拟）下验证三个核心页可完成主要操作——
//   概览页：卡片网格单列、无横向滚动溢出、issue 抽屉全宽；竖屏下
//     × 关闭按钮置于顶部并固定在顶部，关闭 issue / 查看执行的详情 /
//     在 GitLab 中打开 置于底部操作栏并固定在底部（issue #334）；
//   任务页：渲染卡片式列表（无 12 列表格）、卡片含关键操作；
//   设置页：两栏回落单栏（设置导航置顶）。
// 桌面端回归由其余 spec（Desktop Chrome 视口）覆盖。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

test.describe('移动端响应式（issue #270）', () => {
  test.use({ viewport: { width: 375, height: 667 } })

  test('概览页：卡片单列、无横向滚动溢出、抽屉全宽', async ({ page }) => {
    await mockGitLabApis(page)
    await page.goto('/overview')

    // 1. 开放 issue 板块标题与仓库卡片可见
    await expect(page.getByRole('heading', { name: '开放 Issue' })).toBeVisible()
    const cards = page.locator('.issue-repo-card')
    await expect(cards).toHaveCount(2)

    // 2. 卡片网格单列：两张卡片的水平起点一致（同列堆叠）
    const box1 = await cards.nth(0).boundingBox()
    const box2 = await cards.nth(1).boundingBox()
    expect(box1.x).toBeCloseTo(box2.x, 0)

    // 3. 无横向滚动溢出（页面根滚动宽度不超视口；表格场景除外——本页无表格）
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)

    // 4. 打开 issue 抽屉：全宽展示（不再 92vw 留缝）
    await page.locator('.issue-link').first().click()
    const drawer = page.locator('.drawer.issue-drawer')
    await expect(drawer).toBeVisible()
    const db = await drawer.boundingBox()
    expect(db.width).toBeGreaterThanOrEqual(374)
    // 等待抽屉滑入动画结束（drawer-in 240ms）再测量几何——
    // 动画期间 transform 未归位，boundingBox 会偏移视口
    await page.waitForFunction(() => {
      const d = document.querySelector('.drawer.issue-drawer')
      if (!d) return false
      const t = getComputedStyle(d).transform
      return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'
    })
    const settled = await drawer.boundingBox()
    // issue #326 回归：底部操作栏若作为 .drawer-overlay（flex 行布局）
    // 的兄弟节点，会与抽屉横向排布把抽屉挤出屏幕左侧（实测 375px 视口
    // 抽屉左移 131.5px）——抽屉左边界必须贴齐视口左缘（≥0），右边界
    // 不超出视口，主页面被遮罩完整覆盖
    expect(settled.x).toBeGreaterThanOrEqual(-0.5)
    expect(settled.x + settled.width).toBeLessThanOrEqual(375.5)
    // 底部操作栏必须是抽屉子项（不参与 overlay 横向 flex 排布）——该
    // 结构断言不受 issue #334 影响（竖屏下底部操作栏 display:flex 显示，
    // DOM 仍在抽屉内部）
    const baParentCls = await page.locator('.drawer-bottom-actions').evaluate(
      (el) => el.parentElement.className)
    expect(baParentCls).toContain('issue-drawer')
    expect(baParentCls).not.toContain('drawer-overlay')
    // issue #334：竖屏（375×667 portrait）下 × 关闭按钮放在右边栏顶部并
    // 固定在顶部；关闭 issue / 查看执行的详情 / 在 GitLab 中打开 放在
    // 右边栏底部并固定在底部——头部操作区恢复显示（覆盖 860px 断点
    // display:none）但仅保留 ×，底部操作栏恢复显示承载其余操作按钮
    await expect(page.locator('.drawer.issue-drawer .issue-drawer-actions')).toBeVisible()
    await expect(page.locator('.drawer.issue-drawer .issue-drawer-actions .modal-close')).toBeVisible()
    await expect(
      page.locator('.drawer.issue-drawer .issue-drawer-actions .btn:not(.modal-close)'),
    ).toBeHidden()
    // 底部操作栏可见（sticky 常驻底部），含「在 GitLab 中打开」入口
    // （fixture issue 带 web_url）
    await expect(page.locator('.drawer-bottom-actions')).toBeVisible()
    await expect(
      page.locator('.drawer-bottom-actions a', { hasText: '在 GitLab 中打开' }),
    ).toBeVisible()
    // 头部操作区提供 × 关闭入口——点击后抽屉关闭
    const mobileClose = page.locator('.drawer.issue-drawer .issue-drawer-actions .modal-close')
    await expect(mobileClose).toBeVisible()
    await mobileClose.click()
    await expect(drawer).not.toBeVisible()
  })

  test('任务页：窄视口渲染卡片式列表，无 12 列表格', async ({ page }) => {
    await page.goto('/tasks')
    await expect(page.locator('.tasks-card-list')).toBeVisible()
    // 种子数据：1 条成功任务 → 1 张卡片
    const cards = page.locator('.tasks-card')
    await expect(cards).toHaveCount(1)
    await expect(page.locator('.table.tasks-table')).toHaveCount(0)
    // 卡片含关键操作入口（执行/查看）
    await expect(page.locator('.tasks-card a', { hasText: '执行' })).toBeVisible()
    // 无横向滚动溢出
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })

  test('设置页：两栏回落单栏（导航置顶、页面正常滚动）', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.locator('.settings-layout')).toBeVisible()
    // issue #139 断点：窄视口下导航不再 sticky（页面整体滚动）
    const pos = await page.locator('.settings-sidebar').evaluate(
      (el) => getComputedStyle(el).position,
    )
    expect(pos).toBe('static')
    // 设置导航（搜索入口）可见可操作
    await expect(page.locator('.settings-nav')).toBeVisible()
    // 无横向滚动溢出
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })
})
