// 概览页 issue 详情右边栏头部操作区固定在顶部（issue #332）：真实浏览器
// 验证「关闭 issue / 查看执行的详情 / 在 GitLab 中打开 / 关闭右边栏（×）」
// 四个按钮渲染于右边栏顶部头部操作区，且头部 sticky 固定——抽屉内容滚动
// 时头部不随之滚走（computed style 与滚动前后坐标双断言）。
//
// 背景：issue #331 已在 styles.css 给 .issue-drawer .modal-header 增加
// position: sticky（连同 .pipeline-drawer），本用例是 issue #332 的真实
// 浏览器回归测试——现有 overview-drawer-actions-sticky.test.mjs 只做
// styles.css 源码级断言（文本含 position: sticky 即通过），无法发现
// 「源码有规则、真实浏览器不生效」的回归（如滚动容器/祖先 overflow/
// 布局变更导致 sticky 失效）；本用例在真实 Chromium 中打开抽屉、滚动
// 内容，断言头部坐标不随内容滚动而滚动，直接验证用户可见行为。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动且四个按钮齐全的开放 issue（fixture 默认数据无 project_id，
// 「关闭 issue / 查看执行的详情」按钮会因缺 project_id 隐藏——必须显式
// 提供 project_id/iid 才能验证头部四按钮齐全；描述足够长保证抽屉内容
// 可滚动，否则「不随滚动」无从验证）
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证头部操作区'
    + '是否随内容滚动而滚动。').repeat(40)
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 332,
        project_id: 123,
        title: '概览页 issue 详情右边栏头部操作区固定在顶部',
        description: longDesc,
        state: 'opened',
        labels: [{ name: 'bug', color: 'FF0000', text_color: 'FFFFFF' }],
        milestone: null,
        created_at: '2026-08-19 19:45:32',
        updated_at: '2026-08-19 19:45:32',
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/work_items/332',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 0,
      }],
    }],
    errors: [],
    total: 1,
  }
}

test('issue 详情右边栏头部操作区固定在顶部，不随内容滚动（issue #332）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  // 详情接口（评论/活动）：返回空评论，描述文本已足够撑高内容
  await page.route('**/api/issues/123/332/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [],
        engine: 'claude',
        task_id: null,
        task_duration_seconds: null,
        task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  await expect(page.locator('.issue-link').first()).toBeVisible()
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  const header = drawer.locator('.modal-header')

  // 1. 四个按钮全部渲染在头部操作区（.issue-drawer-actions 位于 .modal-header 内）
  const actions = drawer.locator('.issue-drawer-actions')
  for (const name of ['关闭 issue', '查看执行的详情']) {
    await expect(actions.getByRole('button', { name })).toBeVisible()
  }
  // 「在 GitLab 中打开」是链接（新窗口跳转），非 button
  await expect(actions.locator('a', { hasText: '在 GitLab 中打开' })).toBeVisible()
  // × 关闭右边栏按钮（aria-label=关闭）
  await expect(actions.getByRole('button', { name: '关闭右边栏' })).toBeVisible()

  // 2. 头部 computed style 为 sticky 且吸附顶部
  const pos = await header.evaluate((el) => getComputedStyle(el).position)
  expect(pos).toBe('sticky')

  // 3. 抽屉内容必须可滚动（scrollHeight > clientHeight），否则断言无意义
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 4. 滚动抽屉内容后，头部 top 坐标保持不动（不随内容滚走）
  const topBefore = await header.evaluate((el) => el.getBoundingClientRect().top)
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await drawer.evaluate((el) => ({
    scrollTop: el.scrollTop,
    headerTop: el.querySelector('.modal-header').getBoundingClientRect().top,
  }))
  expect(after.scrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.headerTop - topBefore)).toBeLessThan(2) // 头部固定未移动
})

