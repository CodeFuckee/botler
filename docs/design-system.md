# Botler 前端设计规范（Design System）

> 本文档是 Botler Web UI（`frontend/`）的唯一样式规范来源。
> 新增或修改任何界面样式前，**必须先阅读本文档**，并遵循其中的令牌与组件规范。

## 1. 概述

### 1.1 设计基底

Botler UI 采用 **Vercel Geist 设计体系（浅色主题）** —— 一套被 Vercel 旗下大量产品（Vercel Dashboard、Geist UI 组件库等）长期验证的设计语言。

核心特征：

- **浅色、极简**：白/浅灰表面，通过 1px 发丝线（hairline）而非大面积色块建立层次
- **内容优先**：文字对比度高（`#171717` 主文本），辅助信息用灰度而非颜色区分
- **克制的强调**：仅一个主色 `#0070f3`（蓝），语义色（绿/黄/红）只用于状态表达
- **小圆角、轻阴影**：常规 6px 圆角，卡片用「阴影边框」替代粗重边框

### 1.2 适用范围

- 全部页面：仓库管理（`Repos`）、任务列表（`Tasks`）、任务详情（`TaskDetail`）、提示词模版（`Templates`）、系统设置（`Settings`）、目录选择对话框（`FolderPicker`）
- 全部组件与状态：导航、卡片、表单、表格、徽标、日志、提示、模态框
- 样式唯一入口：`frontend/src/styles.css`（CSS 变量定义于 `:root`）

### 1.3 设计原则

1. **极简**：删繁就简，层次靠留白与发丝线，不靠色块堆叠
2. **发丝线优先**：边框一律用 `rgba(0,0,0,0.08)` 发丝线或 `--border: #eaeaea`，不用深色粗边框
3. **状态用色克制**：颜色即语义（成功=绿、警告=黄、错误=红），不允许为了好看而给非状态元素上语义色
4. **一套令牌**：所有颜色/尺寸必须走 CSS 变量，禁止硬编码十六进制值（`#` 开头仅允许出现在 `:root` 定义中）

## 2. 设计令牌总览（CSS 变量）

定义于 `frontend/src/styles.css` 的 `:root`。**改样式先改变量，再改组件。**

| 变量 | 值 | 语义 |
|---|---|---|
| `--bg` | `#fafafa` | 页面背景（Geist background-200） |
| `--bg-card` | `#ffffff` | 卡片、导航、输入框等表面（background-100） |
| `--bg-hover` | `#fafafa` | 悬停背景（列表行 hover、按钮 hover） |
| `--bg-active` | `#f2f2f2` | 按下/激活背景（预留） |
| `--text` | `#171717` | 主文本（gray-1000） |
| `--muted` | `#666666` | 次级/弱化文本（gray-500） |
| `--border` | `#eaeaea` | 常规边框（gray-100） |
| `--border-hover` | `#d4d4d4` | 悬停边框（gray-200） |
| `--primary` | `#0070f3` | 主色：链接、主按钮、焦点、选中态 |
| `--primary-hover` | `#005bd1` | 主色悬停（加深） |
| `--primary-weak` | `rgba(0,112,243,0.08)` | 主色弱底（选中项背景、running 徽标） |
| `--focus-ring` | `0 0 0 3px rgba(0,112,243,0.18)` | 键盘焦点环 |
| `--ok` | `#17a34a` | 成功（Geist success） |
| `--ok-weak` | `rgba(23,163,74,0.1)` | 成功弱底 |
| `--warn` | `#d97706` | 警告（琥珀，已调深保证浅底可读性） |
| `--warn-weak` | `rgba(217,119,6,0.12)` | 警告弱底 |
| `--err` | `#e5484d` | 错误（Geist error） |
| `--err-weak` | `rgba(229,72,77,0.08)` | 错误弱底 |
| `--mono` | `'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace` | 等宽字体（代码/日志/路径） |
| `--nav-bg` | 浅 `#ffffff` / 深 `#0a0a0a` | 顶导航纯色兜底（不支持毛玻璃 / 系统减弱透明时） |
| `--nav-bg-glass` | 浅 `rgba(255,255,255,.78)` / 深 `rgba(10,10,10,.72)` | 顶导航毛玻璃半透明底色（apple-design 材质） |
| `--tracking-display` | `-0.015em` | 大标题负字距（Apple 排版：大字收紧，随字号变化） |
| `--tracking-caption` | `0.02em` | 小字正字距（辅助信息可读性） |
| `--radius` | `6px` | 常规圆角（按钮、输入框、卡片、徽标底） |
| `--radius-lg` | `12px` | 大圆角（模态框） |
| `--shadow-border` | `0 0 0 1px rgba(0,0,0,0.08)` | 发丝线阴影边框（stat-chip 等） |
| `--shadow-card` | `0 0 0 1px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04)` | 卡片阴影 |
| `--shadow-pop` | `0 12px 32px rgba(0,0,0,0.12)` | 弹层阴影（模态框） |

