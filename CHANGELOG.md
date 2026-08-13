# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### Added

- **概览页面**（issue #32）：导航栏新增「概览」tab（位于「仓库」tab 左边），实时展示
  正在执行的任务（running + retrying）卡片：仓库名称、对应 issue（GitLab 链接）、
  agent 实时输出（每 3 秒轮询增量刷新、卡片内滚动跟随最新）。多任务以网格排布，
  最多 2 行 × 3 列（6 个卡片），超过 6 个保持 3 列自动换行、页面滚动；无任务时
  显示空状态。
  - 前端：新增 `frontend/src/pages/Overview.jsx`（`/overview` 路由 + 导航入口）；
    列表一次拉取活跃任务（多值 status 过滤），每个任务独立轮询
    `/api/tasks/{id}/execution?after_byte=` 增量续读日志（`trimLogTail` 截尾防
    卡片无限增长）；`styles.css` 新增 `overview-grid` 3 列网格样式。
  - 后端：`GET /api/tasks` 的 `status` 参数支持逗号分隔多值（如
    `running,retrying`，单值行为不变）；任务列表/详情新增 `issue_url` 字段
    （后端按仓库 URL 拼接 `/issues/<iid>`，前端零拼接）。
  - 测试：后端 `tests/test_api_tasks.py` 新增 8 用例（多值过滤/边界 400/
    issue_url 契约）+ 前端 `tests/overview-page.test.mjs` 14 用例（导航顺序/
    数据流断言/trimLogTail 边界/组件渲染含字段缺失与 API 失败兜底）。
    后端全量 361 passed + 前端全量 83 passed + build 通过。

- **标记库页面**（issue #29 第二轮）：用户澄清需求为「在 botler 项目上增加一个标记库，
  用户可以手动添加删除，上面的建议清单作为默认选项不可删除」——第一轮只交付了
  `docs/labels.md` + 同步脚本（无 UI），本轮补齐 Web UI 管理页面。
  - 新增「标记库」页面（导航入口 + `/labels` 路由）：默认清单 13 个标签
    （类型 8 + 流程 5，与 `docs/labels.md` 一致）展示为内置选项、标记「默认」徽标
    **不可删除**；用户可添加/删除自定义标签（名称/颜色/说明，名称与默认清单或
    已有自定义标签重复、格式非法均被拒绝）。
  - 后端：新增 `botler/labels.py`（默认清单 + 校验）、`api/labels.py`
    （GET 列表 / POST 添加 / DELETE 删除，默认标签删除返回 400）；
    `config.py` 新增 `labels.custom` 段（`update_custom_labels` 写盘前重读磁盘，
    与其它 update_* 一致）。
  - 测试：后端 `tests/test_api_labels.py` 12 用例（含默认清单与
    `scripts/sync_labels.py` 一致性校验）+ 前端 `tests/labels-page.test.mjs` 7 用例
    （页面结构/接口路径/样式类静态断言）。后端全量 345 passed + 前端全量 60 passed
    + build 通过。

- **标记库规范**（issue #29）：统一 chenkaidi 名下全部仓库（botler / shipyard / patio /
  graph2plan / daymark / glimmer）的 issue 标签，解决命名不统一（shipyard 用 `ci`、
  daymark 用 `gitlab-ci`）与部分仓库缺标签的问题。
  - 新增 `docs/labels.md` 标记库规范：类型标签 8 个（bug / feature / optimize / ui /
    docs / test / gitlab-ci / chore）+ 流程标签 5 个（in-progress / review / blocked /
    bot-done / bot-failed），含颜色、说明与使用约定。
  - 新增 `scripts/sync_labels.py` 批量同步脚本：枚举命名空间下全部项目，创建缺失
    标签、更新已有标签的颜色/描述，**不删除任何旧标签**（旧 `ci` 等保留）。
  - 已通过脚本将 13 个标签同步至全部 6 个仓库（创建 48 / 更新 30，重跑幂等验证
    全部"不变"）。

- **Synology SSO 登录**（issue #27）：接入群晖 SSO Server（OIDC / OAuth2 授权码
  模式）作为管理界面登录身份源。设置页新增「Synology SSO 登录」卡片（Well-known
  URL / Application ID / Secret / Scope / 登录有效期 / 回调地址 / 证书校验开关），
  启用后访问 Web UI 需用群晖账号登录（未启用时保持开放访问），会话为签名 cookie
  （HMAC-SHA256，默认 7 天，密钥懒生成于 `backend/data/session_secret.key`，
  Docker 部署由 compose 挂载持久化）。
  - 后端：新增 `botler/auth.py`（`OidcClient` discovery/token/userinfo、
    `create_session`/`verify_session` 签名会话、`SsoGuardMiddleware` 保护
    `/api/*`——登录流程与健康检查除外，webhook 天然放行）；新增 `api/auth.py`
    （`/api/auth/status|login|callback|logout|me`）；`config.py` 新增 `sso` 段
    （白名单 + `update_sso`，掩码 secret 不覆盖）；`main.py` 挂载中间件与路由。
  - 前端：新增 `pages/Login.jsx` 登录页；`App.jsx` 启动探测登录状态、未登录渲染
    登录页、导航栏显示当前账号与退出；`Settings.jsx` 新增 SSO 配置卡片；
    `api.js` 401 兜底跳登录页。
  - 文档：README 新增「Synology SSO 登录」小节与配置表；
    新增 `docs/Synology-SSO-配置指南.md`（群晖侧创建 OIDC 应用步骤 + Botler 侧
    配置 + 常见问题）。
  - 测试：新增 `tests/test_auth.py` 19 个用例（OIDC 全流程 mock：登录 302 参数、
    state 防回放、换 token/userinfo、签名校验、篡改/过期 cookie、401 保护、
    webhook/health 放行）+ `test_api_settings.py` 追加 sso 段 6 个用例。
    后端全量 330 passed + 前端 46 passed + build 通过。

### Fixed

- **领取任务重复入队：缺少「最后发言人」判断、bot-done 依赖 Claude 手打**（issue #34）：
  bot 提问或处理完留评论后，用户仅重新指派（无新回复）触发 webhook，或平台重启
  后对账扫描，都会把「最后发言人是 bot、等用户回复」的 issue 重复领取入队；
  且成功任务的 bot-done 标签依赖 Claude 按模板自行打——Claude 忘打时 issue 无
  终态标签，也会被重复领取。
  - 后端：`gitlab_client.py` 新增 `last_note_author_id`（取最后一条非系统评论的
    作者 id，系统事件不算「发言」，无发言返回 None）；`webhook.py` 与
    `reconciler.py` 在终态标签过滤之后新增该判定——最后发言人是 bot 时不入队，
    用户回复后（或新任务无评论）才领取；`executor.py` 成功收尾时由平台代码直接
    打 bot-done（幂等，打标签失败仅记 warn 不阻塞任务成功），模板同步改为
    「平台自动打标签」。
  - 测试：`test_gitlab_client.py` 新增 5 用例（排除系统评论/无评论/无 author
    字段容错）、`test_webhook.py` 与 `test_reconciler.py` 各新增 3 用例（bot 最后
    发言拒绝/用户最后发言放行/无评论放行）、`test_executor.py` 新增 3 用例
    （成功打 bot-done/打标签失败仍成功/条件终态跳过不打），并更新成功判定用例
    断言（成功路径由代码打 bot-done）。后端全量 375 passed + 前端全量 87 passed。

- **任务页面「操作」列表头与「执行」按钮左边缘不在同一竖线**（issue #33）：
  操作列单元格内容「执行」链接复用了 `.btn-mini`，而该 class 带
  `margin-left: 8px`（原为「详情」按钮与前置失败原因文字留间距而设）——
  表头文字与单元格内容都从相同的左右 padding（10px）处开始排布，
  「执行」按钮的 8px 左外边距使其左边缘比「操作」文字右移 8px，
  两者不在同一竖线上。
  - 修复：`.btn-mini` 移除 `margin-left`；「详情」按钮改用新增的
    `.btn-gap-left { margin-left: 8px }` 显式声明与前置文字的间距
    （通用 class 不再携带上下文相关的左外边距）。
  - 涉及 `frontend/src/styles.css`、`frontend/src/pages/Tasks.jsx`；新增复现
    测试 `tests/tasks-action-col-align.test.mjs` 3 用例（含「详情」按钮间距
    守卫断言，修复前「执行按钮左外边距合计 8px ≠ 0」稳定失败）。
    前端全量 87 passed + 后端全量 361 passed（零改动）。

