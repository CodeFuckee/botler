// 移动端响应式冒烟 E2E（issue #270）：375×667 手机竖屏视口（等效 Chrome
// DevTools 移动模拟）下验证三个核心页可完成主要操作——
//   概览页：卡片网格单列、无横向滚动溢出、issue 抽屉全宽；竖屏下
//     × 关闭按钮置于顶部并固定在顶部，关闭 issue / 查看执行的详情 /
//     在 GitLab 中打开 置于底部操作栏并固定在底部（issue #334）；
//   任务页：渲染卡片式列表（无 12 列表格）、卡片含关键操作；
//   设置页：手机竖屏保持单栏（导航置顶）；平板/窄窗口竖屏（641~860px）
//     保持左右布局——设置左侧导航栏（sticky 紧凑 200px）在左、设置面板在
//     右，类似手机/平板设置页面主从式（issue #339）。
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

  test('竖屏侧边栏抽屉：折叠偏好下搜索入口仍为全宽搜索框（issue #346）', async ({ page }) => {
    // 桌面端折叠过侧边栏（localStorage 持久化 botler.navCollapsed）的用户在
    // 竖屏打开抽屉时，搜索入口不应再命中折叠态 36px 图标竖条（被挤成竖线、
    // 两侧留白），而应与导航项同宽全宽显示、文字可见（issue #346）
    await mockGitLabApis(page)
    await page.goto('/overview')
    await page.evaluate(() => localStorage.setItem('botler.navCollapsed', '1'))
    await page.reload()

    // 打开左侧抽屉（顶栏汉堡按钮，仅 ≤860px 可见）
    const menu = page.locator('.topbar-menu')
    await expect(menu).toBeVisible()
    await menu.click()
    const drawer = page.locator('.sidebar.open')
    await expect(drawer).toBeVisible()
    // 等待抽屉滑入动画（transform 归位）再测量几何
    await page.waitForFunction(() => {
      const s = document.querySelector('.sidebar.open')
      if (!s) return false
      const t = getComputedStyle(s).transform
      return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'
    })

    // 搜索入口：全宽（≥ 抽屉宽 70%，不再 36px 竖条）+ 文字可见
    const searchBtn = drawer.locator('.sidebar-search')
    await expect(searchBtn).toBeVisible()
    await expect(searchBtn.locator('.nav-label')).toBeVisible()
    const db = await drawer.boundingBox()
    const sb = await searchBtn.boundingBox()
    expect(sb.width).toBeGreaterThan(db.width * 0.7)
    // 搜索按钮左右两侧不再留大块空白：两侧留白合计 < 抽屉宽 30%
    expect(db.width - sb.width).toBeLessThan(db.width * 0.3)
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

  test('设置页：手机竖屏保持单栏（导航置顶、页面正常滚动）', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.locator('.settings-layout')).toBeVisible()
    // issue #339：≤640px 手机竖屏内容列装不下两栏（AI 供应商表格
    // min-content≈335px），保持 issue #139 单栏——导航不再 sticky（页面整体滚动）
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

// 平板/窄窗口竖屏（issue #339）：641~860px 竖屏下设置页保持左右布局——设置
// 左侧导航栏（sticky 紧凑 200px）在左、设置面板在右，类似手机/平板设置页面
// 「左侧列表 + 右侧详情」的主从式感觉；≤640px 手机竖屏仍为单栏（上方用例）
test.describe('设置页竖屏左右布局（issue #339）', () => {
  test.use({ viewport: { width: 768, height: 1024 } })

  test('平板竖屏：导航在左、面板在右，无横向滚动', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.locator('.settings-layout')).toBeVisible()
    // 左侧导航栏 sticky 吸顶（覆盖 issue #139 860px 断点的 static），
    // 宽度收窄为紧凑 200px（手机设置列表密度）
    const pos = await page.locator('.settings-sidebar').evaluate(
      (el) => getComputedStyle(el).position,
    )
    expect(pos).toBe('sticky')
    const sb = await page.locator('.settings-sidebar').boundingBox()
    expect(sb.width).toBeGreaterThanOrEqual(190)
    expect(sb.width).toBeLessThanOrEqual(210)
    // 左右并排：侧栏整列位于面板左侧（右边界不越过面板左边界）
    const cb = await page.locator('.settings-content').boundingBox()
    expect(sb.x).toBeLessThan(cb.x)
    expect(sb.x + sb.width).toBeLessThanOrEqual(cb.x + 1)
    // 设置导航（搜索入口）可见可操作
    await expect(page.locator('.settings-nav')).toBeVisible()
    // 无横向滚动溢出（641px 起内容列 ≥425px，AI 供应商表格等最宽元素装得下）
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })
})