## 3. 色板

### 3.1 中性色（表面与文字）

| 用途 | 值 | 说明 |
|---|---|---|
| 页面背景 | `#fafafa` | 整体底色，略灰以衬托白色卡片 |
| 卡片/导航表面 | `#ffffff` | 所有浮于页面背景上的表面 |
| 主文本 | `#171717` | 标题、正文、表格内容 |
| 次级文本 | `#666666` | 辅助说明、表头、时间戳（用 `--muted` 类） |
| 常规边框 | `#eaeaea` | 表格分隔线、输入框边框、卡片内部细分隔 |
| 发丝线 | `rgba(0,0,0,0.08)` | 卡片轮廓、stat-chip 轮廓（Geist 标志性技法） |

### 3.2 主色（唯一强调色）

| 用途 | 值 |
|---|---|
| 主色（链接/主按钮/焦点/选中） | `#0070f3` |
| 悬停 | `#005bd1` |
| 弱底背景（选中项、running 徽标） | `rgba(0,112,243,0.08)` |
| 焦点环 | `rgba(0,112,243,0.18)` 3px |

**使用约束**：主色只用于可交互元素与「当前选中/进行中」的语义表达。装饰性元素（图标、品牌）不得使用主色填充。

### 3.3 语义色（状态用色）

| 语义 | 主值 | 弱底 | 典型用途 |
|---|---|---|---|
| 成功 `--ok` | `#17a34a` | `rgba(23,163,74,0.1)` | succeeded 徽标、保存成功提示、test-chip ✓ |
| 警告 `--warn` | `#d97706` | `rgba(217,119,6,0.12)` | retrying 徽标、日志 WARN |
| 错误 `--err` | `#e5484d` | `rgba(229,72,77,0.08)` | failed 徽标、危险按钮、日志 ERROR、错误提示 |

**使用约束**：语义色只用于表达对应语义，禁止用于装饰。徽标/提示一律「弱底 + 深色文字」组合，不用实心色块。

## 4. 字体

### 4.1 字体栈

```css
/* 正文（英文/中文混排） */
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
  'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;

/* 等宽（代码、日志、路径、占位符） */
font-family: var(--mono);
```

