# Botler 界面优化参考 —— 同类开源项目调研与 UI 设计借鉴

> 对应 issue：#121「界面优化讨论，现在界面布局和样式都不够美观，帮助找一些
> 类似的开源项目，可以参考ui设计」。
> 本文档是**讨论/调研产出**：梳理与 Botler 形态相近的开源项目的 UI 设计，
> 提炼可借鉴点，并给出分页面改进建议；**具体界面改造不在本 issue 实施**，
> 建议按 §6 拆分为后续独立 issue。
> 项目数据（Star / License / 主页）查询自 GitHub / Codeberg / GitLab 官方
> API，截至 2026-08-16；Star 数会随时间变化，仅作量级参考。

## 1. 背景

Botler 是一个自托管的 GitLab issue 自动化机器人平台：通过 webhook 实时监控
issue，指派给 bot 账号后调用 Claude Code / hermes / dsh 引擎开发并推送修复，
Web 前端负责仓库管理、任务监控、流水线状态、模版与系统设置。

用户反馈「界面布局和样式都不够美观」，希望参考同类开源项目的 UI 设计。
本文档回答三个问题：

1. 有哪些形态相似的开源项目值得参考？
2. 它们的 UI 好在哪、具体可以借鉴什么？
3. 结合 Botler 现状，分页面可以怎么改？

## 2. Botler 现有 UI 现状

基于 `frontend/src/` 代码与 [`docs/design-system.md`](design-system.md) 梳理：

### 2.1 已完成的设计基础

- **设计基底**：Vercel Geist 设计体系（浅色主题），设计令牌集中在
  `styles.css` 的 `:root`（`--bg / --text / --primary / --border` 等）；
- **Apple HIG 对齐**（issue #110/#111）：8pt 网格间距 token、150ms ease-out
  过渡、active 按下反馈、44px 触控目标、`prefers-reduced-motion` 降级、
  焦点可见性与 aria-label、空状态/加载态、深色模式跟随系统、语义色
  WCAG AA 对比度；
- **页面清单**：概览（Overview）、仓库（Repos）、任务（Tasks）、任务详情
  （TaskDetail）、模版（Templates）、标记库（Labels）、设置（Settings）、
  登录（Login）；通用组件含 IssueDrawer 抽屉、AddIssueModal、Markdown、
  DialogHost、FolderPicker。

### 2.2 现状短板（与同类项目对比后的观察）

| 维度 | 现状 | 同类项目更优的做法 |
|---|---|---|
| 视觉层级 | 全站同质化卡片 + 单级标题，关键状态靠文字徽章 | 概览页用「数字 + 趋势 + 图形」建立一眼可读的信息层级 |
| 数据可视化 | 概览页状态纯文字/徽章，无图形化 | 状态环、迷你趋势线、进度条（Uptime Kuma / Grafana / Plane） |
| 空状态/加载态 | 已有 spinner 与 empty-state，但较朴素 | 空状态插图/引导文案、骨架屏（Paperless-ngx / Plane / shadcn 风格） |
| 列表交互 | 表格/列表行操作少，无批量操作、无行内搜索过滤 | Gitea / Appsmith / Ant Design 的过滤、排序、批量操作 |
| 微交互 | 过渡克制（150ms ease-out），无 hover 浮层 | shadcn/ui / n8n / PostHog 的浮层卡片、平滑展开 |
| 品牌感 | 顶栏纯文本品牌，无登录页品牌化 | Linear / Plane / PostHog 的登录页与品牌氛围 |
| 信息密度 | 任务表格 12 列，长文本靠省略 | 可折叠详情行、行内抽屉、列定制（Tabler / Ant Design） |

> 注意：Botler 的 Geist 基底本身是「极简、发丝线、单主色」路线，与多数
> 现代 DevOps 工具一致，**不需要推翻重做**，重点是「关键信息可视化 +
> 信息密度 + 微交互 + 品牌感」四方面的增量优化。

## 3. 同类开源项目调研

### 3.1 一览表

