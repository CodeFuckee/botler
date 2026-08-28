// 概览页右侧边栏「回到顶部」按钮的真实浏览器回归测试（issue #457 修复）：
// 上一轮实现用 position: absolute 定位抽屉内按钮——absolute 在滚动容器
// 内按「内容坐标系」锚定（bottom 相对容器 content-box 底缘），滚动后按钮
// 随内容滚出可视区；而按钮显示条件要求 scrollTop > 400（约两屏），
// 「显示条件」与「可见位置」互斥，实测按钮永不显示（人工反馈概览页
// issue 详情 / 流水线详情右边栏缺失）。
// 修复：改为 position: sticky + align-self: flex-end——按钮保持 flex 项
// 身份，sticky bottom 相对滚动容器 scrollport 定位，任何滚动位置都钉在
// 可视区右下角。
// 本用例在真实 Chromium 中验证用户可见行为：
//   1) 抽屉顶部时按钮隐藏（不打扰）；
//   2) 滚动抽屉超过阈值后按钮出现，且钉在抽屉可视区右下角
//      （boundingBox 稳定在可视区底部/右缘附近，不随内容滚出可视区）；
//   3) 继续滚动（接近底部）按钮仍在同一可视位置（sticky 钉住）；
//   4) 点击按钮 → 抽屉滚动回顶部 → 按钮隐藏。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动抽屉 fixture：超长描述撑高 issue 详情抽屉内容（保证
// 抽屉 scrollHeight > 可视高 + 400px 阈值），与 drawer-scrollbar-lock
// 同构
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，'
    + '验证抽屉内回到顶部按钮在滚动后钉在可视区右下角。').repeat(60)
  const issues = Array.from({ length: 8 }, (_, i) => ({
    iid: 500 + i,
    project_id: 123,
    title: `issue-${i} 撑高抽屉内容的标题`,
    description: i === 0 ? longDesc : '',
    labels: [{ name: 'feature', color: '428BCA', text_color: 'FFFFFF' }],
    milestone: null,
    updated_at: '2026-08-23 10:00:00',
    web_url: `https://gitlab.example.com/chenkaidi/botler/-/work_items/${500 + i}`,
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

// 构造多 job 流水线 fixture 撑高流水线详情抽屉
function tallPipelineFixture() {
  const jobs = Array.from({ length: 40 }, (_, i) => ({
    id: 2000 + i,
    name: `job-${i} 很长的任务名称用于撑高流水线详情内容`,
    status: 'success',
    allow_failure: false,
    web_url: 'https://gitlab.example.com/chenkaidi/botler/-/jobs/2000',
    artifacts: [],
  }))
  return {
    pipelines: [{
      repo_id: 1,
      repo_name: 'botler',
      enabled: true,
      pipeline: {
        id: 457,
        status: 'success',
        ref: 'main',
        sha: 'abcdef1234567890',
        created_at: '2026-08-23 10:00:00',
        updated_at: '2026-08-23 10:05:00',
        finished_at: '2026-08-23 10:06:00',
        duration: 300,
        web_url: 'https://gitlab.example.com/chenkaidi/botler/-/pipelines/457',
      },
      stages: Array.from({ length: 8 }, (_, s) => ({
        name: `stage-${s}`,
        status: 'success',
        jobs: jobs.slice(s * 5, s * 5 + 5),
      })),
      commit_time: '2026-08-23 10:00:00',
    }],
    errors: [],
  }
}

// 读取抽屉与按钮的可视区坐标（按钮钉住断言基准）。
// 注意：不能依赖 locator.selector（部分 Playwright 版本返回 undefined），
// 显式传选择器字符串。
async function readPinnedState(page, drawerSel, btnSel) {
  return page.evaluate(({ drawerSel, btnSel }) => {
    const drawer = document.querySelector(drawerSel)
    const btn = document.querySelector(btnSel)
    if (!drawer || !btn) {
      return { missing: true, drawer: !!drawer, btn: !!btn }
    }
    const dr = drawer.getBoundingClientRect()
    const b = btn.getBoundingClientRect()
    return {
      scrollTop: drawer.scrollTop,
      scrollHeight: drawer.scrollHeight,
      clientHeight: drawer.clientHeight,
      // 按钮是否在抽屉可视区内（sticky 钉住的核心断言）
      visible: b.top < dr.bottom && b.bottom > dr.top,
      // 按钮底缘距抽屉可视区底缘的距离（钉在底部：小且稳定）
      bottomGap: Math.round(dr.bottom - b.bottom),
      // 按钮右缘距抽屉可视区右缘的距离
      rightGap: Math.round(dr.right - b.right),
    }
  }, { drawerSel, btnSel })
}

// 通用断言流程：打开抽屉 → 顶部无按钮 → 滚动后按钮钉在右下角 →
// 继续滚动位置不变 → 点击回顶 → 按钮消失
async function assertDrawerBackToTop(page, drawerSel, openLocator, closeLocator) {
  const drawer = page.locator(drawerSel)
  const btn = page.locator(drawerSel + ' .back-to-top.in-drawer')

  await openLocator.click()
  await expect(drawer).toBeVisible()

  // 前置条件：抽屉自身可竖向滚动且内容超阈值（滚动才能触发按钮）
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable, '前置条件：抽屉内容应可竖向滚动').toBe(true)

  // 1) 顶部：按钮不显示（不打扰）
  await expect(btn).toHaveCount(0)

  // 2) 滚动超过阈值（400px）→ 按钮出现且钉在可视区右下角
  await drawer.evaluate((el) => { el.scrollTop = 800 })
  await expect(btn).toBeVisible()
  let state = await readPinnedState(page, drawerSel, drawerSel + ' .back-to-top.in-drawer')
  expect(state.visible, '滚动后按钮应在抽屉可视区内').toBe(true)
  expect(state.bottomGap, '按钮应钉在可视区底部附近（<120px）').toBeLessThan(120)
  expect(state.rightGap, '按钮应靠右缘（<40px）').toBeLessThan(40)
  const pinnedBottomGap = state.bottomGap

  // 3) 继续滚动到接近底部：按钮仍在同一可视位置（sticky 钉住，
  //    不随内容滚出可视区）
  await drawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(50)
  state = await readPinnedState(page, drawerSel, drawerSel + ' .back-to-top.in-drawer')
  expect(state.visible, '接近底部时按钮仍应在抽屉可视区内').toBe(true)
  expect(Math.abs(state.bottomGap - pinnedBottomGap),
    '滚动前后按钮底缘距可视区底缘的距离应稳定（sticky 钉住）').toBeLessThanOrEqual(2)

  // 4) 点击按钮 → 抽屉滚动回顶部 → 按钮隐藏
  await btn.click()
  await expect
    .poll(async () => drawer.evaluate((el) => el.scrollTop), { timeout: 5000 })
    .toBeLessThanOrEqual(1)
  await expect(btn).toHaveCount(0)

  // 关闭抽屉
  await drawer.locator(closeLocator).click()
  await expect(drawer).toBeHidden()
}

test('issue 详情右边栏回到顶部按钮：滚动后钉在可视区右下角，点击回顶（issue #457 修复）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  await page.route('**/api/issues/123/500/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [], engine: 'claude', task_id: null,
        task_duration_seconds: null, task_status: null,
      }),
    })
  })

  await page.goto('/overview')
  const issueLink = page.locator('.issue-link').first()
  await expect(issueLink).toBeVisible()
  await assertDrawerBackToTop(
    page,
    '.issue-drawer',
    issueLink,
    '.issue-drawer-actions .modal-close',
  )
})

test('流水线详情右边栏回到顶部按钮：滚动后钉在可视区右下角，点击回顶（issue #457 修复）', async ({ page }) => {
  await mockGitLabApis(page, {
    issues: tallIssueFixture(),
    pipelines: tallPipelineFixture(),
  })

  await page.goto('/overview')
  const pipelineLink = page.locator('.pipeline-link').first()
  await expect(pipelineLink).toBeVisible()
  await assertDrawerBackToTop(
    page,
    '.pipeline-drawer',
    pipelineLink,
    '.issue-drawer-actions .modal-close',
  )
})