- **测试通知按钮多次点击只有第一次弹出系统通知**（issue #21 第四轮）：
  设置页「弹出测试通知」使用固定 `tag: 'botler-test'` 构造通知——浏览器通知
  中心对相同 tag 的通知做「替换」而非「新弹」：第一条通知还在屏幕上时，后续
  同 tag 通知只更新旧条目、不触发新的弹出动画，用户看到的就是「后续点击没有
  反应」。
  - 修复：`sendTestNotification()` 的 tag 改为每次唯一（模块级自增序号
    `botler-test-<n>`），每次点击都是独立通知、必然弹出；仍保留 tag 机制
    （同一瞬间重复点击不堆积）。
  - 涉及 `frontend/src/notify.js`；新增复现测试 `tests/notify.test.mjs`
    「连续多次点击每次都独立弹出」用例（mock 浏览器通知中心同 tag 替换语义，
    修复前失败 `1 !== 3`）。前端全量 84 passed + 后端全量 361 passed（零改动）。

- **任务页面「尝试」列数值与「恢复」文字不在同一水平线上**（issue #31）：
  任务列表「尝试」列单元格内混排纯文本数值 `attempt_count`（继承 body 14px /
  line-height 1.6）与恢复任务的 `badge.resume`（inline-block，11px，padding
  1px 8px，border 1px）——`.badge` 默认 `vertical-align: baseline`，基线对齐下
  胶囊的 padding/border 下沉到基线以下、小字号视觉中心低于数值中心，两者不在
  同一水平线上。
  - 修复：`.badge.resume` 增加 `vertical-align: middle`（与 `h1 .badge` 的既有
    处理方式一致），胶囊与数值垂直居中对齐。
  - 涉及 `frontend/src/styles.css`；新增复现测试
    `tests/tasks-attempt-badge-align.test.mjs` 2 用例（修复前 1 个稳定失败）。
    前端全量 68 passed + 后端全量 353 passed。

- **任务页面「尝试」列数值与「恢复」文字不在同一水平线（第二轮）**（issue #31）：
  用户反馈第一轮修复后界面仍不正确——真实浏览器实测（Chromium 1280 视口）数字
  中心 y=78、badge 中心 y=102，垂直中心差 24px。根因是 `.tasks-table` 为
  `table-layout: fixed`，「尝试」列固定 68px（td padding 10px → 内容区仅 48px），
  放不下「数值（2 位约 16px）+ 空格（约 4px）+ margin-left 6px + badge
  （40px）」约 66px，`inline-block` badge 被整体折行到第二行——数字在上、恢复
  在下；`vertical-align` 只作用于同一行框，折行后第一轮修复完全无效。
  - 修复：「尝试」列 68px → 88px（内容区 68px ≥ 同行所需），badge 与数值同处
    一行，配合 `vertical-align: middle` 垂直居中对齐；修复后实测垂直中心差
    1.3px。固定列总宽 +20px 由第 4/8 弹性列（30%/22%）吸收，宽视口下无感。
  - 涉及 `frontend/src/styles.css`；复现测试
    `tests/tasks-attempt-badge-align.test.mjs` 新增「列宽足够容纳同行显示」
    静态计算用例（修复前失败，48px < 64px）。
    前端全量 69 passed + 后端全量 353 passed。

- **配置（启用）Synology SSO 后页面无限刷新**（issue #27 第五轮）：`App.jsx` 的
  「时区加载」effect 无条件请求 `/api/settings`（未像通知轮询那样加 auth 守卫）——
  SSO 启用后（配置即时生效）未登录访问该接口被 `SsoGuardMiddleware` 返回 401，
  `api.js` 的 401 兜底跳 `/login` 整页重载；重载后渲染登录页时该 effect 再次无条件
  发起 `/api/settings` → 又 401 → 又重载，**无限刷新循环**（页面停在登录页仍不停刷新）。
  - 修复（双保险）：① `App.jsx` 时区加载 effect 增加 auth 守卫（SSO 启用未登录时
    跳过，登录成功后 auth 变化重新触发加载），登录页不再发起任何受保护请求；
    ② `api.js` 401 兜底增加 SSO 启用判断（新增 `setSsoEnabled()` 由 App 从
    `/api/auth/status` 探测后设置）——仅 SSO 启用时未登录访问才跳登录页，非 SSO
    场景（或探测完成前）的 401 不跳转，杜绝类似循环。
  - 涉及 `frontend/src/App.jsx`、`frontend/src/api.js`。
  - 测试：新增 `tests/app-sso-refresh-loop.test.mjs`（模拟后端 SSO 启用未登录 +
    其余 API 401，断言登录页不得发起受保护请求/不得触发跳转；修复前稳定复现
    `/api/settings` 请求）。前端全量 66 passed + build 通过 + 后端全量 353 passed。

- **设置页 SSO 配置卡片无保存按钮**（issue #27 第四轮）：Synology SSO 登录卡片
  （设置页第一个卡片）内只有表单字段，全局「保存」按钮位于下方「任务调度」卡片中
  ——用户首屏只看到 SSO 卡片，找不到保存按钮，误以为无法保存配置。
  - 修复：SSO 卡片内新增独立「保存 SSO 配置」按钮（只提交 `sso` 段，后端
    `PUT /api/settings` 支持部分更新，不影响其他设置）；sso 段构建逻辑提取为
    `buildSsoPatch()` 供全局 save 与卡片内 saveSso 共用（client_secret 留空 =
    保持现有凭据）；卡片说明文字改为指向卡片内按钮。
  - 涉及 `frontend/src/pages/Settings.jsx`；新增前端测试
    `tests/settings-sso-save-button.test.mjs` 5 用例（修复前 4 个稳定失败）。

- **agent 重复处理已打 bot-done / bot-failed 的 issue**（issue #30）：对账兜底
  （reconciler）与 webhook 入队只按「assignee 是 bot + issue 未关闭」判定，不检查
  标签——平台重启、任务表清理或手动「对账」后，已完成（bot-done，等用户确认关闭）
  的 issue 会被重复入队重新开发；处理失败（bot-failed，需人工介入）的 issue 因失败
  任务不再活跃，会被再次领取→再失败→再入队，**无限重试循环**。
  - 修复：两条入队路径统一过滤终态标签——`reconciler.reconcile_once` 跳过带
    bot-done / bot-failed 标签的 issue（新增模块级常量 `TERMINAL_LABELS`）；
    `webhook` 入队前一律以 API 最新状态确认 assignee 与标签（事件快照 labels 格式
    不可靠，快照干净但 API 已打 bot-done 时同样拒绝），打 bot-done / bot-failed 的
    issue 返回「跳过」不创建任务。
  - 涉及 `backend/botler/reconciler.py`、`backend/botler/webhook.py`。
  - 测试：新增 `tests/test_reconciler.py`（4 用例：bot-done / bot-failed 跳过、
    混合队列只入队干净 issue、普通 issue 回归）+ `tests/test_webhook.py`
    （4 用例：bot-done / bot-failed 拒绝、快照标签不可靠以 API 为准、干净 issue
    回归）。修复前 6 个过滤用例稳定失败。后端全量 353 passed + 前端全量 60 passed。

- **线上前端静默回退旧版，标记库页面「看不到」**（issue #29 第三轮）：`sync_to_github`
  job 未声明 `dependencies`，runner 默认下载前序 stage 中 frontend:build 的 dist
  artifacts 并解压到构建目录，而 pm2 进程（FastAPI 静态托管）的工作目录就在构建目录。
  旧 pipeline 的 sync job 若延迟排队运行（实测 #674 的 sync 在 #675 部署成功 4 分钟后
  才执行，把 #675 部署的新版 dist/index.html 覆盖回 #674 旧版），线上前端即被静默
  降级——CI 显示 success、后端 API 正常，但用户看不到新功能页面。
  - 修复：`sync_to_github` 添加 `dependencies: []`（该 job 用完整克隆到 /tmp 工作，
    不需要任何构建产物），杜绝旧 artifacts 解压覆盖 dist。
  - 涉及 `.gitlab-ci.yml`；本修复推送触发的新流水线将重新部署新版 dist 恢复线上。

