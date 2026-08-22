// 概览页右侧边栏双滚动条修复的真实浏览器回归测试（issue #348）：
// 概览页打开 issue 详情右边栏或流水线右边栏时，页面上出现两个竖直
// 滚动条——主页面一个、右边栏一个；期望右边栏打开时只显示右边栏
// 自身的滚动条（主页面滚动条隐藏、主页面滚动被锁定）。
//
// 背景：issue #348 已在 styles.css 增加 body:has(.drawer-overlay) {
// overflow: hidden } 锁定主页面滚动；单元测试
// overview-drawer-scrollbar.test.mjs 只做 styles.css 源码级断言
// （文本含 overflow: hidden 即通过），无法发现「源码有规则、真实
// 浏览器不生效」的回归（如 :has 兼容性 / 级联被覆盖 / 滚动容器
// 变化导致锁定失效）；本用例在真实 Chromium 中打开/关闭两个右边栏，
// 断言 body computed overflow、滚轮只滚抽屉不动主页面、关闭后恢复，
// 直接验证用户可见行为（只显示右边栏一个滚动条）。
//
// 注意：headless Chromium 使用 overlay 滚动条（不占布局宽度），无法
// 用 clientWidth 差值检测滚动条可见性；改用确定性行为断言——
// overflow: hidden 是隐藏经典滚动条的机制，滚轮只滚动抽屉是用户
// 可见的「主页面被锁定」行为。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动的概览页 fixture（30 个 issue 撑高页面，保证主页面
// 本身可滚动——否则「打开抽屉后锁定滚动」无从验证；第一个 issue
// 带超长描述撑高抽屉内容，保证抽屉自身可滚动）
function tallOverviewFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证打开'
    + '右边栏时主页面滚动被锁定、只保留右边栏自身的滚动条。').repeat(40)
  const issues = Array.from({ length: 30 }, (_, i) => ({
    iid: 400 + i,
    project_id: 123,
    title: `issue-${i} 这是一个用于撑高概览页开放 issue 板块的标题`,
    description: i === 0 ? longDesc : '',
    labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
    milestone: null,
    updated_at: '2026-08-20 09:00:00',
    web_url: `https://gitlab.example.com/chenkaidi/botler/-/work_items/${400 + i}`,
    assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
    user_notes_count: 0,
    state: 'opened',
  }))
  return {
    repos: [{ repo_id: 1, repo_name: 'botler', priority: 10, issues }],
    errors: [],
    total: 1,
  }
}

// 构造带多个 job 的流水线 fixture（job 列表足够长保证抽屉可滚动，
// 与 issue-drawer-sticky.spec.js 的 tallPipelineFixture 同构）
function tallPipelineFixture() {
  const jobs = Array.from({ length: 40 }, (_, i) => ({
    id: 1000 + i,
    name: `job-${i} 这是一个很长的任务名称用于撑高流水线详情内容`,
    status: 'success',
    allow_failure: false,
    web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/1000',
    artifacts: [],
  }))
  return {
    pipelines: [{
      repo_id: 1,
      repo_name: 'botler',
      enabled: true,
      pipeline: {
        id: 348,
        status: 'success',
        ref: 'main',
        sha: 'abcdef1234567890',
        created_at: '2026-08-20 09:00:00',
        updated_at: '2026-08-20 09:05:00',
        finished_at: '2026-08-20 09:06:00',
        duration: 300,
        web_url: 'https://gitlab.example.com/chenkaidi/botler/-/pipelines/348',
      },
      stages: Array.from({ length: 8 }, (_, s) => ({
        name: `stage-${s}`,
        status: 'success',
        jobs: jobs.slice(s * 5, s * 5 + 5),
      })),
      commit_time: '2026-08-20 09:00:00',
    }],
    errors: [],
  }
}