// 竖屏视口（issue #334）：在竖屏显示时，issue 详情右边栏的「关闭右边栏
// （×）」按钮放在右边栏顶部并固定在顶部；「关闭 issue / 查看执行的详情 /
// 在 GitLab 中打开」按钮放在右边栏底部并固定在底部——均不随右边栏内容
// 滚动而滚动。
//
// 背景：issue #270 移动端响应式把 ≤860px 视口下 issue 抽屉头部操作区
// （.issue-drawer-actions）整体 display:none、按钮下沉到抽屉底部 sticky
// 操作栏（.drawer-bottom-actions）；issue #333 竖屏下把四个按钮全部恢复
// 显示在顶部；issue #334 进一步拆分——× 关闭按钮保留在顶部 sticky 头部，
// 其余操作按钮置于底部 sticky 操作栏（thumb 可及）。本用例在真实
// Chromium 竖屏视口打开抽屉，断言 × 渲染于顶部头部操作区且可见、其余
// 按钮渲染于底部操作栏且可见、头部与底部均 sticky 固定（滚动后坐标不随
// 内容移动）、两处互不重复（修复前四个按钮全部在顶部、底部操作栏隐藏，
// 本用例必失败）。
test('竖屏视口：× 关闭按钮固定在顶部、其余操作按钮固定在底部（issue #334）', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  // 详情接口（评论/活动）：返回空评论，描述文本已足够撑高内容
  await page.route('**/api/issues/123/332/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [],
        engine: 'claude',
        task_id: null,
        task_duration_seconds: null,
        task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  await expect(page.locator('.issue-link').first()).toBeVisible()
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  const header = drawer.locator('.modal-header')
  const actions = drawer.locator('.issue-drawer-actions')
  const bottomActions = drawer.locator('.drawer-bottom-actions')

  // 1. 竖屏下头部操作区恢复显示，但只保留 × 关闭右边栏按钮（关闭 issue /
  //    查看执行的详情 / 在 GitLab 中打开 均不在头部，已移到底部操作栏）
  await expect(actions).toBeVisible()
  await expect(actions.getByRole('button', { name: '关闭右边栏' })).toBeVisible()
  await expect(actions.getByRole('button', { name: '关闭 issue' })).toBeHidden()
  await expect(actions.getByRole('button', { name: '查看执行的详情' })).toBeHidden()
  await expect(actions.locator('a', { hasText: '在 GitLab 中打开' })).toBeHidden()

  // 2. 底部操作栏恢复显示（sticky 常驻底部），承载其余操作按钮
  await expect(bottomActions).toBeVisible()
  for (const name of ['关闭 issue', '查看执行的详情']) {
    await expect(bottomActions.getByRole('button', { name })).toBeVisible()
  }
  await expect(bottomActions.locator('a', { hasText: '在 GitLab 中打开' })).toBeVisible()
  // 底部不再重复 × 关闭入口（× 已固定在顶部）
  await expect(bottomActions.getByRole('button', { name: '关闭右边栏' })).toBeHidden()

  // 3. 头部与底部 computed style 均 sticky（吸附顶/底）
  expect(await header.evaluate((el) => getComputedStyle(el).position)).toBe('sticky')
  expect(await bottomActions.evaluate((el) => getComputedStyle(el).position)).toBe('sticky')
  // issue #340：竖屏下底部操作栏按钮右对齐（justify-content: flex-end，
  // 修复前为 flex 默认左对齐）——与桌面端/横屏窄视口（保持左对齐）区分
  expect(await bottomActions.evaluate(
    (el) => getComputedStyle(el).justifyContent)).toBe('flex-end')

  // 4. 抽屉内容必须可滚动（scrollHeight > clientHeight），否则断言无意义
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 5. 滚动抽屉内容后，头部 top 坐标与底部 bottom 坐标均保持不动
  const topBefore = await header.evaluate((el) => el.getBoundingClientRect().top)
  const bottomBefore = await bottomActions.evaluate(
    (el) => el.getBoundingClientRect().bottom)
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await drawer.evaluate((el) => ({
    scrollTop: el.scrollTop,
    headerTop: el.querySelector('.modal-header').getBoundingClientRect().top,
    bottomBottom: el.querySelector('.drawer-bottom-actions').getBoundingClientRect().bottom,
  }))
  expect(after.scrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.headerTop - topBefore)).toBeLessThan(2) // 头部固定未移动
  expect(Math.abs(after.bottomBottom - bottomBefore)).toBeLessThan(2) // 底部固定未移动
})

// 顶部空隙回归测试（issue #335）：概览页 issue 详情右边栏与流水线详情
// 右边栏的 sticky 头部必须紧贴抽屉顶部（无空隙）——修复前 .modal-header
// 的负 margin（抵消 .drawer padding）在 position: sticky + flex 容器下
// 不生效，头部边框盒停在 content-box 顶（16px），抽屉顶部留下 16px 空隙；
// 滚动时内容文字从空隙中露出（elementFromPoint 顶部条带命中内容行），
// 体验差。本用例在真实 Chromium 中断言：
//   1) 静止时头部与抽屉顶部无空隙（headerTop - drawerTop < 1px）；
//   2) 滚动到底后头部仍紧贴顶部（gap 不随滚动变化）；
//   3) 滚动后抽屉顶部 8px 条带的 topmost 元素必须是头部（或其子元素），
//      不是滚动上来的内容行——即「文字从空隙露出」不再发生。
// 返回 drawer 内顶部条带（顶部 8px 处）的 topmost 元素信息
async function topStripInfo(page, drawerLocator) {
  return drawerLocator.evaluate((drawer) => {
    const dr = drawer.getBoundingClientRect()
    const x = dr.left + dr.width / 2
    const y = dr.top + 8
    const el = document.elementFromPoint(x, y)
    const header = drawer.querySelector('.modal-header')
    return {
      drawerTop: dr.top,
      headerTop: header.getBoundingClientRect().top,
      gap: Math.round((header.getBoundingClientRect().top - dr.top) * 100) / 100,
      inHeader: !!header && header.contains(el),
      elCls: el ? (el.className || el.tagName) : null,
    }
  })
}

