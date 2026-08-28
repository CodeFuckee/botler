// 任务执行详情第二层右边栏头部固定在顶部（issue #477）：真实浏览器验证
// 「任务执行详情 — #iid 标题」标题与 × 关闭按钮渲染于右边栏顶部，且头部
// sticky 固定——抽屉内容滚动时头部不随之滚走（computed style 与滚动前后
// 坐标双断言）。
//
// 背景：issue #331 已在 styles.css 给 .issue-drawer .modal-header 与
// .pipeline-drawer .modal-header 增加 position: sticky，但当时有意限定
// 两个抽屉、不波及第二层任务执行详情抽屉（.task-detail-drawer）；issue
// #477 要求任务执行详情右边栏的顶部标题和关闭按钮同样固定在顶部、不随
// 页面滚动而滚动，类似 issue 详情右边栏。本用例在真实 Chromium 中打开
// 任务执行详情第二层抽屉（概览页 issue 抽屉 →「查看执行的详情」），滚动
// 内容，断言头部坐标不随内容滚动而滚动、且紧贴抽屉顶部无空隙。
import { test, expect } from '@playwright/test'
import { mockGitLabApis } from '../support/mock-api.js'

// 构造可滚动且 project_id/iid 齐全的开放 issue（「查看执行的详情」按钮
// 依赖 project_id/iid，fixture 默认数据无 project_id 会隐藏该按钮——
// 必须显式提供；描述足够长保证 issue 抽屉内容可滚动）
function tallIssueFixture() {
  const longDesc = ('这是一段用于撑高 issue 详情右边栏内容的描述文本，验证'
    + '任务执行详情第二层右边栏头部是否随内容滚动而滚动。').repeat(40)
  return {
    repos: [{
      repo_id: 1,
      repo_name: 'botler',
      priority: 10,
      issues: [{
        iid: 477,
        project_id: 123,
        title: '任务执行详情右边栏的顶部标题和关闭按钮固定在顶部',
        description: longDesc,
        state: 'opened',
        labels: [{ name: 'feature', color: '00AAFF', text_color: 'FFFFFF' }],
        milestone: null,
        created_at: '2026-08-24 20:50:58',
        updated_at: '2026-08-24 20:50:58',
        web_url: 'https://gitlab.example.com/chenkaidi/botler/-/work_items/477',
        assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
        user_notes_count: 0,
      }],
    }],
    errors: [],
    total: 1,
  }
}

// 任务执行详情接口数据：一条 succeeded 任务 + 大量日志行/长文件尾部，
// 保证第二层抽屉内容远超一屏（否则「不随滚动」无从验证）
function tallTaskDetailFixture() {
  const logs = Array.from({ length: 80 }, (_, i) => ({
    id: i + 1,
    level: 'info',
    ts: '2026-08-25 10:00:00',
    message: `第 ${i + 1} 行执行日志，用于撑高任务执行详情右边栏内容，验证`
      + '头部标题与关闭按钮是否随内容滚动而滚动。',
  }))
  const task = {
    id: 477,
    status: 'succeeded',
    repo_name: 'botler',
    issue_iid: 477,
    issue_title: '任务执行详情右边栏的顶部标题和关闭按钮固定在顶部',
    engine: 'dsh',
    triggered_by: 'webhook',
    attempt_count: 1,
    exit_code: 0,
    created_at: '2026-08-25 10:00:00',
    started_at: '2026-08-25 10:00:01',
    finished_at: '2026-08-25 10:05:00',
    commit_url: null,
    commit_sha: null,
    dsh_session_id: null,
    usage: null,
    error_message: null,
    logs,
    log_file_tail: ('这是一段很长的执行日志文件尾部内容，用于撑高任务执行'
      + '详情右边栏，验证头部标题与关闭按钮是否随内容滚动而滚动。').repeat(60),
  }
  return { task, tasks: [task] }
}