| 项目 | 定位 | Star（约） | License | 与 Botler 的相似点 |
|---|---|---|---|---|
| GitLab（Pajamas） | DevOps 平台 | —（官方开源） | CE MIT / EE 专有 | 同为 GitLab 生态，issue/流水线 UI 同构 |
| Gitea | 自托管 Git 平台 | 57.4k | MIT | 自托管、列表/表格密集的管理台 |
| Forgejo | Gitea 分支 | 5.3k（Codeberg） | MIT | 同上 |
| Woodpecker CI | CI/CD 引擎 | 7.7k | Apache-2.0 | 流水线/构建列表 UI |
| Huginn | 自动化 Agent 平台 | 49.8k | MIT | 「机器人替你干活」产品形态 |
| n8n | 工作流自动化 | 200.8k | fair-code | 任务执行可视化、暗色 UI |
| Renovate | 依赖更新 Bot | 22.3k | AGPL-3.0 | Bot 处理 issue/PR 的模式 |
| changedetection.io | 网页变化监控 | 33.2k | Apache-2.0 | 监控 + 通知 + 看板 |
| Healthchecks | 定时任务监控 | 10.2k | BSD-3-Clause | 后台任务状态看板 |
| Uptime Kuma | 自托管监控 | 90.2k | MIT | 状态卡片、暗色主题 |
| Grafana | 可观测仪表盘 | 76.3k | AGPL-3.0 | 看板布局、状态面板 |
| Homepage | 自托管导航页 | 32.0k | GPL-3.0 | 仪表盘卡片布局 |
| Plane | 项目管理 | 56.0k | AGPL-3.0 | 现代工作台布局、空状态 |
| Huly | 项目管理/协作 | 27.4k | EPL-2.0 | Linear 风格工作台 |
| PostHog | 产品分析 | 37.7k | MIT（核心） | 数据看板、事件列表 |
| Paperless-ngx | 文档管理 | 44.3k | GPL-3.0 | 列表 + 详情抽屉 + 空状态 |
| Appsmith | 内部工具平台 | 40.7k | Apache-2.0 | 管理面板/表格/表单范式 |
| Refine | React 后台框架 | 35.5k | MIT | 后台 CRUD 页面范式 |
| Chatwoot | 客服平台 | 35.9k | MIT（核心） | 会话列表 + 详情双栏 |
| Novu | 通知基础设施 | 39.6k | MIT（核心） | 工作流可视化 |
| Windmill | 脚本转应用平台 | 17.6k | AGPL-3.0 | 脚本/任务管理台 |
| Trigger.dev | AI 任务平台 | 16.0k | Apache-2.0 | 任务运行监控台 |
| Activepieces | AI 工作流 | 23.8k | MIT（核心） | 自动化流程管理 |
| Immich | 自托管相册 | 110.7k | AGPL-3.0 | 现代感视觉（照片网格） |
| shadcn/ui | React 组件库 | 121.4k | MIT | 组件/令牌/可访问性标杆 |
| Ant Design | 企业级组件库 | 99.1k | MIT | 密集表格/表单/后台范式 |
| Tabler | Dashboard UI Kit | 41.5k | MIT | 后台管理组件套件 |
| CoreUI React | React 后台模板 | 4.9k | MIT | 可整体借鉴的后台模板 |

### 3.2 逐项详解与可借鉴点

#### A. Git / DevOps 平台（最直接同类）

**A1. GitLab（Pajamas 设计系统）** — https://gitlab.com/gitlab-org/gitlab
- 官方开源版（CE 采用 MIT 许可的开源内核，EE 为专有）；UI 设计系统名为
  **Pajamas**（https://design.gitlab.com ），是「GitLab 生态工具的界面」
  最直接的设计语言。
