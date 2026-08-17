// 设置页保存配置 E2E（issue #212）：真实后端（uvicorn）的 GET/PUT
// /api/settings 契约——修改任务调度参数 → 保存 → 提示成功 → 重载页面
// 后配置持久化生效（写回 config.yaml）。
import { test, expect } from '@playwright/test'

test.describe('设置页保存配置', () => {
  test('修改任务调度参数并保存，重载后仍保持', async ({ page }) => {
    let putBody = null
    page.on('request', (req) => {
      if (req.method() === 'PUT' && req.url().includes('/api/settings')) {
        putBody = req.postDataJSON()
      }
    })

    await page.goto('/settings')
    const tasksSection = page.locator('#settings-tasks')
    await expect(tasksSection).toBeVisible()

    // 1. 修改「跨仓库并行上限」为 5（第一个 num-input）
    const maxConcurrent = tasksSection.locator('input.num-input').first()
    await expect(maxConcurrent).toHaveValue('3') // e2e 配置默认 3
    await maxConcurrent.fill('5')

    // 2. 点击任务调度卡片「保存」
    await tasksSection.locator('button.btn-primary', { hasText: '保存' }).click()

    // 3. 保存成功提示（写回 config.yaml）
    await expect(page.locator('.alert.alert-ok')).toContainText('已保存')

    // 4. 请求体断言：worker.max_concurrent_repos=5
    expect(putBody).not.toBeNull()
    expect(putBody.worker).toMatchObject({ max_concurrent_repos: 5 })

    // 5. 重载页面验证持久化（真实后端从 config.yaml 重新读取）
    await page.reload()
    const reloaded = page.locator('#settings-tasks input.num-input').first()
    await expect(reloaded).toHaveValue('5')
  })
})
