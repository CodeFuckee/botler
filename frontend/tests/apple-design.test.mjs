// apple-design skill 全页面优化验收测试（issue #124）：按 apple-design
// skill 的核心原则（响应/材质与深度/动效/排版/可及性）逐条断言落地，
// 并逐页核对 8 个页面共享设计系统、无一遗漏：
//
// 1. 材质与深度（Materials & depth）——顶导航毛玻璃悬浮层（半透明 +
//    backdrop-filter 模糊），内容从其下方滚动；prefers-reduced-transparency
//    时回退纯色材质；prefers-contrast 时补明确边框；
// 2. 动效（Motion）——模态/抽屉「材质化入场」（surface-in/drawer-in/
//    overlay-in，缩放+位移+淡入而非纯 opacity），时长 150–300ms 区间，
//    全部受 prefers-reduced-motion 全局降级；帧级流畅 will-change 提示；
// 3. 响应（Response）——按下即时反馈：按钮 Apple 风格微缩放
//    scale(0.98)，issue-link/section-toggle/modal-close/folder-item/
//    add-method/remote-option/label-choice 补齐 active 态；
// 4. 排版（Typography）——系统字体光学尺寸 font-optical-sizing、字号相关
//    字距 token（--tracking-display 大标题负字距）、表格数字 tabular-nums；
// 5. 页面覆盖清单——概览/仓库/任务/任务详情/模版/标记库/设置/登录 8 页
//    全部存在于路由，且各自页面样式类在 styles.css 均有定义（证明共享
//    设计系统覆盖全页面，无遗漏）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const styles = readFileSync(path.join(ROOT, 'src/styles.css'), 'utf8')
const appSrc = readFileSync(path.join(ROOT, 'src/App.jsx'), 'utf8')

// 提取 styles.css 首个 :root 块（浅色主题）
function firstRootBlock(css) {
  const m = css.match(/:root\s*\{([^}]*)\}/s)
  return m ? m[1] : ''
}
// 提取深色模式 :root 块
function darkRootBlock(css) {
  const m = css.match(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/s)
  return m ? m[1] : ''
}
// 提取指定选择器规则体
function ruleBody(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = styles.match(new RegExp(esc + '\\s*\\{([^}]*)\\}', 's'))
  return m ? m[1] : null
}
// 提取指定选择器的全部规则体（同名规则可能出现在 @media/@supports 内，
// 需按内容特征挑选目标规则，如基础 .topnav 规则应含 position: sticky）
function ruleBodies(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return [...styles.matchAll(new RegExp(esc + '\\s*\\{([^}]*)\\}', 'gs'))].map((m) => m[1])
}
// 从全部规则体中挑出含指定内容的那个
function pickRule(selector, needle) {
  return ruleBodies(selector).find((b) => b.includes(needle)) || null
}
// 提取指定 @media 块（返回块体）
function mediaBlock(query) {
  const m = styles.match(new RegExp('@media\\s*\\(' + query + '\\)\\s*\\{([\\s\\S]*?)\\n\\}'))
  return m ? m[1] : null
}

// ---- 1. 材质与深度：顶导航毛玻璃悬浮层 ----