- 优先使用系统字体栈（Apple/微软经验证的现代字体），不引入外部字体包
- 中文场景依赖系统回退字体（PingFang SC / 微软雅黑），不单独加载中文字体
- 若后续要引入 [Geist 字体](https://vercel.com/font)（npm 包 `geist`），仅作为拉丁字符优先字体，中文回退栈保持不变
- `body` 启用 `font-optical-sizing: auto`（Apple 排版：系统字体光学尺寸随字号自动调节）
- 表格启用 `font-variant-numeric: tabular-nums`（数字列等宽对齐，数字跳变不抖动）
- 字距按字号分层：大标题用 `--tracking-display` 负字距收紧，小字辅助信息用
  `--tracking-caption` 正字距提升可读性，正文保持 `0`（Apple 排版原则）

### 4.2 字号层级

| 场景 | 字号 | 字重 | 备注 |
|---|---|---|---|
| 页面标题 `h1` | 20px | 600 | `letter-spacing: var(--tracking-display)`（负字距收紧） |
| 区块标题 `h2` | 14px | 600 | 全大写 + `0.05em` 字距，灰色（区块功能标签） |
| 正文/表格/输入 | 14px | 400 | 默认 |
| 弱化文本 `.small` | 12.5px | 400 | 辅助说明 |
| 表头 `th` | 12px | 500 | 灰色 |
| 徽标 `.badge` | 12px | 500 | |
| 日志 | 12.5px | 400 | 等宽字体 |
| 行高 | 1.6 | — | 全局 |

## 5. 间距节奏（4px 网格）

所有间距取 4 的倍数：

| 间距 | 值 | 用途 |
|---|---|---|
| 4px | `gap: 4px` | 导航链接之间 |
| 8px | `gap: 8px` | 表单行内元素间距、仓库操作按钮间距 |
| 10px | — | 统计条间距（仅此一处，保持既有节奏） |
| 12px | — | 表单行间距（`form-row` 上下）、徽标内边距 |
| 16px | `margin-bottom: 16px` | 卡片间距、页面标题下距 |
| 20px | `padding: 20px` | 卡片内边距、页面左右留白 |
| 24px | `padding: 24px` | 内容区上下留白 |

## 6. 圆角、阴影与边框

### 6.1 圆角

| 层级 | 值 | 适用 |
|---|---|---|
| 小 | `--radius: 6px` | 按钮、输入框、卡片、列表容器、下拉选择 |
| 大 | `--radius-lg: 12px` | 模态框 |
| 全圆 | `999px` | 徽标（badge）、test-chip |

### 6.2 阴影

| 层级 | 值 | 适用 |
|---|---|---|
| 发丝线边框 | `--shadow-border` | stat-chip 等轻元素 |
| 卡片 | `--shadow-card` | `.card` |
| 弹层 | `--shadow-pop` | 模态框 |

> 技法说明（Geist 标志性做法）：用 `0 0 0 1px rgba(0,0,0,0.08)` 的**阴影边框**替代 `border`，避免边框参与盒子尺寸计算，且在不同表面上呈现一致。

### 6.3 边框

- 常规：`1px solid var(--border)`（`#eaeaea`）
- 悬停：`1px solid var(--border-hover)`（`#d4d4d4`）
- 卡片轮廓：优先用 `--shadow-card` 阴影边框，而非实心 border
- 内部细分隔（表格行、列表项）：`1px solid var(--border)`，最后一项 `border-bottom: none`

## 7. 组件规范

### 7.1 顶部导航（`.topnav`）

- 52px 高、吸顶（`position: sticky`），底部 1px 发丝线阴影分隔
- **毛玻璃材质（apple-design）**：支持 `backdrop-filter` 时底色为半透明
  `--nav-bg-glass` + `blur(20px) saturate(180%)`，内容从其下方滚动（材质层）；
  不支持时回退纯色 `--nav-bg`；`prefers-reduced-transparency: reduce` 时
  回退纯色无模糊；`prefers-contrast: more` 时补明确底部分隔边框
- 品牌：粗体 16px，与链接用 16px 间距
- 导航链接：12px 水平内边距、6px 圆角
  - 默认：`--muted` 灰色
  - 悬停：文本变 `--text` + `rgba(0,0,0,0.04)` 背景
  - 激活（当前页）：`--primary` 蓝字 + `--primary-weak` 蓝底
  - 按下（active）：微缩放反馈

### 7.2 卡片（`.card`）

- 白底 + `--shadow-card` + 6px 圆角 + 20px 内边距 + 16px 下间距
- 卡片内区块标题用 `h2`（灰色小号全大写）

### 7.3 按钮（`.btn`）

| 变体 | 样式 |
|---|---|
| 默认 | 白底、1px `--border` 边框、13px 500 字重；hover 背景 `--bg-hover`、边框 `--border-hover` |
| `.btn-primary` | 实心 `--primary` 蓝底白字；hover 加深为 `--primary-hover`。**每屏最多一个主按钮** |
| `.btn-danger` | 红字；hover 红底弱化 + 红边框。用于破坏性操作 |
| `disabled` | 50% 透明度 + `not-allowed` 光标 |

### 7.4 表单控件（`.input`、`.add-method`、`.remote-option`）

- 输入框：白底、1px `--border` 边框、6px 圆角、`min-width: 0`
  - hover：`--border-hover`
  - 焦点：蓝色边框 + `--focus-ring` 焦点环（键盘可达性）
- 单选/复选：`accent-color: var(--primary)` 跟随主色
- 选项胶囊（`.add-method` / `.remote-option`）：边框样式，选中态 = 蓝边框 + 蓝字 + 蓝弱底（`:has(input:checked)`）

### 7.5 表格（`.table`）

- 全宽、行间 1px `--border` 细线，最后一行无线
- 表头：12px、`--muted`、500 字重
- 行悬停：`--bg-hover` 背景
- 行内链接：`--primary` 蓝、hover 下划线
- 长文本：`.ellipsis`（单行省略）或 `.pre-wrap`（保留换行）

### 7.6 状态徽标（`.badge`）

- 全圆胶囊：1px 内边距 + 8px 水平内边距 + 12px 500 字重
- 配色一律「弱底 + 深色文字」：

| 状态类 | 弱底 | 文字色 |
|---|---|---|
| `status-queued` / `status-interrupted` / `badge-muted` | `rgba(0,0,0,0.05)` | `--muted` |
| `status-running` | `--primary-weak` | `--primary` |
| `status-retrying` | `--warn-weak` | `--warn` |
| `status-succeeded` | `--ok-weak` | `--ok` |
| `status-failed` | `--err-weak` | `--err` |
| `badge-git` | `--ok-weak` | `--ok` |

### 7.7 统计条（`.stats-row` / `.stat-chip`）

- 统计条：10px 间距、可换行
- 统计胶囊：白底 + `--shadow-border` 发丝线 + 6px 圆角
- 状态圆点 `.status-dot`：8px 实心圆，颜色跟随对应状态色

### 7.8 日志（`.log-list` / `.log-view`）

- 白底、`--border` 边框、6px 圆角、等宽字体 12.5px
- 日志行：时间戳灰色（`.log-ts`）、级别固定 42px 宽度左对齐
- 级别着色：`log-error` 红、`log-warn` 琥珀、`log-info` 主文本色

### 7.9 提示（`.alert`）

- 圆角 + 弱底 + 同色系半透明边框 + 深色语义文字
- `alert-error` 可点击关闭（`cursor: pointer`），`alert-ok` 不可关闭
- 变体 `.alert.small`：更紧凑的提示

### 7.10 模态框（`.modal`）

- 遮罩：`rgba(0,0,0,0.4)` 全屏、居中、`z-index: 100`
- 弹层：白底、12px 圆角、`--shadow-pop`、640px 宽（`max-width: 92vw`、`max-height: 80vh`）
- 关闭按钮（`.modal-close`）：无边框、灰色，hover 微灰底
- 目录列表（`.folder-list`）：浅灰底容器 + `--border` 边框，条目 hover 白底

## 7.11 动效（apple-design）

- **材质化入场**：模态/弹窗用 `@keyframes surface-in`（`scale(.97)` +
  `translateY(8px)` + 淡入，`--dur` 200ms + `--ease-spring`）；右侧抽屉用
  `@keyframes drawer-in`（`translateX(100%)` → 0，240ms，空间一致性——从右
  进入）；遮罩用 `@keyframes overlay-in`（先于表面淡入）。所有入场时长
  落在 150–300ms 区间，并受 `prefers-reduced-motion` 全局降级保护
- **帧级流畅**：只动 `transform`/`opacity` 合成属性；高频动画元素
  （spinner、流水线阶段节点、模态、抽屉）声明 `will-change` 提升合成层
- **响应反馈**：按钮按下 `translateY(1px) scale(0.98)` 微缩放；可交互行
  （issue 行/仓库行/标签行）hover 背景过渡；可点击卡片（流水线/开放 issue）
  hover 轻抬升 + 阴影加深

## 8. 交互规范

| 状态 | 通用规则 |
|---|---|
| hover | 可交互元素必须有可见反馈：按钮变灰底、链接变主色、输入框边框加深、列表行灰底 |
| active | 按下即时反馈：按钮 `translateY(1px) scale(0.98)` 微缩放；链接/折叠标题/关闭按钮/选项胶囊 opacity/背景变化（反馈活在按下瞬间） |
| focus | 键盘焦点必须可见：输入框/按钮用 `--focus-ring` 焦点环（3px 半透明蓝） |
| disabled | 一律 `opacity: 0.5` + `cursor: not-allowed` |
| 选中 | 单选/复选胶囊：蓝边框 + 蓝字 + 蓝弱底 |

## 9. 新增样式检查清单

新增组件或页面时逐条核对：

- [ ] 颜色全部使用 `:root` 变量，无硬编码 hex
- [ ] 边框用 `--border` 或发丝线阴影，圆角用 `--radius`
- [ ] 间距为 4 的倍数（10px 等历史例外不扩散）
- [ ] 状态表达只用语义色（ok/warn/err），装饰不用语义色
- [ ] hover 与 focus 状态齐全（含键盘焦点环）
- [ ] 每屏最多一个 `btn-primary`
- [ ] 中文文案与字体栈符合第 4 节规范

## 10. 迁移记录（2026-08-11）

### 变更前（旧深色主题）

- 深蓝灰底色（`#0f1420` / `#171e2e`），蓝主色 `#4f8cff`
- 痛点：整体偏暗压抑、配色廉价感、层次与设计感不足

### 变更内容

| 维度 | 旧 | 新 |
|---|---|---|
| 主题 | 深色 | 浅色（Geist Light） |
| 页面背景 | `#0f1420` | `#fafafa` |
| 卡片表面 | `#171e2e` | `#ffffff` |
| 主文本 | `#dce4f5` | `#171717` |
| 主色 | `#4f8cff` | `#0070f3` |
| 边框 | `#2a3550` | `#eaeaea` / 发丝线 |
| 圆角 | 卡片 10px / 常规 6px | 卡片 6px / 模态框 12px |
| 阴影 | 无 | 发丝线 + 卡片轻阴影 + 弹层阴影 |
| 徽标 | 透明底 + 彩色字 | 弱底 + 深色字胶囊 |

### 决策背景

用户要求「换掉不满意的界面风格、采用经过验证的设计方案、输出完整规范」。经评估选择 **Vercel Geist**：开源、浅色、面向开发者工具（与 Botler 的 GitLab 运维后台定位一致）、色板与交互规范被 Vercel 大量生产环境验证。主色保留蓝色系以延续既有品牌认知。技术路线为纯 CSS 变量重构（不引入组件库，保持零运行时依赖）。