- **任务完成后 issue 被自动关闭，模版库「不关闭」规范不生效**（issue #25 第二轮）：
  运行进程已加载新版全局模版（含「任务完成后不关闭 issue，打 bot-done 等用户确认」），
  但 `executor.run_task` 的成功判定仍是「issue 已被关闭」——exit 0 且 issue 仍打开时
  判失败并重试（最多 2 次），**迫使 Claude 在完成任务后违规调用 API 关闭 issue**
  （生产日志 task_30/31：issue #28 完成开发即被关闭，与用户 20:37 反馈的现象吻合）。
  - 修复：成功判定改为「Claude 正常完成（exit 0 且输出为 JSON result，非『无法解决』
    报告）」即成功，**issue 是否关闭不再参与判定**；「无法解决」仍为失败终态不重试。
  - 同步更新 `config.py` 的 `DEFAULT_TEMPLATE` 兜底模版：删除「调用 API 关闭 issue」
    指令，改为「留结果评论 + 打 bot-done，关闭留给用户确认后手动执行」。
  - 涉及 `backend/botler/executor.py`、`backend/botler/config.py`、
    `backend/botler/gitlab_client.py`（docstring）。
  - 测试：新增 `TestRunTaskSuccessCriteria` 3 用例（修复前 `test_success_when_issue_
    stays_open` 稳定失败：exit 0 + 完成输出 + issue 未关闭 → 旧逻辑重试耗尽判失败）。
    后端全量 333 passed + 前端全量通过。

- **设置页找不到 Synology SSO 配置入口**（issue #27 第三轮）：SSO 配置卡片位于设置页
  第 4 位（实测 1440×1000 视口下在第 1267px 处），被 577px 高的「网页通知」卡片压到
  首屏（1000px）之下，用户打开设置页首屏只能看到「任务调度/界面显示/网页通知」三个
  卡片，误以为没有 SSO 配置功能。
  - 修复：将「Synology SSO 登录」卡片移到设置页**顶部第一位**（首屏顶部直接可见，
    top 76px），其余卡片顺序不变。
  - 涉及 `frontend/src/pages/Settings.jsx`。
  - 测试：新增 `tests/settings-sso-card-order.test.mjs`（断言 SSO 卡片是设置页第一个
    卡片、位于「任务调度/网页通知」之前；修复前该测试稳定失败）+ chromium 无头实测
    SSO 卡片首屏可见。前端全量 53 passed + 后端全量 330 passed + build 通过。

- **任务列表页面宽度缩小时数据行内容超出白色底**（issue #28）：任务列表为 12 列表格，
  各列内容最小宽度之和约 1306px，窄视口下 `.table` 的 min-content 宽度超过 `.card`
  容器可用宽度，表格直接撑破白色卡片（浏览器实测 1400px 视口溢出 266px，760px 视口
  超出视口 586px），`.card` 无 overflow 处理导致行内容画出卡片边缘。
  - 修复：表格外包一层 `.table-wrap` 滚动容器（`overflow-x: auto`），窄视口下表格在
    容器内横向滚动查看全部列，不再溢出卡片、页面不再整体横向滚动。
  - 涉及 `frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`。
  - 测试：新增 `tests/table-wrap.test.mjs`（断言 `.table-wrap` 滚动规则存在 + 任务
    表格被包裹；修复前该测试稳定失败）+ chromium 无头实测 1400/1100/900/760 四档
    视口全部无溢出。前端全量 49 passed + 后端全量 330 passed + build 通过。

- **页面宽度足够时任务列表仍出现水平滚动条**（issue #28 第二轮）：`.table` 为
  `table-layout: auto`，实际宽度被内容撑到 min-content（实测 1628px——≥1600px
  视口下标题/失败原因两个 ellipsis 列各占 520px，其余 10 列共约 625px），而
  `.content` 的 `--content-width` 封顶 1600px，`.table-wrap` 可用宽度最多 1520px，
  任何桌面视口都装不下表格，滚动条必然出现（浏览器实测 1920px 视口溢出 108px、
  1750px 溢出 348px，页面两侧却仍有空白）。
  - 修复：任务表格启用 `table-layout: fixed` 固定布局（专用类
  `.table.tasks-table`）+ 12 列显式分配宽度（固定列合计 800px，标题/失败原因列为
  弹性 30%/22%），表格宽度恒等于容器宽度——宽视口下撑满容器不再滚动，窄视口列宽随
  容器压缩、长文本由 ellipsis 列截断（仓库/提交列同步补上 ellipsis + title），
  760px 以下极小视口保留容器内横向滚动兜底。
  - 涉及 `frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`。
  - 测试：`tests/table-wrap.test.mjs` 追加 2 个断言（`.table.tasks-table` 必须含
    `table-layout: fixed` + 12 条 `th:nth-child(n)` 列宽规则；修复前稳定失败）。
    chromium 无头实测 1920/1750/1600/1500/1366/1100/900 七档视口滚动条全部消失
    （溢出 0），760px 保持滚动。前端全量 51 passed + 后端全量 330 passed + build
    通过。

- **启用 SSO 后 Web UI 打开白屏**（issue #27 第二轮）：`App.jsx` 中网页通知轮询的
  `useEffect` 被声明在两个条件 `return`（auth 检测中、SSO 启用未登录）之后——auth
  加载前后组件执行的 hooks 数量不一致（4 → 5），触发 React error #310
  「Rendered more hooks than during the previous render」，整棵组件树崩溃白屏
  （生产构建下即控制台的 Minified React error #310）。
  - 修复：通知轮询 effect 移到所有条件 `return` 之前声明（hooks 数量恒定），effect
    内部按 auth 状态跳过启动——auth 未就绪或 SSO 启用未登录时不轮询（同时避免登录页
    每 10s 轮询受保护接口 401 反复刷新）。
  - 涉及 `frontend/src/App.jsx`。
  - 测试：新增 `tests/app-hooks.test.mjs`（vite SSR 转译 + react-test-renderer 渲染
    App，驱动 auth 状态流转，断言主界面正常出现；修复前该测试稳定复现 #310）。
    新增 devDependency `react-test-renderer@18.3.1`。前端全量 47 passed + 后端
    全量 330 passed + build 通过。

- **手动编辑 config.yaml 后模板不生效 / 被 UI 保存覆盖**（issue #25）：`ConfigManager`
  只在进程启动时加载一次 config.yaml 并缓存——用户直接编辑 config.yaml（文档约定它
  是唯一事实来源）修改全局模板后，运行中的 botler 仍用旧模板渲染（"修改了全局模版，
  但是没有生效"）；更糟的是之后在 Web UI 保存任意设置，`save()` 用内存旧值整体覆盖
  写回 config.yaml，手动修改被静默丢弃。
  - 修复：`get()` 增加磁盘 mtime 检测，config.yaml 被外部修改后自动重载（无需重启、
  无需再走 Web UI）；所有 `update_*` 写盘前重新读取磁盘、以磁盘最新内容为基底，UI
  保存不再覆盖手动编辑；`update_worker`/`update_claude`/`update_default_template`/
  `update_repos` 此前未先加载 `_data` 就写盘（未 load 时保存会写坏整个配置），统一
  加 `_reload_from_disk()` 保护；磁盘文件缺失/损坏时降级保留当前配置不中断。
  - 涉及 `backend/botler/config.py`（`_loaded_mtime`、`_reload_from_disk`、`get()`
    mtime 检测、全部 `update_*`）。
  - 测试：新增 `tests/test_config_reload.py` 6 个用例（手动编辑后 `get()` 自动生效、
    连续 `get()` 稳定、UI 保存保留手动编辑、UI 保存模板本身回归、损坏/缺失文件降级）。
    后端全量 307 passed + 前端 46 passed + build 通过。

### Added

- **添加 MIT License**（issue #26）：新增 `LICENSE` 文件（MIT 协议，© 2026
  chenkaidi），README 末尾补充 License 小节。纯文档改动，不涉及代码。