- 可借鉴点：
  1. **issue 列表/详情布局**：Botler 的 IssueDrawer 可对照 GitLab issue
     右边栏的「Assignees / Labels / 状态」信息架构；
  2. **流水线阶段图**：Botler 概览页已有 `st-*` 阶段节点（参考 GitLab
     CI/CD 阶段图），可进一步参考其「分组展开 + 失败步骤红点定位」交互；
  3. **徽章/标签语义**：GitLab 的标签色块、状态徽章是「状态即颜色」的
     范本，Botler 已基本对齐；
  4. **导航架构**：GitLab 顶部 tab + 左侧上下文导航的分层，适合功能变多
     时演进（Botler 目前单层顶栏）。

**A2. Gitea** — https://github.com/go-gitea/gitea（57.4k★，MIT，Go）
- 自托管 Git 平台，UI 以「内容优先、干净克制」著称，大量自托管运维工具
  的界面参照物。
- 可借鉴点：
  1. **列表密度与行内操作**：仓库/issue 列表的行 hover 操作、行内标签、
     右侧操作按钮的排布节奏；
  2. **导航层级**：顶栏 + 页面内 tab 的二级导航（如「代码 / Issue / PR /
     设置」），适合 Botler 任务页拆「全部 / 运行中 / 失败」等 tab 视图；
  3. **空状态**：Gitea 的空列表提示文案 + 引导按钮，朴素但不冷场。

**A3. Forgejo** — https://codeberg.org/forgejo/forgejo（Codeberg 5.3k★，MIT）
- Gitea 社区分支，UI 与 Gitea 同源，但更注重「非营利、去商业化」的
  克制风格。可借鉴点同 Gitea；可关注其浅色表格与发丝线边框的细节调校。

**A4. Woodpecker CI** — https://github.com/woodpecker-ci/woodpecker（7.7k★，Apache-2.0，Go）
- 轻量 CI/CD 引擎，流水线列表与运行详情 UI 简洁。
- 可借鉴点：**流水线/构建列表**的状态列配色、运行中动画（pulse/闪烁）、
  步骤耗时展示——与 Botler 概览页流水线板块同构，可直接对照优化。

#### B. 自动化 / Bot 平台（产品形态同类）

**B1. Huginn** — https://github.com/huginn/huginn（49.8k★，MIT，Ruby）
- 经典开源自动化 Agent 平台（「你的代理随时待命」），与 Botler「机器人
  替你处理 issue」产品形态最接近的鼻祖。
- 可借鉴点：
  1. **Agent/任务总览**：其仪表盘用卡片分组展示各 Agent 状态（运行/停用/
     最近运行），对应 Botler 概览页的开放 issue 分组（running / bot-done /
     bot-failed 等）；
  2. **「最近活动」时间线**：任务执行的实时动态流，Botler 任务详情页的
     日志/事件流可参考其时间线式排版。

**B2. n8n** — https://github.com/n8n-io/n8n（200.8k★，fair-code，TypeScript）
- 工作流自动化平台，**暗色主题与动效**是业界标杆之一。
- 可借鉴点：
  1. **暗色模式质感**：n8n 的暗色层次（深灰底 + 发丝线 + 高亮主色）比
     Botler 当前纯黑底暗色更「柔和」，可参考其暗色令牌取值；
  2. **执行日志面板**：工作流每次执行的输入/输出 JSON 查看器，对应
     Botler 任务详情页的日志区（可考虑结构化 JSON 折叠视图）；
  3. **运行状态动画**：执行中的脉冲/旋转指示，Botler 已有 spinner，
     可补充「运行中任务行」的呼吸高亮。

**B3. Renovate** — https://github.com/renovatebot/renovate（22.3k★，AGPL-3.0，TypeScript）
- 依赖更新 Bot（Mend 出品），以「Bot 自动开 PR/处理 issue」的方式工作，
  与 Botler 同一模式。
- 可借鉴点：**Bot 行为透明度**——Renovate 在每个 PR/issue 里写清
  「为什么改、影响什么、如何回滚」。对应到 Botler：任务详情页应突出
  「引擎 / 状态 / 重试原因」，避免用户看 log 猜。

**B4. changedetection.io** — https://github.com/dgtlmoon/changedetection.io（33.2k★，Apache-2.0，Python）
- 自托管网页变化监控，监控项 + 通知渠道 + 看板的组合与 Botler 的
  「仓库 + 任务 + 通知」结构类似。