test('任务执行详情右边栏头部标题与关闭按钮固定在顶部，不随内容滚动（issue #477）', async ({ page }) => {
  await mockGitLabApis(page, { issues: tallIssueFixture() })
  const { task, tasks } = tallTaskDetailFixture()
  // 详情接口（评论/活动）：返回空评论，描述文本已足够撑高 issue 抽屉
  await page.route('**/api/issues/123/477/detail', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        notes: [], engine: 'dsh', task_id: task.id,
        task_duration_seconds: 300, task_status: 'succeeded',
      }),
    })
  })
  // 任务执行详情接口：任务列表 / 任务详情 / 执行数据 / SSE 事件流
  await page.route('**/api/issues/123/477/tasks', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ tasks }),
    })
  })
  await page.route('**/api/tasks/477', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(task),
    })
  })
  await page.route('**/api/tasks/477/execution', (route) => {
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ log_offset: 0, transcript: [], session_id: null }),
    })
  })
  await page.route('**/api/tasks/477/events', (route) => {
    route.fulfill({
      contentType: 'text/event-stream',
      body: 'data: {"kind":"done"}\n\n',
    })
  })

  await page.goto('/overview')
  await expect(page.locator('.issue-link').first()).toBeVisible()
  await page.locator('.issue-link').first().click()
  const drawer = page.locator('.issue-drawer')
  await expect(drawer).toBeVisible()

  // 打开第二层任务执行详情右边栏
  await drawer.getByRole('button', { name: '查看执行的详情' }).click()
  const taskDrawer = page.locator('.task-detail-drawer')
  await expect(taskDrawer).toBeVisible()
  // 等 drawer-in 滑入动画（240ms）完成后再断言几何位置，避免中间帧偏移
  await page.waitForTimeout(600)
  const header = taskDrawer.locator('.modal-header')

  // 1. 头部标题与 × 关闭按钮渲染于头部（.modal-header 内）
  await expect(header.locator('.issue-drawer-title')).toContainText('任务执行详情 — #477')
  await expect(header.getByRole('button', { name: '关闭任务执行详情右边栏' })).toBeVisible()

  // 2. 头部 computed style 为 sticky（吸附顶部）
  expect(await header.evaluate((el) => getComputedStyle(el).position)).toBe('sticky')

  // 3. 抽屉内容必须可滚动（scrollHeight > clientHeight），否则断言无意义
  const scrollable = await taskDrawer.evaluate((el) => el.scrollHeight > el.clientHeight)
  expect(scrollable).toBe(true)

  // 4. 静止时头部紧贴抽屉顶部（无空隙——负 margin 抵消 .drawer padding）
  const flushBefore = await taskDrawer.evaluate((el) => {
    const dr = el.getBoundingClientRect()
    const h = el.querySelector('.modal-header').getBoundingClientRect()
    return { gap: Math.round((h.top - dr.top) * 100) / 100 }
  })
  expect(flushBefore.gap, `头部与抽屉顶部应无空隙（gap=${flushBefore.gap}）`)
    .toBeLessThan(1)

  // 5. 滚动抽屉内容后，头部 top 坐标保持不动（不随内容滚走）
  const topBefore = await header.evaluate((el) => el.getBoundingClientRect().top)
  await taskDrawer.evaluate((el) => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  const after = await taskDrawer.evaluate((el) => ({
    scrollTop: el.scrollTop,
    headerTop: el.querySelector('.modal-header').getBoundingClientRect().top,
    drawerTop: el.getBoundingClientRect().top,
  }))
  expect(after.scrollTop).toBeGreaterThan(100) // 确实发生了滚动
  expect(Math.abs(after.headerTop - topBefore)).toBeLessThan(2) // 头部固定未移动

  // 6. 滚动后抽屉顶部 8px 条带的 topmost 元素必须是头部（或其子元素），
  //    不是滚动上来的内容行——「文字从空隙露出」不再发生（issue #335 同款）
  const strip = await taskDrawer.evaluate((el) => {
    const dr = el.getBoundingClientRect()
    const x = dr.left + dr.width / 2
    const y = dr.top + 8
    const topEl = document.elementFromPoint(x, y)
    const h = el.querySelector('.modal-header')
    return {
      gap: Math.round((h.getBoundingClientRect().top - dr.top) * 100) / 100,
      inHeader: !!h && h.contains(topEl),
      elCls: topEl ? (topEl.className || topEl.tagName) : null,
    }
  })
  expect(strip.gap, `滚动后头部与抽屉顶部应仍无空隙（gap=${strip.gap}）`).toBeLessThan(1)
  expect(strip.inHeader,
         `滚动后抽屉顶部条带应命中头部而非内容（命中：${strip.elCls}）`).toBe(true)
})