- **任务页面显示完成 issue 所用时长**（issue #23）：任务列表新增「用时」列、
  任务详情新增「执行用时」行——从任务开始执行（`started_at`，未开始则退回
  创建时间）到完成（`finished_at`）的耗时，自动换算为 秒/分钟/小时/天；
  未完成或时间缺失显示占位符。纯前端实现：`api.js` 新增 `fmtDuration()`
  （与 `fmtTime` 同规则解析后端 UTC 时间串，非法日期/结束早于开始返回 null）。
  - 涉及 `frontend/src/api.js`（新增 `fmtDuration`）、`pages/Tasks.jsx`
    （新增「用时」列）、`pages/TaskDetail.jsx`（新增「执行用时」行）。
  - 测试：前端新增 11 个用例（秒/分钟/小时/天换算、取整、0 秒、缺字段、
    非法日期、时钟异常负值）。前端全量 46 passed + 后端 289 passed
    （零改动）+ build 通过。
- **设置页增加本地环境检测功能**（issue #22）：新增「本地环境检测」卡片，检测
  botler 服务器上常见 AI agent（claude/codex/gemini/aider/gh）与基础工具
  （git/docker/node/npm/python3/uv）是否安装、已装版本与最新版本；最新版本
  已安装且带发布源的工具并发查询（npm registry / GitHub API，网络不可达显示
  "—"），版本落后高亮提示「可升级」。进入设置页自动检测，可点「重新检测」刷新。
  - 涉及 `backend/botler/environment.py`（新增：工具清单 + which/`--version`
    版本解析 + 最新版本查询，线程池并发 + 整体超时，全部容错）、
    `backend/botler/api/environment.py`（新增 `GET /api/environment`）、
    `backend/botler/api/__init__.py`（注册路由）、`frontend/src/pages/
    Settings.jsx`（新增「本地环境检测」卡片）、`README.md`（API 一览）。
  - 测试：后端新增 28 个用例（版本解析 10 个：v 前缀/文本前缀/多行/无版本/
    空值/4 段取前三；工具检测 7 个：未安装/版本读取失败/超时/异常；最新版本
    查询 8 个：npm/GitHub 正常、404/网络异常/非法 JSON/缺字段/无发布源/未知
    源；整体检测 3 个：全流程/版本一致/网络失败）。后端全量 289 passed +
    前端 30 passed + build 通过。
- **设置页「弹出测试通知」按钮**（issue #21 增量）：通知卡片新增「测试通知」行，
  点击立即弹出一条浏览器系统通知（标题「Botler 测试通知」），用于验证网页通知
  功能是否正常。纯前端实现：`notify.js` 新增 `sendTestNotification()`——绕过设置
  开关直接弹（用户主动点击即验证通知能力本身），权限未决（default）时先请求授权，
  已拒绝/浏览器不支持/弹窗异常时返回原因，设置页按钮旁给出成功或失败提示。
  - 涉及 `frontend/src/notify.js`（新增 `sendTestNotification`）、
    `Settings.jsx`（通知卡片新增测试按钮与结果提示）、`styles.css`（`err-hint`）。
  - 测试：前端新增 6 个用例（已授权直接弹、default 先请求授权、授权被拒、
    已拒绝、浏览器不支持、构造异常），前端全量 30 passed + 后端 261 passed
    （后端零改动）+ build 通过。
- **网页端通知功能**（issue #21）：通过浏览器 Notification API 在用户电脑上弹
  系统通知，通知时机可在设置页「网页通知」卡片配置（总开关 + 4 个时机开关，
  存 `config.yaml notifications` 段，改设置立即生效无需重启）：
  ① 任务需要交互（任务失败，需人工介入）② issue 完成（任务成功）③ issue
  列表为空（对账扫描无待处理 issue）④ 无 issue 可处理（有 issue 但均在处理中）。
  后端新增 `notification_events` 表 + `GET /api/notifications/events` 增量拉取
  接口（游标 after，任务类事件以 task_id 唯一幂等，队列类事件同仓库同类型
  1 小时内节流去重）；前端 `notify.js` 全局轮询（10s）按设置过滤弹通知，
  首次拉取只记游标不弹历史事件。
  - 涉及 `backend/botler/notifier.py`（新增事件记录器）、`database.py`
    （事件表 + 迁移 + 查询）、`executor.py`（成功/失败收尾产生事件）、
    `reconciler.py`（对账扫描产生队列事件）、`api/notifications.py`（新增
    接口）、`api/settings.py` + `config.py`（notifications 段读写与布尔校验）、
    `frontend/src/notify.js`（新增：过滤/弹窗/轮询）、`App.jsx`（挂载轮询）、
    `Settings.jsx`（通知卡片 + 授权按钮）、`styles.css`。
  - 测试：后端新增 27 个用例（事件落库/游标/幂等/节流、executor 收尾事件、
    对账队列事件、notifications API、设置段校验），前端新增 12 个用例
    （开关过滤/未知类型/轮询游标/失败容错）。后端全量 261 passed + 前端
    24 passed + build 通过。
- **任务页面「查看任务执行」按钮，实时查看 agent 进度与聊天记录**（issue #20）：
  任务列表每行新增「操作」列「执行」按钮，点击跳转详情页实时面板——聊天记录
  （Claude Code 会话中的用户/助手文本、工具调用与结果，气泡式展示）每 3 秒
  自动刷新；实时输出（claude stdout 日志流）按字节偏移增量续读、自动滚底，
  任务运行中即可查看（session_id 在读循环里首次出现即提前落库，此前只在
  执行结束后才落库）。
  - 涉及 `backend/botler/executor.py`（新增 `find_session_file` /
    `parse_transcript` / `read_log_delta` 与 `_persist_session_from_chunk`
    读循环接入：运行中落库 session_id）、`backend/botler/api/tasks.py`
    （新增 `GET /api/tasks/{id}/execution`：`log_delta`/`log_offset` 增量
    日志 + `transcript` 聊天消息 + `session_id`）、`frontend/src/pages/
    Tasks.jsx`（「执行」按钮）、`TaskDetail.jsx`（实时执行面板：聊天气泡
    + 实时输出，终态/出错自动停止轮询）、`api.js`（`summarizeToolInput`
    工具调用摘要）、`styles.css`（聊天面板样式）。
  - 测试：后端新增 25 个用例（`parse_transcript` 解析/边界 7 个、
    `read_log_delta` 增量/半行回退/对齐 6 个、`find_session_file` 2 个、
    运行中落库 4 个、`/execution` API 契约 6 个），前端新增
    `summarizeToolInput` 6 个用例。全量 234 passed + 前端 12 passed +
    build 通过。
- **任务页面展示对应任务提交的链接**（issue #19）：任务成功时 executor 用 GitLab
  commits API 按提交信息（模板固定含 `issue #N`）匹配 Claude 推送的提交，完整 sha
  落库 `tasks.commit_sha`；列表页新增「提交」列、详情页新增「提交」行，点击跳转
  GitLab 查看对应 commit（新标签页）。查询失败/找不到不阻塞任务成功（页面显示
  占位符）。历史任务不回填，无提交信息时显示 `—`。
  - 涉及 `backend/botler/gitlab_client.py`（新增 `find_commit_for_issue`：
    message 按 `issue #N` 大小写/空格不敏感、数字边界精确匹配）、
    `backend/botler/database.py`（`tasks.commit_sha` 列 + 旧库迁移）、
    `backend/botler/executor.py`（`_finish_succeeded` 成功后 `_record_commit`
    查询落库）、`backend/botler/api/tasks.py`（透出 `commit_sha` / `commit_url`，
    仓库 URL 去 `.git` 后缀拼接）、`frontend/src/pages/Tasks.jsx` /
    `TaskDetail.jsx`（链接展示，`shortSha` 短 sha 显示）。
  - 测试：后端新增 19 个用例（`find_commit_for_issue` 匹配/边界/异常 9 个、
    executor 落库/降级/端到端 4 个、API 字段契约 4 个、数据库迁移 2 个），
    前端新增 `shortSha` 4 个用例。全量 209 passed + 前端 6 passed。