- 可借鉴点：监控项卡片的信息密度（名称 + 状态 + 最近检查时间 + 快捷
  操作），Botler 概览页 issue 项可借鉴其「一眼看到状态 + 时间」的布局。

**B5. Healthchecks** — https://github.com/healthchecks/healthchecks（10.2k★，BSD-3-Clause，Python/Django）
- 定时任务（cron）监控服务，**界面极简**，是「后台任务状态看板」的
  极佳范例。
- 可借鉴点：状态圆点 + 名称 + 周期 + 最近成功时间的紧凑行布局；整体
  配色极克制（灰底 + 绿/红状态点），可对照 Botler 任务列表的视觉噪音。

**B6. Windmill** — https://github.com/windmill-labs/windmill（17.6k★，AGPL-3.0，Rust/TS）
- 把脚本变成 webhook/应用/工作流的平台，有成熟的脚本与任务管理台。
- 可借鉴点：**脚本/任务运行记录表**（触发方式、耗时、状态、日志入口）
  的列设计与筛选器——与 Botler 任务列表高度同构。

**B7. Trigger.dev** — https://github.com/triggerdotdev/trigger.dev（16.0k★，Apache-2.0，TS）
- AI 任务/工作流托管平台，任务运行监控 UI 现代。
- 可借鉴点：任务运行详情的时间线 + 步骤展开视图、运行列表的状态筛选
  tab——对应 Botler 任务列表的「按状态分组/筛选」。

**B8. Activepieces** — https://github.com/activepieces/activepieces（23.8k★，MIT 核心，TS）
- AI 工作流自动化平台。可借鉴点：流程列表卡片 + 运行历史侧栏，以及
  「流程（模版）」与 Botler 模版页的管理方式。

**B9. Novu** — https://github.com/novuhq/novu（39.6k★，MIT 核心，TS）
- 通知基础设施，含可视化工作流编辑器。可借鉴点：**通知/事件列表**的
  时间线排版与「已读/未读」状态视觉（Botler 通知中心可参考）。

#### C. 监控 / 运维仪表盘

**C1. Uptime Kuma** — https://github.com/louislam/uptime-kuma（90.2k★，MIT，JavaScript）
- 自托管状态监控，「fancy」是其卖点——**状态卡片 + 暗色主题**非常出色。
- 可借鉴点：
  1. **状态卡片网格**：每个监控项一张卡片（名称 + 状态圆点 + 响应时间 +
     最近切换），比 Botler 概览页的列表式 issue 项更有「仪表盘感」；
  2. **暗色主题**：Uptime Kuma 的暗色是「深灰蓝」而非纯黑，层次柔和，
     可作为 Botler 暗色令牌的调校参考；
  3. **状态徽章动画**：down/up 切换时的动效反馈。

**C2. Grafana** — https://github.com/grafana/grafana（76.3k★，AGPL-3.0，TS）
- 可观测仪表盘标杆。可借鉴点：
  1. **看板布局**：面板网格 + 拖拽调整，Botler 概览页可考虑「面板化」
     （仓库概览、流水线、开放 issue 各占一格）；
  2. **状态面板**：数字 + 颜色阈值（绿/黄/红）的 stat 面板，适合
     「待处理 issue 数 / 失败任务数」等关键数字的可视化；
  3. **时间与刷新的可感知性**：面板角落的「更新时间」，Botler 概览页
     各板块轮询间隔不同，可明示刷新时间。

**C3. Homepage** — https://github.com/gethomepage/homepage（32.0k★，GPL-3.0，JS）
- 自托管导航/应用仪表盘。可借鉴点：卡片网格 + 图标 + 状态点的小组件
  布局，适合 Botler 仓库列表的卡片化呈现。

#### D. 项目管理 / 后台管理（视觉与布局参考）

