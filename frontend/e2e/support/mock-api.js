// Playwright 浏览器级 mock API 助手（issue #212）
// 只拦截依赖真实 GitLab 的接口，其余请求放行到真实后端（uvicorn），
// 实现「真浏览器 + 真前端构建 + 真后端」跑稳定 E2E。
import {
  ISSUES_FIXTURE,
  PIPELINES_FIXTURE,
  FORM_META_FIXTURE,
  CREATED_ISSUE,
} from '../fixtures/api-fixtures.js'

/**
 * 注册 GitLab 依赖接口的浏览器级 mock。
 * @param {import('@playwright/test').Page} page
 * @param {object} [opts]
 * @param {Function|object} [opts.issues]     GET /api/issues/overview 响应
 * @param {object} [opts.pipelines]           GET /api/pipelines/overview 响应
 * @param {object} [opts.formMeta]            GET /api/issues/form-meta/* 响应
 * @param {Function} [opts.onCreate]          POST /api/issues 成功回调（返回响应体）
 * @returns {Promise<{createdIssues: object[]}>} 收集的 POST /api/issues 请求体
 */
export async function mockGitLabApis(page, opts = {}) {
  const createdIssues = []
  const resolve = (v) => (typeof v === 'function' ? v() : v)

  // 开放 issue 聚合（概览页）
  await page.route('**/api/issues/overview', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(resolve(opts.issues ?? ISSUES_FIXTURE)),
    })
  })

  // CI/CD 流水线概览
  await page.route('**/api/pipelines/overview', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(opts.pipelines ?? PIPELINES_FIXTURE),
    })
  })

  // 添加 Issue 弹窗：仓库成员 + 标签
  await page.route('**/api/issues/form-meta/*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(opts.formMeta ?? FORM_META_FIXTURE),
    })
  })

  // 创建 issue（POST /api/issues）：记录请求体并返回固定成功响应
  await page.route('**/api/issues', async (route) => {
    const req = route.request()
    if (req.method() !== 'POST') {
      await route.fallback()
      return
    }
    const body = req.postDataJSON()
    createdIssues.push(body)
    const created = opts.onCreate
      ? opts.onCreate(body)
      : CREATED_ISSUE(body)
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify(created),
    })
  })

  return { createdIssues }
}