- **仓库页「对账」按钮**（issue #17）：仓库列表每行右侧按钮区新增「对账」按钮，
  点击后立即扫描该仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues
  补入队，行内直接显示结果——发现 N 个待处理已入队 / 无需处理（并显示扫描的
  issue 数）。与设置页的全局异步对账（`/settings/reconcile-now`）互补：单仓库
  对账同步执行、立即返回结果，用于"马上查看是否有需要处理的 issue"。
  - 涉及 `backend/botler/reconciler.py`（`reconcile_once` 支持 `repo_id` 参数，
    单仓库扫描，GitLab 报错记入 `errors`）、`backend/botler/api/repos.py`
    （新增 `POST /api/repos/{id}/reconcile`：仓库不存在 404、停用仓库返回
    `note` 提示、GitLab 故障 502）、`frontend/src/pages/Repos.jsx`（「对账」
    按钮 + 行内结果展示）。
  - 测试：后端新增 `tests/test_api_reconcile.py` 9 个用例（正常入队、无待处理、
    已有活跃任务不重复入队、仓库不存在 404、停用仓库、GitLab 故障 502、
    单仓库只扫指定仓库、未知仓库空结果、全量扫描回归）。全量 167 passed。

### Fixed

- **issue 实际状态与页面不一致：任务卡在排队/运行**（issue #24）：平台同时存在
  两个 botler 实例（历史遗留 supervisord/uvicorn 实例 + pm2 实例）时，两个
  实例的调度器各自从内存队列领取任务、并发执行同一任务（同秒内「任务成功」
  与「重新执行」并存），互踩同一工作区导致 claude 挂起、任务卡在 running
  占住执行槽，后续任务永久排队——issue 实际已实现但任务页面仍显示排队。
  修复（状态流转原子化，多实例并存时只有首个实例生效）：
  - `backend/botler/database.py`：新增 `claim_task()`（条件 UPDATE
    queued/retrying → running，原子抢占，抢不到即跳过）与 `finish_task()`
    （仅 running/retrying 可流转终态，先完成者生效，慢实例不覆盖）；
    附加字段白名单抽为模块级 `_TASK_FIELDS`
  - `backend/botler/executor.py`：`run_task` 开头原子抢占（已被其他实例
    领取/已终态的任务直接跳过），`_finish_succeeded`/`_finish_failed`
    改为条件终态（收尾被抢先时不再覆盖状态、不重复评论/通知）
  - 测试：新增 `tests/test_task_status.py` 10 个用例（抢占成功/已 running
    抢不到/终态抢不到/双连接只一个赢、条件终态/慢实例不覆盖）+ executor
    并发跳过 2 个用例；既有测试按真实状态流转补齐 claim 前置。
    后端全量 301 passed（+12）。
  - 部署提示（运维层面，非代码）：请停掉历史遗留的 supervisord/uvicorn
    实例，只保留 pm2 实例；`--reload` 模式仅限开发。
- **设置页「弹出测试通知」按钮不弹授权对话框**（issue #21 第三轮）：点击
  测试按钮期望弹出浏览器授权对话框，但 http 访问或自签名证书不受信任时
  页面处于非安全上下文（`isSecureContext === false`），浏览器规范规定此时
  `Notification.permission` 恒为 `'denied'` 且 `requestPermission()` 永不弹框
  ——原实现只检查 `'Notification' in window`，无法区分「不支持」与「非安全
  上下文」，用户只看到笼统的「已拒绝」提示，误以为是 bug。修复：
  - `frontend/src/notify.js`：新增 `notifyFailureReason()`（browser-unsupported
    / insecure-context / denied 三级原因判定），`canNotify()` 增加
    `isSecureContext` 检查，`sendTestNotification()` 非安全上下文时不再
    白调 `requestPermission()` 并返回 `insecure-context` 原因
  - `Settings.jsx`：测试按钮与浏览器授权行区分提示——非安全上下文明确告知
    「需 HTTPS 且证书受信任」；已拒绝时引导「点击地址栏左侧图标将通知权限
    改为允许」。
  - 测试：前端新增 5 个用例（`notifyFailureReason` 四态 + 非安全上下文不
    请求授权不弹通知）。前端全量 35 passed + 后端 289 passed（零改动）+
    build 通过。
- **全局模板写死单仓库路径，任务收到错误指令**（issue #18 诊断发现）：用户
  `data/backend/config.yaml` 的全局模板采用跨会话 issue-agent 模式，但模板
  硬编码 `chenkaidi/shipyard`、`https://home.chenkaidi.top:509`、共享进度文件
  ——botler 对任意仓库执行任务时 Claude 收到「处理 shipyard 队列」指令，
  不处理当前指派的 issue（exit 0 但 issue 不关，任务反复失败）。修复：
  - `backend/botler/templates.py`：新增 `{project_path}`（group/repo，从仓库
    URL 提取）、`{project_path_encoded}`（URL 编码 chenkaidi%2Fbotler）、
    `{gitlab_host}`（host:port 去 scheme）占位符；`build_variables` 支持
    `repo_url` 参数（无 URL 时兜底用仓库名）；executor 渲染时传入仓库 URL。
  - `data/backend/config.yaml`：全局模板中硬编码的仓库路径 / GitLab 地址 /
    进度文件路径参数化为占位符（`~/gitlab_issue_agent/progress.md` →
    `~/{repo_name}_issue_agent/progress.md`，多仓库进度文件隔离）。
  - 测试：`tests/test_config_template.py` 新增 4 个用例（URL 提取、无 .git
    后缀、无 URL 兜底、参数化模板渲染无残留占位符/无 shipyard）。全量
    190 passed + 前端 2 passed。

- **部署后任务频繁 git fetch 失败（403 job token）**（issue #18 任务失败根因）：
  executor 的 git/claude 子进程环境直接继承 `os.environ`——CI 部署在
  gitlab-runner 构建目录里 `pm2 start`，作业环境（`CI_JOB_TOKEN` 等）被继承，
  且 `~/.gitconfig` 的 credential store（`~/.git-credentials`）中失效的
  `gitlab-ci-token` 条目排在有效 `oauth2` 之前。git 凭据顺序中 store 优先于
  `GIT_ASKPASS`：失效 job token 被选用 → GitLab 403
  （`Authentication by CI/CD job token not allowed from shipyard to project
  #123`）→ 403 不触发凭据重试 → askpass 的 bot token 永远用不上 →
  fetch/push 必失败 → 重试耗尽 → `bot-failed` 标签。修复：
  - `backend/botler/executor.py`：新增 `_clean_process_env`（剔除 `CI_*` /
    `GITLAB_CI` / `GIT_CONFIG_*` 环境变量）、`_git_global_config`（生成净化版
    全局 gitconfig——仅剥离 `[credential]` section，保留 `user` / `http` /
    `safe.directory` 等，避免 `/dev/null` 连带丢失 user.name 致 commit 失败、
    自签证书握手失败），`prepare_workspace` 与 `_build_env` 统一使用；claude
    子进程同样注入 `GIT_ASKPASS`（bot token）并禁用 credential store，
    push 凭据与 API 保持一致。
  - 测试：新增 `tests/test_executor_credentials.py` 3 个用例（本地 http 服务器
    + git http-backend 代理模拟 GitLab 认证时序：无凭据 401 → job token 403 /
    bot token 200；断言修复后 fetch 使用 askpass 的 bot token、旧行为对照
    复现 store 抢先用 job token、`_build_env` 净化）。全量 186 passed + 前端
    2 passed。

- **失败详细原因中转义符未格式化**（issue #16）：任务「详情」弹窗与详情页
  「claude 输出尾部」直接展示 claude JSON 输出的 `result` 字段——该字段内嵌
  工具调用记录（再次序列化的 JSON 文本），`\n` `\"` 等转义按字面量存放，
  展示时 `\n` 显示为反斜杠+n 而非换行，可读性差。修复：新增
  `format_display_line` / `_decode_escapes` 逐层解码（先严格 `json.loads`
  展开内嵌 JSON 结构，失败则宽松解码常见转义 `\n \r \t \\ \' \"` 等后递归），
  JSON 行重排时只保留 `type / subtype / session_id / exit_code / error`
  等排查字段，丢弃 `ttft_ms / uuid` 等机器噪音。
  - 涉及 `backend/botler/executor.py`（`_extract_error` 解码 result 后再提取
    trace、`_tail_output` 逐行格式化——失败评论与日志摘要同步受益）、
    `backend/botler/api/tasks.py`（详情页 `log_file_tail` 逐行格式化）。
  - 测试：`tests/test_executor.py` 新增 7 个用例（嵌套工具调用记录解码、混合
    文本 + JSON 片段、纯文本不误伤、JSON 行重排、非 JSON 行原样、`_tail_output`
    解码）、`tests/test_api_tasks.py` 新增 1 个用例（log_file_tail 解码且丢弃
    机器字段）。全量 183 passed + 前端 2 passed。