**D1. Plane** — https://github.com/makeplane/plane（56.0k★，AGPL-3.0，TS）
- 开源 Linear/Jira 替代品，**视觉现代度在开源项目管理里数一数二**。
- 可借鉴点：
  1. **工作台侧边栏**：左侧主导航 + 顶部面包屑 + 内容三栏（列表/详情/
     属性），Botler 任务详情可参考其「列表 + 抽屉」的信息架构；
  2. **空状态设计**：Plane 的空状态插图 + 引导文案 + CTA，值得整体
     学习；
  3. **Loading 骨架屏**：列表加载用骨架而非 spinner，Botler 概览页
     轮询加载可参考。

**D2. Huly** — https://github.com/hcengineering/platform（27.4k★，EPL-2.0，TS）
- 开源 Linear 替代品（Huly）。可借鉴点：**极简三栏工作台**（左侧导航 /
  中间列表 / 右侧详情）与键盘导航、快捷键——Botler 的 IssueDrawer 与
  任务列表可借鉴其「列表-详情」联动与焦点管理。

**D3. PostHog** — https://github.com/PostHog/posthog（37.7k★，MIT 核心，Python/TS）
- 产品分析平台，UI 清新现代。可借鉴点：
  1. **数据看板**：卡片 + 图表 + 时间范围选择器的组合；
  2. **事件/列表页**：过滤条件行 + 表格 + 分页的组合，Botler 任务页
     可参考其「过滤器 + 表格」的排布；
  3. **颜色运用**：PostHog 用色活泼但克制，可给 Botler 徽章体系提供
     配色灵感。

**D4. Paperless-ngx** — https://github.com/paperless-ngx/paperless-ngx（44.3k★，GPL-3.0，Python）
- 自托管文档管理，**列表 + 详情抽屉 + 标签/过滤**的 UI 完成度很高。
- 可借鉴点：
  1. **卡片式文档列表**：缩略图 + 元数据 + 标签，可参考其卡片信息密度；
  2. **过滤侧栏**：按标签/类型/状态的过滤侧栏，Botler 任务页可加
     「按仓库 / 按状态 / 按引擎」过滤；
  3. **空状态与首次使用引导**：新用户引导做得好。

**D5. Appsmith** — https://github.com/appsmithorg/appsmith（40.7k★，Apache-2.0，TS）
- 内部工具/管理面板平台，内置大量表格、表单、图表组件范式。
- 可借鉴点：**数据表格范式**——列排序、过滤、行操作菜单、批量选择、
  分页的组合，Botler 任务表格（12 列）可逐项对照。

**D6. Refine** — https://github.com/refinedev/refine（35.5k★，MIT，TS）
- React 后台框架，其示例后台的**列表/编辑/创建三件套**布局是 React 后台
  的通用范式，可参考其「资源化」的页面组织（列表页 + 抽屉编辑 + 删除
  确认）。

**D7. Chatwoot** — https://github.com/chatwoot/chatwoot（35.9k★，MIT 核心，Ruby）
- 开源客服平台。可借鉴点：**会话列表（左） + 会话详情（右）双栏**布局，
  与 Botler 任务列表 + IssueDrawer 的交互一致，可参考其双栏的宽度节奏
  与焦点管理。

**D8. Immich** — https://github.com/immich-app/immich（110.7k★，AGPL-3.0，TS）
- 自托管相册，UI 现代感强。可借鉴点：**深色主题质感**与**网格/卡片
  布局**的现代审美（可作为整体视觉年轻化的参考）。

#### E. UI 组件库 / 设计系统（实现层参考）

**E1. shadcn/ui** — https://github.com/shadcn-ui/ui（121.4k★，MIT，TS）
- 现代 React 组件的「事实标准」之一：复制粘贴式组件 + Radix 可访问性
  原语 + Tailwind 令牌。
- 可借鉴点（不要求引入 Tailwind，吸收其设计语言即可）：
  1. **组件形态**：Dialog、DropdownMenu、Popover、Toast、Command（命令
     面板）的细节；Botler 目前无下拉菜单/弹出气泡，可补充；
  2. **焦点与键盘导航**：Radix 级的键盘支持；
  3. **令牌体系**：其颜色/圆角/阴影的分层思路，可对照现有 Geist 令牌。