// 断言头部紧贴抽屉顶部且顶部条带无内容露出
async function expectHeaderFlush(page, drawerLocator) {
  const info = await topStripInfo(page, drawerLocator)
  expect(info.gap, `头部与抽屉顶部应无空隙（gap=${info.gap}）`).toBeLessThan(1)
  expect(info.inHeader,
         `抽屉顶部条带应命中头部而非内容（命中：${info.elCls}）`).toBe(true)
}

// 构造可滚动且带多个 job 的流水线 fixture（job 列表足够长保证抽屉可滚动）
function tallPipelineFixture() {
  const jobs = Array.from({ length: 40 }, (_, i) => ({
    id: 1000 + i,
    name: `job-${i} 这是一个很长的任务名称用于撑高流水线详情内容`,
    status: 'success',
    allow_failure: false,
    web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/jobs/1000',
    artifacts: [],
  }))
  return {
    pipelines: [{
      repo_id: 1,
      repo_name: 'botler',
      enabled: true,
      pipeline: {
        id: 335,
        status: 'success',
        ref: 'main',
        sha: 'abcdef1234567890',
        created_at: '2026-08-19 20:00:00',
        updated_at: '2026-08-19 20:05:00',
        finished_at: '2026-08-19 20:06:00',
        duration: 300,
        web_url: 'https://home.chenkaidi.top:509/chenkaidi/botler/-/pipelines/335',
      },
      stages: Array.from({ length: 8 }, (_, s) => ({
        name: `stage-${s}`,
        status: 'success',
        jobs: jobs.slice(s * 5, s * 5 + 5),
      })),
      commit_time: '2026-08-19 20:00:00',
    }],
    errors: [],
  }
}

test('issue 详情右边栏头部紧贴抽屉顶部，滚动后顶部无内容露出（issue #335）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  await page.route('**/api/issues/123/332/detail', (route) => {
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
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()

  // 1. 静止时头部紧贴抽屉顶部（无空隙）
  await expectHeaderFlush(page, drawer)

  // 2. 抽屉内容可滚动（否则「滚动后」断言无意义）
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 3. 滚动到底后头部仍紧贴顶部，且顶部条带命中头部（内容不露出）
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const info = await topStripInfo(page, drawer)
  expect(info.gap, `滚动后头部与抽屉顶部应仍无空隙（gap=${info.gap}）`).toBeLessThan(1)
  expect(info.inHeader,
         `滚动后抽屉顶部条带应命中头部而非内容（命中：${info.elCls}）`).toBe(true)
})

test('流水线详情右边栏头部紧贴抽屉顶部，滚动后顶部无内容露出（issue #335）', async ({ page }) => {
  await mockGitLabApis(page, { pipelines: tallPipelineFixture() })

  await page.goto('/overview')
  const pipelineLink = page.locator('.pipeline-link').first()
  await expect(pipelineLink).toBeVisible()
  await pipelineLink.click()
  const drawer = page.locator('.pipeline-drawer')
  await expect(drawer).toBeVisible()

  // 1. 静止时头部紧贴抽屉顶部（无空隙）
  await expectHeaderFlush(page, drawer)

  // 2. 抽屉内容可滚动（否则「滚动后」断言无意义）
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 3. 滚动到底后头部仍紧贴顶部，且顶部条带命中头部（内容不露出）
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const info = await topStripInfo(page, drawer)
  expect(info.gap, `滚动后头部与抽屉顶部应仍无空隙（gap=${info.gap}）`).toBeLessThan(1)
  expect(info.inHeader,
         `滚动后抽屉顶部条带应命中头部而非内容（命中：${info.elCls}）`).toBe(true)
})