- **部署后任务一直运行失败**（permission_denials）：`data/backend/config.yaml`
  的 `templates.default` 曾被替换为 gitlab-issue-agent 提示词（跨会话领取队列），
  executor 渲染后 Claude 收到错误指令，不处理当前指派的 issue；且 `claude -p`
  无人值守执行未跳过权限确认，Bash/curl/Read/MCP 等一切操作被权限系统拒绝
  （task_7/8/9 日志 `permission_denials`），Claude 自行终止（exit 0）→ 重试耗尽 →
  任务失败。修复：
  - `backend/botler/executor.py`：claude 命令追加 `--dangerously-skip-permissions`
    （无人值守无法交互授权，权限拒绝 → 任务必然失败）；
    `_extract_session_id` / `_extract_error` 新增 `_load_json_output` 容错——
    claude 无 stdin 时 stderr 打印 `Warning: no stdin data received...` 混入
    stdout，整串 `json.loads` 失败导致 session_id 永不落库、断点续跑（--resume）
    失效，现从首个 `{` 起用 `JSONDecoder.raw_decode` 只取首个 JSON 对象。
  - `backend/config.example.yaml` + `data/backend/config.yaml`：恢复标准
    「处理当前 issue」模板；关闭 issue 的 curl 加 `-k`（自建 GitLab 为自签
    证书，不带 -k 时证书错误 → issue 永远关不上）。
  - 测试：后端新增 `tests/test_executor_runtime.py` 5 个用例（cmd 含
    --dangerously-skip-permissions 且 resume 时保留、stderr 前缀下 session_id/
    错误提取/落库容错）、`tests/test_config_template.py` 4 个用例（模板面向
    当前 issue、不含 issue-agent 特征、curl -k、渲染结果）。全量 176 passed +
    前端 2 passed。

- **任务时间时区与浏览器不一致**（issue #14）：后端 SQLite `datetime('now')` 存
  UTC（如 `2026-08-12 01:25:54`），前端 `fmtTime` 原样拼接 `' UTC'` 直接展示，
  浏览器在本机（UTC+8）看到的任务创建/开始/完成时间与执行日志时间戳比本机慢
  8 小时。修复：前端 `fmtTime` 把 UTC 字符串补 `Z` 解析为时刻后，用
  `Intl.DateTimeFormat` 按配置时区格式化（`YYYY-MM-DD HH:mm:ss`，`hourCycle: h23`
  避免午夜 24:00:00）；设置页新增「界面显示 → 显示时区」选项（`ui.timezone`，
  IANA 时区名，可下拉选择常用时区或手动输入，留空 = 跟随浏览器本机时区，即
  "默认和本机一致"），写回 config.yaml 全局持久化，保存后立即生效无需刷新。
  后端 `GET/PUT /api/settings` 支持 `ui` 段并用 `zoneinfo` 校验非法时区名（400）。
  - 涉及 `frontend/src/api.js`（fmtTime 重构 + `setDisplayTz`）、
    `frontend/src/App.jsx`（启动加载时区）、`frontend/src/pages/Settings.jsx`
    （时区输入 + datalist）、`frontend/package.json`（新增 `npm test`）、
    `frontend/tests/fmt-time.test.mjs`（新增，node:test）、
    `backend/botler/config.py`（`ui_timezone` 字段 + `update_ui`）、
    `backend/botler/api/settings.py`（`ui` 段读写 + 校验）、`.gitlab-ci.yml`
    （frontend:build 构建前跑 `npm test`）。
  - 测试：前端新增 2 个用例（Asia/Shanghai +8h 转换、空值占位符，修复前失败
    复现 bug）；后端新增 4 个用例（timezone 默认空、持久化写回 config.yaml、
    清空回退、非法时区名 400）。全量 158 passed + 前端 2 passed。

### Added

- **前端版本号与构建时间显示**（issue #9）：每次构建（CI/CD `frontend:build` 或
  本地 `npm run build`）自动自增版本号（patch 位 +1，如 1.0.0 → 1.0.1）并记录
  构建时间，前端导航栏右侧显示 `v1.0.1 · 2026-08-11 22:19` 徽标（悬停显示构建
  时间）。版本号持久化在 `data/version.txt`（数据目录跨构建/部署持久，构建目录
  被清理不丢）；`frontend/scripts/gen-version.mjs`（新增）在 vite 构建前自增版本
  号并生成 `frontend/public/version.json`（含 version + buildTime），vite 构建时
  自动复制进 dist/；前端新增 `VersionBadge` 组件 fetch 渲染，开发模式（无该文件）
  自动隐藏。
  - 涉及 `frontend/scripts/gen-version.mjs`（新增）、
    `frontend/src/components/VersionBadge.jsx`（新增）、`frontend/package.json`
    （build 前置 gen:version）、`frontend/src/App.jsx`（导航栏挂载徽标）、
    `frontend/src/styles.css`（`.version-badge` 样式）、`.gitlab-ci.yml`
    （frontend:build 注入 `BOTLER_DATA_DIR` 指向持久数据目录，构建后输出
    `dist/version.json` 验证）、`.gitignore`（忽略生成的 version.json）。

- **任务断点续跑**（issue #8）：CI/CD 频繁重新部署导致正在执行的任务进程被杀后，
  重启不再从头重跑——executor 每次执行后把 claude 会话 id（`claude -p` JSON 输出
  的 `session_id`）持久化到 `tasks.claude_session_id`；重试或平台重启恢复
  （调度器 `requeue_interrupted` 重新入队）时用 `claude -p --resume <sid>` 接续
  上次会话，且工作区只 fetch 不清空（保留 Claude 已做的修改），并注入恢复引导语
  让 Claude 检查现状后从断点继续，不重复已完成的工作。会话文件丢失（如 Docker
  未挂载 `~/.claude`）时自动清掉无效 id 降级为全新会话。任务 API 新增 `resumed`
  字段，前端任务列表在「尝试」列显示「恢复」徽标。
  - 涉及 `backend/botler/executor.py`（`_extract_session_id` / `_session_file` /
    `_resume_prompt` / resume 工作区保留）、`database.py`（迁移新增列）、
    `api/tasks.py`、`frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`、
    `docker-compose.yml`（新增 `data/claude-home` 挂载，Docker 部署会话持久化）。
  - 测试：新增 9 个用例（session_id 解析落库、resume 参数与引导语、工作区保留、
    会话文件缺失降级、重启恢复任务自动 resume、API `resumed` 契约）。
  - 说明：pm2 部署下 `~/.claude` 在宿主 HOME 天然持久，无需额外配置；Docker
    部署需重建容器使新挂载生效。

### Changed

- **前端响应式适配 2K 分辨率**（issue #15）：原内容区固定 `max-width: 1100px`，
  2560x1440 屏幕上左右两侧各剩约 730px 空白。优化：内容区宽度改为 CSS 变量
  `--content-width` 驱动，媒体查询分档放宽——视口 ≥1600px 放宽到 1360px（1440p
  笔记本），≥1920px 放宽到 1600px（2K 2560x1440，两侧余量降至约 480px）；表格
  省略列（`--ellipsis-max`）同步由 340px 放宽到 520px，长标题/失败原因列不再被
  压缩。小屏（<1600px）行为不变，保持原 1100px。
  - 涉及 `frontend/src/styles.css`（CSS 变量 + 媒体查询，`.content` 与
    `.table td.ellipsis` 引用变量）。
  - 测试：前端 2 passed + build 通过（纯 CSS 静态改动，无逻辑变更）；后端全量
    158 passed。

