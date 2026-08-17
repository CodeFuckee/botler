// 任务详情 SSE 事件流 E2E（issue #212）：访问真实后端（uvicorn）的
// /api/tasks/{id}/events 事件流——后端从种子执行日志回放归一化事件
// （claude stream-json 每行一个事件），前端逐事件渲染后收到 done 收尾。
// 不 mock：整条链路（浏览器 EventSource → vite preview 代理 → uvicorn
// → 日志回放解析）均为真实运行。
//
// 思考过程显示开关（issue #176）：thinking 事件默认整条隐藏，勾选
// 「显示思考过程」后以展开态渲染，本测试同步验证默认隐藏 → 勾选显示
// 的完整链路。
import { test, expect } from '@playwright/test'

test.describe('任务详情 SSE 事件流', () => {
  test('任务页展示回放事件流并收到 done 收尾', async ({ page }) => {
    await page.goto('/tasks/1')

    // 1. 任务元信息：标题 + 成功徽章
    await expect(
      page.getByRole('heading', { name: '任务 #1' }),
    ).toBeVisible()
    await expect(page.locator('.badge', { hasText: '成功' })).toBeVisible()

    // 2. SSE 事件流：文本 / 工具调用 / 工具结果 / 结果摘要逐事件渲染
    const eventList = page.locator('.event-list')
    await expect(eventList.locator('.event-row').first()).toBeVisible()
    await expect(eventList).toContainText('E2E 事件流冒烟：开始执行任务')
    // 思考过程默认隐藏（issue #176）：thinking 事件内容不应渲染
    await expect(eventList).not.toContainText('先定位问题根因，再动手修复')
    await expect(eventList).toContainText('Bash')
    await expect(eventList).toContainText('echo hello')
    await expect(eventList.locator('.event-result')).toContainText('E2E 冒烟完成，任务成功')

    // 3. 勾选「显示思考过程」→ thinking 事件以展开态显示（issue #176）
    await page.getByLabel('显示思考过程').check()
    await expect(page.getByLabel('显示思考过程')).toBeChecked()
    await expect(eventList).toContainText('先定位问题根因，再动手修复')
    await expect(eventList).toContainText('思考过程')

    // 4. done 收尾：实时推送标记消失（SSE 收到 done 后前端关闭连接）
    await expect(page.locator('#live-panel')).not.toContainText('（实时推送）')

    // 5. 执行日志列表来自真实后端（种子任务日志）
    await expect(page.locator('.log-line').first()).toBeVisible()
    await expect(page.locator('.log-line')).toHaveCount(3)
  })
})