// 断言抽屉打开时主页面滚动被锁定：
//   1) body computed overflow 为 hidden（经典滚动条隐藏的机制）；
//   2) 抽屉自身可滚动（右边栏保留滚动条）；
//   3) 滚轮落在抽屉上：只滚动抽屉内容，主页面 scrollTop 不变
//     （用户无法滚动主页面——「只显示右边栏滚动条」的用户可见行为）。
async function assertDrawerLocksPage(page, drawer) {
  const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow)
  expect(overflow, '抽屉打开时 body overflow 应为 hidden（主页滚动条隐藏）').toBe('hidden')

  const drawerScrollable = await drawer.evaluate(
    (el) => el.scrollHeight > el.clientHeight)
  expect(drawerScrollable, '抽屉自身应保留滚动（只显示右边栏滚动条）').toBe(true)

  const pageBefore = await page.evaluate(() => document.documentElement.scrollTop)
  await drawer.hover()
  await page.mouse.wheel(0, 800)
  await page.waitForTimeout(200)
  const pageAfter = await page.evaluate(() => document.documentElement.scrollTop)
  const drawerTop = await drawer.evaluate((el) => el.scrollTop)
  expect(pageAfter, '抽屉打开时滚轮不应滚动主页面').toBe(pageBefore)
  expect(drawerTop, '滚轮应滚动抽屉自身内容').toBeGreaterThan(0)
}

// 断言抽屉关闭后主页面滚动恢复（body overflow 非 hidden、滚轮可
// 滚动主页面）
async function assertPageScrollRestored(page) {
  const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow)
  expect(overflow, '关闭抽屉后 body overflow 应恢复（非 hidden）').not.toBe('hidden')

  // 先程序化滚动到页面中部再滚轮——若主页面恢复滚动，scrollTop 必变化
  // （否则已处于顶部/底部时滚轮无位移，断言失去意义）
  await page.evaluate(() => {
    const max = document.documentElement.scrollHeight - window.innerHeight
    window.scrollTo(0, Math.floor(max / 2))
  })
  const before = await page.evaluate(() => document.documentElement.scrollTop)
  await page.mouse.move(400, 400)
  await page.mouse.wheel(0, 300)
  await page.waitForTimeout(200)
  const after = await page.evaluate(() => document.documentElement.scrollTop)
  expect(after, '关闭抽屉后主页面应恢复滚轮滚动').not.toBe(before)
}

test('issue 详情右边栏打开时只显示右边栏滚动条，主页面滚动被锁定（issue #348）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallOverviewFixture() })
  // 详情接口（评论/活动）：返回空评论即可
  await page.route('**/api/issues/123/400/detail', (route) => {
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

  // 前置条件：概览页主页面本身可滚动（否则「锁定滚动」断言无意义）
  const scrollable = await page.evaluate(
    () => document.documentElement.scrollHeight > window.innerHeight)
  expect(scrollable, '前置条件：概览页主页面本身可滚动').toBe(true)

  // 打开 issue 详情右边栏
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()
  await assertDrawerLocksPage(page, drawer)

  // 关闭右边栏后主页面滚动恢复
  await drawer.locator('.issue-drawer-actions .modal-close').click()
  await expect(drawer).toBeHidden()
  await assertPageScrollRestored(page)
})

test('流水线详情右边栏打开时只显示右边栏滚动条，主页面滚动被锁定（issue #348）', async ({ page }) => {
  await mockGitLabApis(page, {
    issues: tallOverviewFixture(),
    pipelines: tallPipelineFixture(),
  })

  await page.goto('/overview')
  const pipelineLink = page.locator('.pipeline-link').first()
  await expect(pipelineLink).toBeVisible()

  const scrollable = await page.evaluate(
    () => document.documentElement.scrollHeight > window.innerHeight)
  expect(scrollable, '前置条件：概览页主页面本身可滚动').toBe(true)

  // 打开流水线详情右边栏
  await pipelineLink.click()
  const drawer = page.locator('.pipeline-drawer')
  await expect(drawer).toBeVisible()
  await assertDrawerLocksPage(page, drawer)

  // 关闭右边栏后主页面滚动恢复
  await drawer.locator('.issue-drawer-actions .modal-close').click()
  await expect(drawer).toBeHidden()
  await assertPageScrollRestored(page)
})