- 「添加仓库」按钮移到表单底部单独一行并水平居中（issue #6）：原按钮与「显示名称」
  「webhook 回调地址」输入框挤在同一行，紧挨 webhook 字段易被误解为 webhook 专属
  操作；现拆为两行，按钮独占一行（水平居中，追加需求 note 158 后移除旁侧说明文字
  并加宽按钮——新增 `.btn-wide` 样式）。涉及 `frontend/src/pages/Repos.jsx`、
  `frontend/src/styles.css`。
- CI 健壮性修复：`sync_to_github` 的 git push 加 `timeout 60` 秒超时——git 自身
  无连接超时，github.com 直连被阻断时单次 push 会挂数分钟，既有 10 次重试循环
  演变成无限挂起（实测流水线卡死 20+ 分钟）；加超时后单次尝试快速失败进入
  下一次重试，最多 10 次 ≈ 11 分钟必然收敛。涉及 `.gitlab-ci.yml`。
- CI 部署残留容器清理（issue #13）：检查发现 code01 的 docker 存在 rootful
  （`/var/run/docker.sock`）与 rootless（`/run/user/$(id -u)/docker.sock`）两个
  daemon，历史 Docker 部署容器落在 rootless 侧而 CI 清理只查 rootful，且 runner
  构建目录被清理后 `docker compose down` 依赖配置文件会失败——导致 4 个临时
  容器（botler compose 孤儿 + 3 个 apt 安装中途失败容器）残留数周未被清理。
  修复：deploy job 第 4 步改为**双 daemon** 按容器 label（`com.docker.compose.
  project=botler`）/名称过滤直接 `docker rm -f`（不依赖 compose 配置文件），并
  新增 `after_script` 兜底清理——无论部署成功/失败都会执行；只清理 botler
  项目容器，不误删其他容器（如 shipyard）。已有残留已手动清理（本机 docker
  现存容器：rootful 侧 shipyard 正常运行中）。涉及 `.gitlab-ci.yml`。

### Added

- 新增**备份与恢复**功能（issue #5）：备份范围为 `config.yaml` + `botler.db`
  （SQLite，用 sqlite backup API 生成一致性快照，WAL 下安全），打包为
  `botler-backup-<时间戳>.tar.gz` 存于服务器 `data/backups/`（compose 新增挂载）。
  - 触发：手动（设置页「立即备份」/ `POST /api/backups`）+ 定时（每天 03:00
    Asia/Shanghai，`backup.enabled` 开关）；保留策略按 `retention_days`（前端
    可配置 1~365，默认 30）清理更旧备份。
  - 恢复：Web UI 上传备份文件 / 选择服务器历史备份（`POST /api/backups/restore`
    与 `/restore/upload`），校验成员名白名单（防路径穿越）+ manifest sha256
    校验和（缺 manifest / 篡改文件拒绝恢复），覆盖数据后 `os.execv` 自动重启
    进程（Docker / pm2 / systemd / dev 通用；重启后 running 任务自动重新入队）。
  - API：`GET/POST /api/backups`、`GET /api/backups/{name}/download`、
    `DELETE /api/backups/{name}`；settings API 新增 `backup` 段。
  - 涉及 `backend/botler/backup.py`（新）、`backend/botler/api/backup.py`（新）、
    `config.py`（KNOWN_FIELDS + Settings 新字段）、`api/settings.py`、`main.py`
    （AppContext + 定时任务启停）、`docker-compose.yml`（backups 挂载）、
    `frontend/src/components/BackupManager.jsx`（新，设置页备份管理卡片）、
    `frontend/src/api.js`（下载/上传封装）。新增 `backend/tests/test_backup.py`
    （21 个用例，含路径穿越 / 损坏包 / 校验和失败 / WAL 一致性 / 保留清理等
    边界）与 `test_api_backup.py`（17 个用例）。

- 任务列表**详细失败原因**查看（issue #4 追加需求）：失败摘要之外，executor 每次
  尝试失败时都会提取错误信息（claude JSON 输出中优先提取 `Traceback` 堆栈，否则
  取输出尾部），连同退出码按尝试次数序列化写入 `tasks.error_detail`；任务列表
  失败原因列在存在详细记录时显示「详情」按钮，点击弹窗逐次展示「第 N 次尝试 /
  退出码 / 错误与堆栈」（长内容可滚动），另附完整摘要与日志文件路径，界面默认
  不直接展示详细内容。API `error_detail` 解析为结构化对象，脏数据（非法 JSON）
  返回 `None` 不报错。涉及 `backend/botler/executor.py`、`backend/botler/database.py`
  （迁移新增列）、`backend/botler/api/tasks.py`、`frontend/src/pages/Tasks.jsx`、
  `frontend/src/styles.css`；新增 `backend/tests/test_executor.py`（10 个用例）覆盖
  错误提取（JSON/Traceback/截断/空输出）、error_detail 序列化与重试耗尽 /
  unresolvable 落库，`test_api_tasks.py` 补 error_detail 数据契约用例。

- 任务列表新增**失败原因**列（issue #4）：failed / interrupted 状态直接展示后端
  `error_message`（重试耗尽、Claude 无法解决、获取 issue 失败、平台中断等），
  长文本省略号截断、悬浮显示完整内容；其余状态显示 `—`。后端 `/api/tasks` 原有
  数据契约不变，新增 `backend/tests/test_api_tasks.py`（13 个用例）覆盖列表字段、
  状态过滤、搜索、分页、统计与活跃任务去重。涉及 `frontend/src/pages/Tasks.jsx`。

### Changed

- CI/CD 部署阶段改为 **Docker 部署**（issue #7）：`deploy_to_code01` 不再用 pm2
  直接部署，改为 `docker compose up -d --build`（前端已打进镜像，Dockerfile
  多阶段构建，不再依赖 frontend:build 的 dist 产物）；部署前自动停止旧 pm2
  服务避免 8000 端口冲突；`backend/.env` 缺失时用 CI 变量生成
  （GITLAB_BOT_TOKEN / WEBHOOK_SECRET / ANTHROPIC_*）；容器内 git 凭据
  （执行器 clone/push 依赖）通过挂载 `.git-credentials` + `GIT_CONFIG` 强制
  store helper 提供（优先复用宿主凭据，否则用 GITLAB_BOT_TOKEN 生成）；
  默认镜像源（Docker Hub）不可达时自动改用国内源
  `docker.m.daocloud.io` 重试；部署强制使用 rootful docker socket
  （`/var/run/docker.sock`，rootless context 有容器运行故障）；`docker-compose.yml`
  新增 `GIT_CREDENTIALS_FILE` 可调挂载、`deploy/verify-docker.sh` 同步适配。
  pm2 / systemd 仍保留为手动部署备选。

- Docker 部署**数据目录集中到 `data/`、时区固定 Asia/Shanghai**（issue #7 追加需求）：
  compose 挂载源默认改为 `./data`（相对部署目录，默认 = `data/` 文件夹），
  config.yaml / .env / botler.db / workspace / logs 全部集中在 `data/` 下；
  **CI 部署显式固定 `BOTLER_DATA_DIR=/home/ckd/codes/botler/data`**（用户指定
  绝对路径，不随 gitlab-runner 构建目录漂移——构建目录可能被清理导致数据
  丢失）；容器时区固定为亚洲/上海（compose `TZ: Asia/Shanghai` + 镜像安装
  tzdata 并写入 `/etc/localtime`）；`.gitignore` 忽略 `data/` 文件夹；CI
  `deploy_to_code01` 增加旧位置散落数据（backend/.env、config.yaml、botler.db、
  workspace/、logs/）到 `data/` 的**一次性自动迁移**（目标已有文件则保留 data/
  版本），`.env` 缺失时在 `data/backend/.env` 用 CI 变量生成；`deploy/verify-docker.sh`
  新增容器时区校验（`date +%Z` = CST）。涉及 `docker-compose.yml`、`Dockerfile`、
  `.gitignore`、`.gitlab-ci.yml`、`deploy/verify-docker.sh`、`README.md`。

- 「添加仓库」默认方式改为**本地文件夹方式**：打开仓库管理页时默认选中「本地文件夹
  （读取 git remote）」，而非 GitLab URL 方式；切换方式仍可随时手动选择。涉及
  `frontend/src/pages/Repos.jsx`。
