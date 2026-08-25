// 全页面多尺寸截图配置（issue #445）
//
// 集中管理「CI 截图任务」的三个要素：
//   1. 页面路由清单 PAGE_ROUTES —— 与 frontend/src/App.jsx 的 <Route>
//      一一对应（/tasks/1 使用 e2e 种子数据库中的任务 id=1），保证
//      「应用每个页面」都被截图；
//   2. 视口清单 VIEWPORTS —— 覆盖常见桌面 / 平板 / 手机屏幕尺寸与
//      宽高比（16:9、16:10、4:3、3:4、9:16），对应需求「不同屏幕
//      尺寸、不同比例」；
//   3. 输出目录 SCREENSHOT_DIR 与路径 / 索引页辅助函数。
//
// 本模块被截图 Playwright spec（page-screenshots.spec.js）与单元测试
// （frontend/tests/screenshot-config.test.mjs）共同引用，保证截图范围
// 与测试断言范围始终一致。

// 截图输出目录（相对 frontend/，CI 以 artifacts 整体上传）
export const SCREENSHOT_DIR = 'screenshots'

/**
 * 页面路由清单。
 * - path：应用路由；name：中文页面名（索引页展示）；ready：页面内容就绪
 *   选择器（Playwright 等待其出现后再截图，避免截到 loading/白屏）。
 * - ready 选 comma 分隔的「任一命中即可」选择器：桌面/移动端某些页面
 *   布局不同（如任务页桌面表格 vs 移动卡片列表）。
 */
export const PAGE_ROUTES = [
  { path: '/overview', name: '概览页', ready: '.issue-repo-card' },
  { path: '/repos', name: '仓库管理', ready: 'main.content' },
  { path: '/tasks', name: '任务列表', ready: '.tasks-card-list, .table.tasks-table, main.content' },
  { path: '/tasks/1', name: '任务详情', ready: 'main.content' },
  { path: '/notifications', name: '通知中心', ready: 'main.content' },
  { path: '/stats', name: '统计', ready: 'main.content' },
  { path: '/templates', name: '结果评论模板', ready: 'main.content' },
  { path: '/labels', name: '标签管理', ready: 'main.content' },
  { path: '/plugins', name: '插件', ready: 'main.content' },
  { path: '/skills', name: '技能', ready: 'main.content' },
  { path: '/tools', name: '工具', ready: 'main.content' },
  { path: '/settings', name: '设置', ready: '.settings-layout' },
  { path: '/terminal', name: '终端', ready: 'main.content' },
]

/**
 * 视口（屏幕尺寸 / 比例）清单。
 * 覆盖：宽屏桌面 16:9、常规桌面 16:10、小桌面 16:9、平板横屏 4:3、
 * 平板竖屏 3:4、手机竖屏 9:16、小屏手机 9:16。
 */
export const VIEWPORTS = [
  { name: 'desktop-wide', width: 1920, height: 1080 },   // 16:9
  { name: 'desktop', width: 1440, height: 900 },         // 16:10
  { name: 'desktop-small', width: 1366, height: 768 },   // 16:9
  { name: 'tablet-landscape', width: 1024, height: 768 }, // 4:3
  { name: 'tablet-portrait', width: 768, height: 1024 },  // 3:4
  { name: 'mobile', width: 375, height: 667 },            // 9:16
  { name: 'mobile-small', width: 320, height: 568 },      // 9:16
]

/**
 * 路由 → 输出目录名（截图文件按页面分目录存放）。
 * 清洗 URL 特殊字符为连字符，防止路径穿越 / 非法文件名；
 * 根路径 '/' 映射为 index。
 */
export function routeDirName(route) {
  const cleaned = route.replace(/^\/+/, '').replace(/[\/?#&=.:]+/g, '-')
  return cleaned || 'index'
}

/** 视口 → 截图文件名（含尺寸便于索引页直接标注） */
export function screenshotFileName(viewport) {
  return `${viewport.name}-${viewport.width}x${viewport.height}.png`
}

/** 路由 × 视口 → 相对 SCREENSHOT_DIR 的截图路径 */
export function relativeScreenshotPath(route, viewport) {
  const p = typeof route === 'string' ? route : route.path
  return `${routeDirName(p)}/${screenshotFileName(viewport)}`
}

/**
 * 生成截图索引页 HTML（artifacts 内可离线浏览全部截图）。
 * @param {Array<{route:string,name:string,viewport:string,file:string}>} entries
 * @returns {string} 完整 HTML 字符串
 */
export function buildScreenshotIndexHtml(entries) {
  const esc = (s) =>
    String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
  const items = entries.length
    ? entries
        .map(
          (e) => `<section class="shot">
  <h3>${esc(e.name)} <span class="meta">${esc(e.route)} · ${esc(e.viewport)}</span></h3>
  <a href="${esc(e.file)}" target="_blank"><img src="${esc(e.file)}" alt="${esc(e.file)}" loading="lazy"></a>
</section>`,
        )
        .join('\n')
    : '<p class="empty">暂无截图</p>'
  const groups = new Set(entries.map((e) => e.route))
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Botler 全页面截图（CI 生成）</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 24px; background: #f6f8fa; color: #1f2328; }
  h1 { font-size: 20px; }
  .stats { color: #57606a; font-size: 13px; }
  .shot { background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px; margin: 12px 0; }
  .shot h3 { margin: 0 0 8px; font-size: 14px; }
  .meta { color: #57606a; font-weight: normal; font-size: 12px; }
  img { max-width: 100%; border: 1px solid #eaeef2; border-radius: 4px; }
  .empty { color: #57606a; }
</style>
</head>
<body>
<h1>Botler 全页面截图（CI/CD 生成）</h1>
<p class="stats">共 ${entries.length} 张截图 · ${groups.size} 个页面 × ${VIEWPORTS.length} 种视口（屏幕尺寸/比例）</p>
${items}
</body>
</html>
`
}
