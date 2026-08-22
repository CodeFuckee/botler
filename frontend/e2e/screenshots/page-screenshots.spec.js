// 全页面多尺寸截图（issue #445）
//
// 在 CI/CD 流水线（.gitlab-ci.yml 的 e2e:screenshots job）中对应用的
// 每个页面在多种屏幕尺寸 / 宽高比下截图，输出到 frontend/screenshots/
// （含 index.html 索引页），作为 artifacts 上传供人工查看页面在各
// 视口下的真实渲染效果（布局 / 溢出 / 断点表现）。
//
// 实现方法：
//   - 复用 e2e 基础设施（start-servers.sh 起真实后端 + vite preview，
//     GitLab 依赖接口浏览器级 mock），与 e2e:playwright 同构、数据确定；
//   - 页面与视口清单统一来自 screenshot-config.mjs（单元测试保证与
//     App.jsx 路由一致、覆盖需求要求的尺寸比例）；
//   - 每个「路由 × 视口」组合：设置视口 → 打开页面 → 等待内容就绪
//     （ready 选择器 + 稳定延迟）→ fullPage 整页截图；
//   - 全部完成后生成 index.html 索引页，方便 artifacts 内离线浏览。
import { test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockGitLabApis } from '../support/mock-api.js'
import {
  PAGE_ROUTES,
  VIEWPORTS,
  SCREENSHOT_DIR,
  relativeScreenshotPath,
  buildScreenshotIndexHtml,
} from './screenshot-config.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
// 输出目录：frontend/screenshots（相对仓库根 frontend/）
const OUT_DIR = path.resolve(__dirname, '..', '..', SCREENSHOT_DIR)
// 内容就绪后额外等待时间：等轮询首帧 / 动画 / 懒加载图片稳定后再截图
const SETTLE_MS = Number(process.env.SCREENSHOT_SETTLE_MS || 1500)

test('对应用每个页面在多种屏幕尺寸/比例下截图（issue #445）', async ({ page }) => {
  // 84 张整页截图（12 页面 × 7 视口）远超默认 30s 用例超时，放宽到 20 分钟
  test.setTimeout(20 * 60 * 1000)

  // GitLab 依赖接口浏览器级 mock（与其余 e2e spec 一致），其余接口走真实
  // 种子后端——保证概览页有确定的 issue 数据、页面内容可复现
  await mockGitLabApis(page)

  const entries = []
  let captured = 0
  const startedAt = Date.now()

  for (const route of PAGE_ROUTES) {
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      await page.goto(route.path, { waitUntil: 'domcontentloaded' })
      // 等待页面内容就绪（ready 选择器任一命中；失败则截图仍继续，避免
      // 单页异常中断整个截图任务——截图目的是「记录真实渲染」，空态/错误态
      // 也是该视口下的真实状态）
      try {
        await page.waitForSelector(route.ready, { timeout: 15_000 })
      } catch {
        console.warn(`[截图] 页面内容未在 15s 内就绪: ${route.path} @ ${viewport.name}，仍继续截图`)
      }
      // 稳定延迟：等轮询首帧、动画与图片加载
      await page.waitForTimeout(SETTLE_MS)

      const rel = relativeScreenshotPath(route, viewport)
      const outFile = path.join(OUT_DIR, rel)
      fs.mkdirSync(path.dirname(outFile), { recursive: true })
      // fullPage 整页截图：覆盖横向滚动之外的完整纵向内容，方便检查布局
      await page.screenshot({ path: outFile, fullPage: true })
      entries.push({
        route: route.path,
        name: route.name,
        viewport: `${viewport.name} (${viewport.width}x${viewport.height})`,
        file: rel,
      })
      captured += 1
    }
    console.log(`[截图] 页面完成: ${route.path}（${route.name}）`)
  }

  // 索引页：artifacts 内双击即可浏览全部截图
  fs.mkdirSync(OUT_DIR, { recursive: true })
  fs.writeFileSync(
    path.join(OUT_DIR, 'index.html'),
    buildScreenshotIndexHtml(entries),
  )

  const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1)
  console.log(`[截图] 完成：共 ${captured} 张截图（${PAGE_ROUTES.length} 页面 × ${VIEWPORTS.length} 视口），耗时 ${elapsed}s，输出目录: ${OUT_DIR}`)
})