- 前端 UI 全面改版为 **Geist Light 浅色设计体系**（Vercel 官方设计令牌，经过验证的方案）：
  页面背景 `#fafafa` + 白色卡片表面、主色 `#0070f3` 蓝、发丝线边框/轻阴影层次、
  6px 常规圆角、弱底+深字状态徽标、焦点环等交互规范。原有深蓝灰主题（`#0f1420`/`#4f8cff`）
  移除。全部页面（仓库/任务/模版/设置/目录选择对话框）只换皮肤，组件结构与功能不变。
  涉及 `frontend/src/styles.css`（令牌化全量重写）。
- 新增 **`docs/design-system.md` 前端设计规范**：设计原则、CSS 变量令牌总览、色板/
  字体/间距/圆角阴影/边框规范、全部组件与状态规范、交互规范、新增样式检查清单、
  迁移决策记录。后续前端样式变更以此为唯一规范来源。

### Added

- 新增 **Docker 部署方式**（与 pm2 / systemd 并存，三选一）：
  - `Dockerfile` 多阶段构建：node 构建 React/Vite 前端 → node + python 运行时
    （内置 `git`、`claude` CLI（`@anthropic-ai/claude-code`，执行器核心依赖）、
    后端 Python 依赖；pip 走清华源、npm 走 npmmirror）
  - `docker-compose.yml` 一键部署：端口 `BOTLER_HTTP_PORT`（默认 8000）与数据目录
    `BOTLER_DATA_DIR`（默认 `.`）可调；config.yaml / .env / botler.db / workspace / logs
    全部卷挂载持久化，容器重建不丢数据；内置 healthcheck（`/api/health`）与
    `restart: unless-stopped`
  - 国内无法访问 Docker Hub 时，构建参数 `NODE_IMAGE` / `RUNTIME_IMAGE` 可覆盖
    基础镜像前缀（如 `docker.m.daocloud.io/library/node:20-alpine`），CLI 与
    docker compose 两种方式均支持
  - `deploy/verify-docker.sh` 冒烟验证脚本：静态校验 → compose 配置校验 → 镜像构建
    → 起容器 → 健康检查 → API/前端页面 → 重复 up 幂等 → 数据落盘，共 10 项检查
    （`--full` 用临时数据目录与 18000 端口，不触碰真实数据）

- 目录选择对话框支持配置默认初始定位目录（`browse.default_path`）：打开「添加仓库 → 本地
  文件夹 → 浏览…」对话框时不再从 `/` 开始，而是定位到配置的目录；未配置时默认为服务器用户
  主目录（`~`）。支持 `~` 展开与相对路径，配置的路径不存在时自动回退主目录、主目录不可用
  时回退 `/`，保证对话框始终能打开。可在 Web UI「设置」页或 `config.yaml` 配置，所有用到
  目录选择的地方统一生效（`GET /api/repos/browse` 不带 path 即返回默认初始目录）。涉及
  `backend/botler/dir_browse.py`（新增 `resolve_default_path`，9 个边界用例）、`config.py`
  （新增 browse 配置段 + settings API 读写）、`api/repos.py`、`api/settings.py`、
  `frontend/src/components/FolderPicker.jsx`、`pages/Repos.jsx`、`config.example.yaml`。
- 「本地文件夹」方式添加仓库支持服务器目录选择对话框：路径输入框旁新增「浏览…」按钮，：路径输入框旁新增「浏览…」按钮，
  弹出对话框逐级浏览服务器文件系统（从 `/` 开始；隐藏目录默认隐藏可勾选显示、伪文件系统
  `/proc` `/sys` `/dev` `/run` 自动过滤、无权限目录禁用、含 `.git` 的文件夹标记为 git 仓库），
  选中文件夹后自动回填路径并读取 remote（也可继续手动输入路径）。涉及
  `backend/botler/dir_browse.py`（新模块，含 16 个边界用例）、`api/repos.py`（新增
  `GET /api/repos/browse`）、`frontend/src/components/FolderPicker.jsx`（新组件）、
  `pages/Repos.jsx`、`styles.css`。
- 新增「本地文件夹」方式添加仓库：填写服务器上的本地 git 仓库路径 + webhook 回调地址（可选），
  平台在服务端运行 `git remote -v` 读取 remote 列表，前端展示供用户选择（`POST /api/repos/discover`）；
  添加后 `local_path` / `remote_name` 持久化到 config.yaml 与数据库，任务执行直接在该本地文件夹
  进行（不再 clone 到平台工作区）。涉及 `backend/botler/git_remote.py`（新模块）、`api/repos.py`、
  `database.py`（repos 表新增 `remote_name` 列及旧库自动迁移）、`config.py`、`executor.py`、
  `main.py`、`frontend/src/pages/Repos.jsx`。

### Fixed

- **修复运行任务失败——origin/HEAD 缺失与 fetch 凭据间歇失败**（issue #12）：
  任务重试耗尽后退出码 -1。根因有二：(1) `prepare_workspace` 每次执行
  `git reset --hard origin/HEAD`，依赖 `origin/HEAD` 符号引用——手工添加
  remote 的仓库（如 local_path 方式注册的 botler 自身）无此引用，报
  `ambiguous argument 'origin/HEAD'` **必现失败**；修复为 reset 跟随实际
  checkout 的分支（main → master），不再依赖 origin/HEAD。(2) askpass
  凭据脚本在每次 prepare 结束后被删除，并发任务/重试时序下脚本不存在 →
  git 回退 credential helper 旧凭据 → `HTTP Basic: Access denied` **间歇性
  失败**；修复为保留脚本（每次 prepare 覆盖刷新，0700 权限，token 轮换自动
  生效），并调整 `git_env` 构造顺序防止外部环境变量覆盖凭据注入。涉及
  `backend/botler/executor.py`。测试：新增 2 个用例（origin/HEAD 缺失时
  prepare 应成功；prepare 后 askpass 脚本保留），本地全量 154 passed。

- **修复运行任务必现失败**（issue #11）：任务每次执行都在 `prepare_workspace` 抛
  `AttributeError: 'sqlite3.Row' object has no attribute 'get'`，重试耗尽后退出码
  -1。根因：`database` 层查询返回 `sqlite3.Row`（无 `.get()` 方法），而 `executor`
  的 `_repo_workdir` / `prepare_workspace` 按 dict 风格对 repo 调 `.get()` 访问
  `local_path` / `remote_name`。修复：新增 `_row_get(row, key, default)` 兼容
  访问器（同时支持 sqlite3.Row 与 dict），替换 3 处 `.get()` 调用；涉及
  `backend/botler/executor.py`。测试：新增 2 个用例（`_repo_workdir` 直接收
  sqlite3.Row；`run_task` 全流程从 db 取 Row 仓库正常 succeeded——与 CI 日志
  同调用路径的端到端复现）。
- 修复添加仓库时 webhook 注册报错「注册 webhook 失败: GitLab API 错误 422: Invalid url given」
  （webhook_url 留空时必现，URL 方式添加同样受影响）：根因是 Botler 部署在内网
  （10.0.0.122），GitLab 默认禁止向本地/私有网络地址注册 webhook（SSRF 防护，
  `allow_local_requests_from_web_hooks_and_services` 默认 false）。修复方案：
  ① GitLab 侧在 Admin → Settings → Network → Outbound requests 勾选「Allow requests
  to the local network from webhooks and integrations」；② Botler 侧
  `register_webhook` 检测到 422 + 内网回调地址时，错误信息自动追加可操作提示
  （新增 `_is_private_url` 判定 + 15 个测试用例），涉及 `backend/botler/gitlab_client.py`。
- 修复 `GitLabClient.resolve_project` 无法识别 scp-like SSH URL（`git@host:group/project.git`，
  `git remote -v` 最常见形态）：此前 urlparse 解析不出 scheme，整串被当作项目路径导致 404；
  现按最后一个 `:` 之后的仓库路径解析，并补充 `.git` 后缀剥离与嵌套 group 支持。
  同步新增 `backend/tests/` pytest 测试套件（26 个用例），CI `backend:test` 检测到 `tests/`
  目录后自动运行；`requirements.txt` 新增 `pytest` 依赖。