test('styles.css：顶导航为毛玻璃材质（半透明 + backdrop-filter 模糊，内容从其下滚动）', () => {
  // 纯色兜底：不支持 backdrop-filter / 减弱透明时用 --nav-bg
  // （@media/@supports 内也有 .topnav 规则，按 position: sticky 特征
  //   挑出基础规则——媒体查询内的同名规则不参与本次断言）
  const body = pickRule('.topnav', 'position: sticky')
  assert.ok(body, '应存在基础 .topnav 规则')
  assert.match(body, /background:\s*var\(--nav-bg\)/,
    'topnav 基础规则应使用纯色 --nav-bg 兜底')
  assert.match(body, /position:\s*sticky/, 'topnav 保持 sticky（内容从其下方滚动）')
  // 毛玻璃增强：@supports 块内半透明 + 背景模糊 + 饱和度增强
  const supports = styles.match(/@supports\s*\(backdrop-filter:\s*blur\(1px\)\)[^{]*\{([\s\S]*?)\n\}/)
  assert.ok(supports, '应存在 backdrop-filter @supports 回退增强块')
  assert.match(supports[1], /\.topnav\s*\{/, '@supports 块内应有 .topnav 规则')
  assert.match(supports[1], /backdrop-filter:\s*blur\(20px\)\s*saturate\(180%\)/,
    'topnav 应使用 blur(20px) saturate(180%) 毛玻璃效果')
  assert.match(supports[1], /background:\s*var\(--nav-bg-glass\)/,
    '毛玻璃下应使用半透明 --nav-bg-glass 底色')
})

test('styles.css：导航材质 token 浅色/深色双份定义（--nav-bg / --nav-bg-glass）', () => {
  const light = firstRootBlock(styles)
  assert.match(light, /--nav-bg:\s*#ffffff/, '浅色 --nav-bg 应为白底')
  assert.match(light, /--nav-bg-glass:\s*rgba\(255,\s*255,\s*255,\s*0\.78\)/, '浅色毛玻璃底色应为半透明白')
  const dark = darkRootBlock(styles)
  assert.match(dark, /--nav-bg:\s*#0a0a0a/, '深色 --nav-bg 应为深底')
  assert.match(dark, /--nav-bg-glass:\s*rgba\(10,\s*10,\s*10,\s*0\.72\)/, '深色毛玻璃底色应为半透明深底')
})

test('styles.css：prefers-reduced-transparency 下毛玻璃回退纯色（无模糊）', () => {
  const block = mediaBlock('prefers-reduced-transparency:\\s*reduce')
  assert.ok(block, '应存在 prefers-reduced-transparency: reduce 媒体查询')
  assert.match(block, /\.topnav\s*\{/, '块内应有 .topnav 规则')
  assert.match(block, /background:\s*var\(--nav-bg\)/, '应回退纯色 --nav-bg')
  assert.match(block, /backdrop-filter:\s*none/, '应关闭 backdrop-filter 模糊')
})

test('styles.css：prefers-contrast: more 下悬浮层/卡片补明确边框', () => {
  const block = mediaBlock('prefers-contrast:\\s*more')
  assert.ok(block, '应存在 prefers-contrast: more 媒体查询')
  assert.match(block, /\.card\s*\{[\s\S]*?border:\s*1px\s+solid\s+var\(--border\)/,
    '增强对比度下卡片应补明确边框')
  assert.match(block, /\.topnav\s*\{[\s\S]*?border-bottom:\s*1px\s+solid\s+var\(--border\)/,
    '增强对比度下导航应补底部分隔边框')
})

// ---- 2. 动效：材质化入场 + 帧级流畅 ----

test('styles.css：模态/抽屉材质化入场关键帧齐全（surface-in/drawer-in/overlay-in）', () => {
  for (const name of ['surface-in', 'drawer-in', 'overlay-in']) {
    assert.match(styles, new RegExp('@keyframes\\s+' + name + '\\s*\\{'),
      `应定义 @keyframes ${name}`)
  }
  // surface-in：缩放 + 位移 + 淡入（材质到达，而非纯 opacity）
  const sf = styles.match(/@keyframes\s+surface-in\s*\{([\s\S]*?)\n\}/)
  assert.match(sf[1], /scale\(0\.97\)/, 'surface-in 应从微缩放 0.97 开始')
  assert.match(sf[1], /translateY\(8px\)/, 'surface-in 应带轻微下移')
  // drawer-in：从右滑入（空间一致性）
  const di = styles.match(/@keyframes\s+drawer-in\s*\{([\s\S]*?)\n\}/)
  assert.match(di[1], /translateX\(100%\)/, 'drawer-in 应从 translateX(100%) 滑入')
})

test('styles.css：模态/抽屉/遮罩应用入场动画且时长在 150–300ms 区间', () => {
  const modal = pickRule('.modal', 'animation: surface-in')
  assert.ok(modal, '应存在 .modal 基础规则')
  assert.match(modal, /animation:\s*surface-in\s+var\(--dur\)/, '.modal 应使用 surface-in（--dur）')
  const drawer = pickRule('.drawer', 'animation: drawer-in')
  assert.ok(drawer, '应存在 .drawer 基础规则')
  assert.match(drawer, /animation:\s*drawer-in\s+240ms/, '.drawer 应使用 drawer-in 240ms')
  assert.match(ruleBody('.modal-overlay'), /animation:\s*overlay-in\s+var\(--dur-fast\)/,
    '.modal-overlay 遮罩应使用 overlay-in')
  assert.match(ruleBody('.drawer-overlay'), /animation:\s*overlay-in\s+var\(--dur-fast\)/,
    '.drawer-overlay 遮罩应使用 overlay-in')
  // 时长区间（apple-design：响应 0.3–0.4s，入场 150–300ms 不拖沓）
  const root = firstRootBlock(styles)
  const dur = Number(root.match(/--dur:\s*(\d+)ms/)[1])
  assert.ok(dur >= 150 && dur <= 300, `--dur 应为 ${dur}ms（150–300ms 区间）`)
  assert.match(root, /--ease-spring:\s*cubic-bezier\(0\.16,\s*1,\s*0\.3,\s*1\)/,
    '应使用 spring 风格缓动 token（--ease-spring）')
})

test('styles.css：登录卡与设置页指南内容材质化入场（各页面入场一致）', () => {
  assert.match(ruleBody('.login-card'), /animation:\s*surface-in\s+var\(--dur\)/,
    '登录卡应使用 surface-in 入场')
  assert.match(ruleBody('.guide-content'), /animation:\s*surface-in\s+var\(--dur\)/,
    '设置页 SSO 指南展开应使用 surface-in 入场')
})

test('styles.css：高频动画元素提供 will-change 合成层提示（帧级流畅）', () => {
  for (const [sel, props] of [['.modal', 'transform, opacity'], ['.drawer', 'transform, opacity'],
                              ['.spinner', 'transform'], ['.pipeline-stage-dot', 'opacity']]) {
    assert.match(styles, new RegExp(sel.replace('.', '\\.') + '\\s*\\{([^}]*will-change:\\s*' + props.replace(',', ',\\s*') + '[^}]*)\\}'),
                 `${sel} 应声明 will-change: ${props}`)
  }
})

test('styles.css：新入场动画受 prefers-reduced-motion 全局降级保护', () => {
  const rm = mediaBlock('prefers-reduced-motion:\\s*reduce')
  assert.ok(rm, '应存在 prefers-reduced-motion: reduce 媒体查询')
  assert.match(rm, /animation-duration:\s*0\.01ms\s*!important/,
    '减弱动态下所有动画时长应降为 0.01ms（含 surface-in/drawer-in/overlay-in）')
})

// ---- 3. 响应：按下即时反馈 ----

test('styles.css：按钮按下为 Apple 风格微缩放反馈（scale + 下沉）', () => {
  for (const sel of ['.btn:not(:disabled):active', '.btn-primary:not(:disabled):active',
                     '.btn-danger:not(:disabled):active']) {
    const body = ruleBody(sel)
    assert.ok(body, `应存在 ${sel} 规则`)
    assert.match(body, /scale\(0\.98\)/, `${sel} 应有 Apple 风格按下微缩放 scale(0.98)`)
  }
})

test('styles.css：可交互元素补齐 active 按下态（反馈活在按下瞬间）', () => {
  for (const sel of ['.issue-link:active', '.section-toggle:active', '.modal-close:active',
                     '.folder-item:active:not(:disabled)', '.label-choice:active']) {
    assert.ok(ruleBody(sel), `应存在 ${sel} 按下反馈规则`)
  }
  // 组合选择器（同一规则多条选择器）按源码文本断言
  assert.match(styles, /\.add-method:active,\s*\n\s*\.remote-option:active\s*\{/,
    '应存在 .add-method:active / .remote-option:active 组合按下反馈规则')
})

// ---- 4. 排版：光学尺寸 + 字号相关字距 + 数字对齐 ----

test('styles.css：正文启用系统字体光学尺寸（font-optical-sizing）', () => {
  assert.match(ruleBody('body'), /font-optical-sizing:\s*auto/,
    'body 应启用 font-optical-sizing: auto')
})

test('styles.css：字号相关字距 token 化且大标题引用负字距（Apple 排版）', () => {
  const root = firstRootBlock(styles)
  assert.match(root, /--tracking-display:\s*-0\.015em/, '应定义 --tracking-display 负字距 token')
  assert.match(root, /--tracking-caption:\s*0\.02em/, '应定义 --tracking-caption 正字距 token')
  assert.match(ruleBody('h1'), /letter-spacing:\s*var\(--tracking-display\)/,
    'h1 应引用 --tracking-display（大字负字距收紧）')
})

test('styles.css：表格数字列等宽数字对齐（tabular-nums，数字跳变不抖动）', () => {
  assert.match(ruleBody('.table'), /font-variant-numeric:\s*tabular-nums/,
    '.table 应启用 tabular-nums 等宽数字')
})

// ---- 5. 页面覆盖清单：8 个页面无一遗漏 ----

test('路由：8 个页面全部可用（概览/仓库/任务/任务详情/模版/标记库/设置路由 + 登录条件渲染）', () => {
  const routes = [
    ['/overview', 'Overview'], ['/repos', 'Repos'], ['/tasks', 'Tasks'],
    ['/tasks/:id', 'TaskDetail'], ['/templates', 'Templates'], ['/labels', 'Labels'],
    ['/settings', 'Settings'],
  ]
  for (const [route, comp] of routes) {
    assert.ok(appSrc.includes(`path="${route}"`), `App 路由应包含 ${route}`)
  }
  // 登录页非路由：SSO 启用且未登录时条件渲染（issue #27），断言导入与渲染分支
  assert.match(appSrc, /import\s+Login\s+from\s+'.\/pages\/Login\.jsx'/,
    'App 应导入 Login 页面')
  assert.match(appSrc, /auth\.enabled\s+&&\s+!auth\.user\s*\)\s*return\s+<Login\s*\/>/,
    'SSO 未登录时应渲染 <Login />')
  // 页面组件文件都存在（防止某页被移除后样式残留）
  for (const file of ['Overview', 'Repos', 'Tasks', 'TaskDetail', 'Templates', 'Labels', 'Settings', 'Login']) {
    const p = path.join(ROOT, `src/pages/${file}.jsx`)
    const src = readFileSync(p, 'utf8')
    assert.ok(src.length > 0, `页面文件 src/pages/${file}.jsx 应存在且非空`)
  }
})

test('styles.css：8 个页面各自样式类齐全（共享设计系统覆盖全页面，无遗漏）', () => {
  // 每页取代表性样式类：既证明该页面存在样式支撑，也证明全局
  // apple-design 优化（材质/动效/响应/排版）经共享 token 应用到每页
  const pageSelectors = {
    '概览页': ['.issues-section', '.issues-list', '.issue-item', '.issue-task-log',
              '.pipelines-section', '.pipelines-list', '.pipeline-card', '.pipeline-stage-dot'],
    '仓库页': ['.repo-item', '.repo-name', '.repo-actions', '.add-method', '.remote-option',
              '.test-chip'],
    '任务页': ['.tasks-table', '.stat-chip', '.status-dot', '.pagination', '.badge.resume'],
    '任务详情页': ['.chat-list', '.chat-msg', '.event-list', '.event-row', '.log-list',
                 '.log-line', '.section-toggle', '.log-view-flat'],
    '模版页': ['.section-toggle-h3', '.section-toggle-h2', '.input.textarea'],
    '标记库页': ['.label-list', '.label-chip', '.label-color', '.label-name', '.badge-default'],
    '设置页': ['.table.kv th', '.guide-box', '.guide-content', '.provider-form', '.settings-version .version-badge',
             '.check-input'],
    '登录页': ['.login-page', '.login-card', '.login-brand'],
  }
  for (const [page, selectors] of Object.entries(pageSelectors)) {
    for (const sel of selectors) {
      assert.ok(ruleBody(sel), `${page}样式类 ${sel} 应在 styles.css 中有定义`)
    }
  }
})

test('styles.css：交互行 hover 微反馈齐全（概览 issue 行/仓库行/标签行）', () => {
  assert.match(ruleBody('.issue-item'), /transition:\s*background\s+var\(--dur-fast\)/,
    '.issue-item 应有背景过渡')
  assert.ok(ruleBody('.issue-item:hover'), '应存在 .issue-item:hover')
  assert.match(ruleBody('.repo-item'), /transition:\s*background\s+var\(--dur-fast\)/,
    '.repo-item 应有背景过渡')
  assert.ok(ruleBody('.repo-item:hover'), '应存在 .repo-item:hover')
  assert.ok(ruleBody('.label-chip:hover'), '应存在 .label-chip:hover')
  // 运行中高亮项 hover 不被中性灰覆盖
  assert.ok(ruleBody('.issue-item.issue-item-running:hover'),
            '运行中高亮项应有专属 hover 规则（保持蓝色弱底）')
})