**E2. Ant Design** — https://github.com/ant-design/ant-design（99.1k★，MIT，TS）
- 企业级 React 组件库。可借鉴点：**ProTable 类密集表格**（列配置、
  过滤、批量操作）、**Form 布局**、**Result 空态**、**Descriptions 详情
  描述**——Botler 任务页/设置页可对标。

**E3. Tabler** — https://github.com/tabler/tabler（41.5k★，MIT）
- 免费 Dashboard UI Kit（Bootstrap）。可借鉴点：后台管理页面全套组件
  （卡片、统计卡、表格、导航）的**成熟组合模板**，可当作 Botler 页面
  改版的「积木清单」。

**E4. CoreUI Free React Admin Template** — https://github.com/coreui/coreui-free-react-admin-template（4.9k★，MIT）
- 免费 React 后台模板，含侧边栏布局、表格、图表页。可借鉴点：**后台
  模板的整体骨架**（侧边导航 + 顶栏 + 内容区），若 Botler 未来导航项
  变多，可参考其侧边栏布局。

**E5. Geist（Vercel 设计系统）** — https://vercel.com/geist
- Botler 当前设计基底。保持 Geist 路线，持续吸收其新令牌/组件（如
  AI 相关组件、命令面板）即可，**不建议换基底**。

**E6. GitLab Pajamas** — https://design.gitlab.com
- GitLab 官方设计系统，含组件、图标、令牌规范；作为「GitLab 生态工具」
  的界面规范，优先级最高。

## 4. 设计原则提炼（对 Botler 的建议）

综合上述项目，Botler 后续 UI 优化应守住四条原则：

1. **保持 Geist 极简基底，不做推翻式重设计**：现有发丝线、单主色、
   小圆角路线与主流 DevOps 工具一致，问题不在基底，而在「信息呈现」；
2. **关键状态可视化优先**：把「运行中 / 失败 / bot-done」等从文字徽章
   升级为「状态点 + 颜色 + 数字」组合（参考 Uptime Kuma / Grafana /
   Healthchecks）；
3. **信息密度向列表/表格要效率**：任务页 12 列表格补列排序、过滤、
   行操作菜单、批量操作（参考 Appsmith / Ant Design / Gitea）；
4. **补足品牌感与细节**：登录页品牌化、空状态插图、骨架屏、hover 浮层、
   微动画（参考 Plane / Huly / shadcn/ui）。

## 5. 分页面改进建议

### 5.1 概览页（Overview）
- 顶部加一行「关键数字条」：开放 issue 数 / 运行中任务数 / 失败任务数 /
  流水线成功率，数字 + 语义色（参考 Grafana stat 面板、Uptime Kuma）；
- 开放 issue 分组标题旁加数量徽章（参考 Gitea issue 列表）；
- 流水线板块的失败步骤加红点定位 + 可点击展开日志（参考 GitLab 流水线
  详情）；
- 轮询数据加载改骨架屏（参考 Plane），并显示「上次刷新时间」。

### 5.2 任务页（Tasks）
- 表格补：列排序、状态/仓库/引擎过滤、行内操作菜单（重试/查看详情）、
  批量操作（重试选中失败任务）；
- 状态列从纯文字升级为「圆点 + 文字」；
- 长失败原因改为「展开行」或 hover 浮层（参考 Tabler / Appsmith）。

### 5.3 仓库页（Repos）
- 仓库列表卡片化（参考 Homepage / Gitea），每仓库展示「待处理 issue 数 /
  最近任务状态 / 流水线状态」摘要；
- 行内快捷操作（打开任务、编辑、暂停）hover 出现（参考 Gitea）。

### 5.4 设置页（Settings）
- 表单分区卡片化 + 分组标题（参考 Ant Design Form 布局）；
- 危险操作（重置/删除类）用二次确认对话框 + 危险色描边（已有 DialogHost，
  补视觉层级）；
