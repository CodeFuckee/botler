// 概览页 issue 详情右边栏底部操作按钮被遮挡/裁切回归测试（issue #482）：
// 平板竖屏（≤860px）下，底部操作栏（.drawer-bottom-actions）的按钮
// 底部被裁剪、紧贴抽屉/屏幕底缘（padding-bottom 被 flex 压缩消失），
// 真实设备上叠加浏览器底部工具条/Home 指示条后表现为「底部按钮显示被
// 遮挡」。
//
// 根因：.drawer 是高度固定（height:100%）的 flex 列滚动容器；底部操作
// 栏自身 overflow-x: auto 使其成为滚动容器，flex 布局中滚动容器的自动
// 最小尺寸为 0（min-height:auto → 0），内容超高时负自由空间压缩所有可
// 收缩 flex 项，操作栏被压到约 29px（自然高度约 68px）——按钮溢出栏底
// 2px 被 .drawer overflow 裁剪，padding-bottom（16px）被压缩消失。
//
// 本用例在真实 Chromium 平板竖屏视口打开抽屉，断言：
//   1) 底部操作栏高度 ≥ 按钮高度 + 上下 padding（未被 flex 压缩）；
//   2) 按钮底缘不超出抽屉/视口底缘（未被裁剪、未贴边）；
//   3) 滚动抽屉内容后操作栏仍固定在底部（sticky 不随内容滚动，
//      修复后行为保持不变）。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 撑高内容的 fixture：描述足够长保证抽屉内容可滚动（触发 flex 压缩）
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证底部操作区'
    + '是否被遮挡/随内容滚动。').repeat(60)
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 482,
        project_id: 123,
        title: '平板竖屏 issue 详情右边栏底部按钮遮挡复现',
        description: longDesc,
        state: 'opened',
        labels: [{ name: 'bug', color: 'FF0000', text_color: 'FFFFFF' }],
        milestone: null,
        created_at: '2026-08-19 19:45:32',
        updated_at: '2026-08-19 19:45:32',
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/work_items/482',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 0,
      }],
    }],
    errors: [],
    total: 1,
  }
}

// 打开抽屉并等滑入动画结束（外壳 transform 归位，几何才稳定）
async function openIssueDrawer(page) {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  // 详情接口（评论/活动）：返回空评论，描述文本已足够撑高内容
  await page.route('**/api/issues/123/482/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [], engine: 'claude', task_id: null,
        task_duration_seconds: null, task_status: null,
      }),
    })
  })
  await page.goto('/overview')
  await expect(page.locator('.issue-link').first()).toBeVisible()
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.drawer.issue-drawer')
  await expect(drawer).toBeVisible()
  await page.waitForFunction(() => {
    const d = document.querySelector('.drawer.issue-drawer')
    if (!d) return false
    const shell = d.parentElement
    if (!shell || !shell.classList.contains('drawer-shell')) return false
    const t = getComputedStyle(shell).transform
    return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'
  })
  return drawer
}

// 平板竖屏视口（768×1024，iPad 10.2" 竖屏）——底部操作栏被 flex 压缩、
// 按钮贴底裁切的最小复现场景（375px 手机竖屏同样复现）
for (const [w, h] of [[768, 1024], [375, 667]]) {
  test(`竖屏 ${w}×${h}：底部操作栏不被压缩、按钮不被裁剪（issue #482）`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h })
    const drawer = await openIssueDrawer(page)
    const bottomActions = drawer.locator('.drawer-bottom-actions')
    await expect(bottomActions).toBeVisible()

    // 抽屉内容必须可滚动（触发 flex 负自由空间压缩）
    const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
    expect(scrollable).toBe(true)

    const info = await bottomActions.evaluate((ba) => {
      const bar = ba.getBoundingClientRect()
      const btns = [...ba.querySelectorAll('.btn')].filter(
        (b) => b.getBoundingClientRect().height > 0)
      const cs = getComputedStyle(ba)
      const padTop = parseFloat(cs.paddingTop)
      const padBottom = parseFloat(cs.paddingBottom)
      const btnBottoms = btns.map((b) => b.getBoundingClientRect().bottom)
      return {
        barHeight: bar.height,
        // 自然高度 = 最高按钮 + 上下 padding（含 border-top 1px）
        naturalHeight: Math.max(...btns.map((b) => b.getBoundingClientRect().height))
          + padTop + padBottom + 1,
        barBottom: bar.bottom,
        minBtnBottom: Math.min(...btnBottoms),
        maxBtnBottom: Math.max(...btnBottoms),
        padTop, padBottom,
        viewportH: window.innerHeight,
        drawerClientH: ba.closest('.drawer').clientHeight,
      }
    })

    // 1. 操作栏高度 ≥ 自然高度（未被 flex 压缩；修复前 29px < 68px 必失败）
    expect(info.barHeight, `操作栏高度 ${info.barHeight} 不应小于自然高度 ${info.naturalHeight}`)
      .toBeGreaterThanOrEqual(info.naturalHeight - 1)
    // 2. 按钮底缘不超出抽屉底缘（不被裁剪、不贴边；修复前按钮底 669 >
    //    视口底 667，超出 2px 被 overflow 裁剪）
    expect(info.maxBtnBottom, `按钮底缘 ${info.maxBtnBottom} 不应超出视口底缘 ${info.viewportH}`)
      .toBeLessThanOrEqual(info.viewportH - 1)
    // 3. 按钮底缘距操作栏底缘保留 padding-bottom 间距（不贴边，修复前
    //    padding-bottom 被压缩消失、按钮紧贴屏幕底缘）
    expect(info.barBottom - info.maxBtnBottom,
      `操作栏底 padding 应保留（barBottom=${info.barBottom}, 按钮底=${info.maxBtnBottom}）`)
      .toBeGreaterThanOrEqual(12)
  })
}

// 滚动后底部操作栏仍固定在底部（sticky 常驻，不随内容滚动）——
// 修复加入 flex-shrink: 0 后该行为保持不变
test('竖屏 768×1024：滚动后底部操作栏固定在底部不随内容滚动（issue #482）', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 })
  const drawer = await openIssueDrawer(page)
  const bottomActions = drawer.locator('.drawer-bottom-actions')
  await expect(bottomActions).toBeVisible()

  const bottomBefore = await bottomActions.evaluate(
    (el) => el.getBoundingClientRect().bottom)
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await drawer.evaluate((el) => ({
    scrollTop: el.scrollTop,
    barBottom: el.querySelector('.drawer-bottom-actions').getBoundingClientRect().bottom,
  }))
  expect(after.scrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.barBottom - bottomBefore)).toBeLessThan(2) // 底部固定未移动
})