// 竖屏标题与 × 关闭按钮同行 + 省略号（issue #341）：竖屏（375×667）下
// issue 详情右边栏头部原先 flex-wrap: wrap——标题过长时 × 关闭按钮被挤到
// 第二行；本用例在真实 Chromium 竖屏视口用超长标题断言：
//   1) 头部 flex-wrap computed style 为 nowrap（标题与按钮不换行）；
//   2) 标题与 × 关闭按钮同一行（bounding top 相等）；
//   3) 标题溢出省略（scrollWidth > clientWidth + text-overflow: ellipsis
//      + white-space: nowrap + overflow: hidden）——标题尽可能显示、
//      其余以省略号截断；
//   4) × 关闭按钮完整可见、未被压缩出视口。
// 修复前竖屏头部 flex-wrap 为 wrap（标题与按钮分行），本用例必失败。
test('竖屏视口：超长标题与 × 关闭按钮同行且省略号截断（issue #341）', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  const longTitle = ('这是一个超长的 issue 标题，用于验证竖屏下标题与关闭按钮'
    + '同一行时超出部分以省略号截断并尽可能完整显示，避免按钮被挤到第二行').repeat(6)
  const fixture = tallIssueFixture()
  fixture.repos[0].issues[0].title = longTitle
  await mockGitLabApis(page, { issues: fixture })
  // 详情接口（评论/活动）：返回空评论即可
  await page.route('**/api/issues/123/332/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [], engine: 'claude', task_id: null,
        task_duration_seconds: null, task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  // 等 drawer-in 滑入动画（240ms）完成后再断言几何位置，避免中间帧偏移
  await page.waitForTimeout(600)
  const header = drawer.locator('.modal-header')

  // 1. 头部禁止换行（标题与按钮同一行）
  expect(await header.evaluate((el) => getComputedStyle(el).flexWrap)).toBe('nowrap')

  // 2. 标题与 × 关闭按钮同行 + 标题省略号 + 按钮完整可见
  const layout = await drawer.evaluate((el) => {
    const title = el.querySelector('.issue-drawer-title')
    const close = el.querySelector('.issue-drawer-actions .modal-close')
    const tr = title.getBoundingClientRect()
    const cr = close.getBoundingClientRect()
    const cs = getComputedStyle(title)
    return {
      sameLine: Math.abs(tr.top - cr.top) < 5,
      titleOverflow: title.scrollWidth > title.clientWidth,
      textOverflow: cs.textOverflow,
      whiteSpace: cs.whiteSpace,
      overflowX: cs.overflowX,
      closeWidth: cr.width,
      closeInViewport: cr.right <= window.innerWidth && cr.left >= 0,
    }
  })
  expect(layout.sameLine,
         '标题与 × 关闭按钮应处于同一行（垂直居中偏移 <5px，换行时差 >38px）').toBe(true)
  expect(layout.titleOverflow, '超长标题应发生溢出（scrollWidth > clientWidth）').toBe(true)
  expect(layout.textOverflow).toBe('ellipsis')
  expect(layout.whiteSpace).toBe('nowrap')
  expect(layout.overflowX).toBe('hidden')
  expect(layout.closeWidth, '× 关闭按钮不应被压缩').toBeGreaterThan(0)
  expect(layout.closeInViewport, '× 关闭按钮应完整落在视口内').toBe(true)
})

// 边界（issue #341）：竖屏下短标题不截断——标题完整显示、与 × 关闭按钮
// 同行、按钮完整可见（省略号只在标题放不下时触发）
test('竖屏视口：短标题完整显示、不省略（issue #341 边界）', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 })
  const fixture = tallIssueFixture()
  fixture.repos[0].issues[0].title = '短标题测试'
  await mockGitLabApis(page, { issues: fixture })
  await page.route('**/api/issues/123/332/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [], engine: 'claude', task_id: null,
        task_duration_seconds: null, task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  // 等 drawer-in 滑入动画（240ms）完成后再断言几何位置
  await page.waitForTimeout(600)
  const header = drawer.locator('.modal-header')

  expect(await header.evaluate((el) => getComputedStyle(el).flexWrap)).toBe('nowrap')
  const info = await drawer.evaluate((el) => {
    const title = el.querySelector('.issue-drawer-title')
    const close = el.querySelector('.issue-drawer-actions .modal-close')
    const tr = title.getBoundingClientRect()
    const cr = close.getBoundingClientRect()
    return {
      sameLine: Math.abs(tr.top - cr.top) < 5,
      noOverflow: title.scrollWidth <= title.clientWidth,
      closeVisible: cr.width > 0 && cr.height > 0,
    }
  })
  expect(info.sameLine,
         '标题与 × 关闭按钮应处于同一行（垂直居中偏移 <5px，换行时差 >38px）').toBe(true)
  expect(info.noOverflow, '短标题不应被截断（无省略号）').toBe(true)
  expect(info.closeVisible, '× 关闭按钮应完整可见').toBe(true)
})