- 版本信息/引擎状态等只读信息用 Descriptions 式排版（参考 Ant）。

### 5.5 任务详情 / IssueDrawer
- 详情字段按「执行引擎 / 状态 / 时间 / 重试」分组展示（参考 GitLab issue
  右侧栏）；
- 日志区支持「折叠 JSON / 只读错误 / 复制」工具条（参考 n8n 执行详情）；
- 双栏宽度与移动端抽屉动效（参考 Chatwoot / Huly）。

### 5.6 登录页（Login）
- 品牌化设计：Logo + 产品名 + 简洁说明 + 单卡片表单（参考 Linear /
  Plane / PostHog 登录页）。

## 6. 落地路线建议

本 issue 只产出调研结论。建议按模块拆成独立 issue 逐步实施（每个 issue
沿用 `ui` 标签，优先级建议）：

1. **概览页关键数字条 + 状态可视化**（工作量小、见效快）；
2. **任务页过滤/排序/批量操作**（中等，与现有表格测试交互较多）；
3. **空状态/骨架屏/登录页品牌化**（纯视觉，风险低）；
4. **暗色主题令牌调校**（对照 Uptime Kuma / n8n 的暗色层次）；
5. **组件补充**：DropdownMenu / Popover / Toast（对照 shadcn/ui），
   需先扩展 design-system.md 令牌。

每项改动仍须遵守 `docs/design-system.md`：令牌走 CSS 变量、不出现硬编码
色值、动效时长 150–300ms、对比度达 WCAG AA、补对应前端测试。

## 7. 数据说明与链接汇总

- Star / License 数据查询自 GitHub / Codeberg / GitLab 官方 API，截至
  2026-08-16，仅作量级参考；
- License 标注「核心」表示核心开源、部分企业功能另有条款（GitHub API
  返回 NOASSERTION 的以仓库 LICENSE 文件为准）；
- 优先参考顺序：**GitLab Pajamas（最直接） > Gitea（列表范式） >
  Uptime Kuma / Grafana（状态可视化） > Plane / Huly（现代视觉与空状态）
  > shadcn/ui / Ant Design（组件细节）**。

| 项目 | 主页 |
|---|---|
| GitLab / Pajamas | https://gitlab.com/gitlab-org/gitlab / https://design.gitlab.com |
| Gitea | https://github.com/go-gitea/gitea |
| Forgejo | https://codeberg.org/forgejo/forgejo |
| Woodpecker CI | https://github.com/woodpecker-ci/woodpecker |
| Huginn | https://github.com/huginn/huginn |
| n8n | https://github.com/n8n-io/n8n |
| Renovate | https://github.com/renovatebot/renovate |
| changedetection.io | https://github.com/dgtlmoon/changedetection.io |
| Healthchecks | https://github.com/healthchecks/healthchecks |
| Uptime Kuma | https://github.com/louislam/uptime-kuma |
| Grafana | https://github.com/grafana/grafana |
| Homepage | https://github.com/gethomepage/homepage |
| Plane | https://github.com/makeplane/plane |
| Huly | https://github.com/hcengineering/platform |
| PostHog | https://github.com/PostHog/posthog |
| Paperless-ngx | https://github.com/paperless-ngx/paperless-ngx |
| Appsmith | https://github.com/appsmithorg/appsmith |
| Refine | https://github.com/refinedev/refine |
| Chatwoot | https://github.com/chatwoot/chatwoot |
| Novu | https://github.com/novuhq/novu |
| Windmill | https://github.com/windmill-labs/windmill |
| Trigger.dev | https://github.com/triggerdotdev/trigger.dev |
| Activepieces | https://github.com/activepieces/activepieces |
| Immich | https://github.com/immich-app/immich |
| shadcn/ui | https://github.com/shadcn-ui/ui |
| Ant Design | https://github.com/ant-design/ant-design |
| Tabler | https://github.com/tabler/tabler |
| CoreUI React | https://github.com/coreui/coreui-free-react-admin-template |
| Geist | https://vercel.com/geist |
