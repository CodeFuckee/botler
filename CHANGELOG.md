# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]
### Added

- **config.py 泛化配置写回（issue #193）**：`ConfigManager` 原先 15+ 个
  结构重复的 `update_*` 方法（`update_worker` / `update_gitlab` /
  `update_claude` / `update_dsh` / `update_browse` / `update_backup` /
  `update_ui` / `update_notifications` / `update_sso` / `update_minio` /
  `update_webhook` / `update_repos` / `update_custom_labels` /
  `update_ai_providers` / `update_image_models` / `update_vision_models` /
  `update_default_template` / `update_resume_template` /
  `update_comment_template`，每个都是「读 yaml → 局部改 → 写回 → 重载」
  的同一套模板）收敛为 1 个泛型实现 `update_section(section, patch)`：
  - schema 描述：新增 `SECTION_SCHEMAS` + `SectionSchema`（字段白名单 /
    掩码字段 / 空白归一 / 空串恢复默认 / 整体列表替换），新增配置段只需
    登记一行，不再复制粘贴方法；`KNOWN_FIELDS` 与 schema 一致性由测试兜底；
  - 掩码语义集中：`api_key` / `owner_token` / `client_secret` /
    `authorization` / `access_key` / `secret_key` 的空串 / 含 `*` 掩码值
    统一「不覆盖真实凭据」（原 `sso.client_secret` 空串会清空的行为一并
    收敛为保持现有值）；
  - 原子写保留：写回统一走 `save()` 的 temp + rename 原子写，模拟写中断
    （rename 阶段 / dump 中途崩溃）不损坏 config.yaml；
  - 调用方迁移：`api/settings.py` / `api/plugins.py` / `api/labels.py` /
    `api/repos.py` 及测试全部改调 `update_section`，`remove_repo` 保留
    专用实现（按 project_id 过滤，语义独立）；
  - 新增测试 `backend/tests/test_config_section.py` 37 例：白名单 / 未知段
    拒绝 / 掩码保持（6 字段 × 掩码 / 空串 / 真实覆盖）/ minio 空白归一 /
    templates 空串恢复默认 / 列表段整体替换 / schema 一致性 / 原子写中断
    模拟。

- **SQLite 连接复用与统一连接管理（issue #191）**：`database.py` 的 `_conn()`
  每次调用都执行 `sqlite3.connect(timeout=30)` + `PRAGMA journal_mode=WAL` +
  row_factory 设置，高频路径（任务列表、概览轮询、通知拉取、日志写入）每秒
  打开/关闭连接纯属浪费；改为每线程一条长连接复用、写事务跨线程串行化：
  - 连接复用：`threading.local` 按线程各持一条连接（`_get_connection()`），
    WAL + busy_timeout + row_factory 只在首次创建时设置一次（journal_mode 为
    数据库级持久属性），复用不再重复 PRAGMA；`check_same_thread` 保持默认
    True——每个连接只被所属线程使用；
  - 写串行化：写路径 `_conn(write=True)` 先取进程级写锁（`_write_lock`）再
    显式 `BEGIN IMMEDIATE` 抢占写锁（避免 DEFERRED 事务升级写锁与并发写冲突），
    WAL 下读事务不受写锁影响可并发；新增 `close()` 供测试清理/优雅停机；
  - 嵌套兼容：同一线程外层已进入 `_conn` 时内层透传同一连接、事务由最外层
    统一收口，既有调用模式（测试中 `with db._conn()` 内再调写方法）行为不变；
  - 新增测试 `backend/tests/test_database_conn_reuse.py` 7 例：同线程复用
    只 connect 一次（无重复 PRAGMA）、跨线程连接隔离、并发写无
    `database is locked`（8 线程压测全部落库）、写事务回滚后可复用、
    嵌套透传、纯读不开事务、设置生效一次；
  - 新增基准脚本 `backend/scripts/benchmark_db.py`：同负载混合读写
    （list_tasks + add_log）实测 ops/sec 56 → 225（约 4 倍）、P95 延迟
    19.2ms → 5.1ms（本机对比连接复用前后）。

- **概览页 DeepSeek 余额卡片增加「每小时余额变化速率」显示（issue #304）**：
  「如果有账户余额信息时，概览增加账户余额减少（变化）速度显示，按小时
  为单位」——余额卡片每个币种条目新增速率文案（如「每小时减少 5.00」），
  数据源为前端观测样本历史，后端无改动：
  - 新增 `frontend/src/balanceRate.js` 纯函数模块：每次余额轮询/手动刷新
    成功时把观测样本 `{ts, infos:[{currency, total_balance}]}` 追加到
    localStorage（键 `botler.overview.dsBalanceHistory`），按最早/最近
    观测窗口计算每小时平均变化速率（减少为负、增加为正——通常为充值/
    赠送到账、无变化为 0）；样本容量上限 200 条、有效期 7 天、最短观测
    窗口 1 分钟（更短噪声大不计速率）；存储不可用/数据损坏时全部兜底
    回退，不影响页面使用；
  - `Overview.jsx` 集成：余额加载成功链路追加样本 + 计算速率，卡片每个
    币种条目渲染速率文案（减少/增加/无变化/暂无速率数据四种态），悬浮
    提示观测窗口（如「2 小时」「30 分钟」）；
  - 新增 i18n 文案 7 条（zh-CN / en-US 双语：rateDecrease / rateIncrease /
    rateStable / rateNone / rateHint / rateWindowHour / rateWindowMinute）
    与 `.deepseek-balance-rate` 样式；
  - 新增测试 `frontend/tests/overview-balance-rate.test.mjs` 21 例：balanceRate
    纯函数（样本追加/清洗/过期丢弃/容量截断/速率计算各分支/窗口格式化）
    + Overview 源码集成断言 + 渲染级四种文案（减少/增加/无变化/暂无数据）；
  - 同步更新 README（ai_providers 配置表与 issues API 表补充速率说明）。


- **概览页 issue 详情右边栏支持修改负责人并同步 GitLab（issue #303）**：
  「概览页issue详情右边栏，增加可以修改issue的负责人，并同步到gitlab上，
  负责人通过下拉菜单的方式选取，直接通过gitlab api读取项目成员」——
  概览页弹出的 issue 右边栏「负责人」行新增「编辑」按钮，编辑态通过下拉
  菜单选取项目成员（数据源直接读取 GitLab 项目成员 API），保存后同步
  更新 GitLab 上该 issue 的负责人：
  - 后端新增 `GET /api/issues/{project_id}/members`：项目成员清单
    （负责人下拉数据源，GitLab members/all + issue #93 的 user_id 补齐，
    成员精简为 {id, username, name}，id 为 GitLab 用户 id——更新负责人
    assignee_ids 需要该值；查询失败 502，不可降级为空）；
  - 后端新增 `PUT /api/issues/{project_id}/{iid}/assignee`：更新 issue
    负责人（assignee_id 为项目成员用户 id，None 清除负责人；
    GitLabClient 新增 `update_issue_assignee` 统一归一为空数组显式清除；
    编辑走 owner token（issue #130），成功后清空概览缓存并返回更新后
    负责人列表）；`_trim_assignees` 与 `_project_members` 抽取复用
    （前者与 overview 聚合的负责人精简共用，后者与添加 issue 弹窗的
    成员处理共用）；
  - 前端 `IssueDrawer` 抽屉 KV 表「负责人」行新增「编辑」按钮（缺
    project_id 旧缓存数据隐藏，与关闭/编辑标记按钮同约定）：编辑态
    加载项目成员下拉选择（「不指定」+ 项目成员，当前负责人按 username
    预选、负责人已不是项目成员时回退「不指定」），保存调用
    `PUT /api/issues/{project_id}/{iid}/assignee`，成功后本地负责人
    即时更新并通知父组件刷新列表（onAssigneeUpdated → loadIssues）；
    失败保留编辑态可重试、取消不调接口；成员加载失败展示错误 + 重试；
    同步新增 `assignee-edit` / `assignee-select` 样式；
  - 同步更新 README 的 issues API 文档（两处 API 表，新增
    members/assignee 两行）；
  - 新增测试：后端 `TestIssueMembers` 9 例（成员精简 / user_id 补齐 /
    查不到剔除 / 异常元素过滤 / 空成员 / 404×2 / 502×2）、
    `TestUpdateIssueAssignee` 8 例（更新成功 / 清除 / 404×3 / 502×2 /
    清缓存）、GitLabClient `TestUpdateIssueAssignee` 3 例（assignee_ids
    转发 / None 清除 / 空列表清除）、前端
    `frontend/tests/overview-issue-drawer-assignee-edit.test.mjs` 12 例
    （源码数据流 + 编辑按钮显隐 / 预选 / 保存参数 / 清除 / 成功失败 /
    加载失败重试 / 空成员池 / 取消 / 请求中禁用），全量测试无 regression。


- **概览页 issue 详情侧边栏展示完成耗时（issue #300）**：
  「概览页面，issue详情的有侧边栏，如果是任务完成了，显示完成耗时」——
  概览页弹出的 issue 右边栏「任务」行下方新增「完成耗时」行：该 issue
  最近任务成功终态（succeeded）时展示完成耗时（`finished_at - created_at`，
  与 issue #180 完成耗时统计语义一致：系统接收时间 → bot-done 打标时间），
  未完成/从未执行显示「—」：
  - 后端 `GET /api/issues/{project_id}/{iid}/detail` 新增
    `task_duration_seconds` 字段（该 issue 最近任务完成耗时秒数：仅
    succeeded 终态且 created_at/finished_at 均存在、解析成功、用时非负
    时返回，复用 `find_latest_task` 取最近一条；未完成/时间数据异常
    返回 `null`）；`_task_duration_seconds(latest)` 与 `_task_engine_name`
    并列，detail 一次查询同时产出 engine/task_id/完成耗时；
  - 前端 `IssueDrawer` 抽屉 KV 表「任务」行下方新增「完成耗时」行：
    `d.task_duration_seconds` 为有限非负数时经 `fmtSeconds` 展示人类可读
    耗时（如「3 小时 25 分钟」），详情加载中显示「加载中…」，未完成/
    加载失败/异常值（负数、NaN、字符串）均显示「—」兜底，不因坏数据
    崩溃；
  - 同步更新 README 的 detail 接口文档（两处 API 表）；
  - 新增测试：后端 `TestIssueDetail` 5 例（成功任务返回耗时 / failed 终态
    不返回 / 无任务 null / 缺时间字段与非法格式 null / 负用时 null，既有空
    notes 精确断言同步补 `task_duration_seconds` 字段）、前端
    `frontend/tests/overview-issue-drawer-duration.test.mjs` 5 例（源码
    数据流 + 渲染已完成/未完成/加载失败/异常值兜底），全量测试无 regression。


- **标记库默认标签一键同步到全部仓库（issue #307）**：
  「标记库增加一个添加默认标记的功能，点击默认标记后，自动将标记同步到已添加到
  所有仓库，包括启用和未启用的」——标记库页每个默认标签新增「同步到所有仓库」
  按钮，点击后自动把该标签同步到平台已添加的全部仓库（含启用与未启用的，软删除
  仓库除外）：
  - 后端新增 `POST /api/labels/{name}/sync`：校验 name 为内置默认标签（非默认
    标签 400 拒绝）后，遍历 DB 全部未删除仓库（`list_repos` 不过滤 enabled，
    天然覆盖启用与未启用），对每个仓库先 `list_project_labels` 比对、缺失才
    `create_project_label`（只补缺失、不覆盖用户已有颜色/描述，与「添加仓库时
    补齐默认标签」issue #157 语义一致）；身份复用 issue 创建链路——per-repo
    client（仓库 remote URL 内嵌 token）优先、无 token 回退全局 bot token；
    单仓库失败为尽力而为，不中断其余仓库，返回
    `{label, total_repos, created, already_exists, failed}` 汇总；
  - 前端标记库页默认标签区块：每个默认标签展示「同步到所有仓库」按钮（请求中
    禁用防重复点击并显示「同步中…」），成功展示「已同步「xxx」到 N 个仓库：新建
    X 个、已存在 Y 个、失败 Z 个（失败仓库名）」，失败展示后端错误信息；页面
    说明文案同步标注同步范围含启用与未启用仓库；
  - 同步更新 `docs/labels.md`（新增「平台内一键同步」章节）与 README API 表；
  - 新增测试：后端 `TestSyncDefaultLabel` 6 例（启用+未启用仓库全部补齐 / 已存在
    跳过不覆盖 / 非默认标签 400 / 无仓库空跑 / 单仓库失败不中断 / per-repo client
    优先）、前端 `labels-page.test.mjs` 同步按钮 6 例（源码断言 + 渲染点击调用 /
    结果汇总 / 失败透传 / 请求中禁用），全量测试无 regression。


- **任务调度处增加「暂停窗口豁免优先级阈值」设置（issue #299）**：
  「任务调度处增加设置，当优先级高于多少的时候，可以不受定时暂停窗口的
  影响持续开发」——任务调度「定时暂停窗口」配置新增豁免优先级阈值
  `worker.pause_priority_threshold`（0~999，默认 0 = 关闭）：
  - 语义：仓库调度优先级（`repos[].priority`，1~999，数字越小越优先）
    不差于该阈值（`priority <= 阈值`）的仓库，在暂停窗口内仍可开始新
    任务（不受窗口影响）；阈值 0 = 关闭豁免，所有仓库都受暂停窗口约束
    （issue #169 行为不变）；
  - 调度器 `_dispatch`：暂停窗口内不再一律不派发，而是先按阈值过滤候选
    仓库——仅保留优先级不差于阈值的仓库参与派发，其余仓库任务保留在
    队列等待窗口结束后自动开始；`max_concurrent_repos` 并行上限与运行中
    任务豁免语义不变；进入窗口的日志在配置阈值时说明豁免范围；
  - 设置 API：`GET /api/settings` worker 段返回
    `pause_priority_threshold`，`PUT` 校验 0~999 整数（布尔/负数/超上限/
    非整数 400 拒绝，与 pause_weekdays 同思路）；
  - 设置页「任务调度」卡片「定时暂停窗口」区块新增「豁免优先级」输入框
    （数字输入 0~999，placeholder「0（关闭）」，附说明文字），随全局
    「保存」写回 config.yaml；
  - 同步更新 `config.example.yaml` / README 配置表；
  - 新增测试：后端调度器 `TestPausePriorityExemption` 6 例（窗口内高优
    先级派发 / 低优先级保留至窗口结束 / 阈值边界含等号 / 阈值 0 保持
    暂停语义 / 缺省优先级回退 100 / 混合队列只派发豁免仓库）、设置 API
    `TestPausePriorityThresholdSettings` 7 例（默认 0 / 落盘回读 / 0 关闭
    / 不影响其他字段 / 非法值 400×7）、前端
    `settings-pause-windows.test.mjs` 3 例（卡片区块标注 / 保存提交 /
    渲染回显并提交），全量测试无 regression。


- **仓库设置页面 logo 同步到 GitLab 按钮（issue #297）**：
  「仓库设置页面生成 logo 后，增加按钮可以直接同步 gitlab 上，作为仓库
  的图标」——仓库管理页「生成图标」生成 logo 后，仓库行新增「同步到
  GitLab」按钮，一键把本地生成的 logo 上传为 GitLab 项目图标（头像）：
  - 后端新增 `POST /api/repos/{id}/sync-logo`：校验仓库存在/未软删除/
    已生成 logo/logo 文件存在后，读取本地 logo 文件，经 GitLab API
    `PUT /projects/{id}` 的 `avatar` 文件参数（multipart）上传；身份
    复用 issue 创建链路（per-repo client——仓库 remote URL 内嵌 token
    优先，无 token 回退全局 bot token，均需 Maintainer 及以上角色）；
    成功返回 ok + 项目 `path_with_namespace` + 新 `avatar_url`；
    错误映射：仓库不存在 404 / 已删除 400 / 尚未生成 logo 400 / logo
    文件缺失 404 / 读取失败 500 / GitLab 调用失败 502（透传错误详情）；
  - `GitLabClient` 新增 `update_project_avatar(project_id, filename,
    data, mime)` 方法（PUT /projects/{id} multipart 上传头像）；
  - 前端仓库行操作组在 `repo.logo_path` 非空时展示「同步到 GitLab」
    按钮（请求中禁用防重复点击并显示「同步中…」），成功展示「已同步到
    GitLab（项目路径）」、失败展示后端错误信息；`Icon` 新增 `upload`
    语义图标（lucide Upload）；
  - 同步更新 README 的模块说明与 API 表（新增 sync-logo 行）；
  - 新增测试：后端 `TestSyncLogo` 7 例（正常路径含上传参数断言 / 不依赖
    AI 与生图模型配置 / 仓库不存在 / 软删除 / 尚未生成 / 文件缺失 /
    GitLab 失败 502 / 读取失败 500）、前端
    `frontend/tests/repos-logo-sync.test.mjs` 5 例（渲染条件 / 点击调
    用 / 请求中禁用 / 成功 / 失败），icons 清单补 `upload`，全量测试无
    regression。


- **概览页 issue 详情侧边栏展示任务 id（issue #290）**：
  「概览页的右侧的 issue 详情侧边栏，如果已经执行了，显示对应的任务 id」——
  概览页弹出的 issue 右边栏新增「任务」行：已执行过（有任务记录）的 issue
  展示其最近一次任务的 id（`#id`），从未执行显示「—」：
  - 后端 `GET /api/issues/{project_id}/{iid}/detail` 新增 `task_id` 字段
    （该 issue 最近一条任务记录 id，复用 `find_latest_task` 按 id 倒序取
    最新，重新指派/对账补入队/手动重试的多条任务记录取最新一条；无任务
    记录返回 `null`）；`_issue_engine` 重构为 `_task_engine_name(latest)`
    + 全局回退两层，detail 一次查询同时产出 engine 与 task_id；
  - 前端 `IssueDrawer` 抽屉 KV 表「执行引擎」行下方新增「任务」行：
    `d.task_id` 为正整数时展示 `#id`（title 提示最近任务），详情加载中
    显示「加载中…」，从未执行/加载失败/异常值（0、字符串、负数）均
    显示「—」兜底，不因坏数据崩溃；
  - 同步更新 README 的 detail 接口文档（两处 API 表）；
  - 新增测试：后端 `TestIssueDetail` 3 例（有任务返回 id / 多任务取
    最新 / 无任务 null，既有空 notes 精确断言同步补 `task_id` 字段）、
    前端 `frontend/tests/overview-issue-drawer-task-id.test.mjs` 5 例
    （源码数据流 + 渲染正常/从未执行/加载失败/异常值兜底），全量测试
    无 regression。


- **概览页「其他」分组 issue 置顶按钮（issue #308）**：
  「其他」分组（尚未处理/处理中的开放 issue）每条 issue 增加置顶按钮
  （pin 图标）：点击把该 issue 移到手动调度顺序最前并保存（复用 issue
  #287 的手动调度顺序机制与 `PUT /api/issues/{project_id}/manual-orders`
  接口，`issue_manual_orders` 表），任务调度器派发时优先按手动顺序，
  置顶即第一个处理：
  - 置顶不依赖当前排序/过滤视图——任意排序方法、过滤激活状态下均可
    一键置顶（仅写手动顺序，不修改可见子集，与拖动排序的过滤禁用
    约束不同）；调度器执行顺序视图下置顶后立即跳到组首展示；
  - 已置顶（手动顺序首位）的 issue 按钮高亮主色 + `aria-pressed`
    标识，重复点击不重复保存；保存中隐藏按钮避免并发覆盖；保存失败
    回滚顺序并提示（复用 `overview.manualOrderError` 错误提示）；
  - 仅「其他」分组展示（bot-failed / bot-done / 运行中分组不展示），
    仓库与 issue 均无 project_id 时不展示；
  - 新增纯函数 `pinIssueToTop`（置顶 iid 到首位、保序去重、非数组/非
    整数兜底）与 i18n 文案（`overview.pinIssue` /
    `overview.pinIssueTitle` / `overview.pinIssuePinned`，中英文）；
  - 新增前端测试 `frontend/tests/overview-issue-pin.test.mjs` 14 例
    （纯函数边界 + 渲染条件 + 交互保存/去重/失败回滚 + i18n 键齐全），
    前端全量测试无 regression。

### Changed

- **前端路由级代码分割，首屏 JS 体积下降约 42%（gzip）（issue #202）**：
  `frontend/src/App.jsx` 原本静态 import 全部 12 个页面组件，Vite 构建为单个
  大 bundle（首屏一次性下载全部页面代码，483.79 kB / gzip 142.79 kB）。改为
  React.lazy + Suspense 按路由懒加载：
  - 新增 `frontend/src/pages/lazy.jsx`：全部页面（概览/仓库/任务/任务详情/
    统计/模版/标记库/插件/技能/设置/终端/登录）统一 React.lazy 包装，
    App.jsx 按路由懒加载，页面切换期间 `<Suspense>` fallback 展示轻量
    loading 态（`.page-loading` 居中 spinner + 「加载中…」，复用 HIG 既有
    spinner/muted 视觉，含 aria-live 播报）；
  - `vite.config.js` 增加 `manualChunks` vendor 拆分：react/react-dom/scheduler
    独立 `react-vendor` chunk、react-router 独立 `router-vendor` chunk、
    lucide-react 独立 `icons-vendor` chunk、@xterm 独立 `xterm-vendor` chunk
    （仅终端页按需加载）——共享依赖缓存稳定，任一页面改动不再影响 vendor
    chunk hash；
  - 构建产物验证：`dist/assets` 由「1 个主 bundle」变为 12 个按页面划分的
    chunk + 4 个 vendor chunk；首屏 JS（入口 + react-vendor + router-vendor +
    icons-vendor）由 483.79 kB 降至 247.58 kB（gzip 142.79 kB → 83.13 kB，
    raw 下降约 48.8%、gzip 下降约 41.8%）；终端重依赖（@xterm 291 kB）确认
    不在首屏预加载清单，仅打开终端页时按需下载；
  - 测试同步：新增 `frontend/tests/app-lazy-routes.test.mjs`（懒加载不影响
    渲染：目标页面按路由渲染、未访问页面不进入渲染树、不同路由按需加载各自
    chunk、lazy.jsx 导出全部 lazy 包装）；`app-default-page` / `app-shortcuts`
    增加懒加载轮询等待消除 chunk 异步时序抖动；labels/overview/plugins/skills/
    stats/terminal/apple-design 等源码断言测试改为校验 lazy.jsx 包装写法；
    前端全量测试通过（仅既有 1 例源码头文件断言失败，与本次改动无关）。


  原流水线阶段顺序 security → build → e2e → deploy → sync → release
  （e2e 在部署前执行，E2E 未通过不部署）。现调整为
  security → build → deploy → e2e → sync → release：
  - `e2e:playwright` 移至 `deploy_to_code01` 之后执行——部署不再等待 e2e
    （新版本更快上线），e2e 门禁改为阻断后续 sync/release
    （「E2E 未通过不同步/不发版」，docs_only_skip 的 on_success 传播）；
  - 同步更新 `.gitlab-ci.yml` 头部 Stage 拓扑与阶段说明、e2e/release job
    注释，以及 README「E2E 架构」中关于 e2e 阶段位置的描述；
  - 纯 CI 配置调整（无后端/前端代码改动），YAML 校验 + GitLab CI Lint
    验证通过，流水线阶段顺序实测为 deploy → e2e → sync。
- **executor.py 按职责拆分重构（issue #192）**：`backend/botler/executor.py`
  3182 行巨型文件拆分为 `backend/botler/executor/` 包（纯重构，行为零变化）：
  - `executor/workspace.py`：git 工作区管理（prepare / 默认主分支三级解析 /
    clean / 拉取冲突检测与 agent 手工解决交接 / untracked 残留尽力清理），
    `WorkspaceMixin` 约 550 行；
  - `executor/process.py`：引擎子进程执行（claude CLI 生命周期 / hermes /
    dsh SDK 进程内运行、停止/超时/输出 drain）、引擎输出 JSON 解析与结果
    判定、CI 流水线等待，`ProcessMixin` 约 1200 行；
  - `executor/session.py`：会话文件查找、transcript 解析、日志增量读取、
    `[PROGRESS]` 进度账本解析与渲染，`SessionMixin` 约 300 行；
  - `executor/prompt.py`：提示词渲染 / 任务环境注入 / gitconfig 与输出脱敏 /
    转义解码，`PromptMixin` 约 190 行；
  - `executor/__init__.py`（原 executor.py 主模块）：保留 `ClaudeExecutor`
    主类的引擎分发与状态机编排，914 行（验收标准「主文件 <1000 行」达成）；
  - 行为保持：全部函数/方法原样迁移（mixin 聚合，方法体仅新增测试兼容的
    动态符号引用），对外符号（`ClaudeExecutor` / `format_display_line` /
    `find_session_file` / `parse_transcript` / `read_log_delta` /
    `read_session_prompt` / `ExecutorError` / `DshRunner` 等）由主模块统一
    再导出，引用方（main / scheduler / api/tasks / 测试）零改动；
  - 新增 `backend/tests/test_executor_split.py` 49 例，覆盖四个新模块的
    纯函数与 mixin 方法（脱敏 / 转义解码 / 会话解析 / 日志增量 / untracked
    清理 / 冲突检测 / 结果判定）；后端全量 2200+ 用例通过，无回归。


## [1.3.63] - 2026-08-19
### Added

- **新增《插件开发指南》文档（issue #305）**：
  issue「在项目文档中增加，关于插件开发相关信息的文档，单独的一个文件，然后
  在 readme 中添加指引」为纯文档需求，本期新增 `docs/插件开发指南.md`（插件
  开发完整指引，与既有 `docs/插件体系设计方案.md` 互补）：
  - **插件模型与注册表 API**：PluginKind 四类插件分类（executor / model_provider /
    vision_model_provider / notifier）、Plugin 基类字段、register_plugin / get_plugin /
    has_plugin / list_plugins / plugin_names 便捷函数、PluginRegistry 完整方法表、
    重复注册 / 未注册两类异常；
  - **四类插件接口逐一详解**（附最小可运行示例与启用配置）：ExecutorPlugin.run
    （worker.engine 引用，未注册回退 claude）、ImageProviderPlugin.generate
    （resolve_request_url 自定义 Base URL 语义，issue #150）、
    VisionProviderPlugin.describe（supports_image_url / MinIO 强制 http URL，
    issue #163/#164）、NotifierPlugin.send_*（未启用返回 None、失败不阻塞收尾、
    幂等建议）；
  - **外部插件加载机制**：worker.plugin_paths 启动加载（失败仅记日志不阻塞启动）+
    插件管理页安装 / 卸载 / 重载 / 设置（issue #145）；
  - **测试与调试**：现有插件测试文件清单、自定义插件单元测试示例、全量回归命令；
  - **最佳实践与 FAQ**：向后兼容 / 最小侵入 / 容错 / 幂等 / 中文注释等约定，
    重复注册冲突、未注册查询、引擎回退、自定义 Base URL 404、热加载等常见问题；
  - README「插件体系」章节新增指向该指南的入口（目录结构 docs/ 行同步补充）；
  - 纯文档变更（新增指南文档 + README 指引 + CHANGELOG），无代码 / 测试改动，
    docs-only 提交跳过构建与部署流水线（issue #57）。


- **自动发版机制：中间版本号 +1 时发布新版本并重置 CHANGELOG（issue #294）**：
  新增发版脚本并集成到 CI/CD，每次 main 分支 push 流水线成功后自动检测
  「中间版本号（minor）进位」触发发版——构建版本号由
  `frontend/scripts/gen-version.mjs` 每次构建自增 patch 位（逢百进位，
  issue #179/#283），当 patch 从 99 进位到 minor（如 1.3.99 → 1.4.0）或
  尚无任何版本 tag（首次发版）时，执行发版：
  - `backend/botler/release.py`：发版判定与执行核心——`parse_version`
    严格解析 x.y.z；`latest_released_version` 从 git tag（v 前缀 +
    语义版本）识别最近发布版本；`should_release` 判定触发条件（无 tag
    首次发版 / 当前 minor > 最近发布 minor 触发，patch 自增跳过、版本
    倒退报错）；`run_release` 编排发版——复用 issue #289 的
    `release_changelog()` 把 [Unreleased] 封版为版本节 `## [x.y.z] - 日期`
    并重置 [Unreleased]、归档超龄版本，随后提交 CHANGELOG 变更
    （`chore: 发布 vX.Y.Z 并重置 CHANGELOG（issue #294）`，全角括号引用
    不触发 autoclose）、打 git tag `vX.Y.Z` 标记里程碑、推送主分支 + tag；
    支持 `--dry-run` 预览（不写盘/不提交/不打 tag）、`--no-push` 本地演练、
    `--force` 强制发版；
  - `scripts/release.py`：发版 CLI 入口——版本号缺省按
    `--version-json`（CI 用 frontend:build 产物 `frontend/dist/version.json`）
    > `--version-file`（默认 `<仓库>/data/version.txt`）顺序读取；
    `--push-url` 推送地址（CI 用 `GITLAB_BOT_TOKEN` 构造），缺少推送凭据
    时报错中止而非静默跳过；
  - `.gitlab-ci.yml`：新增 `release` stage（sync 之后）与 `release:auto`
    job——仅 main 分支 push 且含代码变更的流水线执行（docs-only 提交 /
    tag / MR / 其他分支流水线跳过），stage 顺序保证 deploy 成功后才发版，
    只下载 frontend:build 的 dist 产物读取本次构建版本，发版提交仅改
    CHANGELOG/归档文档 → 触发 docs-only 流水线自动跳过构建（避免循环）；
  - **测试**：新增 `backend/tests/test_release.py` 23 例（版本解析合法/
    非法、最近发布版本识别（最高语义版本/前缀过滤/无匹配）、触发判定
    （首次发版/minor 进位触发、patch 自增跳过、major 进位触发、版本倒退
    与非法格式报错）、版本来源读取优先级与容错、run_release 全流程
    （封版重置打 tag 提交/tag 指向发版提交/提交信息规范）、dry-run 不改
    动任何文件、force 强制发版、缺少推送凭据报错、版本缺失报错）；
    后端全量测试无 regression。


- **概览页「Issue 完成耗时」增加每个开启仓库的平均耗时与走势（issue #288）**：
  概览页「Issue 完成耗时」板块（issue #180）在原有全局平均耗时 + 逐日走势图
  基础上，新增**每个已开启仓库**的平均耗时与走势拆分展示：
  - `backend/botler/database.py`：`succeeded_durations()` 返回附带
    repo_id / repo_name（LEFT JOIN repos，仓库软删除/名称缺失回退「未知
    仓库」）的 (repo_id, repo_name, 完成日, 用时秒数) 列表，供按仓库拆分；
  - `backend/botler/api/issues.py`：`GET /api/issues/completion-stats`
    响应新增 `repos` 数组——每个已启用仓库的
    `{repo_id, repo_name, completed_count, avg_seconds, trend}`（仓库按
    配置优先级升序、与 overview 一致；无已完成任务仓库
    completed_count=0 / avg_seconds=null / trend=[]；已禁用仓库不出现，
    其历史任务仍计入全局统计；无任何成功任务时 repos=[]）；
  - `frontend/src/pages/Overview.jsx`：板块内新增各仓库明细列表
    `completion-repo-list`——逐仓库渲染仓库名 / 平均耗时 / 完成数量 /
    紧凑迷你走势图（`CompletionTrendChart` 新增 `compact` 紧凑模式：
    更小画布与数据点、隐藏日期刻度文字、保留悬浮提示），无数据仓库
    显示「暂无数据」；
  - `frontend/src/locales/zh-CN.json` / `en-US.json`：新增
    `overview.completionPerRepoTitle` / `overview.repoNoData` /
    `overview.repoTrendAria` 文案；`frontend/src/styles.css`：新增
    各仓库明细列表 / 行 / 名称 / 数值 / 紧凑走势图样式；
  - **测试**：`backend/tests/test_api_issues.py` `TestCompletionStats`
    新增 4 例（多仓库独立聚合、仓库按优先级排序、禁用仓库排除、无任务
    仓库空值），`test_empty` 断言同步补 `repos` 字段；
    `frontend/tests/overview-completion-stats.test.mjs` 新增源码/样式/
    渲染断言（各仓库行、平均耗时、紧凑折线与数据点、无数据仓库占位）。

- **概览页「其他」分组拖动调整调度顺序（issue #287）**：
  概览页「开放 Issue」的「其他」分组（尚未处理/处理中的 issue）在
  「调度器执行顺序」排序下支持**拖动 issue 上下移动**来手动改变调度顺序：
  - `backend/botler/database.py`：新增 `issue_manual_orders` 表（repo_id +
    issue_iid + position，PRIMARY KEY(repo_id, issue_iid)、UNIQUE(repo_id,
    position)），迁移版本推进到 v19（旧库补建表）；新增
    `list_manual_orders` / `get_manual_order_position` /
    `replace_manual_orders`（整组顺序全量替换，position 从 0 连续编号，
    空列表清空）三个方法；
  - `backend/botler/api/issues.py`：新增 `GET/PUT
    /api/issues/{project_id}/manual-orders` 读写接口（PUT 校验：非正整数/
    重复 iid 剔除保序、空列表清空、超长截断到 200；仓库不存在/未启用
    404；成功后清空 overview 缓存）；`GET /api/issues/overview` 每个仓库
    条目新增 `project_id` 与 `manual_order` 字段（iid 按 position 升序，
    供前端初始渲染与 PUT 定位仓库）；
  - `backend/botler/scheduler.py`：`_task_sort_key` 增加手动标记/位置
    前缀 `(手动标记, 手动位置, 标签权重, issue 创建时间, task_id)`——
    设置过手动顺序的 issue 优先派发（标记 0），按拖动后位置升序；未设置
    的 issue（标记 1）仍按原默认顺序（标签权重 → 创建时间）排后，实现
    「手动改变调度顺序」对实际调度的生效；
  - `frontend/src/pages/Overview.jsx`：新增纯函数 `applyManualOrder`
    （手动顺序前置、缺失 iid 跳过、其余保持原序）与 `moveItem`（列表
    移动）；「其他」分组在「调度器执行顺序」排序 + 无过滤 + 多条目时
    启用 HTML5 拖放（li draggable + gripVertical 手柄 + 拖起半透明/悬停
    落点高亮 + 组头提示），落点提交整组顺序 PUT 保存（乐观更新、失败
    回滚并提示）；保存成功 20 秒内轮询合并保留本地顺序，防 overview
    旧缓存回弹；排序切换/过滤激活/bot 终态分组/单条目时禁用拖动；
  - `frontend/src/components/Icon.jsx`：新增 `gripVertical` 语义图标
    （拖动排序手柄）；`frontend/src/styles.css`：拖动手柄/拖拽态/落点
    高亮/组头提示样式；`frontend/src/locales/zh-CN.json` / `en-US.json`
    新增拖动排序文案（`overview.manualOrderHint` /
    `overview.manualOrderTitle` / `overview.manualOrderError` 等）；
  - **测试**：新增 `backend/tests/test_manual_order.py` 17 例（数据库
    增删改查与仓库隔离、调度器手动顺序优先于标签权重/相对次序/未设置
    排后/空顺序不影响默认派发、API 读写往返/非法输入归一化/超长截断/
    未知仓库 404/未启用 404/overview 透传 manual_order 与 PUT 清缓存），
    迁移测试版本断言同步更新到 v19；新增
    `frontend/tests/overview-issue-manual-order.test.mjs` 18 例（纯函数
    边界、默认调度器排序下可拖/其他排序禁用/bot 分组不可拖/过滤禁用/
    单条目禁用/project_id 兜底、manual_order 预置初始生效且非调度器
    排序不影响展示、拖拽落点 PUT 载荷与展示更新、保存失败回滚与提示
    关闭、i18n 中英文键齐全），图标语义清单同步补 `gripVertical`；
    前端全量测试 1135 例通过、覆盖率门禁通过，后端全量测试无
    regression。

- **概览页开放 Issue 排序方法切换（issue #286）**：
  概览页「开放 Issue」板块新增排序方法切换组件，默认按「调度器执行顺序」
  排序——与任务调度器派发语义一致（仓库优先级 → issue 标签优先级 →
  issue 创建时间升序，创建早的先处理，issue #234），方便用户预判各分组
  issue 的处理顺序（尤其「其他」分组的未处理 issue，issue #287 将在此
  基础上做拖动改调度顺序）：
  - `frontend/src/pages/Overview.jsx`：过滤条顶部新增「排序」行，三个
    选项按钮（调度器执行顺序 / 最近更新 / 创建时间，`aria-pressed` 选中
    态，悬浮说明）；新增纯函数 `loadIssueSort` / `saveIssueSort` /
    `issueLabelWeight` / `schedulerOrderKey` / `sortIssuesByMethod` 与
    存储键 `botler.overview.issueSort`——排序偏好持久化到 localStorage
    （沿用 issue #230 过滤偏好的存取模式：未知排序键/损坏数据回默认、
    无存储环境静默忽略），刷新后保持；`issueLabelWeight` 语义对齐
    `scheduler._task_sort_key`（配置 `worker.issue_priority` 中首个命中
    标签的索引即权重、未命中排最后，优先级顺序从 `/api/settings` 读取，
    未配置回退内置默认 bug > test > feature）；
  - `frontend/src/styles.css`：`.issue-sort-option` 排序按钮样式（与过滤
    状态按钮同风格）；`frontend/src/locales/zh-CN.json` / `en-US.json`
    新增排序文案与悬浮提示（`overview.sort` / `overview.sortBy.*` /
    `overview.sortHint.*` 等）；
  - `frontend/tests/overview-issue-running-top.test.mjs` /
    `overview-issue-filter.test.mjs`：既有断言默认展示顺序的用例固定
    「最近更新」排序注入（排序语义由新测试覆盖）；
  - **测试**：新增 `frontend/tests/overview-issue-sort.test.mjs` 21 例
    （存取纯函数边界：无存储/损坏数据/未知键回默认、非法键不写入、存储
    异常静默；权重计算：索引定权/未命中排最后/空优先级回默认/自定义优先级；
    三种排序语义与稳定性、非数组兜底；渲染：默认选中与默认排序生效、切换
    生效并持久化、预置偏好初始生效、分组内排序且分组结构不变、无 issue
    不渲染排序条；i18n 中英文键齐全），前端全量测试与后端全量测试无
    regression。

- **概览页开放 Issue 分组折叠/展开（issue #285）**：
  概览页「开放 Issue」按 bot 终态标签分组展示（运行中 / bot-failed /
  bot-done / 其他），长列表占据大量纵向空间。本次为每个分组增加折叠开关，
  方便用户折叠长列表：
  - `frontend/src/pages/Overview.jsx`：分组头部新增折叠按钮
    （chevronDown/chevronRight 指示展开/折叠态，`aria-expanded` 无障碍
    语义），折叠后隐藏组内 issue 列表、保留组标题与计数；新增纯函数
    `loadCollapsedGroups` / `saveCollapsedGroups` / `toggleGroupCollapsed`
    与存储键 `botler.overview.collapsedGroups`——折叠偏好持久化到
    localStorage（沿用 issue #230 过滤偏好的存取模式：损坏数据/未知分组
    key 逐项兜底、无存储环境静默忽略），刷新后保持；
  - `frontend/src/styles.css`：`.issue-group-toggle` 图标按钮样式（悬浮
    高亮）；`frontend/src/locales/zh-CN.json` / `en-US.json` 新增折叠/
    展开按钮文案与悬浮提示（`overview.collapseGroup` /
    `overview.expandGroup` / `overview.collapseGroupHint` /
    `overview.expandGroupHint`）；
  - **测试**：新增 `frontend/tests/overview-issue-group-collapse.test.mjs`
    16 例（存取纯函数边界：无存储/损坏数据/未知 key 剔除/getItem 抛异常
    静默；切换纯函数新 Set 语义；渲染：默认全展开、点击折叠隐藏组内列表
    且标题计数保留、组间互不影响、localStorage 预置折叠态初始生效、点击
    写入持久化、aria 文案切换），前端测试 1096 例全通过、覆盖率门禁通过。

- **技能管理页面（issue #282）**：
  新增「技能」页面，展示所有配置的执行引擎（executor 插件）所拥有的技能，
  并支持查看 / 编辑 SKILL.md 以及其他技能相关的 md 文件（如 README.md /
  API.md / 嵌套文档）。技能 = 引擎技能目录下含 SKILL.md 的目录：
  - `backend/botler/skills.py`（新增）：技能目录管理核心——内置引擎技能根
    解析（claude → `~/.claude/skills`、hermes → `$HERMES_HOME/skills`、
    dsh → `$DSH_HOME/skills` + `~/.agents/skills`，外部执行引擎插件可声明
    `skills_dir` 属性覆盖）；技能枚举（递归查找含 SKILL.md 的目录并解析
    frontmatter `description` 作为技能说明，支持嵌套技能如
    `software-development/spike`）；技能目录内 md 文件枚举 / 读取 / 写入；
    安全约束统一收敛在 `safe_md_path`——仅允许 md / markdown 文件、禁止
    路径穿越（`..` / 绝对路径 / 符号链接逃逸）、单文件 2MB 写入上限；
  - `backend/botler/api/skills.py`（新增）：`GET /api/skills`（按引擎分组
    返回技能列表 + 目录根 exists 标记 + 默认引擎标记）、`GET
    /api/skills/{engine}/files?skill=...`（技能 md 文件列表）、`GET
    /api/skills/{engine}/file?skill=...&path=...`（读取）、`PUT
    /api/skills/{engine}/file`（保存）；engine 必须是已注册执行引擎插件、
    skill 必须是含 SKILL.md 的合法目录，非法输入 400 / 404 拒绝；
  - 前端：`frontend/src/pages/Skills.jsx`（新增）——引擎 tab 切换 → 技能
    列表（描述）→ md 文件 chips（SKILL.md 带「技能说明」徽章）→ 编辑器
    （textarea 编辑 + Markdown 预览切换 + 保存，保存即时生效；切换前有
    未保存修改先确认）；`App.jsx` 导航新增「技能」入口并注册 `/skills`
    路由；`styles.css` 技能页样式；i18n 新增 `nav.skills` 中英字典；
    `Icon.jsx` 补充 `fileText` / `eye` 图标；
  - **测试**：新增 `backend/tests/test_skills.py` 31 例（引擎技能根解析含
    HERMES_HOME / DSH_HOME 覆盖、技能枚举与 frontmatter 解析、md 文件
    枚举、路径安全（穿越 / 绝对路径 / 非 md / 符号链接逃逸 / 空路径）、
    读写往返与大小上限、技能目录解析），`test_api_skills.py` 20 例（分组
    列表、文件列表 / 读取 / 保存、嵌套技能寻址、未注册引擎 / 不存在技能
    404、穿越与非 md 400）；前端新增 `frontend/tests/skills-page.test.mjs`
    11 例（导航 / 路由 / 页面结构 / 后端接口静态断言 + mock fetch 渲染与
    引擎切换 / 保存交互），`icons.test.mjs` 图标语义清单同步补充
    `eye` / `fileText`（与 `Icon.jsx` 新增图标一一对应），前端测试
    1080 例全通过、后端全量测试 2077 passed 无 regression。

- **任务失败原因自动分类与处理建议（issue #274）**：
  任务失败时详情页只显示错误信息原文与日志，用户要自己判断失败类型（环境 token
  失效/网络/超时、引擎命令缺失/API Key 失效、还是 agent 无法解决）。本次在任务
  收尾时对失败原因做规则分类，分类结果落库并在详情页/失败评论/统计看板联动展示：
  - `backend/botler/failure_classify.py`（新增）：规则分类模块——按正则/关键字匹配
    错误信息分为 `env`（网络/认证/超时/磁盘）、`engine`（命令缺失/SDK 错误/API key
    无效）、`unsolvable`（agent 明确报告无法解决/自测未过）与 `unknown`（兜底，分类
    错误不报错）；匹配顺序 unsolvable > engine > env，多段文本（失败原因/错误详情/
    执行输出）综合判定；`CATEGORY_LABELS` / `CATEGORY_ADVICE` 提供分类展示名与处理
    建议文案；规则可配置扩展——代码常量 `DEFAULT_RULES` 为内置默认，config.yaml 的
    `failure_classify.rules` 可整体覆盖（非法分类/非法正则被忽略，不影响任务收尾）；
  - `backend/botler/database.py`：tasks 表新增 `failure_category` 列（迁移 v18，旧库
    自动补列），`_TASK_FIELDS` 白名单支持写入；`dashboard_stats` 的 `failure_reasons`
    每条附分类（category/category_name）并新增 `failure_categories` 分类分布（落库值
    优先，旧任务按 error_message 实时分类兜底，统计口径与任务列表一致）；
  - `backend/botler/executor.py`：`_finish_failed` / `_finish_asked` 收尾时分类落库，
    失败评论带分类前缀（「> **失败分类：环境类（env）** — 处理建议」）；评论模版新增
    `{failure_category}` / `{failure_category_badge}` / `{failure_advice}` 占位符
    （templates.PLACEHOLDERS 同步登记）；
  - `backend/botler/config.py` / `config.example.yaml`：`failure_classify.rules`
    配置读取与示例（空配置 = 内置默认规则）；
  - `backend/botler/api/tasks.py`：任务详情/列表返回 `failure_category` 与
    `failure_advice`（详情页展示分类徽章 + 建议，无分类返回空串不报错）；
  - 前端：`frontend/src/failure-categories.js`（新增，分类名/徽章 class 共享），
    `TaskDetail.jsx` / `TaskDetailDrawer.jsx` 失败/中断任务展示分类徽章 + 处理建议，
    `Stats.jsx` 失败原因 Top 列表附分类徽章并新增「失败原因分类分布」区块，
    `styles.css` 分类徽章配色（env=警示 / engine=主色 / unsolvable=错误色 / unknown=灰）；
  - **测试**：新增 `backend/tests/test_failure_classify.py` 36 例（典型失败分类正确、
    多段文本综合判定、unknown 兜底不报错、规则可配置扩展与非法规则容错、标签/建议
    文案），`test_database_migrate.py` 补 failure_category 迁移 3 例，
    `test_api_stats.py` 补分类聚合 2 例，`test_api_tasks.py` 补详情分类/兜底 2 例，
    `test_executor_report.py` 补失败评论分类前缀与落库 2 例；既有 user_version 断言
    更新至 v18；前端测试 1069 例全通过（新增 `frontend/tests/failure-categories.test.mjs` 5 例），后端全量测试无 regression。

- **右上角展示当前登录用户信息与退出入口（issue #271）**：
  SSO 启用后登录流程完整但界面没有当前登录用户展示（多账号/多人共用时身份混淆、
  退出只能清 cookie）。本次在顶部导航右侧落地完整用户区：
  - `frontend/src/components/UserMenu.jsx`（新增）：导航栏用户区组件——SSO 登录后
    显示 OIDC claims 的 name/picture（昵称/头像）与「退出登录」按钮（调用现有
    POST /api/auth/logout，成功后回登录页）；头像加载失败或无 picture 时回退首字母
    占位（验收标准 3）；会话过期时间 tooltip 展示（exp 为 unix 秒，fmtTime 兼容
    转换，与 #221 过期提示联动）；未启用 SSO 时显示「未登录（开放模式）」弱提示
    （验收标准 2，不打扰）；用户信息复用 /api/auth/me 获取（刷新失败回退
    /api/auth/status 探测结果，不报错）；
  - `backend/botler/auth.py`：会话 cookie 与终端 token 的 payload 增加 picture
    字段（OIDC claims 头像随会话携带，旧会话缺失时前端回退首字母）；
  - `frontend/src/api.js`：fmtTime 兼容 unix 秒级数字时间戳（会话过期 exp）；
  - `frontend/src/App.jsx`：导航栏右侧以 UserMenu 组件承接原 user-chip 职责；
    `frontend/src/styles.css`：用户头像 / 首字母占位 / 昵称截断 / 开放模式弱提示
    样式；i18n：nav.notLoggedIn / nav.sessionExpiry 中英字典；
  - **测试**：新增 `frontend/tests/user-menu.test.mjs` 8 例（昵称头像展示、复用
    /api/auth/me、无 picture 与加载失败回退首字母、过期时间 tooltip、未启用 SSO
    弱提示、me 失败降级、退出调用与跳转），`fmt-time.test.mjs` 补 unix 秒/毫秒/
    非法数字 3 例，后端 `test_auth.py` 补 picture 断言；更新
    `version-badge-settings-page.test.mjs`（user-chip 迁入 UserMenu 组件后改查
    组件源码，行为契约不变）。

- **键盘快捷键（概览页 / 任务页常用操作，issue #269）**：
  平台所有操作依赖鼠标点击（新建 issue 点按钮、刷新点按钮、打开任务详情点行），高频
  操作无快捷键效率低。本次引入全局 keydown 监听 + 集中管理的 keymap.js，低成本覆盖
  高频操作：
  - `frontend/src/keymap.js`（新增）：快捷键绑定集中管理（验收标准 4）——`SHORTCUT_DEFS`
    定义表（帮助面板与分发共用同一数据源，新增快捷键只需登记一条 + 页面注册同名 action）、
    单键 + 组合键（`g o` / `g s` 序列，2 秒超时复位、失败降级单键）匹配器、全局 keydown
    分发（`useShortcuts` hook，卸载自动清理；动作表经 getter 实时读取，避免闭包捕获
    过期状态）、启用开关（localStorage 键 botler.shortcuts，默认开启，每次按键实时读取
    即改即生效）、防误触 `isTypingTarget`（input/textarea/select/contenteditable 聚焦
    不触发，验收标准 1）；Esc 不拦截（交由 DialogHost 与各弹窗已有处理）、ctrl/meta/alt
    系统组合键不抢占、长按 repeat 去重；
  - 快捷键集：`n` 新建 issue（概览页，打开首个仓库添加弹窗）、`r` 刷新当前页数据
    （概览页刷新开放 issue/活跃任务/流水线/灵感，任务页刷新任务列表）、`t` 跳转任务列表、
    `g o` 概览 / `g s` 设置（全站生效）、`/` 聚焦搜索框（任务页搜索已存在即联动；
    #216 全局搜索落地后由 keymap 登记扩展）；输入框聚焦不误触；
  - `frontend/src/components/ShortcutHelpModal.jsx`（新增）：页面右上角「快捷键帮助」
    按钮打开的弹窗（验收标准 2）——键位表来自 SHORTCUT_DEFS 单一数据源 + 「启用键盘
    快捷键」开关（一键禁用，验收标准 3，与设置页开关同键位、任意一处切换即时全局生效）；
    关闭方式与现有 Modal 一致（× / 遮罩 / Esc）；
  - `frontend/src/pages/Overview.jsx` / `Tasks.jsx`：页面级注册 n / r / / 动作（复用
    已有 setAddIssueRepo / loadIssues / refreshList / focusElement 等，零重复逻辑）；
    `frontend/src/App.jsx`：导航栏右上角「快捷键帮助」按钮 + 全站级 t / g o / g s 跳转
    （useNavigate）；`frontend/src/pages/Settings.jsx`：「界面显示」卡片新增「启用键盘
    快捷键」开关行（botler.shortcuts）；`frontend/src/components/Icon.jsx` 注册 keyboard
    图标；`frontend/src/styles.css` 帮助按钮 / 弹窗 / kbd 键位样式（跟随深浅色主题变量）；
    i18n：shortcuts.* 中英字典 14 键；
  - **测试**：新增 `frontend/tests/keymap.test.mjs` 28 例（定义表完整、单键/组合键匹配
    与超时复位/失败降级、防误触判定、启用开关读写与异常兜底、分发处理器（开关关闭 /
    输入框聚焦 / 修饰键 / Esc / repeat / 空事件）、useShortcuts hook 生命周期与最新动作
    表回归）、`shortcut-help-modal.test.mjs` 9 例、`overview-shortcuts.test.mjs` 5 例
    （n 打开弹窗 / r 刷新 / 输入框聚焦不误触）、`tasks-shortcuts.test.mjs` 4 例
    （r 刷新 / / 命中与防误触）、`app-shortcuts.test.mjs` 4 例（帮助按钮与面板 / t / g o /
    g s 跳转 / 开关关闭不跳转）、`settings-shortcuts-toggle.test.mjs` 2 例；全量前端单元
    测试（node --test + c8 覆盖率门禁）通过，无 regression。


- **前端界面国际化（中英文切换，issue #268）**：
  平台可对接 GitLab 开放社区，但前端全部文案硬编码中文（jsx 字符串），英文用户/团队协作有门槛，
  魔法字符串散落各处也不利于维护。本次引入轻量自研 i18n（React Context + JSON 字典，不引入
  第三方依赖）：
  - `frontend/src/i18n.jsx`（新增）：`I18nProvider` / `useI18n` / `translate`——默认语言 zh-CN，
    en-US 缺失 key 自动回退中文、key 完全缺失原样返回（未翻译文案回退中文不报错）；语言选择持久化
    到 localStorage（键 botler.lang，与主题 botler.theme issue #217 同模式），刷新后保持；切换
    即时生效并同步 `<html lang>`（屏幕阅读器/浏览器翻译友好）；无 Provider 环境（SSR/单组件测试）
    回退中文，现有测试不受影响；
  - `frontend/src/locales/zh-CN.json` / `en-US.json`（新增）：238 个 key 中英字典，覆盖导航 /
    概览页（余额/开放 Issue/灵感/流水线/完成耗时/用量统计/对话抽屉）/ 任务页（列表/表格/翻页/
    抽屉/失败详情/确认对话框）/ 设置页「界面显示」语言行；中文值与原硬编码文案逐字一致（关键
    测试用稳定文案）；
  - `frontend/src/main.jsx` + `index.html`：应用挂载包 I18nProvider，首屏 inline 脚本按本地偏好
    设置 `<html lang>` 防闪变（与主题同模式）；
  - `frontend/src/App.jsx`：导航文案经 t() 国际化，右上角新增语言快捷切换下拉（中文/English，
    与 user-chip 同侧）；`frontend/src/pages/Settings.jsx`：「界面显示」卡片新增「界面语言」行
    （botler.lang，即时切换并持久化）；`frontend/src/pages/Overview.jsx` / `Tasks.jsx`：高频静态
    文案（标题/按钮/表头/空状态/提示/确认对话框/工具提示）全部经 tr() 翻译，后端错误消息与动态
    内容（任务状态/仓库名/issue 标题等）保持原文；
  - `frontend/src/styles.css`：导航语言切换器样式；
  - **测试**：前端新增 `frontend/tests/i18n.test.mjs` 10 例（translate 中英/回退/缺 key/插值、
    localStorage 读写与非法值兜底、Provider 默认中文/预置 en-US/即时切换并持久化、无 Provider
    回退中文不崩溃、Overview/App 集成在 en-US 下渲染英文）；既有源码断言类测试改为「i18n key +
    字典中文值」双重校验；全量前端单元测试（node --test + c8 覆盖率门禁）通过，无 regression。


- **统计分析看板页：成功率 / 引擎对比 / 仓库排行（issue #264）**：
  平台积累了任务执行数据（status/engine/时长/来源）但只有概览页的 DeepSeek 余额卡片与单仓库平均耗时
  （issue #180），任务成功率、各引擎表现对比、仓库处理量排行都无聚合视图。本次新增独立「统计」页
  （导航入口），数据来自本地任务表聚合，无 GitLab 请求压力：
  - `backend/botler/api/stats.py`（新增）：`GET /api/stats/dashboard`——overview（任务总数/成功率/
    平均耗时/失败数/中断数）+ by_engine / by_repo / by_source（分组对比，空引擎显示「未指定」、
    来源展示 webhook/手动/对账）+ failure_reasons（失败原因 Top 10，failed/interrupted 任务
    error_message 空白归一化 + 截断，与 #40 失败分类口径联动）；days 参数 0=全部 / N=最近 N 天；
    复用概览页 10 秒 TTL 缓存模式（按 days 分桶）；
  - `backend/botler/database.py`：新增 `dashboard_task_rows`（按创建时间 UTC 过滤 + LEFT JOIN 仓库名）
    与 `dashboard_stats`，聚合逻辑为模块级纯函数 `aggregate_dashboard` / `_task_duration_seconds`
    （耗时口径与任务列表「处理用时」及 issue #180 一致：finished_at - created_at，缺字段/非法/负值
    防御性剔除），成功率/耗时/失败原因口径与任务列表同表同源（验收标准 1）；
  - `frontend/src/pages/Stats.jsx`（新增）：总览卡片（任务总数/成功率/平均耗时/失败数）+ 引擎对比 +
    仓库排行 + 来源分布（纯 CSS 条形图，避免引入 recharts 等重依赖）+ 失败原因 Top 分布；时间段选择
    （最近 7 天 / 30 天 / 全部）localStorage 持久化（刷新后保持）；无任务数据渲染空态不报错；接口失败
    显示错误提示不崩溃；`frontend/src/components/Icon.jsx` 注册 chart 图标（BarChart3）；
  - `frontend/src/App.jsx`：导航新增「统计」入口与 `/stats` 路由；`frontend/src/styles.css`：统计页
    卡片/条形图/分组表格/失败原因列表样式（跟随深浅色主题变量）；
  - **测试**：后端新增 `backend/tests/test_api_stats.py` 25 例（耗时口径、失败原因归一化/截断/Top 上限、
    空输入合法结构、按引擎/仓库/来源分组与排序、days 时间段过滤、API 空库零值结构/days 参数 422）；
    前端新增 `frontend/tests/stats-page.test.mjs` 12 例（源码断言：接口路径/days 参数/时间段持久化/
    五个板块/纯 CSS 条形图/导航路由/样式类；渲染断言：默认 days=7、有数据渲染各分组、空态、错误提示、
    切换时间段重拉）。


- **结构化执行报告评论：任务收尾在 issue 留改动文件 / diff 统计 / 测试摘要（issue #252）**：
  任务完成后执行器只在 issue 留模版决定的通用收尾语，用户要了解「改了什么、改了哪些文件、测试跑没跑过」
  得去任务详情页翻日志。本次打通「采集 → 渲染 → 可配置模版」全链路：
  - `backend/botler/report.py`（新增）：采集与渲染纯函数——`collect_diff_data`（git diff --numstat /
    --name-status 相对任务开始前 main 基线，解析改动文件表 + 新增/删除列表 + 行数合计）、
    `parse_test_summary`（从执行日志提取 pytest / jest / flutter / go test 的 pass/fail/error/skipped
    计数，多轮取最新）、`build_diff_table`（Markdown 文件/增/删表格）、`render_comment` +
    `strip_empty_sections`（占位符替换后空段落自动隐藏，无数据不报错）、内置默认成功/失败评论模版；
  - `backend/botler/database.py`：tasks 表新增 `base_sha` 列（任务首次执行开始时工作区 HEAD，迁移 v17，
    旧库启动自动补列）；
  - `backend/botler/executor.py`：三引擎（claude / hermes / dsh）执行入口 `_capture_base_sha` 采集基线
    （重试/续跑不覆盖首次值，采集失败不阻塞任务）；成功收尾 `_leave_success_comment` 与失败收尾
    `_finish_failed` 改为 `_build_report_comment` 按模版渲染结构化评论（成功：结果摘要 / 改动文件 / 测试
    摘要 / 提交链接 / 用时；失败：失败原因 / 相关文件 / 测试摘要 / 日志尾部）；渲染失败回退内置模版，
    写评论失败仍不阻塞任务收尾；
  - `backend/botler/templates.py`：PLACEHOLDERS 新增 `{diff_stat}` / `{test_summary}` /
    `{commit_link}` / `{commit_sha}` / `{duration}` / `{result_summary}` / `{error_message}` /
    `{log_tail}`（仅评论模版生效）；
  - `backend/botler/config.py` + `api/settings.py` + `config.example.yaml`：新增 `templates.comment`
    结果评论模版配置（留空保存 = 恢复内置默认，非字符串 400），设置 API GET/PUT 打通；
  - 前端：`frontend/src/pages/Templates.jsx` 新增「结果评论模版」页签（与全局默认/中断恢复同机制，
    支持全部占位符，留空保存恢复内置默认）；
  - **测试**：后端新增 `backend/tests/test_report.py` 22 例（numstat/name-status 解析、无基线/无工作区
    空结果、pytest/jest/flutter/go 摘要提取、表格与时长渲染、空段落隐藏、默认成功/失败模版无数据不报错）、
    `backend/tests/test_executor_report.py` 7 例（base_sha 首次/续跑/失败、成功评论含 diff 与测试摘要、
    失败评论含原因与相关文件、无 diff 隐藏段落、自定义模版生效）、`test_database_migrate.py` 迁移 v17
    补列断言、`test_api_settings.py` comment 模版读写/清空/400；前端单元测试 976 例全通过。


- **任务执行 token 用量采集与费用统计：三引擎（claude / hermes / dsh）任务详情展示用量卡片，概览页按仓库/引擎/时间段聚合（issue #235）**：
  需求——每个任务都会调用大模型 API，但任务详情只展示状态/日志/提交信息，完全没有记录每次执行消耗了多少 token、调用
  了哪些模型、估算费用是多少；claude CLI 的 stream-json 输出、dsh SDK 的 usage 字段、hermes 的消息记录里都含用量
  数据但没采集。本次打通「采集 → 落库 → 估算费用 → 展示/统计」全链路：
  - `backend/botler/database.py`：新增 `task_usage` 表（engine / model / prompt_tokens / completion_tokens /
    total_tokens / estimated_cost / currency / raw_usage JSON，迁移 v16，旧库启动自动建表）；新增
    `save_task_usage`（同任务覆盖语义，重试以最后一次执行为准）/ `get_task_usage` / `get_task_usage_map`（列表
    批量查询避免 N+1）/ `usage_stats`（按 repo_id / engine / since / until 过滤，返回 summary + by_repo /
    by_engine / by_date 聚合）；
  - `backend/botler/usage.py`（新增）：`parse_claude_result_usage`（result 事件行 usage——input + 缓存读写
    计入 prompt，modelUsage 取模型名，SDK 自带 total_cost_usd 优先作费用）、`extract_dsh_usage`（SDK 会话事件
    流 assistant/chunk 的 usage chunk 累加，DeepSeek OpenAI 兼容字段）、`estimate_cost` / `find_pricing`
    （config 单价表，精确匹配优先再子串匹配，无单价返回 None=只展示 token 数）、`finalize_usage`（统一归一化）；
  - `backend/botler/config.py`：新增 `usage` 配置段——`currency`（默认 USD）+ `pricing` 单价表（每项 model /
    input_per_million / output_per_million，model 支持子串匹配），已写入 `config.example.yaml`；
  - `backend/botler/dsh_runner.py`：`DshRunner` 执行后从 `result.events` 聚合 usage 落 `runner.usage`，结果行
    附带 usage（日志可诊断）；`backend/botler/hermes_sdk_runner.py`：`HermesSdkRunner` 执行后从 agent 会话级
    计数器（session_prompt_tokens / session_completion_tokens / session_total_tokens /
    session_estimated_cost_usd 等）聚合 usage 落 `runner.usage`，结果行附带 usage；
  - `backend/botler/executor.py`：`_persist_engine_usage` 统一落库（费用：引擎自带费用 > config 单价估算 >
    None 只展示 token 数；落库失败仅记日志不阻塞收尾），三引擎执行路径（claude 停止/超时/正常、hermes、dsh
    停止/超时/正常 + 碰撞重跑）全部接入，执行日志新增「token 用量已记录」行；
  - `backend/botler/api/tasks.py`：任务详情/列表（`include_usage=1` 可选展示，批量查询）附 `usage` 字段（无
    用量数据为 null，前端显示「无数据」而不是报错）；`backend/botler/api/usage.py`（新增）：
    `GET /api/usage/stats` 按仓库/引擎/时间段聚合接口（非法日期 400）；
  - 前端：`frontend/src/components/UsageCard.jsx`（新增）用量卡片组件（引擎/模型/输入/输出/总 tokens/估算费用，
    无数据「无数据」、无单价「未估算（未配置单价）」）+ 列表摘要 `UsageSummary`；`TaskDetail.jsx` 与
    `TaskDetailDrawer.jsx` 元信息区下方展示用量卡片；`Tasks.jsx` 任务列表「显示用量」勾选（可选展示列，窄视口
    可隐藏）；`Overview.jsx` 新增「Token 用量统计」板块（仓库/引擎/时间范围过滤器 + 合计 tokens/费用 + 按引擎/
    仓库分组表格，60 秒低频轮询，空态与失败提示不崩溃）；`styles.css` 新增用量卡片/列表/统计板块样式；
  - **测试**：后端新增 `backend/tests/test_task_usage.py` 34 例（DB 覆盖/聚合/过滤、claude result 解析缓存
    token 计入、dsh chunk 累加、费用估算精确/子串/无单价、executor 落库与日志、API 详情/列表/统计与无数据
    null）；`test_database_legacy_cst.py` / `test_database_migrate.py` 迁移版本断言更新至 v16；前端新增
    `frontend/tests/task-detail-usage.test.mjs` 7 例、`tasks-usage-column.test.mjs` 5 例、
    `overview-usage-stats.test.mjs` 6 例；前后端全量测试通过，无 regression。


- **前端可见版本号（含 commit/时间）+ 后端 /api/health 版本对齐 + 新版部署后页面提示刷新，排查「这个功能部署了吗」不再靠猜（issue #233）**：
  需求——`VersionBadge.jsx` 组件与 `gen-version.mjs` 构建版本生成早已存在（issue #9），但版本信息展示缺 commit、后端
  `/api/health` 版本号硬编码 `1.0.0` 与前端生成版本不一致、新版部署后页面无任何提示，用户无法直观确认当前部署版本。
  本次打通「构建产物 → 前后端展示 → 更新感知」全链路：
  - `frontend/scripts/gen-version.mjs`：构建产物 `version.json` 新增 `commit` 字段（issue #233）——`CI_COMMIT_SHA`
    优先（GitLab CI 注入）、本地回退 `git rev-parse HEAD`、非 git 环境（Docker 构建无 .git）静默省略该字段（前端
    降级显示版本 + 构建时间）；新增纯函数 `shortCommit`（sha 截断前 8 位）与 `currentCommit`；新增 `BOTLER_PUBLIC_DIR`
    环境变量覆盖输出目录（单测用临时目录，不污染真实 `public/version.json`）；
  - `frontend/src/components/VersionBadge.jsx`：设置页「关于 → 版本信息」卡片展示 `v版本 · commit 短号 · 构建时间`，
    无 commit 时静默省略（不显示占位符）；
  - `frontend/src/version-update.js`（新增）：版本更新检查模块——`createVersionChecker` 启动后立即检查一次并每 60s
    轮询 `/version.json`（与 VersionBadge 同源），首次成功只记录基线不提示、版本变化只提示一次（忽略后不再重复打扰）、
    轮询失败静默跳过；`detectVersionChange` 纯函数判定版本变化；
  - `frontend/src/App.jsx`：挂载版本检查器，检测到新版本（新版部署完成）渲染右下角刷新横幅（`.version-update-banner`，
    toast 风格，含「立即刷新」整页重载与「忽略」按钮）；`frontend/src/styles.css` 新增横幅与 commit 短号样式；
  - `backend/botler/version.py`（新增）：`read_version_info` 优先读取构建产物 `frontend/dist/version.json`（与前端
    VersionBadge 同源，含版本 + 构建时间 + commit），缺失时回退 `data/version.txt`（`BOTLER_DATA_DIR` 约定，本地开发/
    CI 测试兜底），双缺失返回 None；`build_health_payload` 组装 `/api/health` 响应（version 字段永远存在，无信息时
    `0.0.0`，健康检查本身不受版本文件缺失影响）；
  - `backend/botler/main.py`：`/api/health` 移除硬编码 `"version": "1.0.0"`，改用 `load_version_info()`（dist 产物 →
    data/version.txt 回退），新增 `build` 字段携带构建时间与 commit；
  - **测试**：前端新增 `frontend/tests/version-update.test.mjs` 9 例（detectVersionChange 纯函数边界 / 首次仅记基线 /
    版本变化触发且只触发一次 / 轮询失败静默 / start-stop 生命周期）、`frontend/tests/version-badge-commit.test.mjs` 7 例
    （Badge 渲染 commit 与构建时间 / 无 commit 省略 / gen-version 生成 commit 字段 / App 挂载提示 / 设置页文案），
    `gen-version.test.mjs` 扩充 4 例（shortCommit 边界 / currentCommit / 主流程临时目录生成含 commit 的 version.json /
    非 git 环境省略 commit）；后端新增 `backend/tests/test_version.py` 12 例（version.json 全量/最小/空字段容错、
    非法 JSON 与缺版本号回退 txt、双缺失 None、health 负载有/无版本信息与统计可缺省）；前后端全量测试通过，无 regression。

### Fixed

- **dsh 会话根目录压缩编码归一化：遗留明文 session.jsonl 不再让 runtime 拒绝启动（issue #302）**：
  任务 #415（tender_system issue #9）反复失败的根因——deepseek-harness runtime 的
  `session-persistence-jsonl` 插件默认以 zstd 压缩写会话文件（`session.jsonl.zstd`），
  且加载/恢复会话前会做根级编码检查（ensureRootEncoding）：只要会话根目录存在旧版部署
  遗留的明文 `session.jsonl`（压缩模式 none），整个 runtime 拒绝启动，报
  `session artifact ".../session.jsonl" uses .jsonl, but this backend is configured for
  compression "zstd"`，重试/降级全新会话全部在启动处直接失败：
  - `backend/botler/dsh_sessions.py`（新增）：`effective_session_root`（dsh 会话根目录
    解析：显式配置 `dsh.session_root` 用之，否则 `<工作区>/.sessions`，与 SDK 行为一致）、
    `normalize_session_root_encoding`（把会话根目录归一化到 zstd——遗留明文
    `session.jsonl` 转成 runtime 兼容的 zstd 分帧格式（首帧恰为头部一行，后续帧按批
    压缩，解码后与明文逐字节一致）并删除明文；已存在 zstd 副本时以 zstd 为准仅删明文；
    空明文直接清理；单目录修复失败不阻塞整体）；
  - `backend/botler/executor.py`：`_run_dsh_once` 在每次 dsh 执行前调用归一化并记日志
    （修复数量为 0 时不刷日志），归一化失败不阻塞执行（runtime 自会报错）；
  - `backend/requirements.txt`：新增 `zstandard>=0.22`（归一化压缩用，Docker / pm2 部署
    均随 requirements 安装）；
  - **测试**：新增 `backend/tests/test_dsh_sessions.py` 12 例（明文→zstd 转换逐字节
    一致、首帧恰为一行头部、zstd 副本优先、空明文清理、缺失目录/无遗留/无关文件不动、
    多项目批量、effective_session_root 解析），`test_executor.py` 新增 2 例
    `_run_dsh_once` 接线（真实归一化执行 + 无遗留时不写日志），全部通过无 regression。

- **security:semgrep 扫描显式放宽单规则单文件超时（--timeout 30），消除大文件规则
  偶发超时导致 CI 误判失败（issue #274 收尾联动）**：
  `js-hardcoded-secret` 规则对长字符串字面量执行前瞻正则匹配，`Overview.jsx` 等
  大文件在并发负载下偶发超过 semgrep 默认 5 秒单文件超时，触发「防假绿」健康检查
  报 `Timeout when running ci.js-hardcoded-secret` 使流水线失败（流水线 #1143 实测）。
  本次为 JSON 阻断扫描与 SARIF 报告扫描两处均补充 `--timeout 30`（作业 timeout 20m
  不受影响），同类偶发超时不再误伤流水线。

### Changed

- **CI 后端测试 pytest-xdist 并行加速，1500+ 用例串行改并行（issue #211）**：
  背景——`backend:test` 串行跑全部 pytest 用例（流水线 #1169 实测 2112 例），
  步骤4（运行测试）用时 327 秒且随用例增长线性上升，CI 反馈慢：
  - `backend/requirements.txt`：新增 `pytest-xdist>=3.0`（多进程分片，与
    pytest-cov 兼容，各 worker 覆盖率自动合并）；
  - `.gitlab-ci.yml` `backend:test` 步骤4：`pytest -n "${PYTEST_WORKERS:-auto}"`——
    默认 `auto`（CPU 核数，code01 为 16 核），可用 CI 变量 `PYTEST_WORKERS`
    覆盖为固定值（如 4 / 8）；日志新增并行 worker 数与「改造前串行基线 327 秒 →
    本次并行用时」对比行（验收标准「对比并行前后耗时，记录到 job 日志」）；
  - **数据库测试隔离确认**：backend/tests 各用例使用独立 `tmp_path` SQLite 临时库
    （per-test 隔离），无共享状态，并行安全（验收标准「全量用例并行下无 flaky」）；
  - `frontend/package.json`：`npm test` / `npm run test:coverage` 的 `node --test`
    增加 `--test-concurrency=8` 显式并行（node --test 默认已按核数并行，显式固定
    8 worker 平衡并行度与同 stage 其他 job 资源占用）；
  - `README.md`「测试」章节同步补充并行运行方式；
  - **测试结果一致**：并行模式 2135 例全量通过、覆盖率门禁（70%）不受影响，
    coverage.xml 合并上报正常，无 flaky。

- **任务列表/详情页新增单任务「停止」与详情页「重试」操作，解决单任务操作入口分散（issue #214）**：
  需求——概览页 issue 右边栏有「重试」（issue #117）与「关闭 issue」，调度器支持单任务
  `request_stop`（executor 已实现）但任务列表/详情页 UI 无「停止当前任务」按钮，用户只能
  「一键停止所有任务」（issue #35）或等任务自然结束；任务详情页也无法重试失败任务——
  - `backend/botler/database.py`：新增 `stop_task(task_id)`——单任务手动停止：活跃任务
    （queued/running/retrying）标记 interrupted（写 `用户手动停止（单任务停止）` 错误信息
    与 finished_at、落 warn 日志），终态任务拒绝（bad_state）、不存在 not_found；条件
    UPDATE 兜底并发，与 `stop_active_tasks`（issue #35）语义一致：interrupted 为终态，
    平台重启后不会自动恢复执行；
  - `backend/botler/scheduler.py`：新增 `remove_queued(task_id)`——单任务停止时把排队中
    任务从调度器内存队列移除（否则派发后 worker claim_task 状态不匹配跳过，stats queued
    计数失真）；
  - `backend/botler/api/tasks.py`：新增 `POST /api/tasks/{task_id}/stop`——404（任务不
    存在）/ 400（非活跃状态不可停止）/ 200（先落库 interrupted → `executor.request_stop`
    登记停止请求并终止引擎进程（幂等）→ `scheduler.remove_queued` 移除排队任务），返回
    `{task_id, status: "interrupted"}`；顺序保证与 stop_all 一致（先落库再登记停止请求）；
  - `frontend/src/pages/Tasks.jsx`：任务列表操作列新增「停止」按钮（仅 running 任务，危险
    样式，点击弹确认框——停止不可逆，确认后调 `POST /api/tasks/{id}/stop`，请求中禁用
    显示「停止中…」，成功提示 `任务 #N 已停止` 并刷新列表，失败行内报错）；
  - `frontend/src/pages/TaskDetail.jsx`：任务详情页 kv 表格新增「操作」行——running 任务
    显示「停止」按钮（确认框 + 危险样式 + 请求中禁用），failed/interrupted 任务显示
    「重试」按钮（复用任务列表页手动重试 issue #36 的 `POST /api/tasks/{id}/retry` 逻辑，
    接续断点续跑会话）；操作成功/失败行内横幅反馈（失败不整页替换，加载错误仍走整页提示）；
  - **测试**：新增 `backend/tests/test_task_stop_single.py` 15 例（db 层三种活跃状态停止落库 /
    终态与不存在拒绝 / 重复停止拒绝；API 200/400/404 / 停止后状态与日志 / 排队任务从调度
    队列移除 / executor.request_stop 调用链路）；新增 `frontend/tests/tasks-stop-button.test.mjs`
    7 例（源码断言 / 仅 running 渲染 / 确认取消不调用 / 确认后调用并提示 / 失败提示 /
    请求中禁用）、`frontend/tests/task-detail-stop-retry.test.mjs` 11 例（源码断言 /
    running 显示停止 / failed 与 interrupted 显示重试 / succeeded 无操作 / 停止确认取消 /
    停止与重试调用与成功提示 / 停止失败行内提示不整页替换 / 请求中禁用）；前后端全量测试
    通过，无 regression。

- **概览页仓库卡片新增「发掘」：agent 根据项目实现的功能去 GitHub 搜索类似仓库、翻找用户需求 issue，整理成若干条需求写入该仓库 issue（issue #189）**：
  需求——概览页面，仓库的右边增加一个「发掘」按钮，点击之后根据项目的实现的功能，去 GitHub 上搜索类似仓库，
  并翻找类似仓库里 issue，查看用户对类似项目提出的需求，整理成若干条需求后，把需求写到对应仓库的 issue 里，
  分配人选择仓库的 owner，一条需求一个 issue——
  - `backend/botler/api/discover.py`（新增）：`POST /api/repos/{repo_id}/discover` 同步执行——
    校验仓库（不存在 404 / 软删除与未启用 400）→ 收集项目上下文（复用自省 issue #187 收集链路：本地
    项目文件夹优先、GitLab 仓库 API 兜底，均缺失时仅基于仓库元信息继续并提示模型如实说明，收集失败
    不阻塞）→ 调 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key 非空的项，issue #166/#187
    同一链路）基于项目功能生成 GitHub 搜索关键词（严格 JSON 数组，取前 4 个）→ 调 GitHub REST API
    search/repositories 搜索类似仓库（按 star 降序取前 5、跨关键词去重、最多考察 5 个）→ 翻找类似仓库
    开放 issue（issues API 每仓前 15 条、过滤 pull_request、总量封顶 40 条）→ 调 AI 对话模型把原始需求
    整理成若干条需求（严格 JSON 数组：标题 + 说明 + 参考来源；去重、封顶 8 条）→ 逐条创建 GitLab issue：
    标题带【发掘】前缀（超 255 字符截断）、标签 feature（需求语义）、分配人 = 仓库 owner（与自省同一
    解析链路：GitLab 项目 owner 优先、仓库 remote 用户名兜底、解析失败不指定分配人）、描述含需求说明
    与参考来源链接；写 issue 与概览页其他 issue 编辑一致（`_issue_edit_call`）：必须使用 owner token，
    绝不回退 bot token；创建成功后清空概览缓存。GitHub 匿名调用（可选环境变量 `GITHUB_TOKEN` 提升
    限额），限流 403/429 与网络错误 502 明确报错引导；错误映射：未配置 AI 对话模型 400 引导设置页、
    AI 失败/空回复/搜索词或需求解析失败 502、无相似仓库/无需求 issue 502、创建 issue 失败 502（提示
    已创建条数）；
  - `backend/botler/api/__init__.py`：注册 discover 路由；
  - `frontend/src/pages/Overview.jsx`：每个仓库卡片右上角操作组（`.issue-repo-actions`）在「自省」
    按钮后新增「发掘」按钮（compass 图标，请求中禁用并显示「发掘中…」，成功后刷新开放 issue 列表），
    新增 `DiscoverResult` 结果组件（加载中 / 已创建 N 个发掘 issue 链接列表 / 失败原因）；
  - `frontend/src/components/Icon.jsx`：新增 `compass`（发掘按钮）Lucide 语义图标；
  - `frontend/src/styles.css`：新增 `.discover-btn` / `.discover-result` 样式（与自省按钮同风格，
    白字不换行、结果小字提示与跳转链接 hover 下划线）；
  - **测试**：新增 `backend/tests/test_api_discover.py` 31 例（正常路径本地上下文 / GitLab 兜底 /
    跨关键词仓库去重 / 分配人三种解析路径 / 需求标题去重与条数封顶 / 标题截断 / 概览缓存失效 /
    GitHub 限流与网络错误 / 无相似仓库 / 无需求 issue / AI 失败空回复与解析失败 / 创建 issue 失败 /
    GitHub 请求头 GITHUB_TOKEN 可选 / JSON 数组解析兼容代码围栏与前后缀），新增
    `frontend/tests/overview-discover.test.mjs` 11 例（源码/样式断言 / 渲染按钮成组 / 点击接口参数 /
    请求中禁用 / 成功链接列表与刷新 / 失败提示），更新 `icons.test.mjs` 语义名清单补 `compass`；
    前后端全量测试通过，无 regression。

- **仓库管理页新增「生成图标」：agent 基于 README 生成 logo 提示词并调用生图模型生成 logo（issue #188）**：
  需求——在仓库设置页面，每个仓库的右侧增加一个「生成图标」的按钮，点击后让 agent 根据这个项目的
  readme.md 来生成最适合这个项目的图标的提示词，并使用这个提示词调用生图模型来生成 logo（要求简约
  美观大方）；生成的 logo 显示在仓库页面每个仓库的最左侧，用户可点击放大并下载——
  - `backend/botler/api/repo_logo.py`（新增）：`POST /api/repos/{repo_id}/generate-logo` 同步执行——
    校验仓库（不存在 404 / 软删除与未启用 400）→ 收集 README（复用自省 issue #187 收集链路：本地
    项目文件夹优先、GitLab 仓库 API 兜底，均缺失时仅基于仓库元信息继续并提示模型合理推断，收集失败
    不阻塞）→ 调用 AI 对话模型（复用设置页「AI API 供应商」第一个启用且 Key 非空的项，issue #166/
    #187 同一链路）生成 logo 生成提示词（系统提示词约束：直接输出英文 prompt、简约美观大方、扁平化
    极简几何风格、≤200 词）→ 调用生图模型（复用设置页「生图模型」第一个启用且 Key 非空的项，issue
    #135/#137 的 ImageModelClient）生成 logo → 首张图片落盘 `backend/data/logos/<repo_id>.<ext>`
    （随 docker-compose 挂载持久化）并写 repos 表 `logo_path` / `logo_updated_at` / `logo_mime`；
    超时 180s；错误映射：未配置 AI 对话模型 / 生图模型 400 引导设置页、AI 提示词失败与空回复 /
    生图失败与空结果 502；重复点击同名文件覆盖重生成；
  - `backend/botler/api/repo_logo.py`：`GET /api/repos/{repo_id}/logo` 读取已生成 logo（Content-Type
    按 logo_mime，img src 直连）；`?download=1` 附加 `Content-Disposition: attachment` 供下载；
    未生成 404 / 文件缺失 404 引导重新生成；
  - `backend/botler/database.py`：repos 表新增 `logo_path` / `logo_updated_at` / `logo_mime` 三列
    （_SCHEMA 覆盖新库，_migrate v15 为旧库补列），`update_repo` 支持写入；`api/repos.py`
    `_repo_row_to_dict` 输出 logo 元信息（前端按 `logo_path` 是否非空决定展示、`logo_updated_at`
    作 img src 缓存击穿参数）；
  - `frontend/src/pages/Repos.jsx`：仓库管理页每个仓库行最左侧展示已生成 logo（未生成时虚线占位框，
    点击「生成图标」生成），右侧操作组新增「生成图标」按钮（请求中禁用并显示「生成中…」，成功后
    刷新列表并显示「已生成 logo」结果，失败展示后端错误）；点击 logo 打开放大弹窗（大图 + 「下载
    logo」按钮，走 `?download=1`）；`frontend/src/components/Icon.jsx` 新增 `download` / `image` /
    `sparkles` 三个 Lucide 语义图标；
  - `frontend/src/styles.css`：新增 `.repo-logo` / `.repo-logo-btn` / `.repo-logo-placeholder` /
    `.logo-btn` / `.modal.repo-logo-modal` / `.repo-logo-view` 样式（缩略图 hover/active 微反馈、
    大图入场微缩放，apple-design 动效节奏）；
  - **测试**：新增 `backend/tests/test_api_repo_logo.py` 19 例（正常路径本地 README / GitLab 兜底 /
    无 README 元信息兜底 / 收集故障降级 / 重复生成覆盖 / 列表带 logo 字段 / 读取接口与下载头 /
    各类 400/404/502 边界）与 `backend/tests/test_database_migrate.py` 迁移 3 例（旧库补列 / 新库
    建表 / update_repo 写入），更新既有迁移测试 user_version 断言 14 → 15；新增
    `frontend/tests/repos-generate-logo.test.mjs` 6 例（源码/样式断言 / 渲染缩略图与占位 / 请求中
    禁用 / 成功刷新 / 失败提示 / 放大弹窗与下载），更新 `icons.test.mjs` 语义名清单；前后端全量测试
    通过、覆盖率达标，无 regression。

- **灵感组件保持原始位置，AI agent 对话保持右侧边栏形式（issue #293）**：
  issue #184 曾把概览页改为双栏布局——灵感板块移入右侧常驻边栏、AI 对话改为
  右侧抽屉；本次按 issue #293 要求回退布局——「灵感组件还是和原来一样，放在
  开放 issue 组件的下方」，仅保留 AI 对话的右侧边栏抽屉形式——
  - `frontend/src/pages/Overview.jsx`：移除双栏布局容器（`overview-layout` /
    `overview-main` / `overview-sidebar`），灵感板块（`inspirations-section`）
    从右侧常驻边栏移回主流程，位于「开放 Issue」板块（`issues-section`）与
    「CI/CD 流水线」板块（`pipelines-section`）之间；灵感 AI 对话面板保持
    右侧边栏抽屉（`drawer chat-drawer`）不变；
  - `frontend/src/styles.css`：删除 `.overview-layout` / `.overview-main` /
    `.overview-sidebar` 双栏布局样式（含 ≥1180px 双栏断点与边栏吸附），
    灵感板块样式恢复全宽板块（位于开放 Issue 与 CI/CD 流水线之间）；
  - **测试**：更新 `overview-inspirations.test.mjs` 布局断言（源码与渲染均
    断言灵感板块位于开放 Issue 与 CI/CD 流水线之间、不再有双栏容器与右侧
    边栏）与 `overview-issue-task.test.mjs` 板块顺序断言（开放 Issue →
    灵感 → CI/CD 流水线 → Issue 完成耗时），全量前端测试通过，无 regression。

- **概览页仓库卡片新增「自省」按钮：调用 AI agent 审查项目并写入改进建议 issue（issue #187）**：
  需求——概览页仓库的右边增加一个「自省」按钮，点击后调用 agent 审查项目的功能和实现情况，
  对项目的改进提出建议，并把建议写到对应仓库的 issue 里，分配人选择仓库的 owner——
  - `backend/botler/api/introspection.py`（新增）：`POST /api/repos/{repo_id}/introspect`
    同步执行审查——校验仓库（不存在 404 / 软删除与未启用 400）→ 收集项目上下文（本地
    项目文件夹优先：文件树 + README + 关键清单文件，深度/条目/长度封顶；无本地文件夹时
    回退 GitLab 仓库 API 的文件树 + README，收集失败仅记日志不阻塞）→ 调用 AI 对话模型
    （复用设置页「AI API 供应商」第一个启用且 Key 非空的项，与灵感对话 issue #166 同一
    链路，超时 120s）生成审查报告 → 在仓库创建 issue（标题带【自省】前缀与时间戳、描述
    为审查报告、标签 `optimize`、分配人 = 仓库 owner：GitLab 项目 owner 优先、仓库 remote
    用户名兜底、解析失败不指定分配人不阻塞）→ 清空概览缓存；写 issue 走 owner token
    （`_issue_edit_call`，与概览页其他 issue 编辑一致，未配置返回 400 引导设置）；
    标题超长按 GitLab 255 字符硬上限截断（复用 issue #186 规则）；
  - `frontend/src/pages/Overview.jsx`：开放 Issue 板块仓库卡片右上角操作组新增「自省」
    按钮（与「对账」「添加 Issue」并排），点击调 `POST /api/repos/{repo_id}/introspect`，
    请求中禁用并显示「自省中…」防重复点击；成功后刷新开放 issue 列表并展示「已创建自省
    issue」+ GitLab 跳转链接，失败展示后端错误信息；
  - `frontend/src/styles.css`：新增 `.introspect-btn` / `.introspect-result` 样式
    （与对账按钮/结果同一视觉层级）；
  - **测试**：新增 `backend/tests/test_api_introspection.py` 17 例（正常路径本地上下文 /
    GitLab 上下文兜底 / 上下文失败降级 / 分配人四种解析路径 / 标题 255 上限 / 缓存失效 /
    AI 失败与空回复 502 / 创建失败 502 / 未配置 AI 模型与 owner token 400 等）与
    `frontend/tests/overview-introspect.test.mjs` 7 例（渲染 / 点击参数 / 请求中禁用 /
    成功刷新与跳转链接 / 失败提示），前后端全量测试通过，无 regression。

- **概览页「灵感」板块与 AI agent 对话改为右侧边栏形式（issue #184）**：此前灵感
  板块是横贯全宽的网格、AI 对话是居中弹窗——长页面上灵感要上下滚动寻找，对话弹窗
  还会遮挡整页内容；本次将概览页改为双栏布局，灵感组件与 AI 对话都以「右侧边栏」
  形式呈现——
  - `frontend/src/pages/Overview.jsx`：新增 `overview-layout` / `overview-main` /
    `overview-sidebar` 双栏结构，主内容（DeepSeek 余额 / 开放 Issue / CI/CD 流水线 /
    Issue 完成耗时）在左列，灵感板块（`inspirations-section`）迁入右侧边栏 `<aside>`；
    灵感 AI 对话面板由居中弹窗（`modal-overlay` + `modal chat-modal`）改为右侧边栏
    抽屉（`drawer-overlay` + `drawer chat-drawer`，复用 issue #85 右侧抽屉体系：
    从右侧滑入，遮罩点击 / × / Esc 关闭，消息列表内部滚动、头部与输入区固定）；
  - `frontend/src/styles.css`：新增双栏布局样式（宽屏 ≥1180px 双栏、右侧边栏 360px
    并随滚动吸附顶部，窄视口单列堆叠在主内容下方）与 `chat-drawer` 抽屉样式；
  - **测试**：更新 `overview-inspirations.test.mjs` 布局断言（源码双栏结构：主板块
    位于 `overview-main`、灵感板块位于 `overview-sidebar` 内；渲染时灵感标题/内容
    位于右侧边栏、主内容在左列；对话面板断言 `drawer chat-drawer` 抽屉结构且不再
    使用居中 modal；关闭后抽屉卸载）与 `overview-issue-task.test.mjs` 板块顺序断言
    （灵感板块 DOM 顺序移至主内容之后、视觉位于右侧边栏），全量前端测试通过
    （858 例），无 regression。


- **设置页「网页通知」卡片新增独立保存按钮，开关可单独保存生效（issue #292）**：
  用户反馈「设置里的网页通知增加一个保存按钮，现在无法保存设置」——此前「网页通知」
  卡片内只有开关与说明文字，保存入口是「任务调度」卡片里的全局「保存」按钮，卡片在
  其下方且说明仅提示「点击上方保存」，用户修改开关后找不到保存入口，误以为无法保存
  （与 issue #27 SSO / #141 Webhook / #142 界面显示卡片同款问题）——
  - `frontend/src/pages/Settings.jsx`：新增「保存网页通知配置」按钮（`saveNotify`，
    只提交 `notifications` 段，后端 `PUT /api/settings` 支持部分更新，不影响
    worker/claude/ui/webhook 等其他设置），成功后卡片内显示「已写回 config.yaml」
    提示；卡片说明文字由「点击上方「保存」」改为「点击下方「保存网页通知配置」」；
    全局「保存」按钮仍同时提交 notifications 段，两处保存行为一致；
  - **测试**：新增复现用例 4 例（`settings-notifications-save-button.test.mjs`：
    卡片内应含「保存网页通知配置」按钮并绑定 `saveNotify`、`saveNotify` 只提交
    notifications 段、说明文字不再指向「上方保存」、全局保存按钮保留），修复前
    全部失败、修复后通过；前端全量测试通过，无 regression。

- **修复 dsh 引擎重试/恢复时 SDK 会话 id collision 导致任务反复失败：碰撞即如实降级
  为全新会话（issue #291）**：任务 #388/#390/#391 首次运行失败后，重试与平台重启
  恢复均复用 `tasks.dsh_session_id` 已落库 id 调 `harness.run(prompt, session_id=)`
  断点续跑，但 deepseek-harness SDK 0.1.0rc6 的 runtime 语义是「跨进程 resume 的
  seed 必须与磁盘已持久化的事件前缀逐事件一致（seq-aligned 重放）」，botler 的恢复
  引导语必然不匹配 → 每次重试必撞 `already has a persisted log on disk that does
  not match this live session (id collision)` → 3 次重试耗尽 → 任务 failed（生产
  日志 task_388/390/391 证实：每次尝试 1 秒内被打回，`attempt_count=3`）——
  - `backend/botler/executor.py`：新增 `_dsh_collision` 识别 SDK id collision
    （结果行 `finish_reason=error` 且输出含 `id collision` 特征）；`_run_dsh_once`
    提取 `_run_round` 内部函数承载「构造 runner → 等待 → 收尾」单轮执行，碰撞检测
    命中即生成新会话 id 落库、以全新提示词（不含恢复引导语）重跑一次，聊天记录
    同步重置为全新会话视角；降级不无限递归（重跑再撞则如实失败），如实记 warn 日志
    说明降级原因；**降级不丢进度记忆**：新增 `_dsh_downgrade_prompt` 把
    `task_progress` 进度账本交接单渲染进降级提示词（已完成步骤 + 证据 / 下一步 /
    禁止重做已标记 done 的步骤），配合保留的工作区，新会话按账本接续而非从头
    重复实现（issue #281 用户抱怨的原始痛点——平台重启必然打断运行中的任务，
    若恢复即失忆，每次部署都会造成 agent 从头重做）；
  - **测试**：新增碰撞降级复现用例 5 例（`test_executor_dsh.py`：resume 撞 collision
    换新 id + fresh prompt 重跑成功、降级提示词携带进度账本交接单、二次碰撞直接
    失败防死循环、普通 error 不误降级、`_dsh_collision` 判定单测），修复前全部
    失败、修复后通过；后端全量 pytest（1789 例）与前端测试（854 例）通过，
    无 regression。

- **新增 CHANGELOG.md 发布轮转机制（issue #289）**：此前 `CHANGELOG.md` 遵循 Keep a
  Changelog 约定却没有任何发布/重置机制，所有条目永远堆积在 `[Unreleased]` 下（曾达
  4500+ 行、200+ 条），版本节从未生成、历史也不会归档，文件无限膨胀。本次新增发版工具：
  - `backend/botler/changelog_release.py`：`release_changelog()` 把 `[Unreleased]`
    封版为 `## [x.y.z] - 日期` 版本节并重置 `[Unreleased]`；按 `keep` 保留最近 N 个
    版本节，更早的版本节按时间正序归档到 `docs/CHANGELOG-archive.md`（首次自动创建）；
    版本号缺省读 `data/version.txt`、日期缺省今天；空 `[Unreleased]` / 版本重复 /
    文件缺失 / 缺 Unreleased 节等场景抛 `ChangelogReleaseError` 且不改动文件；
    支持 `dry_run` 预览；
  - `scripts/release_changelog.py`：CLI 封装（`--version/--date/--keep/--dry-run`），
    发版时一键封版 + 重置 + 归档；
  - **测试**：新增 `test_release_changelog.py` 17 例（封版重置/内容原样保留/前言与
    小节保留/版本文件与默认版本/连续多次发版新版本置顶/归档轮转与追加/空节与重复版本
    与非法版本报错/失败不改文件/dry-run 不写盘/解析单测），修复前模块不存在（复现
    `ModuleNotFoundError`）、修复后通过；后端全量 pytest（1784 例）与前端测试通过，
    无 regression。

- **修复定时暂停窗口对全角字符的兼容性：中文输入法常见格式（如「9:00—12:00」全角
  破折号、全角冒号）此前被窗口串解析器判为非法，保存被拒或配置被剔除，导致「设置了
  暂停窗口、到点后仍在运行新任务」（issue #284）**：`parse_window` 解析前对窗口串做
  全角字符归一化（全角冒号/分号/逗号/括号、全角破折号 —、en-dash –、全角连字符 －、
  波浪号 ～/~、全角空格 U+3000 → 半角），并同步到保存侧——
  - `backend/botler/pause_window.py`：新增 `normalize_window` 归一化函数，解析前
    统一转换，`in_pause_window` 运行期判断自动兼容全角配置；
  - `backend/botler/api/settings.py`：设置页保存全角窗口串不再 400，落盘前归一化为
    半角规范格式（config.yaml 保持 `HH:MM-HH:MM`）；
  - `backend/botler/config.py`：读取 config.yaml 时同样归一化，手动编辑写全角也能
    生效（不再被防御性剔除）；
  - **测试**：新增全角兼容复现用例 11 例（`test_pause_window.py` +9、API 保存归一化
    +2：全角保存 200 且落盘半角、归一化后仍非法照常 400），修复前全部失败、修复后
    通过；后端全量 pytest 与前端测试通过，无 regression。


- **中断恢复机制 Phase 1 落地：dsh 引擎「会话 id 任务开始即落库 + SDK resume + 进度账本交接单」（issue #281）**：
  按 issue #281 用户补充意见（「支持以指定 id 创建全新会话，按照技术方案开始实现」）
  落地 `docs/中断恢复机制改进方案.md`（v1.1）Phase 1，仅改造 dsh / DeepSeek Harness
  SDK 引擎，claude / hermes 保持现状：
  - **会话 id 任务开始即落库（先落 id 再开跑）**：`_run_dsh_once` 在构造
    `DshRunner` 前预生成会话 id（`botler-<task_id>-<ts>-<rand>`）并原子写
    `tasks.dsh_session_id`——任何时刻被强杀/平台重启，id 都已落库可恢复
    （此前为「执行结束后才从结果行解析落库」，强杀时收尾不执行 → id 丢失 →
    恢复退化为全新会话 → 重复实现）；前置落库失败 = 任务失败（不静默降级）；
  - **以指定 id 创建/接续 SDK 会话**：`DshRunner(session_id=<id>)` 调
    `harness.run(prompt, session_id=<id>)`——新建任务以预生成 id 创建会话，
    恢复任务以已落库 id 接续会话，SDK 同一参数承载两种语义（等价 CLI
    `dsh --profile tui --resume <id>`，以 SDK 方式实现而非 CLI 子进程）；
  - **resume 可恢复性校验**：`session_root` 已配置但目录不存在 → 如实降级为
    全新会话（清除旧 id + 全新提示词），不再「假装对话已保留」；
  - **进度账本 `task_progress`**：新增表（v14 迁移）+ `record/list/latest` 方法，
    只增不改快照式（同一 step 可追加多行，取每步最新状态，历史可追溯）；
    dsh 提示词追加「进度上报约定」节，agent 以 `[PROGRESS] step=N status=…
    desc="…" evidence="…"` 行上报里程碑，executor 增量解析落库（强杀时已收口
    部分不丢）；
  - **确定性恢复交接单**：`_resume_prompt` 升级为交接单渲染，新增
    `{progress_summary}` 占位符——账本有记录时渲染「已完成步骤 + 证据 +
    下一步」确定性状态（非模型自查反推），账本为空时如实说明「无进度记录」，
    不再声称「对话与工作区改动已保留」；`templates.resume` 用户可编辑机制
    （issue #116）保持兼容；
  - **文档同步**：README「断点续跑」章节补充 issue #281 新机制说明；
  - **测试**：新增 id 前置落库/降级/账本解析/交接单渲染/表迁移等 13 例
    （`test_executor_dsh.py` +9、`test_database_migrate.py` +4），后端全量
    pytest 通过，无 regression。
  - **任务详情展示 dsh 会话 id（Phase 1 补全）**：任务详情/列表 API 返回
    `dsh_session_id` 字段（未落库返回 null），`resumed` 标记由「仅 claude」
    扩展为「claude 或 dsh 任一恢复过会话即为 true」；前端任务详情页与
    列表抽屉新增「dsh 会话」行展示会话 id（供人工确认恢复链路），新增
    API 数据契约测试 2 例。

- **补登 300+ 平台版本逢百进位显式复现用例（issue #283）**：
  `frontend/tests/gen-version.test.mjs` 在既有 300+ 场景用例（1.0.310 → 1.3.11、
  1.0.305 → 1.3.6 等）基础上，新增与 issue 症状完全对应的显式用例「平台版本恰为
  300 时高位版本号加一（1.0.300 → 1.3.1）」。该用例在修复前实现（patch === 100
  才进位）上失败、在当前 base-100 归一化实现上通过；前端全量测试与后端全量测试
  通过，无 regression。

- **修复版本自增规则：patch 位超过 99 后逢百进位持续生效，高位版本号同步加一（issue #283）**：
  issue「修复版本超过99后恢复，无限自增，我已经实现了逢100加一，具体实现可以查看
  issue #179，但是现在平台版本已经到了300+，并没有高位版本号加一」。此前
  `frontend/scripts/gen-version.mjs` 的 `nextVersion` 仅在 patch 位自增后
  **恰好等于 100** 时才向 minor 进位，且 issue #179 曾约定「已超 99 的历史版本号
  不做回写修正、仅按数字自增（1.0.299 → 1.0.300）」；当平台版本号已累积到 300+
  （如当前 `1.0.310`）后，patch 位永远到不了 100，minor/major 高位版本号不再进位，
  违背「逢100加一」预期。本次将进位规则改为 **base-100 归一化**：
  - **patch → minor**：patch 位自增后 ≥ 100 即按 100 整除进位到 minor、余数保留
    （1.0.99 → 1.1.0 保持不变；已超 99 的历史值同样进位：1.0.199 → 1.2.0、
    1.0.299 → 1.3.0）；
  - **minor → major**：minor 位 ≥ 100 时按 100 整除进位到 major、余数保留
    （1.99.99 → 2.0.0 保持不变；1.150.5 → 2.50.6）；
  - **major 不设进位上限**（99.99.99 → 100.0.0 保持不变）；
  - **效果**：平台版本号 300+ 后高位版本号同步加一（当前 `1.0.310` 下次构建 →
    `1.3.11`），版本号无限自增（issue #283）；
  - **实现**：`gen-version.mjs` 的 `nextVersion` 进位判断由 `patch === 100`
    改为整除取整 + 取余的归一化写法，其余结构不变；
  - **测试**：`frontend/tests/gen-version.test.mjs` 由 14 例扩展至 18 例——更新
    原「超 99 历史值仅 +1 不回写」两条断言为逢百进位（1.0.299 → 1.3.0、
    1.0.199 → 1.2.0），新增 300+ 场景（1.0.310 → 1.3.11、1.99.150 → 2.0.51、
    1.150.5 → 2.50.6、1.0.305 → 1.3.6）；前端全量测试与后端全量测试通过，
    无 regression。

- **图标语义清单补登 Web 终端 terminal 图标，修复既有用例失败（issue #183）**：
  `frontend/src/components/Icon.jsx` 自 issue #183 集成 Web 终端起已提供
  `terminal` 语义图标（`terminal: TerminalIcon`），但 `frontend/tests/icons.test.mjs`
  的 `EXPECTED_NAMES` 语义清单未同步补登，导致「ICONS 键集合应与语义清单完全一致」
  用例失败（CI 前端测试步骤中该用例一直失败，因 c8 吞掉失败退出码而 job 仍报
  success，主分支静默带病运行）。本次在语义清单中补登 `terminal`（位于 `tag` 与
  `trash` 之间，保持字母序），并注明来源；前端全量测试 853 例全部通过。


- **构建版本自增规则改为「逢100进一」（issue #179）**：
  需求「修改一下版本自增到规则，逢100进一，意思就是1.0.99的下个版本号是1.1.0，
  1.99.99的下一个版本号是2.0.0」。此前 `frontend/scripts/gen-version.mjs` 每次构建
  仅对 patch 位做数字加法（1.0.99 → 1.0.100，patch 位无上限）；本次改为 99→100
  逐级进位：
  - **patch → minor**：patch 位自增到 100 时归零并向 minor 进位
    （1.0.99 → 1.1.0，issue 示例）；
  - **minor → major**：minor 位随之到 100 时归零再向 major 进位
    （1.99.99 → 2.0.0，issue 示例）；
  - **major 不设进位上限**（99.99.99 → 100.0.0）；已超过 99 的历史版本号不做
    回写修正，仅对后续自增生效（1.0.299 → 1.0.300，patch 300 非 100 不进位）；
  - **实现**：`gen-version.mjs` 将进位逻辑提取为导出的纯函数 `nextVersion(current)`
    （非法/缺失输入沿用既有行为——从 1.0.0 重新自增），主流程仅在脚本作为主程序
    直接执行时运行（import 无副作用），便于单元测试；
  - **测试**：新增 `frontend/tests/gen-version.test.mjs`（14 例：普通自增 /
    issue 示例 1.0.99 → 1.1.0、1.99.99 → 2.0.0 / minor 逐级进位 / 0 起始 /
    超 99 历史值仅 +1 / 大数 / 非法与空值输入回退等边界用例）；前端全量测试
    （801 例）与覆盖率门禁通过，无 regression。

- **项目内所有 UI 图标统一改为 Lucide 系列图标（issue #177）**：
  需求「将项目中所有的图标都改成Lucide 系列的icon」。此前前端界面图标为 emoji /
  Unicode 符号混合（🤖、💭、🔧、🏁、📁、💬、↻、✓/✗、▸/▾、× 等），风格不一且随
  系统字体渲染差异大。本次全量替换为 **Lucide 系列图标**（`lucide-react`）：
  - **统一入口**：新增 `frontend/src/components/Icon.jsx`——`Icon` 组件按语义名
    （`ICONS` 映射）渲染对应 Lucide 图标，未知名回退 × 图标；装饰性图标默认
    `aria-hidden`，`size` / `aria-label` 等 props 透传；
  - **全局尺寸**：`styles.css` 新增 `.lucide { width:1em; height:1em; ... }` 规则，
    图标随字号缩放并与文字基线对齐，替代 emoji 依赖 `font-size` 控制尺寸的方式；
  - **替换范围**：概览页（issue 分组标题 / bot 状态徽章 / 事件行 / 灵感板块 /
    流水线 / 对账 / 刷新 / 余额 / 空状态等）、任务列表与详情（停止 / 对账 / 刷新 /
    思考过程 / 工具调用 / 结果 / 返回 / 外链等）、设置页（保存成功 / 测试结果 /
    警告 / 锁定等）、仓库页、标签库、模版页、登录页、顶部导航（品牌 / 用户 /
    搜索 / 折叠）、全部弹窗与抽屉的关闭按钮（× → Lucide X）等；
  - **保留范围**：AI 供应商品牌 logo（`providers.jsx` 内联 SVG 圆底图形）属品牌
    标识，Lucide 无对应图形，不在图标替换范围内；后端通知/日志纯文本中的 emoji
    属文案内容，不适用 SVG 图标库；
  - **测试**：新增 `frontend/tests/icons.test.mjs`（4 例：ICONS 语义映射完整且均渲染
    svg、未知名回退 ×、size / aria-label 透传、默认尺寸）；同步更新
    `overview-issue-groups` / `overview-issue-running-top` / `overview-issue-running-highlight` /
    `overview-issue-task` / `overview-inspirations` / `overview-deepseek-balance` /
    `overview-issues` / `overview-section-order` / `tasks-refresh-button` /
    `stop-all-button` / `tasks-pagination` / `tasks-reconcile-all-button` /
    `tasks-responsive-cols` / `tasks-retry-button` / `backup-manager` /
    `templates-collapsible-editor` / `task-detail-thinking-toggle` / `dialog` /
    `pipeline-commit-time` / `overview-add-issue*` 等既有用例（按钮 / 组标题 / 徽章 /
    提示文本改为 textOf 提取或 Lucide 类名断言）；E2E `overview.spec.js` /
    `task-detail.spec.js` 同步适配。前端全量测试（781 例）、覆盖率门禁与 E2E 均通过，
    无 regression。

### Added

- **概览页开放 Issue 新增过滤条——按标签多选 + 按状态（全部/开放/进行中）过滤，仓库多、issue 多时快速聚焦（issue #230）**：
  需求——概览页开放 issue 聚合按仓库分组展示，但无按标签（bug/feature/need-verify 等）或状态过滤，仓库多、issue
  多时用户想只看「待处理 bug」或「带 need-verify 的」只能肉眼翻。本次在前端实现纯前端过滤（数据已全量，零后端
  改动）：
  - `frontend/src/pages/Overview.jsx`：新增过滤条（`.issue-filter-bar`）——状态分段按钮（全部/开放/进行中：
    进行中与置顶 running 组同源判定，有 running/retrying 任务即命中；开放 = 无运行中任务）+ 标签多选胶囊（候选
    来自未过滤全量数据去重排序，含 bot-done/bot-failed 等平台语义标签；多选 OR 语义命中任一即展示）；过滤仅作用于
    条目、保留仓库分组结构，过滤激活时无匹配条目的仓库整卡隐藏、全部无匹配显示「没有匹配过滤条件的 issue」+
    清除过滤按钮；新增纯函数 `loadIssueFilter` / `saveIssueFilter`（localStorage 键 `botler.overview.issueFilter`，
    非法值/无存储兜底默认、存储异常静默，与 theme.js 同款注入式 storage 设计，刷新后保持）、`issueLabelNames` /
    `collectLabelOptions`（标签名提取与标签池汇总，对象/字符串/null 元素防御）、`matchesIssueStatus` /
    `matchesIssueLabels` / `filterIssuesByFilter`（状态+标签组合过滤，组内顺序不变）；
  - `frontend/src/styles.css`：新增过滤条样式（状态按钮与标签胶囊选中态高亮、清除过滤按钮推到最右）；
  - **测试**：新增 `frontend/tests/overview-issue-filter.test.mjs` 27 例（load/save 解析与规范化边界、标签池防御、
    状态/标签/组合过滤语义、过滤条渲染、点击过滤生效、多选 OR 并集、localStorage 预置初始过滤与点击持久化、
    无匹配空态、无 issue 不渲染过滤条、零 issue 仓库卡片回归保护、过滤激活整卡隐藏）；前端全量测试通过，无
    regression。

- **界面显示新增深色模式三态——跟随系统 / 浅色 / 深色，夜间查看任务状态不再刺眼（issue #217）**：
  需求——夜间无人值守查看任务/日志时浅色 UI 刺眼，提供 CSS 变量 + `prefers-color-scheme`
  自动适配 + 设置页「界面显示」手动三态切换，偏好持久化 localStorage + 后端配置同步：
  - `frontend/src/theme.js`（新增）：主题三态模块——`THEME_MODES`（system/light/dark）、
    纯函数 `loadThemePreference` / `saveThemePreference`（localStorage 键 `botler.theme`，
    非法值/无存储/隐私模式兜底不崩溃，与 SettingsNav issue #168 同款注入式 storage 设计）、
    `resolveTheme`（system 跟随系统偏好）、`applyTheme`（设置 `<html data-theme>` 与
    `color-scheme`，原生滚动条/表单控件同步）、`watchSystemTheme`（仅 system 模式响应
    系统深色变化自动适配，手动模式不响应）；
  - `frontend/index.html`：首屏 inline 脚本在应用 JS 加载前按本地偏好设置 `<html
    data-theme>`，深色用户不再先看到浅色白屏闪变；`main.jsx` 挂载前兜底再应用一次；
  - `frontend/src/styles.css`：深色令牌重构为三态生效——跟随系统分支
    `@media (prefers-color-scheme: dark) { :root:not([data-theme='light']) }`（系统深色
    且未强制浅色时翻转）+ 手动深色分支 `:root[data-theme='dark']`（两处令牌必须同步，文件
    内注释提示）；深色配色按 issue 要求改为**蓝白新拟物风格：深色底 `#1a1d23` + 蓝色强调
    `#3b82f6`**（链接/文字用 `#60a5fa`，对深底对比度 ≥ 4.5:1 达 WCAG AA），浅色 `:root`
    声明 `color-scheme: light`；顺手 token 化两处硬编码白（仓库 logo 占位底
    `background: #fff` → `var(--bg-card)`、聊天用户气泡 `color: #fff` →
    `var(--on-primary)` + 实底 `var(--primary-strong)`，避免深色模式白块/白字刺眼）；
  - `frontend/src/pages/Settings.jsx`：「界面显示」卡片新增「界面主题」三态下拉
    （跟随系统 / 浅色 / 深色，`ui.theme`），切换即时预览并写本地 localStorage；
    `buildUiPatch` 携带 `theme`（未配置按 `system` 兼容旧配置）；卡片内「保存界面显示
    配置」保存后立即应用 + 写 localStorage（与后端 config.yaml 双向同步）；
  - `frontend/src/App.jsx`：启动拉取 `/api/settings` 后应用后端 `ui.theme` 并同步本地
    偏好（跨设备权威配置）；注册系统深色变化监听，system 模式下 OS 切换深浅即时跟随；
  - `backend/botler/config.py`：新增 `ui.theme` 配置字段（默认 `system`），`_ui_theme`
    防御非法值回退跟随系统，KNOWN_FIELDS["ui"] 纳入 theme；
  - `backend/botler/api/settings.py`：GET 返回 `ui.theme`；`_validate_ui` 校验三态取值
    （非法值 400）；
  - `backend/config.example.yaml` / `README.md` / `docs/design-system.md`：补充 `ui.theme`
    说明与深色模式三态设计规范（生效机制、持久化双通道、使用约束）；
  - **测试**：新增 `frontend/tests/theme.test.mjs` 7 例（load/save 边界、resolve 三态
    跟随系统、applyTheme 设置 data-theme 与 color-scheme、watchSystemTheme 响应与取消）、
    `frontend/tests/settings-ui-theme.test.mjs` 6 例（三态下拉源码断言 / 即时预览与本地
    持久化 / buildUiPatch 携带 theme / saveUi 双向同步 / 说明文字 / 渲染回显与保存提交
    PUT ui.theme=dark）；后端 `backend/tests/test_api_settings.py` 新增 TestUiThemeSettings
    8 例（默认 system / dark、light 写回 config.yaml / 切回 system / 非法值与类型拒绝 /
    部分更新不串扰 / yaml 手写非法值回退）；同步更新 `apple-hig.test.mjs`（深色令牌断言
    改为 issue #217 配色 #1a1d23/#21252d/#e8eaed/#3b82f6 + 手动深色/手动浅色分支断言）与
    `apple-design.test.mjs`（导航材质深色值更新）；前后端全量测试通过，无 regression。

- **修订《中断恢复机制改进方案》v1.1：dsh / DeepSeek Harness SDK 引擎先行（issue #281）**：
  按 issue #281 用户补充意见（「deepseek harness sdk 引擎先行……每次开始任务的
  时候想把会话 id 写入到任务详情中，终端恢复后如果发现有会话 id，直接使用
  resume 会话 id 来继续任务，使用 deepseek harness sdk 的方式实现，而不是
  cli 的方式」）修订 `docs/中断恢复机制改进方案.md`：
  - **dsh（DeepSeek Harness SDK）引擎先行**：Phase 1 由「claude 引擎先行」
    调整为「dsh 先行」——任务开始（run 启动前）即原子写落库
    `tasks.dsh_session_id` 并在任务详情展示会话 id，杜绝「进程被强杀时 id
    尚未落库、恢复退化为全新会话」；
  - **SDK resume 恢复路径**：终端/进程恢复后若发现任务详情有会话 id，经
    `DshRunner(session_id=<id>)` SDK 进程内接续会话（等价 CLI
    `dsh --profile tui --resume <id>` 语义，以 SDK 方式而非 CLI 子进程实现），
    会话文件缺失时如实降级；
  - **根因补充**：新增「dsh 会话 id 执行结束后才落库（`_persist_dsh_session_id`
    在收尾路径解析结果行），强杀/重启时收尾不执行 → id 丢失 → 全新会话」
    分析；
  - **待确认问题更新**：Phase 1 开工前探测 SDK 是否支持「以指定 id 创建全新
    会话」，不支持则退化为「run 返回 id 立即补写 + 执行中心跳续写」；
  文档修订为 v1.1，仍为 docs-only 方案文档（`docs/中断恢复机制改进方案.md` +
  `CHANGELOG.md`），未改动任何功能代码。

- **新增《中断恢复机制改进方案》设计文档（issue #281）**：
  issue「现在的中断恢复机制，会频繁的造成 agent 反复的检查实现和重复实现，
  帮我思考一下有没有更好的中断恢复机制，类似 hermes -c 命令，不需要实现，
  只需要给出方案」为讨论型需求，本期不实施改造，仅产出方案文档
  `docs/中断恢复机制改进方案.md`：
  - **根因分析**：现状「断点续跑」（issue #8/#47/#84）只持久化会话转录
    （`claude_session_id` / `hermes_history` / `dsh_session_id`），恢复时靠
    `DEFAULT_RESUME_PROMPT` 让 agent 自查 git 工作区反推进度——LLM 反推
    不可靠导致反复检查实现、反复重新实现；会话文件丢失时静默降级为全新会话
    但引导语仍称「改动已保留」，语义矛盾加剧重做；
  - **借鉴对象**：拆解 `hermes -c`（`--resume latest` + `--in DIR` 自动发现
    最近会话、SessionDB 血缘链解析、checkpoints 工作区快照/回滚），对照出
    hermes 是「状态索引自动解析 + 完整上下文还原」，Botler 是「id 手工传递 +
    提示词让模型反推」；
  - **推荐方案**：结构化进度账本（`task_progress` 表，步骤 + 验证证据）+
    会话自动发现（`task_sessions` 注册表，按 workdir/issue 定位最近有效会话，
    血缘链解析） + 工作区检查点（`cache/checkpoints/` 快照/还原/回收） +
    确定性恢复交接单（渲染「已完成步骤 + 证据 / 唯一下一步」，替代自查引导语）；
    附统一降级阶梯（账本/会话/检查点逐级可用时如实降级）；
  - **实施计划**：Phase 1 账本 + 交接单（claude 先行）→ Phase 2 会话注册表
    三引擎统一 → Phase 3 工作区检查点 + 验证证据复用，每阶段独立提交回归；
    备选方案 A（仅优化提示词，治标）与 C（agent 内建 checkpoint 工具，过重）
    对比后推荐方案 B；
  - 纯文档变更（新增设计文档 + CHANGELOG），无代码/测试改动，无需跑测试与
    部署流水线（docs-only 跳过，issue #57）。
- **Web 终端集成——浏览器内直接使用系统终端，无需再打开系统终端（issue #183）**：
  需求「集成一个终端，软件不在需要打开终端」，方案为后端 terminado + Tornado 独立
  终端服务进程、前端 xterm.js 多标签 + 快捷键 + 复制粘贴、部署统一入口代理独立
  进程安全隔离：
  - **独立终端服务进程**：新增 `backend/terminal_service.py`（Tornado + terminado），
    提供标准 WebSocket 终端服务（terminado JSON 协议），默认只监听
    `127.0.0.1:8765`（安全隔离，不对外暴露端口）；环境变量可调
    `BOTLER_TERM_PORT` / `BOTLER_TERM_BIND` / `BOTLER_TERM_SHELL` /
    `BOTLER_TERM_MAX_TERMINALS`；新增依赖 `tornado>=6.4`、`terminado>=0.18`；
  - **共享用户验证**：主后端新增 `POST /api/terminal/token`（SSO 启用时需登录，
    未启用签发 local 用户），签发**短时效 token**（默认 60 秒，
    `BOTLER_TERM_TOKEN_TTL` 可调）；token 与会话 cookie 同构（同一 HMAC 会话
    密钥）但以 `typ:"term"` 声明隔离——cookie 不能当终端 token 用、终端 token
    不能当 cookie 用（`botler/auth.py` 新增 `create_terminal_token` /
    `verify_terminal_token`）；终端服务握手校验 token，失败以 close code 4001
    拒绝、不创建 PTY；
  - **反向代理开箱即用**：主后端新增 `backend/botler/api/terminal.py`——
    `/api/terminal/ws/<name>`（WebSocket 双向转发，token/查询串透传）与
    `/api/terminal/health`（探活）反向代理到独立终端服务进程
    （`BOTLER_TERM_UPSTREAM` 可调），浏览器与主后端同源，无需额外部署即可使用；
  - **前端多标签终端**：顶部导航新增「终端」入口（`/terminal`），
    `frontend/src/pages/Terminal.jsx` 多标签管理（新建/关闭/切换，上限 8 个，
    `Alt+T` 新建 / `Alt+W` 关闭当前标签，避开浏览器保留快捷键；`Ctrl+Shift+C`
    复制 / `Ctrl+Shift+V` 粘贴为 xterm.js 原生能力）；
    `frontend/src/terminal/`：`protocol.js`（terminado 协议编解码 + WS 地址构造）、
    `attach.js`（等价 @xterm/addon-attach 的适配层——AttachAddon 只收发原始文本
    且无 resize，与 terminado JSON 协议不兼容，适配层补齐 stdin/resize 编码与
    连接状态回调）、`TerminalView.jsx`（xterm.js + FitAddon + 适配层，动态导入
    兼容 CJS 产物）；新增依赖 `@xterm/xterm@^5.5.0`、`@xterm/addon-fit@^0.10.0`；
    vite 开发/预览代理启用 `ws: true` 支持终端 WebSocket；Icon 组件新增
    `terminal` 图标、styles.css 新增终端页样式；
  - **部署**：pm2（`deploy/botler.config.cjs` 新增 `botler-terminal` 进程，
    CI `deploy_to_code01` 同步停止/启动并新增 `/api/terminal/health` 健康检查，
    失败即部署失败）/ docker compose（新增 `terminal` 服务，与 botler 共享
    `data/backend/data` 卷的会话密钥，不映射宿主端口）；Nginx 统一入口参考
    配置 `deploy/nginx-terminal.conf`（`/terminal/` WebSocket 升级代理）；
  - **文档**：README 新增「Web 终端」章节、`docs/web-terminal.md` 完整设计文档
    （架构 / 认证 / 协议 / 部署 / 安全 / 测试）；
  - **测试**：后端新增 `test_terminal_token.py`（10 例：签发/校验/篡改/过期/
    隔离/边界）、`test_terminal_service.py`（6 例：健康检查、无 token / 伪造 /
    过期 token 拒绝、有效 token 跑真实 PTY 回显、多标签会话隔离）、
    `test_api_terminal.py`（7 例：token 端点 SSO 开关、健康/WS 反向代理真实
    终端服务）；前端新增 `terminal-protocol.test.mjs`（协议/地址/快捷键/适配层）
    与 `terminal-page.test.mjs`（导航入口/路由/样式/新建关闭标签/token 失败
    提示）；前后端全量测试与覆盖率门禁通过，无 regression。


- **issue 详情页评论/活动中的 Git 提交 SHA 支持点击跳转 GitLab 提交页（issue #181）**：
  需求「issue详情页面，如果回复有git提交，则实现可以点击提交，跳转到gitlab对应的提交页面」。
  此前机器人完成评论中的提交短 SHA（如「提交Commit：d6adbde（feat: …）」）与系统活动
  「mentioned in commit …」均为纯文本，无法直接跳转查看提交内容；本次在 issue 详情右边栏
  将评论 / 活动 / 描述中的 Git 提交引用（7-40 位十六进制、词边界）渲染为可点击链接，点击
  跳转到对应 GitLab 提交页面（短 SHA 提交页 GitLab 302 跳转到完整提交）：
  - **实现**：`frontend/src/components/Markdown.jsx` 新增可选 `projectUrl` prop 与导出的
    纯函数 `linkifyCommits`（无 projectUrl 时行为完全不变，设置页文档等场景不受影响；行内
    code 与既有 [链接](url) 内的 SHA 不重复链接化）；`frontend/src/components/IssueDrawer.jsx`
    新增导出的 `projectUrlFromIssueWebUrl`（由 issue `web_url` 推导项目 web 基地址，
    work_items / issues 两种 URL 形态均支持，无法识别返回空串不渲染链接），描述与评论
    Markdown 传入 projectUrl，活动纯文本经 `linkifyCommits` 渲染；
    `frontend/src/styles.css` 新增 `.commit-link` 样式（等宽字体 + 主色无下划线，与 GitLab
    提交引用渲染风格一致）；
  - **测试**：`frontend/tests/markdown.test.mjs` 新增提交链接用例（短 SHA / 完整 40 位 SHA /
    多处 SHA / 大写 hex URL 归一化小写 / 无 projectUrl 纯文本 / 非 hex 词、混入非 hex 字母、
    不足 7 位、41 位超长不误判 / 行内 code 与围栏代码块内不链接化 / 既有链接内不重复链接化 /
    列表与引用块内同样生效 / 空值安全）；`frontend/tests/overview-issue-notes.test.mjs` 新增
    `projectUrlFromIssueWebUrl` 推导与评论、活动提交链接渲染及无 web_url 兜底用例；前端全量
    测试通过，无 regression。

- **概览页最下方新增「Issue 完成耗时」板块——平均每个 issue 完成所需时间与逐日走势图（issue #180）**：
  需求「概览页面增加显示，平均每个issue完成所需要的时间，以及这个这个时间的走势图，放在概览页面最下方」。
  在概览页 CI/CD 流水线板块之后（页面最下方）新增统计板块，展示已完成 issue 的平均完成耗时与
  逐日平均耗时走势图：
  - **数据源**：本地 `tasks` 表成功终态（succeeded）任务，不依赖 GitLab API——任务成功时系统会给
    issue 打 bot-done 标签（executor issue #49），完成耗时 = `finished_at - created_at`（系统接收
    时间 → bot-done 打标时间，与任务详情/任务列表「处理用时」语义一致）；缺时间字段、解析失败或
    用时为负（时钟异常）的任务行不计入统计；
  - **后端**：新增 `GET /api/issues/completion-stats`（`backend/botler/api/issues.py` + 数据层
    `Database.succeeded_durations`）——返回 `completed_count`（已完成数量）、`avg_seconds`（全部
    平均耗时秒数）、`trend`（按完成日 UTC 分组逐日平均耗时，日期升序）；本地数据量小直接实时计算，
    不做缓存；
  - **前端**：概览页新增 `completion-stats-section` 板块（标题/平均耗时数字/完成数量 + 轻量 SVG
    折线走势图 `CompletionTrendChart`，无第三方图表库依赖——折线 + 数据点 + 日期/数值标注，每点
    带悬浮提示，viewBox 等比例自适应宽度）；60 秒低频轮询（数据来自本地库，无 GitLab 请求压力）；
    `api.js` 新增 `fmtSeconds` 秒数人类可读格式化（与 `fmtDuration` 输出格式一致，后者改为复用
    该函数），供平均耗时/走势图数值展示；
  - **测试**：后端 `tests/test_api_issues.py` 新增 `TestCompletionStats`（4 例：空库 / 仅 succeeded
    计入 / 总体平均与逐日分组 / 非法与负耗时跳过）；前端新增 `tests/overview-completion-stats.test.mjs`
    （11 例：接口与轮询源码断言、板块位于页面最下方、平均耗时与走势图渲染、空状态、接口失败兜底、
    `fmtSeconds` 边界、走势图空/单点渲染）；后端全量 pytest 与前端全量 `node --test` 通过，无 regression。

- **概览页 DeepSeek 账户余额卡片新增「去充值」链接按钮，点击跳转 DeepSeek 开放平台充值页（issue #178）**：
  需求「deepseek账户余额页面可以增加一个链接按钮，点击之后跳转到deepseek页面，方便充值」。
  此前余额卡片仅展示余额数据与「刷新」按钮，用户需要自行打开 DeepSeek 平台寻找充值入口；
  本次在余额卡片操作行新增「去充值」链接按钮，一键跳转官方充值页，方便充值：
  - 前端 `pages/Overview.jsx`：新增常量 `DEEPSEEK_TOPUP_URL = 'https://platform.deepseek.com/top_up'`
    （DeepSeek 开放平台充值页）；余额卡片「刷新」按钮旁新增
    `<a className="btn btn-small deepseek-topup-link" href={DEEPSEEK_TOPUP_URL}
    target="_blank" rel="noreferrer">` 链接按钮，文案「去充值」，配 Lucide ExternalLink
    图标与 title 提示；新标签页打开，`rel=noreferrer` 与项目外链约定一致；
  - 前端 `styles.css`：新增 `.deepseek-topup-link` 样式类（复用 .btn / .btn-small 外观，
    按钮间距由 .form-row gap 提供）；
  - 交互边界：卡片渲染（configured=true）即提供「去充值」入口——余额查询报错、余额为
    空（余额为 0 正是需要充值的场景）时按钮仍可用；未配置 deepseek api（configured=false）
    时整卡不渲染，链接也不出现；
  - 测试：`frontend/tests/overview-deepseek-balance.test.mjs` 新增 6 例——源码断言（充值页
    地址常量 / 链接类名 / href / target=_blank / rel=noreferrer / 文案与图标）+ 渲染断言
    （已配置渲染链接且指向充值页 / 未配置不渲染 / 余额接口报错仍可用 / 余额为空仍可用）+
    样式类断言；前端全量测试与覆盖率门禁通过，无 regression。

- **任务详情页事件流默认隐藏思考过程，事件流右侧新增「显示思考过程」开关（issue #176）**：
  需求「任务详情页面事件流默认隐藏思考过程，在事件流右边增加一个 checkbox，可以打开
  思考过程显示」。任务执行事件流中的 thinking（思考过程）事件此前以折叠条展示（默认
  收起但摘要可见），本次调整为**默认整条隐藏**，并在事件流标题右侧新增「显示思考过程」
  checkbox：
  - **默认隐藏（未勾选）**：thinking 事件整条不渲染，事件流只展示文本/工具调用/工具
    结果/状态/结果摘要等非思考事件，界面更聚焦执行过程本身；
  - **勾选后展开显示**：勾选「显示思考过程」后 thinking 事件以展开态渲染（`<details
    open>`，内容直接可见，仍可单独收起）；开关状态实时驱动，勾选后新推送的思考事件
    立即显示；
  - **交互位置**：开关位于「事件流」折叠标题右侧（`.event-stream-header` flex 布局，
    窄屏自动换行），复用既有 `.checkbox-label` 样式与主色 accent 焦点风格；事件流区块
    折叠时开关保留在标题行，不影响区块折叠交互；
  - **测试**：新增 `frontend/tests/task-detail-thinking-toggle.test.mjs`（4 例：默认
    隐藏思考事件且文本/工具事件不受影响、勾选后展开显示与取消勾选再次隐藏、勾选状态下
    新推送思考事件立即显示、无思考事件时开关正常显示）；`task-events-stream.test.mjs`
    同步更新 thinking 默认隐藏断言；E2E `frontend/e2e/tests/task-detail.spec.js` 同步
    适配——先断言思考过程默认隐藏、再勾选「显示思考过程」验证展开显示（真实后端 SSE
    回放链路）。前端全量测试（777 例）、覆盖率门禁与 E2E 均通过，无 regression。

- **任务执行环境详情记录——任务开始时采集环境快照落库 tasks.environment，任务详情页「元信息」区折叠面板展示（issue #276）**：
  需求「任务详情记录的是结果（状态/日志/提交），不记录执行环境：当时用的 claude 版本、
  模型 id、基于哪个 commit 基线开始的、git 分支状态」。环境差异（模型升级、CLI 版本
  变化）导致的行为变化无法追溯。本次新增**执行环境快照**：
  - **采集（`backend/botler/env_snapshot.py`）**：任务首次执行开始时采集并落库
    `tasks.environment` JSON（迁移 v13，旧库自动补列）——引擎名与版本（claude
    `--version` / hermes / dsh SDK，复用 `environment.detect_tool` 不做网络查询）、
    实际模型（dsh → `worker.dsh_model`；hermes → `~/.hermes/config.yaml` 的
    `model.default`；claude → `~/.claude/settings.json`）、起始 commit sha 与分支
    （`prepare_workspace` 之后工作区 HEAD）、平台版本（与前端 VersionBadge 同源的
    version.json / data/version.txt）、config 关键项 hash（执行相关配置项 sha256）；
    只采一次（重试/断点续跑不覆盖首次快照）；**采集全程尽力而为**——单项失败只影响
    对应字段，整体异常落库 `{"error": "环境快照获取失败"}` 标记，任务照常执行、
    不增加明显执行延迟；
  - **展示（任务详情页「元信息」区）**：新增「执行环境快照」折叠面板（默认展开可收起），
    展示引擎/模型/起始提交/平台版本/配置哈希/采集时间；无快照的旧任务显示
    「暂无环境快照」，采集失败显示「环境快照获取失败」（任务照常执行不阻塞）；
  - **API**：`GET /api/tasks` / `/api/tasks/{id}` 返回解析后的 `environment` 对象；
  - **测试**：新增 `backend/tests/test_env_snapshot.py`（32 例：config hash 稳定性与
    变化、平台版本读取与回退、真实 git 仓库的起始提交/分支采集、引擎版本检测、
    dsh/hermes/claude 模型解析、序列化往返、采集失败容忍不抛异常）；
    `test_database_migrate.py` 新增 v13 迁移用例并更新 user_version 断言；
    `test_api_tasks.py` 新增 environment 数据契约用例；`test_executor.py` 新增
    `_capture_env_snapshot` 采集落库/只采一次/失败容错用例；
    前端新增 `frontend/tests/task-detail-env-snapshot.test.mjs`（4 例：快照展示、
    折叠收起恢复、采集失败提示、旧任务「暂无环境快照」兼容）。全量测试无 regression。

- **新增鸿蒙端（Web 套壳）并在 CI/CD 中加入鸿蒙编译（issue #173）**：
  需求「这个额外实现一个鸿蒙端，使用web套壳，同时在ci cd的流程中加入鸿蒙的编译」。
  额外实现一个 **HarmonyOS NEXT 鸿蒙端**，使用系统 Web 组件（WebView）套壳加载
  Botler Web 前端（React/Vite 产物由 FastAPI 同源托管），并在 CI/CD 流程中
  加入鸿蒙的**真实编译**：
  - **鸿蒙工程（`harmony/`，Stage 模型）**：`AppScope`（bundleName
    `com.botler.app` / 版本 / 图标）+ `entry` 模块（`module.json5` 声明
    INTERNET 权限与 EntryAbility；`Index.ets` 用 Web 组件加载
    `common/AppConfig.ets` 的 `WEB_URL`（默认 `http://10.0.0.122:8000`，
    部署机内网地址，按环境可改），原生壳补充加载动画（LoadingProgress）/
    加载失败提示与重试（onErrorReceive + controller.refresh()）/ 返回键
    历史回退（onBackPress + accessBackward）；目标 SDK HarmonyOS 6.1.1
    （API 24），工程模型版本 6.0.2；应用图标由 PIL 绘制（品牌蓝圆角方块 +
    白色 B）；
  - **CI/CD 鸿蒙编译（`harmony:build` 作业）**：`build` 阶段新增与
    frontend:build / backend:test 并行的编译门禁——先跑结构校验
    （`harmony/scripts/validate_harmony.py`，配置缺失/引用断裂秒级失败），
    再 `ohpm install` + `hvigorw assembleHap`（本机华为命令行工具链
    `~/command-line-tools`：hvigor 6.24.3 + ohpm 6.1.2 + HarmonyOS 6.1.1
    SDK）做真实 ArkTS 编译，产出未签名 HAP 为 artifact（CI 只验证可编译性，
    正式签名发布需 DevEco Studio 自动签名），编译失败即阻断流水线
    （docs_only_skip 的 on_success 传播，「鸿蒙端不可编译不部署」）；
    `.docs_only_skip` 白名单补充 `harmony/**/*` 与 `**/*.ets`、`**/*.json5`，
    鸿蒙改动不会被误判为 docs-only 跳过；
  - **测试**：新增 `backend/tests/test_harmony_project.py`（10 例）——JSON5
    迷你解析器（注释/尾逗号/单引号/无引号键/字符串内注释符不误判）、真实
    harmony 工程通过全部结构校验（防回退）、破坏性用例（移除 INTERNET
    权限/Web 组件/WEB_URL、非 http(s) 地址、移除 targetSdkVersion、删除
    必需文件均能检出）；`harmony/scripts/validate_harmony.py` 为纯标准库
    实现（内置迷你 JSON5 解析器），CI 与 pytest 双端复用；
  - **文档**：`harmony/README.md`（目录结构 / 环境要求 / 命令行编译步骤 /
    WEB_URL 修改 / DevEco Studio 真机运行与签名 / CI 集成说明）、README.md
    新增「鸿蒙端」章节与目录树条目、`docs/设计方案.md` 技术栈表与 Phase 3
    补充鸿蒙端。
  - **健壮性修复**：`Index.ets` 返回键处理（onBackPress）对
    `WebviewController.accessBackward()/backward()` 增加 try-catch 异常保护
    （Web 组件绑定前调用可能抛异常，ArkTS 编译警告），失败时按普通返回
    处理并记录 hilog 日志，消除编译警告、避免极端场景下应用崩溃。
- **hermes 引擎集成方式改为 hermes agent SDK 进程内集成（issue #171）**：
  需求「帮我把hermes的集成方式改成hermes agent sdk的集成方式」。此前 hermes
  引擎（issue #47）经「子进程 + 部署机独立 hermes venv」运行
  `backend/hermes_runner.py`（`hermes.command`/`hermes.args` 配置 + Docker
  挂载 hermes venv，botler 不打包不管理）；本次改为与 dsh 引擎（issue #84）
  对齐的 **SDK 进程内集成**——hermes-agent（`run_agent.AIAgent`）以源码
  editable 安装进 botler 自身 venv，`HermesSdkRunner`
  （`backend/botler/hermes_sdk_runner.py`）在 botler 进程内 worker 线程
  调用，停止/超时经 `AIAgent.interrupt()` 跨线程中断（语义等价旧模式
  SIGKILL 进程组）：
  - **执行器**：`executor._run_hermes_once` 从子进程 Popen 改为 SDK runner
    worker 线程模型（与 `_run_dsh_once` 同构），输出协议不变（事件行 +
    结果行 NDJSON），SSE 实时输出 / 结果判定（`_hermes_result`）/ 断点续跑
    （`tasks.hermes_history` 落库）全部复用；`HermesSdkRunner` 经
    `register_task_env_overrides(task_id, {"cwd": 工作区})` 注册会话级工作区
    cwd 覆盖 + worker 内临时注入 `TERMINAL_CWD`/git 凭据（`GIT_ASKPASS` 等）
    并还原（进程内模式替代旧模式的子进程 env 注入）；
  - **配置**：移除 `hermes.command` / `hermes.args`（`hermes:` 段现为空），
    设置页引擎选项文案更新为「hermes — hermes-agent SDK」；本地环境检测
    （environment.py）从 hermes CLI `which/--version` 改为 `run_agent` 模块
    检测（与 dsh 同模式，`pkg: hermes-agent` 读版本）；
  - **部署**：新增 `deploy/install-hermes-agent.sh` 一键安装（默认源码
    `~/.hermes/hermes-agent`、`HERMES_SOURCE_DIR` 可覆盖；pip
    `--ignore-requires-python` 适配 pm2 Python 3.14——上游 pyproject
    requires-python `<3.14` 封顶已过时、cp314 wheel 实测可用；uv 回退；
    幂等 + import 校验 fail fast）；CI `deploy_to_code01` 主依赖安装后自动
    调用；Docker 部署新增 `docker-entrypoint.sh`（容器启动时对挂载源码
    幂等 editable 安装，未挂载跳过并告警），`docker-compose.yml` 补充
    hermes 挂载说明与 `HERMES_SOURCE_DIR`；
  - **清理**：删除旧 `backend/hermes_runner.py` 及其测试（
    `test_hermes_runner.py` / `test_hermes_runner_stream.py`），CI bandit /
    semgrep 路径同步移除该文件；`docs/hermes-engine-deployment.md` 重写为
    SDK 模式，README 同步；
  - **测试**：新增 `test_hermes_sdk_runner.py`（16 例：SDK 探测 / 成功结果行 /
    prompt/history/session/task_id 透传 / 事件行 / 停止中断 / 异常容错 /
    env 与 cwd 注入还原）、`test_executor_hermes.py` 重写为假 SDK runner
    模式（27 例：构造参数 / 停止超时 125/124 / SDK 未装报错 / SSE / 日志 /
    resume / 结果判定）、`test_executor_stream.py` hermes 用例迁移、
    `test_environment.py` hermes 检测改 module 模式、`test_executor_dsh.py`
    hermes 回归用例适配、新增 `test_deploy_hermes_sdk.py`（13 例）与
    `test_dockerfile_hermes.py`（12 例）部署产物防回退；后端全量 1596 +
    新增用例 + 前端全量无 regression。
- **设置页新增「MinIO 对象存储」配置卡片并启用识图图片上传（issue #170）**：
  issue #163/#164 已完成 MinIO 后端链路（图片 SHA-256 哈希上传 public 桶 +
  识图请求传 http URL + nginx 代理访问），但设置页暂未提供配置卡片（issue #163
  CHANGELOG 明确「设置页暂未提供卡片、可编辑 config.yaml 或经 API 配置」）。
  本次补齐 UI 并实际启用：
  - **设置页卡片**：「外部服务接入」分组新增「MinIO 对象存储」区块
    （`settings-minio`）——`minio.enabled`（开关）/ `endpoint` / `secure` /
    `access_key` / `secret_key` / `bucket` / `public_base_url` / `verify_ssl`
    全部可配置；Access Key / Secret Key 为密码输入框、掩码占位显示、留空 =
    保持现有凭据（后端掩码不覆盖，与 SSO client_secret / webhook
    authorization 同模式）；卡片内独立「保存 MinIO 配置」按钮（只提交 minio
    段，不影响其他设置），全局「保存」同步提交 minio 段（与 webhook 同模式）；
    左侧导航栏关键词映射新增 `settings-minio`（导航自动出现「MinIO 对象存储」
    子选项，无需手工同步）；
  - **启用运行实例**：部署机 `data/backend/config.yaml` 写入 minio 段——
    `enabled: true`、endpoint `127.0.0.1:9000`、凭据 minioadmin（与
    `data/backend/.env` 的 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 同源）、
    `bucket: public`、`public_base_url: https://home.chenkaidi.top:509/minio-public`
    （nginx 代理 MinIO public 桶地址，配置参考 `deploy/nginx-minio-public.conf`，
    issue #164）；已端到端验证：图片字节上传 public 桶成功、返回
    `public_base_url/bucket/<sha256 哈希>` 对象 URL；
  - **文档**：README 设置页分组说明（补齐识图模型 + MinIO 对象存储）与 minio
    配置表、`config.example.yaml` minio 段注释同步更新；
  - **测试**：新增前端 `tests/settings-minio-card.test.mjs`（卡片挂载与位置 /
    字段齐全 / 凭据留空保持现有 / PUT minio 段 / 导航关键词），同步更新
    settings-nav-labels / settings-nav / settings-nav-collapse 三份导航测试
    （设置区块 15 → 16，新增 `settings-minio` 名称快照），后端沿用 issue #163
    既有 minio API 用例；前端全量 769 用例无 regression。
- **定时暂停窗口：按时间规则停止开始新任务（issue #169）**：
  可在设置页「任务调度」卡片配置暂停窗口（如 `09:00-12:00`、`14:00-18:00`），
  窗口内调度器**停止开始新任务**，**已经开始执行的任务可以继续执行**，
  **未开始执行的任务保留在队列中，等到窗口结束后自动开始执行**：
  - **配置项**（`worker` 段，config.example.yaml 已加注释示例）：
    `pause_windows`（窗口串数组 `HH:MM-HH:MM`，24 小时制，支持跨天如
    `22:00-02:00`，空 = 不启用）/ `pause_weekdays`（生效星期 0-6，空 = 每天）/
    `pause_timezone`（判断时区 IANA 名，空 = 服务器本地时区）；
  - **调度器**：新增 `botler/pause_window.py` 纯函数（窗口解析 / 判断，含跨天与
    星期、时区换算）；`TaskScheduler._dispatch` 派发前实时判断窗口状态，窗口内
    不派发（webhook / 对账仍照常入队），状态翻转记日志；运行中任务由独立 worker
    线程继续执行不受影响；
  - **设置 API**：`GET /api/settings` 返回 `worker.pause_windows / pause_weekdays /
    pause_timezone / pause_active`（pause_active 为服务端实时计算的暂停状态），
    PUT 支持写入；非法窗口格式 / 非法星期 / 非法时区名 400 拒绝；
  - **设置页 UI**：「任务调度」卡片新增「定时暂停窗口」区块——窗口逐行 textarea、
    生效星期复选框（不勾选 = 每天都生效）、判断时区输入（常用时区 datalist）；
    `pause_active=true` 时显示「当前处于暂停窗口」提示；
  - **防御**：config.yaml 被手动编辑写坏时非法窗口串自动忽略、全部非法 = 不启用，
    保证调度服务可用性优先；
  - **测试**：新增后端纯函数（`tests/test_pause_window.py`：解析 / 窗口内外 / 跨天 /
    星期 / 时区 / 坏配置回退）、调度器集成（`tests/test_scheduler_pause_windows.py`：
    窗口内不派发 / 窗口外派发 / 排队任务窗口后自动开始 / 运行中任务不中断 / 未配置
    行为不变）、设置 API（读写 / 校验 400 / 部分更新）与前端
    `tests/settings-pause-windows.test.mjs` 8 条用例，全量无 regression。
- **CI 引入代码覆盖率门禁：后端 pytest-cov + 前端 c8 双端覆盖率报告与阈值阻断（issue #210）**：
  此前 1500+ 后端 / 727 前端用例跑完即弃，不产出任何覆盖率报告，executor.py /
  gitlab_client.py / scheduler.py 等核心模块覆盖情况完全不可见。本次打通「测量 →
  报告 → 门禁 → 徽章」全链路：
  - **后端（pytest-cov）**：`requirements.txt` 新增 `pytest-cov`，`backend:test`
    以 `--cov=botler --cov-report=term-missing --cov-report=xml --cov-fail-under=70`
    运行——`term-missing` 在日志逐文件列出缺失行（核心模块覆盖率直接可见），
    `coverage.xml` 以 `coverage_report`（Cobertura）artifact 上传 GitLab，MR 页面
    显示覆盖率与对比；**总覆盖率阈值 70%**，低于阈值 pytest 非零退出阻断流水线；
  - **前端（c8/v8）**：`package.json` 新增 `test:coverage`（`c8 --check-coverage
    --lines 70 --statements 70 --branches 60 --functions 50`），统计 `src/` 源码，
    CI 上传 `coverage/cobertura-coverage.xml`，行/语句 70%、分支 60%、函数 50%
    阈值，低于阈值阻断；
  - **GitLab 集成**：两 job 均上传 `coverage_report` artifact；项目设置
    `test_coverage_regex` 解析 pytest-cov `TOTAL` 行启用**覆盖率徽章**
    （`badges/main/coverage.svg`，项目主页/README 可引用）；
  - **基线**（首次测量）：后端 88%（executor 85% / scheduler 80% /
    gitlab_client 59%），前端行 86.9% / 分支 80.3%；阈值均低于基线留足缓冲，
    防「新代码无测试悄然合入」；
  - 覆盖率产物（`coverage.xml` / `frontend/coverage/` 等）gitignore 排除，
    不入库不镜像 GitHub；本地命令与徽章引用方式已写入 README「测试」章节。
- **引入 Playwright 浏览器级 E2E 测试（issue #212）：真实浏览器覆盖关键用户链路**：
  此前前端测试只有源码静态断言 + react-test-renderer 渲染断言、后端只有 API 单测，
  「添加仓库 → webhook 触发 → 任务执行 → 概览展示」全链路无浏览器级验证。本次在
  `frontend/` 引入 `@playwright/test`（1.62.x，与部署机缓存的 chromium-1234 匹配），
  新增浏览器级 E2E 基建与核心链路用例：
  - **配置**：`frontend/playwright.config.js`——测试目录 `e2e/tests`、单用例超时 30s、
    断言超时 10s、**重试策略 retries: 2**（失败自动重试并保留 trace，防 flaky）、
    chromium 单项目、失败产物（html 报告 + trace）输出 `playwright-report/` 与
    `test-results/`（均 gitignore）；
  - **测试用例（5 条，`frontend/e2e/tests/`）**：① 概览页加载与 issue 展示（仓库卡片、
    issue 标题/iid、bot-done 分组）；② 添加 Issue 弹窗提交（表单校验拦截空标题 →
    填写标题+勾选标签提交 → 请求体断言 → 列表刷新展示新 issue）；③ 设置页保存配置
    （修改任务调度参数 → 保存成功提示 → 请求体断言 → **重载页面验证写回 config.yaml
    持久化**）；④ 任务详情 SSE 事件流（真实后端回放种子执行日志：文本/思考/工具调用/
    工具结果/结果摘要逐事件渲染 + done 收尾标记消失）；⑤ 添加 Issue 弹窗标题必填校验；
  - **mock API 模式**：`e2e/support/mock-api.js` 仅拦截依赖真实 GitLab 的接口
    （issues/overview、pipelines/overview、issues/form-meta、POST issues），其余接口
    （settings/tasks/灵感/通知/auth/SSE）走**真实后端**（uvicorn），前后端契约与
    SSE 事件流真实验证、零真实 GitLab 依赖、数据完全确定；
  - **服务编排**：`e2e/scripts/start-servers.sh` 一键起真实后端（uvicorn，独立端口
    8011 避开生产 8000，`BOTLER_CONFIG`/`BOTLER_DB` 指向临时目录）+ 真实前端构建产物
    （vite preview，`preview.proxy` 把 /api 代理到后端，`E2E_BACKEND_URL` 可覆盖）+
    种子数据库（`e2e/scripts/seed-e2e-db.py`：1 仓库 + 1 成功任务 + claude stream-json
    执行日志 + 任务日志 + 灵感）；`vite.config.js` 增加 `preview.proxy`（生产默认仍指向
    8000，行为不变）；
  - **CI**：`.gitlab-ci.yml` 新增 `e2e` stage 与 `e2e:playwright` job（位于 build 之后、
    deploy 之前，失败阻断部署）——依赖 frontend:build 的 dist 产物，本机起 uvicorn +
    vite preview 后 Playwright 真浏览器跑全部用例，失败产物 7 天保留；`@playwright/test`
    浏览器优先复用部署机 `~/.cache/ms-playwright` 缓存（chromium-1234），下载兜底走
    npmmirror 镜像；
  - **测试**：实现前全部 E2E 用例在无实现时可复现失败，实现后本地 5/5 通过（含真实
    后端 SSE 回放），前端 `npm test` 727 用例与后端 pytest 全量无 regression。
- **设置页左侧边栏支持整体折叠/展开，折叠后收成窄栏、内容区占满全宽（issue #168）**：
  设置页左侧导航栏（SettingsNav，issue #139 引入）原为固定 240px 列，只能折叠内部
  分组子项；本次新增**侧边栏整体折叠**能力：
  - 展开态（默认）：保持现状——头部新增「收起侧边栏」按钮（«），搜索框 + 全部分组
    导航照常展示；
  - 折叠态：侧边栏收成 44px 窄栏（仅保留「展开侧边栏」按钮 »），设置内容区占满
    全宽，最大化阅读/编辑空间；
  - 偏好持久化：折叠/展开状态写入 localStorage（`botler.settings.sidebarCollapsed`，
    '1' = 折叠 / '0' = 展开），刷新或重新进入设置页保持用户上次选择；SSR/隐私模式
    无存储环境默认展开且不崩溃；
  - 无障碍：折叠/展开按钮带 `aria-label` / `aria-expanded` / `aria-controls`，折叠时
    隐藏导航面板（`display: none`）并保留窄栏展开入口；
  - 布局：`.settings-layout` 首列改为 `auto`，侧边栏宽度自身驱动（240px ↔ 44px，
    带宽度过渡动画），窄视口（≤860px）单栏回落不变；
  - **测试**：前端新增 `settings-nav-collapse.test.mjs` 11 用例（loadSidebarCollapsed /
    saveSidebarCollapsed 纯函数边界：无存储/异常存储/乱值/写回；源码链路：按钮与
    aria 属性、持久化键；渲染：默认展开 / 点击收起隐藏导航露出窄栏 / 点击展开恢复
    15 个子项与搜索框 / aria-expanded 翻转；持久化：折叠写 '1'、展开写 '0'、预置值
    初始状态；CSS：折叠窄栏与展开按钮样式）；实现前先红后绿、实现后前端全量测试
    无 regression。

- **概览页 issue 右边栏新增「查看执行的详情」按钮，点击后弹出第二层右边栏显示任务执行详情（issue #167）**：
  概览页点击 issue 弹出的右边栏（IssueDrawer）操作区新增「查看执行的详情」按钮，点击后再
  弹出一个右边栏（TaskDetailDrawer）展示该 issue 的任务执行详情：
  - 后端：新增 `GET /api/issues/{project_id}/{iid}/tasks` 接口——按 project_id + issue_iid
    返回该 issue 的全部任务执行记录（id 倒序最新在前，同 issue 因重新指派/对账补入队/手动
    重试产生的多条任务记录全部返回），任务字典复用任务列表接口序列化（status/engine/
    commit_url/时间等）；新增数据库方法 `list_tasks_by_issue`；仓库定位与 detail/close
    接口一致（project_id 匹配「已启用」仓库，不存在/未启用 → 404）；
  - 前端：新增 `frontend/src/components/TaskDetailDrawer.jsx` 第二层右边栏——打开时拉取
    任务记录列表（默认选中最新一条，可点击切换历史任务），选中任务后拉取任务详情
    （GET /api/tasks/{id}，含执行日志）、实时执行数据（GET /api/tasks/{id}/execution，
    活跃任务每 3 秒增量续读聊天记录）与 SSE 事件流（逐事件展示执行过程，seq 去重防
    断线重连重复）；详情区含元信息表（状态/引擎/来源/尝试/退出码/时间/用时/提交/错误
    信息）、事件流、聊天记录、执行日志与「查看完整任务页」跳转；无任务记录显示空态，
    加载失败可重试；关闭方式 × / 点击遮罩 / Esc（Esc 只关第二层，第一层 issue 抽屉
    detailOpen 时不响应 Esc 避免两层同关）；`styles.css` 新增任务记录列表与详情区样式；
  - **测试**：后端 `test_api_issues.py` 新增 `TestIssueTasks` 6 用例（空列表 / 多条任务
    最新在前 / commit_url 拼接 / 不串扰其他 issue / 仓库不存在 404 / 未启用 404）；
    前端新增 `overview-issue-task-detail-drawer.test.mjs` 17 用例（源码链路 / 纯函数
    taskStatusMeta·renderEvent·renderChatMessage 边界 / 按钮打开第二层与任务列表展示 /
    切换任务重拉详情 / 无任务空态 / 列表与详情加载失败重试 / ×、遮罩关闭只关第二层 /
    Esc 关闭约定）；实现前先红后绿、实现后前后端全量测试无 regression。

- **设置页面添加左侧边栏测试，确保每个设置项都有对应的名称（issue #174 第二轮重写）**：
  前端 `tests/settings-nav-labels.test.mjs` 重写并扩展至 19 用例。第一轮只做
  **静态源码解析**，漏掉了真实运行时缺陷（见本版 Fixed 同 issue 条目），
  本轮补齐「真实运行时」测试层：
  - 源码链路（静态）：Settings.jsx 全部 15 个设置区块都有名称来源（区块内
    直接 `<h2>` / `data-nav-label` 覆盖 / 卡片组件内 `<h2>`），解析出的导航
    名称不得等于原始 id；15 个已知设置项名称快照逐一断言（settings-ai-providers
    / settings-image-models / settings-vision-models / settings-backup 四个
    卡片区块的名称由卡片组件内 h2 提供）；`collectSettingsGroups` 基于真实
    源码结构生成导航时每个子项名称非空且不等于原始 id；渲染 SettingsNav 断言
    侧边栏展示全部 15 个名称、无 `settings-` 前缀 id 泄露；SETTING_KEYWORDS
    与设置区块 id 双向一致；无名称来源区块 label 回退原始 id 的兜底行为文档化；
  - 真实运行时（动态）：找出**依赖异步数据提供名称的三个设置项**——
    settings-ai-providers / settings-image-models / settings-vision-models。
    三个卡片组件分别用真实组件 + 不同 fetch 时序验证三态：数据加载中渲染
    标题 h2、加载失败渲染标题 h2 并支持点击重试、数据到达后渲染标题 h2；
    再真实渲染 Settings 页，断言渲染树中每个设置区块都有 h2、15 项名称全部
    用户可读、无 settings- 前缀 id 泄露。

- **灵感记录增加与 AI agent 对话功能，方便用户探讨灵感（issue #166）**：
  概览页「灵感」板块每条灵感操作区新增「💬 对话」按钮，点击打开对话面板，
  围绕该灵感与 AI agent 多轮探讨（完善想法、补充边界场景、评估可行性、
  给出分步落地建议），对话历史持久化到本地数据库：
  - 后端：新增 `backend/botler/chat_models.py` 对话模型统一调用封装——
    复用设置页「AI API 供应商」（ai_providers，issue #46）配置的文本对话
    模型，支持 OpenAI 兼容 `chat/completions`（deepseek / openai / moonshot /
    qwen / zhipu / siliconflow / ollama / openrouter / custom）、Gemini
    `generateContent`、Anthropic `messages` 三种协议；供应商选择取列表
    第一个启用且 API Key 非空的项（未配置时 400 引导设置页）；
  - 数据层：新增 `inspiration_messages` 表（id / inspiration_id / role /
    content / created_at，v11 迁移），删除灵感时级联清理对话消息；
  - API：`GET /api/inspirations/{id}/messages` 返回对话历史（时间升序）、
    `POST /api/inspirations/{id}/messages` 发送消息并返回 AI 回复——用户
    消息 + AI 回复成对落库；AI 调用失败/网络错误返回 502 并回滚已保存的
    用户消息（对话历史保持成对完整，前端保留输入可重试）；传给模型的上
    下文 = 系统提示（角色设定 + 灵感内容与仓库名）+ 最近 20 条历史；
  - 前端：`Overview.jsx` 灵感条目新增「💬 对话」按钮，对话面板复用
    `.modal` 体系（遮罩/×/Esc 关闭，Enter 发送 / Shift+Enter 换行），
    消息气泡列表（用户右 / AI 左）+ 发送中禁用 + 错误展示（输入保留）；
    `styles.css` 新增对话面板与气泡样式；
  - **测试**：后端新增 `test_chat_models.py` 24 用例（三种协议请求构造 /
    响应解析 / 非 JSON / 非 2xx / 无内容 / 网络错误 / 连续同角色防御 /
    供应商选择），`test_api_inspirations.py` 扩展 16 用例（历史空 / 发送
    正常 / 上下文含灵感与历史 / 404 / 空内容与超长 / 未配置与未启用 400 /
    AI 失败与网络错误回滚 / 空回复回滚 / 级联删除），`test_database_migrate.py`
    扩展 v11 迁移与消息 CRUD 用例；前端 `overview-inspirations.test.mjs`
    扩展 8 用例（按钮与接口源码断言 / 打开面板加载历史 / 空历史引导 / 发送
    渲染与输入清空 / 空白不发送 / 失败保留输入 / 加载失败可关闭）；实现前
    先红后绿、实现后前后端全量测试无 regression（后端 1500+、前端 708 用例
    全绿）。


- **添加 issue 对话框标题输入框右侧新增语音输入按钮（issue #165）**：
  概览页「添加 Issue」弹窗中，标题输入框右侧新增语音输入按钮（🎤），点击后
  通过浏览器原生 Web Speech API（`SpeechRecognition`，Chrome / Edge /
  Safari 支持）将语音实时转文字填入标题，无需后端额外接口：
  - 识别过程中 interim 结果实时预览、final 结果确认填入标题；多段语音
    （final 段 + 末尾 interim 段）按顺序拼接；识别语言固定 `zh-CN`；
  - 识别中按钮进入「listening」态（主色描边 + 呼吸光晕），再次点击停止
    （`rec.stop()`）；识别自动结束 / 关闭弹窗卸载组件时兜底清理
    （`rec.abort()`），避免识别实例泄漏；
  - 异常场景中文提示：浏览器不支持（Firefox 等）、麦克风权限被拒绝、
    未检测到语音、找不到麦克风、网络错误等，提示紧贴输入行下方展示；
  - 语音填入标题与键盘输入共用 issue #103 的「描述为空自动复制标题」联动
    逻辑（统一 `applyTitle` 入口，描述为用户手写内容时不被语音结果覆盖）；
  - **测试**：新增 `frontend/tests/overview-add-issue-voice.test.mjs` 9 用例
    （按钮渲染与输入框同行 / SpeechRecognition 创建与 start / interim 实时
    填入与 final 确认 / 多段语音拼接 / 描述空跟随与手写描述不覆盖 / 停止 /
    不支持与权限拒绝 / 无语音错误提示 / 卸载 abort 清理），实现前可复现
    失败、实现后全部通过；前端全量测试无 regression（701 用例全绿）。


- **识图模型调用时用户上传的图片计算哈希值上传 MinIO，识图请求传 http URL 而非 base64（issue #163）**：
  调用识图模型时，用户上传的图片此前直接以 base64 内联进请求体（OpenAI 兼容
  data URL / Gemini inline_data），图片 base64 可达数十万字符，网关/模型对
  请求体大小敏感、且会撑爆错误提示（issue #156 的截断展示也是为此）。本次实现
  「图片先哈希上传 MinIO、识图请求传 http URL」：
  - 新增 `backend/botler/minio_client.py`：识图图片对象存储——图片字节计算
    SHA-256 哈希，**对象名 = 哈希值**（同内容图片天然幂等去重，对象已存在不
    重复上传），桶不存在自动创建，返回 `public_base_url/bucket/<哈希>` 形式的
    http URL（上传时携带 MIME 类型，模型经 URL 拉图时 Content-Type 正确）；
  - 新增 `minio` 配置段（`config.yaml`，`enabled` / `endpoint` / `secure` /
    `access_key` / `secret_key` / `bucket` / `public_base_url` / `verify_ssl`，
    凭据支持 `${ENV}` 引用、缺省回退环境变量 `MINIO_ROOT_USER` /
    `MINIO_ROOT_PASSWORD` 与部署（issue #160）同源）；设置 API GET 返回
    （凭据掩码）、PUT 支持写入（掩码/空串凭据保持现有值），设置页暂未提供
    卡片、可编辑 config.yaml 或经 API 配置；
  - `VisionModelClient.describe` 增加 `image_store` 注入：MinIO 启用且配置
    完整时，OpenAI 兼容识图模型（`openai_vision` / `custom`）的
    `image_url.url` 改传 http URL（不再是 base64 data URL）；Gemini 官方
    generateContent 接口不支持任意 http URL 图片输入（仅支持 base64
    inline_data / file_data），保持 base64 内联并记 warning 日志；
    未配置 / 配置不完整回退 base64 原行为（原部署零影响）；
  - 依赖新增 `minio>=7.2`（部署 job `uv pip install -r requirements.txt`
    自动安装）；`config.example.yaml` / `.env.example` / README 同步说明
    （含「识图模型必须能访问 public_base_url、桶需允许匿名读或使用预签名
    URL」的部署提示）；
  - **测试**：新增 `test_minio_client.py` 12 用例（SHA-256 哈希命名 / http
    URL 构造 / 桶自动创建 / 幂等去重 / 空图片与上传失败错误 / 配置构造与
    env 回退 / settings 接线），`test_vision_models.py` 扩展 OpenAI /
    custom URL 模式、Gemini 降级（仍 base64）、上传失败转识图错误用例，
    `test_api_settings.py` 扩展 minio 段 GET / PUT 校验（类型、URL 格式、
    掩码凭据保持）与 vision-model-test 端点 image_store 注入用例；
    实现前可复现失败、实现后全部通过；后端全量测试无 regression。


- **灵感「添加 Issue」创建成功后自动从灵感列表删除（issue #162）**：
  概览页灵感板块「添加 Issue」一键提交为 GitLab issue 成功后，该灵感
  仅保留在本地数据库中、且不会自动移除——灵感内容已转为 GitLab issue，
  保留会诱导用户重复点击、每次重复提交都新建一个 issue（造成重复内容）。
  本次按需求「成功推送到 gitlab 后，从灵感列表里删除」实现：
  - 后端 `POST /api/inspirations/{id}/add-issue` 创建 issue 成功后删除
    该灵感记录（本地数据库操作，防御性捕获告警，不阻塞返回成功）；
    失败路径（GitLab 故障 / 未配置 owner token / 仓库未启用等）不走到
    删除逻辑，灵感保留可重试；
  - 前端创建成功后除刷新开放 issue 列表外，同时刷新灵感列表移除条目
    （不等 15 秒轮询），成功提示（新 issue 编号 + 链接）保留展示；
  - **测试**：后端 `test_api_inspirations.py` 扩展正常路径断言「创建成功
    后灵感已删除、overview 不再展示」并新增失败场景保留断言（GitLab
    故障 / 未配置 owner token / 仓库未启用），实现前可复现失败、实现后
    全部通过；前端 `overview-inspirations.test.mjs` 扩展「创建成功后刷新
    灵感列表」「提交失败不刷新（条目保留）」断言，同样先红后绿；前后端
    全量测试无 regression。


- **添加仓库页面增加调度优先级设置选项（issue #161）**：仓库调度优先级（issue #51，
  1~999 整数、默认 100、数字越小越优先）此前仅能在后端 API 与仓库「设置」弹窗中配置，
  「添加仓库」表单未暴露该选项，新添加的仓库只能取后端默认 100。本次在添加仓库表单
  新增「调度优先级」输入项：
  - 默认值 100（与后端缺省一致），提交时 `POST /api/repos` 携带 `priority`；
  - 留空则不带 `priority` 字段，由后端按默认 100 处理；
  - 非法输入（非整数、<1、>999）前端拦截并提示「优先级需为 1~999 之间的整数」，
    不发请求；
  - 添加成功后表单重置回默认 100。
  - **测试**：新增 `frontend/tests/repos-add-priority.test.mjs` 6 用例（源码级
    placeholder / 1~999 校验 / 提交 body 断言 + 渲染级默认值 / 提交携带 / 留空不带 /
    非法拦截 / 成功后重置），实现前全部可复现失败，实现后全部通过；同步把
    `repos-edit-modal.test.mjs` 弹窗输入框查找限定在弹窗内（添加表单新增默认值 100 的
    优先级输入框后，全局查找会错位命中），前端全量测试无 regression。

- **后端部署时增加 MinIO 对象存储服务：docker compose 与 pm2 两种部署形态均可用（issue #160）**：
  后端部署（pm2 为主、docker 为辅）时需一并提供一个 MinIO 对象存储服务，作为后续功能的
  存储底座。本次两种部署形态全部打通：
  - **Docker 部署**：`docker-compose.yml` 新增 `minio` service——镜像
    `minio/minio:RELEASE.2025-04-22T22-12-26Z`（`MINIO_IMAGE` 可覆盖为国内镜像源）、
    API 端口 9000 / console 端口 9001（`MINIO_API_PORT` / `MINIO_CONSOLE_PORT` 可覆盖）、
    数据卷挂载 `${BOTLER_DATA_DIR}/minio/data:/data`（与 botler 数据同根目录，容器重建不丢）、
    根凭据 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 环境变量注入（默认 minioadmin，
    生产环境务必覆盖）、`/minio/health/live` 健康检查、`restart: unless-stopped`；
    `deploy/verify-docker.sh --full` 冒烟同步增加 minio 容器 / 健康检查 / 数据目录校验
    （19000/19001 临时端口，不碰真实环境）；
  - **pm2 部署**：新增 `deploy/install-minio.sh` 一键安装 minio server 二进制（版本
    锁定 `RELEASE.2025-04-22T22-12-26Z`、默认装 `$HOME/.local/bin`、`MINIO_DOWNLOAD_URL`
    可覆盖下载源、幂等跳过、临时文件 + mv 原子替换、安装后 `minio --version` 校验 fail
    fast）；`deploy/botler.config.cjs` 新增 `botler-minio` app（`interpreter: none` 托管
    原生二进制，数据目录 `$BOTLER_DATA_DIR/minio/data`，凭据从 `data/backend/.env`
    读取、缺失回退默认值，日志 `logs/pm2-minio-*.log`）；CI `deploy_to_code01` 在依赖
    安装后自动调用安装脚本、把 minio 凭据写入 `data/backend/.env`（幂等，CI 变量优先）、
    停止旧部署时一并删除旧 `botler-minio`、健康检查阶段增加 `/minio/health/live` 探活
    （失败即部署失败）；
  - **凭据与文档**：`backend/.env.example` 声明 `MINIO_ROOT_USER` /
    `MINIO_ROOT_PASSWORD` / `MINIO_API_PORT` / `MINIO_CONSOLE_PORT`；README 两种部署
    方式均补充 minio 启动 / 验证 / 数据目录 / 可调参数说明。
  - **测试**：新增 `test_deploy_minio.py` 27 用例（安装脚本存在性/版本锁定/镜像覆盖/
    幂等/原子替换、pm2 配置 app/args/凭据/日志、compose service 定义、CI 部署 job
    自动安装与健康检查、.env.example 与 README 同步），实现前全部可复现失败，实现后
    全部通过；后端全量测试无 regression。

### Fixed

- **webhook 推送渲染非法 JSON 导致推送失败——修复 issue 描述含换行/引号等特殊字符时飞书返回 HTTP 400（issue #298）**：
  线上现象——用户在设置页关闭「网页通知」后，发现「消息推送 Webhook」也不再推送了
  （飞书收不到任务完成消息）。诊断：webhook 推送与网页通知开关在代码上完全独立
  （`notifications.enabled` 只影响浏览器系统通知，`webhook.enabled` 独立控制推送），
  二者无耦合；实际根因是任务收尾推送时 payload 渲染缺陷——`WebhookPusher.build_payload`
  对占位符做逐项原始 `str.replace`，issue 描述含换行时 `body` 字段出现裸换行，渲染出
  非法 JSON，飞书等目标以 HTTP 400（code 9499 Bad Request）拒绝（推送仍在尝试，仅
  目标拒绝）。本地数据库 task_logs 20 条推送记录 100% 相关：全部失败任务（#183/#210/
  #212/#214/#217/#230）的 issue 描述均含换行，全部成功任务均无换行；用户关闭网页通知
  后恰好连续 3 个收尾任务的描述都含多行，推送连续失败，感知为「关闭网页通知导致
  webhook 也不推送」（时间巧合，非因果关系）。本次修复：
  - `backend/botler/webhook_push.py`：`build_payload` 改为 JSON 感知渲染——模板按
    JSON 解析后在字符串值上替换占位符、再整体 `json.dumps` 序列化，issue 正文/标题
    中的换行、双引号、反斜杠等特殊字符被正确转义，渲染结果始终是合法 JSON；
  - **飞书双编码模板兼容**：`content` 等字段是「JSON 字符串内嵌 JSON 文本」的常见
    写法，修复对这类值先判定（占位符换安全哨兵后仍可解析为 JSON 对象/数组）再按
    JSON 转义变量后替换，内外层 JSON 都合法，标题含引号也能正常推送；
  - **非 JSON 模板兼容**：body_template 不是合法 JSON 时退回逐项字符串替换
    （历史行为不变），不影响既有配置；
  - **测试**：`backend/tests/test_webhook_push.py` 新增 `TestBuildPayloadJsonEscaping`
    4 例（多行正文 / 引号与反斜杠 / 默认模板多行 / 飞书双编码模板内外层均合法），
    修复前全部可复现失败（渲染出非法 JSON，`json.loads` 抛
    `Invalid control character` / `Expecting ',' delimiter`），修复后通过；既有渲染
    断言同步适配序列化格式（键冒号后空格，内容不变）；后端全量测试通过，无 regression。

- **GitLab 瞬时故障（502/限流/网络抖动）退避重试——修复排队任务批量失败且 issue 无任何回复评论（issue #280 诊断修复）**：
  08-17 生产事故诊断：GitLab（SafeLine WAF 前置）短暂不可用返回 502
  （`GitLab is not responding` / WAF 兜底页），botler 任务启动阶段拉取 issue
  一次 502 即全部判 failed（当日 44 个排队任务在 42 秒内批量失败），且失败
  评论、bot-failed 标签调用同样遇 502 **各试一次即放弃**——受影响 issue 上
  「没有任何的回复和评论」，用户无法感知任务失败。本次修复：
  - **瞬时故障分类**：`botler/gitlab_client.py` 新增 `is_transient_error()`——
    429/500/502/503/504 与传输层故障（连接超时/拒绝/DNS 解析失败）视为瞬时
    可重试，其余 4xx 永久性错误不重试；
  - **GET 读取退避重试**：`_request`/`_paged` 重构为 `_http_request_with_retry`，
    GET（幂等读取：issue 查询/对账扫描/概览等）遇瞬时故障指数退避重试最多 3 次
    （1s/2s/4s + 抖动）；写操作（评论 POST / 标签 PUT）不重试，避免网络抖动
    导致重复提交；
  - **任务启动重试**：`executor.run_task` 拉取 issue 遇瞬时故障按指数退避重试
    （最多 `ISSUE_FETCH_MAX_ATTEMPTS=5` 次、间隔 5s→60s），重试耗尽才判
    failed——一次短暂故障不再把整批排队任务全部打挂；
  - **收尾反馈重试**：`_finish_failed`/`_finish_asked` 的失败评论、
    bot-failed/blocked 标签，以及任务启动的「处理中」评论均走新增
    `_transient_retry`（最多 5 次退避重试），GitLab 恢复后反馈仍能送达，
    用户不再面对「无任何回复评论」；
  - **测试**：`backend/tests/test_gitlab_client.py` 新增瞬时分类与 GET 重试 11 例
    （502 重试成功 / 重试耗尽抛错 / 传输层超时重试 / 404 不重试 / POST 不重试 /
    分页重试）；`backend/tests/test_executor.py` 新增启动拉取瞬时重试与失败评论
    重试 5 例（旧代码下可复现失败——失败评论仅 1 次调用即放弃）；后端全量测试
    （147 例）与覆盖率门禁（≥70%）通过，无 regression。

- **Web 终端反向代理遗漏显式关闭——终端服务拒绝（伪造/过期 token）时浏览器端永远收不到断开事件导致挂起（issue #183）**：
  Web 终端（issue #183）WebSocket 反向代理 `_pump` 在终端服务以非 1000 关闭码
  （如 token 校验失败 close 4001）结束时，`to_client` 捕获 `ConnectionClosed`
  后异常被吞掉，endpoint 的 try 块随之**正常完成但从未调用 `websocket.close()`**
  ——starlette 在 WebSocket endpoint 正常返回时不会自动向客户端发送 close，
  浏览器端 `receive()` 永远阻塞收不到断开事件：前端终端标签一直停留在「连接中」、
  后端 `test_ws_proxy_forged_token_rejected` 无限挂起（CI backend:test 卡死超时）。
  修复：
  - **实现**：`backend/botler/api/terminal.py` `_pump` 改为返回终端服务会话的
    WebSocket 关闭码（`ConnectionClosed` 时取上游 `rcvd.code`，正常结束默认 1000）；
    endpoint 在 `finally` 中**显式关闭浏览器端连接**并原样传递上游关闭码与原因
    （认证拒绝 4001 → 浏览器端也收到 4001），同时保留上游连接异常（`connect`
    失败等）以 1011 关闭的兜底；
  - **测试**：`backend/tests/test_api_terminal.py`
    `test_ws_proxy_forged_token_rejected` 补充断言 `WebSocketDisconnect.code == 4001`
    （修复前该用例无限挂起，可复现失败；修复后 0.6s 通过）；终端相关三组测试
    （test_api_terminal / test_terminal_service / test_terminal_token，24 例）与后端
    全量测试（1723 例）均通过，覆盖率 88.46%（阈值 70%），无 regression。


- **config.yaml 保存改为原子写 + 重载失败不污染内存——修复并发读半截文件导致设置保存偶发 500（issue #181 CI 诊断）**：
  E2E settings 测试偶发失败的根因：`ConfigManager.save()` 此前直接 `open(path, "w")`
  截断写盘，并发读（`get()` 的 mtime 自动重载 / `update_*` 写盘前 `_reload_from_disk`）
  会读到**半截 config.yaml**——PyYAML 抛 `YAMLError`，或更糟：解析出残缺配置
  （`gitlab` 段缺 `url`）后 `load()` 先把残缺内容赋给内存 `_data` 再 `_to_settings` 抛
  `KeyError: 'url'`，重载失败但内存已被污染；`update_webhook` 等随后在污染数据上
  `_to_settings` 再次 KeyError → `PUT /api/settings` 500 → 前端无「已保存」提示、
  E2E 设置保存用例失败（且 worker 段已落盘，重试时默认值错乱）。修复：
  - **实现**：`backend/botler/config.py` `save()` 改为原子写（同目录临时文件 +
    `os.replace`，写入过程任何时刻读取都只能得到完整旧文件或完整新文件）；
    `load()` 改为**先完整解析并校验（`_to_settings`）成功后才替换内存 `_data`**，
    磁盘文件损坏/半截时抛异常、内存保持旧值不污染，`update_*` 降级继续用完整内存
    配置工作；不残留临时文件；
  - **测试**：`backend/tests/test_config_reload.py` 新增 3 例——残缺 YAML（gitlab 段缺
    url）重载失败不污染内存且 `update_webhook` 不再 KeyError（修复前复现 CI 的
    `KeyError: 'url'`）、并发读写下任何时刻读到的都是完整配置（修复前读线程捕获
    半截/残缺）、原子写无临时文件残留；修复前全部可复现失败，修复后通过；后端全量
    测试（1699 例）与前端全量测试（828 例）、E2E 连续 3 轮均通过，无 regression。

- **GitLab 传输层故障（DNS 失败/连接拒绝/超时）裸抛 httpx 异常导致线程崩溃——统一转 GitLabError（issue #212 配套）**：
  `GitLabClient._request` / `_paged` 此前只处理 HTTP 状态码（401/403/404/>=400），
  DNS 解析失败、连接拒绝、超时等传输层故障会裸抛 `httpx.ConnectError` 等异常，绕过
  调用方 `except GitLabError` 的优雅降级——对账首轮线程直接崩溃打印 traceback、
  APScheduler 定时任务标记失败。修复：两处请求统一 `except httpx.HTTPError` 转抛
  `GitLabError("GitLab 请求失败（path）: ...")`，与「GitLab 故障不中断整体」的既有
  设计一致（E2E 用假 GitLab 地址启动真实后端时日志干净、对账照常降级重试）；
  `test_gitlab_client.py` 55 用例全量通过。

- **调度器同权重 issue 未按创建时间排序——同优先级时改为按 issue 创建时间升序派发，创建时间越早的 issue 越先处理（issue #234）**：
  需求约定「issue 标记优先级没有区别的情况下，按照创建时间的排序来处理 issue，创建
  时间越早的 issue，越早处理」。此前（issue #76 实现）队内同标签权重时按 **issue
  更新时间**升序选任务派发——issue 被编辑/评论后 updated_at 会推进，更新晚的 issue
  反而排到更新早的 issue 前面，与「先创建先处理」的约定不符。修复：
  - 数据库：`tasks` 表新增 `issue_created_at` 列（迁移 v12，旧库自动补列），入队时
    记录 GitLab issue 创建时间（`normalize_issue_created_at` 归一化为 UTC 无后缀串，
    与 `normalize_issue_updated_at` 共用同一解析实现）；
  - 调度器：`_task_sort_key` 同权重排序键由「issue 更新时间」改为「issue 创建时间」，
    创建时间缺失的历史任务按 issue 更新时间、再按任务提交时间 `created_at` 兜底；
  - 入队链路：webhook / 对账补入队 / 概览页手动重试新建任务三处均记录
    `issue_created_at`；
  - 文档：README 调度顺序说明同步改为「同优先级按 issue 创建时间升序」；
  - **测试**：`test_scheduler_issue_priority.py` 新增/改写 3 用例（同权重按创建时间
    升序、创建时间优先于更新时间——创建早但更新晚的 issue 先派发、创建时间缺失时
    按更新时间兜底）；`test_database_migrate.py` 新增 `TestMigrateIssueCreatedAt` 5
    用例（旧库补列 / 新库建列 / create_task 落库 / 缺省空串 / 归一化函数）；
    `test_api_issues.py` 手动重试用例补充创建时间断言；实现前先红后绿、实现后后端
    全量测试无 regression。

- **从灵感一键添加 Issue：灵感内容超过 255 字符时创建失败——标题截断到 GitLab 上限（255 字符）并加省略号标记、描述保留完整内容（issue #186）**：
  用户反馈「从灵感添加到 issue 的时候，限制了 255 个字符，但是我在 gitlab 的描述里
  却可以输入超过 255 个字符」。根因：灵感「添加 Issue」把灵感内容**同时**作为 issue
  标题与描述提交，而 GitLab issue 标题是服务端硬限制（最大 255 字符，超长时创建接口
  直接 400 `title is too long (maximum is 255 characters)`，实测复现），描述字段上限
  远大于标题（1MB 量级）——灵感内容一超过 255 字符，整次创建就被 GitLab 拒绝，
  表现为「被限制在 255 字符」。修复：`add_issue_from_inspiration` 在内容超过
  `GITLAB_ISSUE_TITLE_MAX_LEN`（255，gitlab_client 新增平台常量）时，标题截断到
  254 字符并追加省略号「…」（总长 255），**描述始终保留完整灵感内容**——GitLab
  描述支持远超 255 字符，用户可在描述里看到全部正文。配套测试：
  `test_api_inspirations.py` 新增 2 用例（超长内容标题截断 + 描述完整、255/256
  字符截断边界），实现前先红后绿（修复前超长标题 480 字符直传，GitLab 400 拒绝），
  实现后后端全量测试无 regression。

- **设置页左侧导航栏把 settings-ai-providers / settings-image-models / settings-vision-models 等原始 id 当名称展示（issue #174 第二轮）**：
  用户反馈侧边栏仍出现 settings-vision-models 等类似名称的设置项（第一轮只
  加了静态测试、未修复运行时代码）。根因：SettingsNav 挂载时**只读取一次**
  `.settings-content` 的 DOM 生成导航（useLayoutEffect 空依赖），而三个卡片
  组件（AiProvidersCard / ImageModelsCard / VisionModelsCard）在数据加载中
  （内部 useState 初始为 null）直接 `return null`——区块内没有 `<h2>`，导航
  名称回退成原始 id（settings-ai-providers / settings-image-models /
  settings-vision-models），数据到达后导航不会重建，异常永久保留。修复：三个
  卡片与 BackupManager 同款，加载中/加载失败也渲染卡片标题 `<h2>`（失败时
  展示错误原因并支持点击重试），保证 SettingsNav 挂载时每个设置区块内始终有
  名称来源。配套测试：settings-nav-labels.test.mjs 新增运行时用例（加载中 /
  失败 / 就绪三态标题 h2 + 真实渲染 Settings 每个区块都有 h2），实现前先红
  后绿，全量前端 727 用例、后端 1500+ 用例无 regression。

- **识图模型「测试」彻底移除 base64 内联——图片一律上传 MinIO（默认桶 public + 公开只读 + nginx 代理访问，issue #164 用户反馈续修）**：
  用户反馈「不要使用 base64」，要求图片上传 MinIO、nginx 代理 MinIO 桶、桶权限
  公开只读、无桶则创建名为 `public` 的桶。此前（issue #163/#164 第一轮）MinIO
  未启用时识图测试仍会静默回退 base64 data URL 内联（OpenAI 兼容网关如阿里云
  百炼 qwen 直接拒绝，报「url error, please check url」）。本次整改：
  - **OpenAI 兼容识图模型（`openai_vision` / `custom`）禁止 base64 内联**：
    `VisionModelClient.describe` 在未启用 / 配置不完整 MinIO（`image_store=None`）
    时直接报错，引导配置 `minio.enabled + endpoint + access_key + secret_key +
    public_base_url`，绝不把图片 base64 塞进请求体；Gemini 官方 generateContent
    接口仅支持 base64 inline_data（Google API 限制），保持 base64 内联输入并
    记录说明；
  - **默认桶改为 `public` 并自动设为公开只读**：`minio.bucket` 默认值
    `botler-images` → `public`（config 默认 / env 回退 / 示例同步）；桶不存在
    自动创建，创建后（及已存在的老桶）自动调用 `set_bucket_policy` 设置
    「匿名 `s3:GetObject`」公开只读策略（实例内幂等只设置一次），失败统一转
    `MinioStoreError` 并提示检查 MinIO 凭据权限——识图模型（含外部公网网关）
    需匿名读图片 URL；
  - **nginx 代理 MinIO 桶**：新增 `deploy/nginx-minio-public.conf`（location
    `/minio-public/` → `proxy_pass http://127.0.0.1:9000/public/`），对象 URL
    形如 `https://<站点>/minio-public/<sha256 哈希>`，`public_base_url` 建议填
    nginx 代理地址（无需暴露 9000 端口）；README / config.example.yaml /
    .env.example 同步部署说明；
  - **测试**：`test_vision_models.py` 新增「OpenAI / custom 未启用 MinIO 时
    describe 报错引导（不发请求）」2 用例，原 base64 载荷相关用例全部改为
    http URL 模式断言；`test_minio_client.py` 新增「默认桶 public / 桶自动创建
    并设公开只读 / 策略幂等只设一次 / 已存在桶补设策略 / 策略失败转业务错误」
    5 用例并更新桶名断言；`test_api_settings.py` 桶名断言同步；实现前可复现
    失败、实现后全部通过；后端全量测试无 regression（1488 passed）。

- **识图模型「测试」点击报错「✗ Failed to fetch」——async 端点内同步调用模型冻结事件循环 + 网关拒绝 base64 data URL 图片时给出可操作诊断（issue #164）**：
  设置页「识图模型」卡片点「测试」上传图片后，浏览器报「✗ Failed to fetch」、
  后端访问日志无该请求记录；即使有响应，对阿里云百炼等 OpenAI 兼容网关
  （如 `qwen3.6-flash`）也会必现「HTTP 400 url error, please check url」。
  两个根因：
  - **事件循环被冻结**：`POST /api/settings/vision-model-test` 是 `async def`
    端点，内部却直接同步调用 `VisionModelClient.describe`（同步 httpx 请求，
    最长 60s）——整个 uvicorn 事件循环被阻塞期间，浏览器并发请求连接级失败
    （连接不被 accept/处理，表现为「✗ Failed to fetch」，且访问日志无记录），
    全站轮询 / SSE 任务流同步卡死。修复：模型调用移入线程池
    （`asyncio.to_thread`），事件循环不再被模型请求阻塞（与 `test_image_model`
    的同步 `def` 由 FastAPI 自动线程池化保持一致）。
  - **base64 data URL 被网关拒绝且提示晦涩**：未启用 MinIO 图片上传时图片以
    base64 data URL 内联进请求体，部分 OpenAI 兼容网关（阿里云百炼 qwen）不接受
    data: URL，返回「url error, please check url」；错误文案只堆叠请求信息、
    无解决方向。修复：`openai_vision` / `custom` 供应商在图片为 base64 内联
    模式、网关返回图片 URL 类错误（`url error` / `invalid url` / `image_url`
    / `data:image`）时，错误信息自动追加诊断提示——引导启用 MinIO 图片上传
    （识图请求改传 http URL，issue #163 的 `minio.enabled` + `public_base_url`）
    或更换支持 base64 内联的网关；http URL 模式下不加该提示（避免误导）。
  改动：`backend/botler/api/settings.py`（线程池化）、
  `backend/botler/plugins/vision_models.py`（诊断提示）。
  **测试**：`test_api_settings.py` 新增「慢速模型调用不阻塞事件循环」回归用例
  （共享事件循环下并发 GET 必须及时返回——修复前被阻塞约 2.5s、修复后不受影响），
  `test_vision_models.py` 新增 3 个用例（base64 data URL 被拒时带 MinIO/base64
  提示、http URL 模式不加提示、无关 400 错误不加提示）；
  实现前可复现失败、实现后全部通过；后端全量测试无 regression。

- **灵感页面「添加 Issue」未自动设置 git 仓库用户为负责人：提交时存储值缺失则按 remote url 运行时读取兜底并写回仓库表（issue #159）**：
  概览页灵感板块「添加 Issue」一键提交为 GitLab issue 时，分配人应默认取该仓库 remote url
  中的用户名（如 `https://user:token@host/...` 的 user，issue #153 引入的「仓库用户」）。
  实测复现：issue #153 之前入库的存量仓库 `remote_username` 未落库为 NULL，灵感一键提交
  issue（如灵感 #13 → issue #158）时后端只读存储值、不尝试读取 remote url，导致创建的
  issue 没有分配人，与需求「自动设置 git 仓库用户为负责人」不符。本次修复：分配人解析在
  存储值为空时按仓库 remote url **运行时读取**用户名兜底（`git_remote.read_repo_remote_username`，
  读取顺序与设置页「重新读取 remote URL」一致：local_path → workspace/<name> → 存储 url），
  读到后**写回仓库表**（设置页同步可见、后续提交直接命中缓存）；仍读不到（URL 无凭据 /
  目录不可读 / 非 git 仓库）保持原行为（不指定分配人），任何异常都不阻塞 issue 创建。
  改动：
  - 后端 `api/inspirations.py` `add_issue_from_inspiration()`：`remote_username` 为空时调用
    `read_repo_remote_username(repo)` 兜底读取并落库，再按用户名解析 GitLab 用户 id 设为
    分配人；模块与函数文档同步补充 #159 说明；
  - **测试**：`test_api_inspirations.py` 新增 `TestAddIssueFromInspirationRuntimeRemoteUser`
    3 用例——存量仓库（存储值为空、local_path 带内嵌凭据 remote）提交后分配人正确解析且
    仓库表写回 `agent`、运行时读不到用户名保持不指定分配人、运行时读取抛异常降级不阻塞
    创建。修复前复现用例失败（`assignee_id=None`），修复后全部通过；后端全量 1428 例 +
    前端全量 686 例通过。

- **识图模型测试失败时展示「后端 POST 给上游 API 的信息」：错误提示统一带上实际请求地址 + 请求头（API Key 掩码）+ 请求体（图片 base64 截断）（issue #156）**：
  设置页「识图模型」卡片点「测试」上传图片调用识图模型，失败时前端只展示后端返回的错误文本；
  部分失败路径的信息不足以定位问题——网络层失败（超时/连接失败/DNS/SSL）只报「请求超时（>Ns）」
  或「网络请求失败: {httpx 异常}」，不含实际 POST 出去的请求内容；接口返回 2xx 但响应缺少内容/
  未包含文本描述时只带响应片段。按用户反馈（「后端给 ai 模型供应商 post 的信息，要显示出来」）
  本次让所有失败路径的错误提示统一包含「后端 POST 给上游 API 的信息」——实际请求地址、请求头
  （Authorization / X-goog-api-key 等密钥统一掩码、Bearer 前缀保留）、请求体（JSON 载荷原文，
  base64 图片数据截断展示并标注总长度），用户可据此对照网关/供应商确认 URL、载荷结构与密钥配置。
  改动：
  - 后端 `plugins/vision_models.py`：新增 `format_request_info()`（脱敏摘要助手：请求头认证密钥
    掩码、请求体超长字符串截断 / key-token 类字段整体掩码）与 `_post_json()`（统一 POST 入口，
    网络层异常时把本次请求头/请求体附加到异常对象，供上层拼进错误）；Gemini / OpenAI / 自定义
    三个识图供应商的全部失败路径（4xx/5xx、JSON 解析失败、响应缺内容 / 未包含文本描述）统一
    带上完整 POST 请求信息；
  - 后端 `vision_models.py` `VisionModelClient.describe()`：超时/网络异常（`httpx.TimeoutException`
    / `httpx.HTTPError`）从异常携带的 request 提取请求地址，并读取供应商附加的请求头/请求体
    拼出完整 POST 请求信息；外部插件未附加时回退为仅请求地址（原行为）；
  - 前端无需改动：失败提示展示后端 `error` 原文，`.err-hint` 样式已支持多行/长文本折行
    （issue #151 同款）；
  - **测试**：`test_vision_models.py` 新增 10 用例（超时/连接失败带请求体与密钥掩码、401/429
    非 2xx 带请求体、JSON 解析失败带请求体、Gemini / OpenAI / 自定义缺内容带请求体、
    `format_request_info` 掩码/截断/敏感字段单元测试）；`test_api_settings.py` 透传用例扩展
    断言请求头/请求体随错误原文一并返回前端。
    修复前新用例全部可复现失败（错误信息不含「请求体」），修复后全部通过。

- **设置页左侧导航栏缺「识图模型」子选项：导航栏改为读取设置页设置区块动态生成，不再硬编码（issue #155）**：
  设置页左边导航栏没有「识图模型」的子选项——issue #152 新增「识图模型」卡片时挂在了生图模型
  同一个区块内，而导航栏结构是硬编码的 `SETTINGS_GROUPS`，只给该区块配了「生图模型」一个子项，
  左边导航栏与右边设置页设置选项对不上。按需求重构导航栏架构：**通过读取设置页中实际渲染的
  设置项来生成导航，而不是硬编码**，彻底解决两边对不上的 bug。改动：
  - 前端 `SettingsNav.jsx`：移除硬编码的 `SETTINGS_GROUPS`，新增并导出 `collectSettingsGroups()`——
    运行时扫描设置页 `.settings-content` 内容区中实际渲染的分组标题（`h2.settings-group-title`）
    与设置区块（`section.settings-section`，锚点 id 即区块 id）生成导航结构；label 默认取区块内
    首个 `<h2>` 文本，需要短导航名时用 `data-nav-label` 属性覆盖；未归属任何分组的区块进
    「其他设置」兜底分组（不会悄悄丢失）；`SETTING_KEYWORDS` 仅作搜索关键词辅助映射，不参与
    结构生成——任何新设置卡片挂载到设置页后导航栏自动出现对应子选项，天然一一对应；
  - 前端 `Settings.jsx`：生图 / 识图两张卡片拆分为两个独立区块（`settings-image-models` /
    `settings-vision-models`），「识图模型」成为独立设置项；「系统设置」分组标题统一为
    `h2.settings-group-title` 约定（原为 `<h1>`，导航读取的固定约定）；owner-token 区块加
    `data-nav-label="Owner GitLab Token"` 提供短导航名；顺手修正 `settings-environment` /
    `settings-backup` 区块的嵌套结构（environment 未闭合就开 backup）；
  - **测试**：`settings-nav.test.mjs` 重构为架构断言——导航从设置页 DOM 生成（6 组 15 项，
    含「识图模型」子选项）、全量覆盖不变式（设置页每个区块都出现在导航中、导航每项都能在
    设置页找到对应区块，双向对得上）、未分组区块进兜底组、搜索/折叠/滚动等交互行为保持可用；
    `settings-ai-providers-card.test.mjs` 同步「系统设置」标题断言（h1 → h2 约定）。修复前新
    用例可复现失败（导航无「识图模型」子选项、无 `collectSettingsGroups`），修复后全部通过。


### Added

- **添加仓库时自动在目标 GitLab 项目补齐标记库缺失的默认标签（issue #157）**：
  「添加仓库」成功后，若目标项目（GitLab 仓库）上不存在标记库内置的默认标签
  （`labels.DEFAULT_LABELS`，即 docs/labels.md 规范里的 14 个类型/流程标签），平台自动
  在 GitLab 上逐个创建这些标签，免去新仓库手动补标签、或依赖脚本批量同步的前置步骤。
  与 `scripts/sync_labels.py` 的区别：只补缺失、不覆盖已存在标签的颜色/描述（避免覆盖
  用户自定义）；同步为尽力而为——读取远端标签或创建失败只记日志，不阻塞仓库添加
  （仓库主体「项目识别 + webhook 注册」已就绪，标签缺失不影响平台正常工作）。改动：
  - 后端 `gitlab_client.py`：新增 `create_project_label()`（POST /projects/:id/labels，
    name/color/description 组装，description 缺省不携带）；
  - 后端 `api/repos.py`：新增 `_sync_default_labels()`（list 远端标签 → 逐个创建缺失的
    内置默认标签），在 `add_repo` 注册 webhook 成功后调用，复用同一 GitLab 客户端
    （全局 token 失效时沿用 remote url 内嵌 token 的兜底 client）；
  - 前端无需改动（响应结构不变，标签补齐结果记入后端日志）；
  - **测试**：`test_api_repos.py` 新增 6 用例（全缺→14 个全建、部分缺失→只建缺失、
    同名不同色→不覆盖、读标签失败→跳过不阻塞、创建失败→跳过单个不阻塞、仓库已存在
    409→不做同步），既有 local_path 添加用例补充「14 个默认标签全部补齐」断言；
    `test_gitlab_client.py` 新增 `create_project_label` 请求体组装 2 用例。实现前新用例
    全部可复现失败（`GitLabClient` 无 `create_project_label` 属性），实现后全部通过。

- **设置页新增「识图模型」卡片：可配置具有视觉理解的模型，并支持上传图片调用模型描述图片（issue #152）**：
  设置页面新增识图模型配置能力，复用生图模型（issue #135/#137）的成熟模式——内置
  Gemini 视觉（默认模型 `gemini-2.5-flash`）与 OpenAI 视觉（默认模型 `gpt-4o`）两个
  预设 + 自定义类型（OpenAI 兼容 chat/completions 接口，可配硅基流动 / DeepSeek-VL /
  qwen-vl 等网关），每个模型可配置名称 / 识图模型类型 / Base URL / API Key / 默认模型 /
  启用开关，并有「测试」按钮：点击后用户上传一张图片，后端调用配置的模型描述图片
  内容，前端展示模型返回的描述文本。改动：
  - 后端插件体系 `plugins/base.py`：`PluginKind` 新增 `vision_model_provider` 分类 +
    `VisionProviderPlugin`（`describe()`：图片字节 + MIME + 描述指令 → 文本描述），
    `resolve_request_url()` 复用自定义 Base URL 完整直用语义（issue #150）；
  - 后端 `plugins/vision_models.py`（新）：内置识图供应商插件 `gemini_vision`
    （generateContent 接口，图片 inline_data 输入 + 文本输出）、`openai_vision`
    （chat/completions 接口，image_url data URL 输入 + 文本输出）、`custom`（OpenAI
    兼容 chat/completions，无默认端点/模型，Base URL 完整直用），统一
    `VisionModelError` 诊断（非 2xx / 非 JSON / 无文本结果均带请求地址与排查提示）；
  - 后端 `vision_models.py`（新）：`VisionModelClient.describe()` 统一调用入口 +
    `VISION_MODEL_PRESETS`（插件注册表派生）+ `find_enabled` / `client_from_config`；
  - 后端 `config.py` / `api/settings.py`：`Settings.vision_models` 配置段读写（api_key
    落盘 config.yaml、API 只返回掩码、掩码/留空 = 保持现有，与 image_models 同模式）；
    新增 `POST /api/settings/vision-model-test`（multipart 上传图片 + 表单配置，成功
    返回 ok=true + 描述文本，失败 ok=false + 原因，不抛 500）；
  - 前端 `providers.jsx` 新增 `VISION_MODEL_PRESETS`（gemini_vision / openai_vision /
    custom），`VisionModelsCard.jsx`（新）设置页「识图模型」卡片（列表增删改 + 测试
    按钮：点击弹图片选择框 → 自动提交识别 → 展示描述文本），`api.js` `request()` 支持
    FormData body（multipart），`Settings.jsx` 挂载卡片（生图模型卡片之后），
    `Plugins.jsx` 插件管理页新增「识图模型供应商」分组；
  - **测试**：`test_vision_models.py`（新）21 用例（预设 / 缺图片 / 缺 API Key / 缺
    Base URL / Gemini 请求构造与文本解析 / OpenAI 请求构造与解析 / custom 完整直用 /
    find_enabled / client_from_config）；`test_api_settings.py` 新增 `TestVisionModels
    Settings` 14 用例（读写 / 掩码 / 整体替换 / 校验）+ `TestVisionModelTestEndpoint`
    9 用例（缺图片 / 缺 provider / 成功回传描述 / 掩码回退 / 列表行测试 / 错误降级 /
    verify_ssl 跟随）；`test_api_plugins.py` 更新插件列表断言（新增 vision_model_provider
    分组）；`settings-vision-models-card.test.mjs`（新）13 用例（卡片挂载位置 / 表单
    字段 / 保存段 / 测试按钮上传流程 / 描述展示 / 预设清单 / logo 复用）。修复前新用例
    全部可复现失败（模块/组件不存在），实现后全部通过。

### Added

- **仓库设置页新增「仓库用户」：读取 remote url 获取仓库用户，灵感组件「添加 Issue」时将其设为默认分配人（issue #153）**：
  仓库设置弹窗新增「仓库用户」展示与「重新读取 remote URL」按钮——仓库用户即该仓库
  remote URL userinfo 里的用户名（如 `https://user:token@host/...` 的 `user`，用户在
  git 配置里填写的账号）；添加/更新仓库时自动从原始 remote URL 捕获。灵感组件点击
  「添加 Issue」一键提交 issue 时，后端把仓库用户按项目成员解析为 GitLab 用户 id 设为
  issue 分配人（未配置 / 解析失败则保持原行为不指定分配人，不阻塞创建）。改动：
  - 后端 `git_remote.py`：新增 `read_remote_username()`（本地目录 `git remote -v` → URL
    userinfo 用户名，读取尽力而为失败返回 None）与 `read_repo_remote_username()`（仓库
    行读取三级回退：local_path 的 git remote → workspace 克隆目录 → 存储 url）；
  - 后端 `database.py`：repos 表新增 `remote_username` 列 + v10 迁移（旧库启动自动补列，
    与既有轻量迁移链一致）；`upsert_repo` / `update_repo` 支持该列；
  - 后端 `config.py` / `main.py`：`RepoConfig` 新增 `remote_username`，config.yaml ↔ db
    双向同步（config 仍是唯一事实来源）；
  - 后端 `api/repos.py`：`_repo_row_to_dict` 返回 `remote_username`；添加仓库时按原始
    remote URL（识别前，可能内嵌凭据）自动捕获用户名；更新 url 时重新推导；新增
    `POST /api/repos/{id}/remote-user`——重新读取并落库（含 None 清除旧值）；
  - 后端 `api/inspirations.py`：`add_issue_from_inspiration` 分配人 = 仓库用户——按
    项目成员（members/all）匹配用户名解析用户 id，成员项缺 user_id 时按用户名查 /users
    补齐（与添加 issue 弹窗同源），成员接口故障/查不到时降级不指定分配人并记日志；
  - 前端 `RepoEditModal.jsx`：设置弹窗新增「仓库用户」字段（只读展示 + 「重新读取 remote
    URL」按钮调新增接口，读取中禁用防重复，失败展示错误），`styles.css` 新增
    `remote-user-row` 样式；`Overview.jsx` 灵感「添加 Issue」按钮提示补充分配人说明；
  - **测试**：`test_api_repos.py` 新增 `TestRemoteUser` 8 用例（local_path / url 读取并
    落库、无凭据返回 null 并清除、local_path 失效回退 url、添加/更新自动捕获、列表返回、
    404）；`test_api_inspirations.py` 新增 `TestAddIssueFromInspirationAssignee` 7 用例
    （成员解析分配、缺 user_id 按用户名补齐、非成员兜底 /users、查不到降级、成员接口
    故障降级、未配置/空白不指定）；`test_database_migrate.py` /
    `test_database_legacy_cst.py` 更新迁移版本断言（v9 → v10）并新增旧库补列用例；
    `repo-edit-remote-user.test.mjs`（新）6 用例（源码断言 + 展示/占位/重新读取/失败
    提示）。修复前新用例全部可复现失败，实现后全部通过。

### Fixed

- **识图模型测试：列表行「测试」按钮缺失字段被 FormData 转成字符串 "undefined"，被当作真实
  Base URL 发起请求，报「Request URL is missing an 'http://' or 'https://' protocol.」（issue #154）**：
  用户配置好 baseurl / api_key / model 后点列表行「测试」按钮报网络请求失败——列表行测试只提交
  name + provider，前端把缺失字段 `undefined` 直接 `FormData.append()`，浏览器会转成字符串
  "undefined"；后端测试端点把非空字符串视为真实配置值、不再回退已保存配置，于是以
  base_url="undefined" 构造请求，httpx 立即报「Request URL is missing an 'http://' or
  'https://' protocol.」。改动：
  - 前端 `VisionModelsCard.jsx`：`onFileChange` 组装 multipart 时对缺失字段补空串
    （`testPayload.base_url || ''` 等），走后端「按 name 回退已保存配置」逻辑；
  - 后端 `api/settings.py`：新增 `_normalize_test_form_value()`——把 "undefined" / "null" /
    "None" 等占位文本归一为空值（兜底防护，前端回归或第三方客户端同样受益）；回退后若 Base
    URL 非空且不以 http(s):// 开头，返回明确中文提示而非晦涩的 httpx 协议错误；
  - **测试**：`test_api_settings.py` 新增 `test_test_row_button_undefined_placeholders_fallback`
    用例——提交 base_url / api_key / model 均为 "undefined" 时断言回退已保存配置（修复前用例
    复现失败：captured 值就是 "undefined"，与用户反馈错误一致），修复后全部通过；随后补充
    `test_test_custom_row_button_undefined_falls_back_to_saved`（自定义识图模型 qwen3-vl-flash +
    阿里云 compatible-mode 完整地址，与用户反馈场景一致）与
    `test_test_invalid_scheme_returns_clear_error`（Base URL 不带协议时返回明确中文提示，
    不再报晦涩的 httpx 协议错误），全部通过。

- **生图模型测试：OpenAI 接口返回 SSE 流（text/event-stream）时按事件解析并下载生成图片，不再报「不是有效 JSON」（issue #151 用户反馈）**：
  用户配置的生图接口（聚合网关类）真实返回为 SSE 流——多行 `data: {json}` 事件逐步上报
  进度（progress/status），最终事件 `status: "succeeded"` 且 `results[0].url` 为生成图片
  地址。此前 OpenAI provider 只按普通 JSON 解析，SSE 内容解析失败仅能展示原始流内容，
  用户拿不到图片。改动：
  - 后端 `plugins/models.py`：新增 `_parse_sse_events()` / `_flush_sse_pending()`——解析
    SSE `data:` 事件（单行 JSON 逐行解析优先，多行 JSON 字段按规范以换行拼接；跳过
    `data: [DONE]` 流结束标记）；OpenAI `images/generations` / `images/edits` 响应检测到
    `text/event-stream` Content-Type（或 body 以 `data:` 开头，兼容网关缺 Content-Type）
    时走 `_generate_from_sse()`——取最终 `succeeded` 事件的 `results[].url`，用同一 http
    客户端（继承 verify_ssl / 超时配置，显式跟随 CDN 302 跳转）下载图片返回；任务
    `failed` 时给出 `failure_reason` / `error` 原因；流未完成 / 无结果给出可诊断错误
    （含最终事件状态、事件数与请求地址）；另兼容网关把多个 `data: {json}` 事件挤在
    同一行、仅以空白分隔（无换行）的非标准形态——逐行解析无事件时按 `data:` 标记切分
    逐段解析（issue #151 用户实际粘贴返回内容形态），否则该形态仍只能报「不是有效
    JSON」倾倒整行原始内容、拿不到图片；
  - **测试**：`test_image_models.py` 新增 `TestOpenAISseResponse` 8 用例（成功事件下载
    图片、failed 报失败原因、仅 running 无结果报诊断错误、图片 URL 下载失败、多行 data
    事件、`[DONE]` 标记跳过、缺 Content-Type 时按 body 形态识别、单行多事件空格分隔
    形态）；`test_api_settings.py`
    新增 2 端到端用例（POST /api/settings/image-model-test 模拟 SSE 成功回传图片 base64 /
    失败展示原因）；修复前用例可稳定复现（SSE 被当普通 JSON 报「不是有效 JSON」并整体
    倾倒原始流内容），修复后全部通过。

- **生图模型测试：OpenAI 接口返回非 JSON 内容时直接展示接口原始返回内容（issue #151 后续反馈）**：

  首次修复后用户反馈「如果 OpenAI 接口返回内容不是有效 JSON，则直接将接口返回的内容
  显示出来」——旧实现统一走「带状态码 / 截断 200 字符片段 / 请求地址 + 冗长排查提示」
  的诊断文案，接口实际返回了什么（如网关错误页、纯文本提示）被截断隐藏，不利于直接
  判断。改动：
  - 后端 `plugins/models.py`：`_parse_json_response()` 新增 `show_raw` 参数，
    OpenAI `images/generations` / `images/edits` 调用传入 `show_raw=True`——JSON 解析
    失败时错误信息直接完整展示接口原始返回内容（保留换行，不再 200 字符截断、不再
    包裹冗长提示）；Gemini 保持原有带响应片段 / 请求地址的诊断文案不变；
  - 前端 `styles.css`：`.err-hint` 补 `white-space: pre-wrap` + `word-break: break-word`，
    多行 / 长文本的接口原始返回内容原样折行展示；
  - **测试**：`test_image_models.py` 更新 OpenAI 3 用例（空响应体、纯文本、超 200 字符
    长文本、HTML 错误页 → 原始返回内容完整出现在错误信息，不截断）；`test_api_settings.py`
    新增 1 端到端用例（POST /api/settings/image-model-test 模拟 OpenAI 200 + 超长纯文本，
    错误信息含完整原始内容）；`settings-image-models-card.test.mjs` 新增 1 用例（
    `.err-hint` 需 pre-wrap 保留换行）；改动前新用例可复现失败（内容被截断到 200 字符），
    改动后全部通过。

- **生图模型测试时接口返回空/非 JSON 内容报「Expecting value: line 1 column 1 (char 0)」无法定位（issue #151）**：
  配置生图模型后点击「测试」报
  `{"ok":false,"error":"生图测试失败: Expecting value: line 1 column 1 (char 0)"}`
  ——网关/代理或自定义 Base URL 指向错误端点时，生图接口常返回 HTTP 2xx 但 body 为空
  或为 HTML 错误页，供应商插件直接 `resp.json()` 抛 `json.JSONDecodeError`，被设置页
  兜底 `except` 捕获后只显示原始解析异常，用户无法判断是网关拦截还是 Base URL 配错
  端点。改动：
  - 后端 `plugins/models.py`：新增 `_parse_json_response()`，Gemini `generateContent` 与
    OpenAI `images/generations` / `images/edits` 响应解析统一经它处理——JSON 解析失败时
    转为带状态码 / Content-Type / 响应内容片段 / 请求地址的 `ImageModelError`（并提示
    检查自定义 Base URL 端点或网关返回内容），不再裸抛 `JSONDecodeError`；
  - **测试**：`test_image_models.py` 新增 4 用例（Gemini / OpenAI × 空响应体 / HTML 或
    纯文本非 JSON 响应 → 可诊断错误信息）；`test_api_settings.py` 新增 1 端到端用例
    （POST /api/settings/image-model-test 模拟 200 + 空 body，返回错误信息含
    「不是有效 JSON」与「空响应体」）；修复前用例可稳定复现（裸抛
    `Expecting value: line 1 column 1 (char 0)`），修复后全部通过。

- **生图模型调用时自定义 Base URL 被再次拼接接口路径，应直接使用配置的完整地址（issue #150）**：
  用户配置完整调用地址（如代理网关 `https://grsai.dakka.com.cn/v1/draw/completions`）
  后，后端生图调用仍在其后拼接 `/images/generations`、`/images/edits`、
  `/models/{model}:generateContent`，导致请求打到错误的
  `.../v1/draw/completions/images/generations` 地址而失败。改动：
  - 后端 `plugins/base.py`：`ImageProviderPlugin` 新增 `resolve_request_url(base_url, api_path)`
    ——用户自定义 base_url（非空且不等于预设默认）视为完整请求地址直接原样使用，不再拼接
    操作路径；未配置 / 等于预设默认（含尾斜杠归一）保持官方接口拼接行为；
  - 后端 `plugins/models.py`：Gemini `generateContent` 与 OpenAI `images/generations` /
    `images/edits` 请求地址统一改用 `resolve_request_url` 解析；
  - 前端 `ImageModelsCard.jsx`：配置说明补充「自定义 Base URL 直接作为完整请求地址使用」
    语义提示（含完整地址示例）；
  - 文档：`README.md` 配置表、`config.example.yaml` 注释、`image_models.py` 类注释同步说明；
  - **测试**：`test_image_models.py` 新增 4 用例（OpenAI generations / edits 自定义完整 URL
    原样直用、Gemini 自定义完整 URL 原样直用、等于默认 base_url 含尾斜杠仍按官方接口
    拼接）；修复前用例可稳定复现（URL 被拼接 `/images/generations`、`/images/edits`、
    重复 `:generateContent`），修复后全部通过；`settings-image-models-card.test.mjs` 新增
    自定义 Base URL 直用提示断言。

- **生图模型 404 错误提示误导网关式自定义 Base URL（issue #150 后续反馈）**：
  用户反馈按 issue #150 配置 grsai 的 GPT Image 2 后测试仍报
  `OpenAI 请求失败: HTTP 404 404 page not found（请求地址:
  https://grsai.dakka.com.cn）`——经本地实测确认：裸域名
  `https://grsai.dakka.com.cn` 返回 404 page not found，而完整地址
  `https://grsai.dakka.com.cn/v1/draw/completions` 返回 HTTP 200
  （`apikey is empty`），即保存的 Base URL 只填了域名，按「自定义
  Base URL 原样直用」语义请求裸域名必然 404，修复代码本身行为正确。
  改动：
  - 后端 `plugins/models.py`：OpenAI 404 提示由「官方地址通常以 /v1 结尾」
    改为明确「自定义 Base URL 将作为完整请求地址直接使用（不再拼接
    /images/generations 等接口路径），请填写含完整路径的地址（如
    …/v1/draw/completions），不能只填域名」；Gemini 404 同步补充 Base URL
    检查提示（原仅展示请求地址）；
  - **测试**：`test_image_models.py` 新增 OpenAI 自定义完整 URL 404 提示
    断言与 Gemini 404 提示用例，改动前可复现失败（旧提示无「完整请求地址」
    语义、Gemini 无提示），改动后全部通过。

- **生图模型「测试」无提示、OpenAI 404 无法定位且成功不返回图片（issue
  #149）**：用户配置生图模型后点「测试」没有任何提示，接口返回
  `{"ok":false,"error":"OpenAI 请求失败: HTTP 404 404 page not found"}`。
  根因与改动：
  - 前端 `ImageModelsCard.jsx`：列表行「测试」按钮的结果此前只在编辑表单内
    渲染，列表模式下 `testResult` 无展示位，点击后无任何提示——新增共用
    `TestResult` 组件，列表模式（非编辑）与编辑表单内均展示成功/失败结果；
  - 后端 `plugins/models.py`：OpenAI `images/generations` / `images/edits`
    请求未携带 `response_format=b64_json`，OpenAI 默认只返回 url 字段，
    即使接口成功也解析不到图片数据（会报「响应未包含图像数据」）——请求体
    与 multipart 表单显式补上 `b64_json`；
  - 后端 `plugins/models.py`：Gemini / OpenAI 非 2xx 错误信息补充实际请求
    地址（`请求地址: …`），OpenAI 404 时附加 Base URL 检查提示（官方地址
    通常以 `/v1` 结尾）——帮助定位「404 page not found」根因（Base URL 缺
    `/v1` 路径段或域名不对）；
  - 后端 `api/settings.py`：`POST /api/settings/image-model-test` 成功时
    回传首张图片 `image_base64` + `mime_type`，前端拼 data URL 用 `<img>`
    展示生成的图片；
  - **测试**：`test_image_models.py` 新增 4 用例（OpenAI generations /
    edits 请求含 `response_format=b64_json`、Gemini / OpenAI 错误信息含
    请求地址、OpenAI 404 含 Base URL 提示），`test_api_settings.py` 新增
    成功返回 `image_base64` 用例，`settings-image-models-card.test.mjs` 新增
    列表模式展示结果、成功展示生成图片断言；修复前用例可稳定复现（列表无
    提示、payload 无 response_format、错误信息无请求地址、响应无
    image_base64），修复后全部通过。

- **远端默认主分支跟踪引用缺失时工作区准备失败（issue #148）**：任务 #249
  （graph2plan，125#38）执行报 `[executor] git checkout 失败 (exit 128):
  fatal: 'origin/main' is not a commit and a branch 'main' cannot be created
  from it`。根因：工作区仓库当前分支 master ≠ 远端默认主分支 main，且本地
  缺失 `refs/remotes/origin/main` 跟踪引用——仓库为单分支克隆
  （`--single-branch`）或手工配置了受限 fetch refspec 时，`git fetch --prune`
  只拉取配置的分支，远端默认分支（经 `ls-remote --symref` 权威解析）从未在
  本地落盘；`_checkout_default_branch` 却假设 `git checkout -B main --track
  origin/main` 必然可用。改动：
  - 后端 `executor.py`：`_checkout_default_branch` 执行前先校验本地跟踪引用
    `refs/remotes/<remote>/<branch>` 是否存在（新增 `_remote_tracking_ref_exists`），
    缺失时用命令行显式 refspec `git fetch <remote> <branch>:refs/remotes/<remote>/<branch>`
    拉取补齐（命令行 refspec 不受受限配置影响，也保证后续 `reset --hard
    <remote>/<branch>` 有目标可依）；切回分支不再用 `--track`——受限 refspec
    下 git 无法把跟踪引用映射回远端分支名，`--track` 即使引用已补齐仍报
    "cannot set up tracking information"，改为 checkout 后直接写
    `branch.<name>.remote` / `branch.<name>.merge` 建立上游，标准仓库结果与
    `--track` 等价；
  - **测试**：`test_executor_local_path.py` 新增 2 用例（单分支克隆切回默认
    主分支不失败、本地已在默认分支名但跟踪引用缺失时 `reset --hard` 不失败）；
    后端全量 1308 用例通过（含既有 default-branch/pull 回归）。

- **默认主分支解析兜底硬编码 main，master 默认分支仓库工作区准备失败
  （issue #148 复测）**：用户复测任务 #249 仍失败，怀疑目标仓库（graph2plan）
  默认主分支是 master 而非 main。排查确认 `_resolve_default_branch` 在
  `git ls-remote --symref` 探测失败（网络/认证抖动）且本地缺
  `refs/remotes/origin/HEAD`（手工加 remote 的仓库常见）时，兜底链一路
  回退到**硬编码 "main"**——远端只有 master 时后续 `fetch/checkout main`
  必然失败。改动：
  - 后端 `executor.py`：`_resolve_default_branch` 重构为逐级降级探测、全程
    不硬编码 main：① `git ls-remote --symref <remote>`（服务端权威 HEAD
    符号引用，校验分支真实存在）；② `git remote show <remote>` 解析
    "HEAD branch:" 行（ls-remote 不可用时的二次服务端探测，新增
    `_remote_default_branch_via_show`）；③ 本地跟踪引用兜底（优先
    `refs/remotes/<remote>/HEAD` 符号引用，再按 main → master → 字典序
    扫描已拉取的跟踪分支，新增 `_local_default_branch`）；全链路失败才
    最终回退 "main"；
  - 后端 `config.py` `DEFAULT_TEMPLATE`：第 3 点「直接推送到 main 分支 /
    git push origin main」改为「推送到当前分支（平台已自动切到仓库默认
    主分支，可能是 main / master 等，不要假设分支名）/ git push origin
    HEAD」——agent 会话内不再硬编码 main；`config.example.yaml` 同步；
  - 文档：`docs/设计方案.md` §5.5 工作区准备命令补充 `git remote show`
    二级探测与本地跟踪引用兜底说明、§5.6 示例模版同步 push HEAD 写法；
  - **测试**：`test_executor_local_path.py` 新增
    `TestPrepareWorkspaceDefaultBranchResolution` 2 用例（ls-remote 探测
    失败 + 本地缺 origin/HEAD 时 master 默认分支仓库 prepare 不失败并停在
    master；ls-remote 与 git remote show 都失败时经本地跟踪引用兜底解析
    master）；修复前两用例稳定复现 `git fetch 失败 (exit 128): couldn't
    find remote ref main`，修复后通过；`test_config_template.py` 断言同步
    push HEAD。

### Changed

- **任务开始前自动切回默认主分支并 git pull 同步最新代码（issue #147）**：
  需求「全局模版库中前两点（任务开始先校验当前分支、非主分支切回默认主分支；
  每次开发前 git pull 同步远端最新代码）通过代码实现，完成后再让 agent 操作，
  节省 token」。此前这两点写在全局模版里由 agent（LLM）自行执行，每次任务都
  重复消耗 token；且执行器工作区准备硬编码 `main → master` 回退，未按仓库真实
  默认主分支处理。改动：
  - 后端 `executor.py`：`prepare_workspace`（claude / hermes / dsh 三引擎共用）
    新增 `_resolve_default_branch`（优先 `git ls-remote --symref` 解析服务端
    权威默认主分支并校验分支真实存在，兜底本地 `refs/remotes/<remote>/HEAD`
    符号引用 → `main` → `master`，兼容 `git init --bare` 默认 HEAD 指向不存在
    分支的裸仓库）与 `_checkout_default_branch`（校验当前分支，非默认主分支或
    detached HEAD 时 `git checkout -B <默认主分支> --track <remote>/<默认主分支>`
    切回）；非恢复路径在 `fetch` / `reset --hard` / `clean -fd` 之后显式执行
    `git pull --rebase <remote> <默认主分支>` 兜底同步 fetch 之后的新推送；
    `resume=True`（断点续跑）保持只 fetch 不动工作区，不强制切分支/拉取；
  - 后端 `config.py`：`DEFAULT_TEMPLATE` 工作要求第 1 点改为「本地工作区已由
    平台自动切到默认主分支并 git pull 拉取最新代码……（无需自行切换分支或
    git pull）」；部署侧全局模版库中的前两点指令可随之移除；
  - 文档：`docs/设计方案.md` §5.5 工作区准备命令更新（默认主分支解析 + 显式
    git pull）、§5.6 示例模版同步为当前 `DEFAULT_TEMPLATE`（含 no-close 政策与
    全角括号 issue 引用规范）；`README.md` 执行器职责描述补充自动切回默认主分支
    与 git pull；
  - **测试**：`test_executor_local_path.py` 新增 `TestPrepareWorkspaceDefaultBranchAndPull`
    6 用例（非默认分支自动切回默认主分支、默认主分支非 main（trunk）场景、
    detached HEAD 重新检出默认主分支、远端新提交经 git pull 同步、prepare 显式
    执行 `git pull --rebase`、bare HEAD 指向不存在分支时回退已有 main）；
    既有 11 用例与凭据注入回归（test_executor_credentials.py）全部通过。

### Added

- **git pull 拉取冲突时保留现场交由 agent 手工合并（issue #147 补充）**：
  需求「如果拉取代码的时候出现了冲突，让 agent 来进行合并」。此前
  `prepare_workspace` 的 `git pull --rebase` 遇到合并冲突（本地提交与远端
  分叉、untracked 残留被远端新提交占用等）会直接抛错导致任务在准备阶段失败，
  agent 没有机会介入合并。改动：
  - 后端 `executor.py`：`prepare_workspace` 的 `git pull --rebase` 包上
    冲突判定 `_is_pull_conflict`——工作区实际处于冲突状态（`.git/rebase-merge`
    / `.git/rebase-apply` / `.git/MERGE_HEAD` 存在或 `git ls-files -u` 有未合并
    路径）或 git 输出含明确冲突标志（CONFLICT / could not apply / untracked
    文件将被远端新提交覆盖等）时，不再抛错：保留冲突现场并登记工作区到
    `_pull_conflict_workdirs`，由 agent 手工合并；凭据/网络等非冲突失败照常
    报错。`_build_prompt` / `_resume_prompt`（claude / hermes / dsh 三引擎
    共用）检测到冲突工作区时在提示词末尾追加「先手工解决冲突」指引
    （git status 查看冲突 → 手工合并 → git add + git rebase --continue /
    git commit，严禁 force push）；下一次干净 prepare 自动清除冲突登记；
  - 文档：`docs/设计方案.md` §5.5 补充「拉取冲突处理」说明；`README.md`
    执行器职责描述同步；
  - **测试**：`test_executor_local_path.py` 新增
    `TestPrepareWorkspacePullConflict` 7 用例（pull --rebase 真实冲突不抛错、
    冲突现场保留、提示词追加解决指引、非冲突失败照常抛错、冲突登记在干净
    prepare 后清除、未合并路径/untracked 覆盖错误文本判定、无冲突提示词不
    追加指引）；既有 17 用例全部通过。

- **插件管理页面（issue #145）**：
  需求「增加一个插件页面可以插件进行管理，所有插件的安装、卸载和插件的设置
  都在这个界面」。基于插件体系（issue #140）落地设计文档规划的「前端插件
  管理页」演进项，新增独立「插件」页面（顶部导航入口，路由 `/plugins`）：
  - 后端 `api/plugins.py`（新）：`GET /api/plugins` 按分类（executor /
    model_provider / notifier）分组返回全部已注册插件，含内置/外部来源
    （外部插件附来源路径）与模型供应商默认预设（display_name /
    default_base_url / default_model，与设置页「生图模型」预设同源）；
    `POST /api/plugins/install` 安装外部插件模块——隔离注册表试加载校验
    （文件存在 / 模块可导入且至少注册一个插件 / 与已安装插件无同名同分类
    冲突），通过后写入 `worker.plugin_paths` 并热加载到全局注册表（无需
    重启），任何校验失败 400 拒绝且不落盘，热加载防御性失败回滚配置；
    `POST /api/plugins/uninstall` 卸载外部插件（配置与注册表同时移除，
    内置插件命中「未安装」校验不可卸载）；`POST /api/plugins/reload` 按
    当前 `plugin_paths` 清空并重载外部插件；`PUT /api/plugins/settings`
    设置默认执行引擎（executor 插件设置，复用 `worker.engine`，白名单由
    插件注册表派生，外部引擎插件自动纳入）；
  - 后端 `plugins/base.py`：`PluginRegistry` 增加外部插件来源跟踪
    （`_external_by_path` / `_external_path`）与 `registered_external` /
    `path_of` / `remove_external` / `clear_external`，`load_external`
    支持 `errors` 收集器（安装校验需要精确失败原因）；
  - 前端 `pages/Plugins.jsx`（新）：按分类分组展示全部插件卡片（名称/描述/
    版本/内置徽章/外部来源路径/供应商默认预设）；「安装外部插件」表单
    （多行路径输入，逐项提交）；外部插件「卸载」按钮（confirmDialog
    确认）；「默认执行引擎」radio 设置（executor 插件）；「重新加载外部
    插件」按钮；操作结果 alert 反馈；`App.jsx` 新增「插件」导航与 `/plugins`
    路由；`styles.css` 新增 `.plugin-list` / `.plugin-card` /
    `.badge-external` / `.plugin-install-input` / `.plugin-engine-options`
    等样式；
  - 文档：`README.md` 插件体系章节补充插件管理页说明、API 一览新增
    `/api/plugins` 五个端点；
  - 测试：后端 `test_api_plugins.py`（新）17 用例（列表分组与内置完整性 /
    供应商预设 / 外部来源标记 / 安装成功落盘+热加载 / 空白 strip+重复安装
    拒绝 / 空路径与文件不存在拒绝 / 模块未注册插件拒绝 / 模块加载失败拒绝 /
    与内置插件冲突拒绝 / 卸载移除配置+注册表 / 未安装与空路径拒绝 / 重载
    幂等 / 引擎设置合法持久化与非法拒绝）；前端 `plugins-page.test.mjs`
    （新）12 用例（导航与路由 / 页面结构与 API 端点源码断言 / 分组渲染
    内置与外部插件 / 安装提交 POST / 引擎切换保存 PUT）。



- **灵感一键提交为 GitLab issue（issue #143）**：
  需求「灵感组件，在编辑按钮左边添加一个，添加issue的按钮，点击之后将灵感的
  文本，作为issue的标题和描述，通过gitlab api添加issue，默认添加标记feature
  以及ui」。
  实现：
  - 后端 `api/inspirations.py`：新增 `POST /api/inspirations/{id}/add-issue`——
    灵感内容同时作为 issue 标题与描述，默认标签 `feature` + `ui`（GitLab 会
    自动创建不存在的标签），不指定分配人；写操作走 owner token（复用
    `issues._issue_edit_call`，未配置 owner token 返回 400 引导设置、绝不回退
    bot token）；灵感不存在 404 / 仓库不存在或已软删除 400 / 仓库未启用 400 /
    GitLab 故障 502；创建成功后清空概览缓存，返回精简 issue 对象
    （含 iid/web_url 供前端提示与跳转）；
  - 前端 `Overview.jsx`：灵感条目操作区在「编辑」按钮左侧新增
    「📌 添加 Issue」按钮——请求中禁用防重复提交，成功展示新 issue 链接并
    立即刷新开放 issue 列表，失败展示错误；板块说明文案同步更新；
  - 样式 `styles.css`：新增 `.inspiration-add-issue-btn`（主色描边区分动作、
    禁用态）（创建成功提示沿用既有 `.alert-ok` 样式）；
  - 文档：`README.md` 后端 API 表新增 `POST /api/inspirations/{id}/add-issue`
    一行；
  - 测试：后端 `test_api_inspirations.py` 新增 `TestAddIssueFromInspiration`
    8 用例（正常路径 / 多行内容保留换行 / 灵感不存在 404 / 仓库未启用 400 /
    仓库软删除 400 / GitLab 故障 502 / 未配置 owner token 400 / 成功后清空
    概览缓存）；前端 `overview-inspirations.test.mjs` 新增 6 用例（按钮位于
    编辑左侧源码断言 / 提交调用路径 / 渲染按钮顺序 / 点击提交并刷新列表 /
    提交中禁用防重复 / 失败显示错误）。


- **灵感 / CI/CD 页面可配置是否显示未启用项目（issue #142）**：
  需求「灵感页面和 ci\cd 页面显示是否显示未启用项目，增加通过设置去配置」。
  实现：
  - 设置项 `ui.show_disabled_repos`（默认 `true` = 显示，保持现状）：灵感板块
    与 CI/CD 流水线板块是否展示 Botler 中未启用的仓库；`false` 时两个板块只
    展示已启用仓库，未启用仓库不再发起 GitLab 流水线查询（省流量）；
  - 后端 `config.py`：`Settings` 新增 `ui_show_disabled_repos` 字段（布尔，
    缺省/非布尔回退默认），`KNOWN_FIELDS["ui"]` 加入 `show_disabled_repos`；
    `api/settings.py` 读写（GET 返回 / PUT 校验布尔，非法值 400）与保存后
    清空流水线概览缓存（开关立即生效，不等 10 秒 TTL）；
  - 后端 `api/pipelines.py` / `api/inspirations.py`：`_collect` 与
    `inspiration_overview` 在设置关闭时过滤 `enabled=false` 的仓库；
  - 前端 `Settings.jsx`「界面显示」卡片新增「显示未启用项目」复选框
    （`ui.show_disabled_repos`），随全局「保存」提交；
  - 文档：`README.md` 关键配置表、`backend/config.example.yaml` 新增
    `ui.show_disabled_repos` 说明；
  - 测试：`test_api_settings.py` 新增 `TestShowDisabledReposSettings` 5 用例
    （默认 true / false 持久化 / 重新开启 / 非布尔拒绝 / 部分更新不影响
    timezone）；`test_api_pipelines.py` 新增设置关闭时过滤未启用仓库用例
    （不发起 GitLab 查询）；`test_api_inspirations.py` 新增默认包含未启用
    仓库 / 关闭时过滤 / 已启用仓库灵感不受影响 3 用例；前端
    `settings-show-disabled-repos.test.mjs`（新）3 用例（开关存在 / 说明覆盖
    灵感与 CI/CD / 保存提交）。


- **插件体系：执行引擎 / 大模型 API 供应商 / webhook 发送任务消息插件化（issue #140）**：
  需求「平台想要实现一个插件体系，把执行引擎、大模型 api 供应商、webhook 发送
  任务消息都做成插件的形式，设计一个实现方案」。
  实现：
  - 设计文档 `docs/插件体系设计方案.md`（新）：插件分类 / 插件基类与注册表 /
    三类插件接口定义 / 内置插件迁移清单 / 调用方改造点 / 配置设计 / 测试计划 /
    演进方向；
  - 后端 `botler/plugins/base.py`（新）：插件体系核心——`PluginKind` 枚举
    （executor / model_provider / notifier 三分类）、`Plugin` 基类（分类 + 标识 +
    描述 + 版本）、`ExecutorPlugin` / `ImageProviderPlugin` / `NotifierPlugin`
    三类插件接口、`PluginRegistry` 注册表（注册 / 查询 / 列表 / 外部加载，
    重复注册抛冲突异常，未注册抛缺失异常）、全局注册表单例与便捷函数
    （`get_plugin` / `list_plugins` / `has_plugin` 等）；
  - 后端 `botler/plugins/executors.py`（新）：执行引擎插件——`claude`
    （Claude Code CLI，默认）/ `hermes`（hermes runner，issue #47）/ `dsh`
    （deepseek-harness SDK，issue #84）三个内置引擎插件（适配器委托现有实现，
    断点续跑语义不变）；`executor._engine` 校验改为注册表驱动（未注册引擎名
    回退 claude），`_run_once` 按引擎名查插件委托执行（原 claude 分支抽为
    `_run_claude_once`）；
  - 后端 `botler/plugins/models.py`（新）：大模型（生图）供应商插件——
    `ImageResult` / `ImageModelError` / 默认超时与尺寸常量迁移至此，
    `gemini_nano_banana`（Gemini generateContent）与 `openai_gpt_image`
    （OpenAI images API）两个内置供应商插件（实现自 `image_models._generate_*`
    迁移）；`image_models.ImageModelClient` 改为按 provider 查插件注册表委托
    调用，`IMAGE_MODEL_PRESETS` 由注册表派生，对外导出与行为完全兼容；
  - 后端 `botler/plugins/notifiers.py`（新）：任务消息发送通道插件——
    `webhook`（外部 Webhook HTTP 推送，issue #136）与 `in_app`（网页通知事件，
    issue #21）两个内置通道插件；executor 收尾统一向全部 notifier 插件分发
    （成功 / 失败事件，webhook 推送前拉取 issue 完整信息，任一通道失败仅记
    日志不阻塞任务收尾）；
  - 后端 `config.py` / `config.example.yaml`：新增可选 `worker.plugin_paths`
    （外部插件 Python 模块路径列表，启动时 `importlib` 加载注册，失败仅记
    日志不阻塞启动）；`api/settings.py` 引擎白名单改为插件注册表派生（新增
    引擎自动可配置）、`plugin_paths` 读写与校验（字符串数组、空白项剔除）；
    `main.py` 应用启动时加载外部插件；
  - 文档：`README.md` 新增「插件体系」章节与配置说明（`worker.plugin_paths`）、
    目录结构更新；
  - 测试：`test_plugins.py`（新）22 用例（注册表注册/查询/重复冲突/缺失/列表
    顺序/独立注册表隔离/外部加载·缺失文件·异常容错/插件能力方法默认行为/
    全局注册表内置插件完整性）；`test_executor_plugins.py`（新）19 用例
    （引擎名校验走注册表·未注册回退 / _run_once 三引擎委托路径·断点续跑参数
    透传 / 引擎插件元信息 / notifier 插件分发·单通道失败不阻塞其他通道）；
    `test_api_settings.py` 追加 `TestPluginPathsSettings` 5 用例（plugin_paths
    读写 / 空白剔除 / 非法值拒绝 / 部分更新）+ 全量回归（引擎白名单行为不变）。


- **设置页整理分组并新增左侧导航栏：可搜索设置项、折叠/展开分组子项（issue #139）**：
  需求「设置页设置项太多，整理一下设置页面的分支，并在设置左侧添加一个导航栏，
  导航栏可以搜索设置项，同时也可以折叠和展开分组中的子项」。
  实现：
  - 前端 `components/SettingsNav.jsx`（新）：设置页左侧导航栏组件，导出分组配置
    `SETTINGS_GROUPS`（6 组 14 项，子项带锚点 id / 显示名 / 搜索关键字）——
    顶部搜索框按名称与关键字过滤设置项（中英文别名、配置键均可命中），搜索时自动
    展开命中分组并显示命中数，无结果给出空状态提示；每个分组可折叠/展开其子项
    （`aria-expanded` 无障碍状态），导航头部提供「全部收起 / 全部展开」；
    点击子项平滑滚动（`scrollIntoView` smooth）到页面相应设置区块并高亮当前项；
  - 前端 `pages/Settings.jsx`：设置页改为两栏布局（左侧导航 + 右侧内容区），
    按功能整理为 6 个分组（外部服务接入 / 系统设置 / 执行引擎 / 运维与数据 /
    账号与安全 / 关于），每个设置区块包裹 `<section id=...>` 锚点（与导航子项
    一一对应），分组间插入分组标题；既有卡片顺序与功能全部保持不变；
  - 前端 `styles.css`：新增 `settings-layout` / `settings-sidebar` / `settings-nav*`
    等样式（240px 吸顶导航 + 内容区，Geist 设计令牌），`scroll-margin-top` 保证
    滚动定位不被 sticky 顶导航遮挡，窄视口（≤860px）回落单栏导航置顶；
  - 文档：`README.md` 配置说明补充设置页导航分组与搜索/折叠交互说明；
  - 测试：`frontend/tests/settings-nav.test.mjs`（新）15 用例（导航挂载与两栏布局 /
    分组标题与锚点归属一一对应 / SETTINGS_GROUPS 配置完整性 / 渲染分组与子项 /
    搜索过滤·关键字命中·自动展开·无结果空状态·清空恢复 / 分组折叠展开·全部收起
    展开 / 点击子项 scrollIntoView 滚动与高亮 / 样式规则）+ 设置页既有用例全量回归。

- **设置里配置了 deepseek api 时，概览页展示 DeepSeek 账户余额（issue #138）**：
  需求「如果设置里配置了 deepseek api，则在概览页面显示 deepseek 账户里的余额」，
  请求接口为 `GET https://api.deepseek.com/user/balance`（`Authorization: Bearer <TOKEN>`）。
  实现：
  - 后端 `deepseek_balance.py`（新模块）：`resolve_deepseek_credentials` 凭据解析链
    （与 executor._dsh_credentials 一致：dsh 段 api_key > 设置页「AI API 供应商」
    provider=deepseek 且启用的项 > 环境变量 `DEEPSEEK_API_KEY`）；
    `DeepSeekBalanceClient`——httpx 代调 user/balance（10s 超时，verify_ssl 跟随全局，
    `Authorization: Bearer` 认证，预设 base_url 尾部 `/v1` 归一化后拼 `/user/balance`，
    与需求示例地址一致）；API Key 只存在于服务端，明文不流转到前端；
  - 后端 `api/settings.py`：新增 `GET /api/settings/deepseek-balance`——未配置 Key 返回
    configured=false（前端不渲染卡片）；已配置则代调查询返回
    `{configured, balance:{is_available, balance_infos, fetched_at}, error}`；
    查询失败返回 error 字段（不抛 500，与 webhook-test 同容错策略）；
  - 前端 `pages/Overview.jsx`：新增「DeepSeek 账户余额」卡片——配置了 deepseek api 时
    展示（未配置整卡不渲染），显示账户可用状态 / 币种 / 总余额 / 赠送 / 充值余额 /
    更新时间，60 秒低频轮询 + 「↻ 刷新」按钮；接口失败保留上次数据不打扰页面；
  - 前端 `styles.css`：新增 deepseek-balance-* 余额卡片样式（与其他概览板块视觉一致）；
  - 文档：`README.md` API 一览新增余额端点、配置说明补充 `ai_providers[]` 行并说明
    余额 Key 解析链；
  - 测试：后端 `test_deepseek_balance.py`（新）18 用例（凭据解析链优先级 / 停用与空
    key 跳过 / 环境变量兜底 / base_url 归一化 / 客户端请求构造与响应解析 / 非 2xx、
    超时、网络异常 / 端点未配置、成功、接口报错、环境变量配置且无 Key 泄露）+
    `test_api_settings.py` 全量回归；前端 `overview-deepseek-balance.test.mjs`（新）
    9 用例（源码断言 / 渲染卡片各字段 / 未配置不渲染 / 接口报错与网络失败容错 /
    刷新按钮重新请求 / 空余额空状态）+ 概览页既有 240 用例全量回归。

- **设置页「识图模型」改为「生图模型」，并新增测试按钮验证配置可用（issue #137）**：
  需求「把设置里的识图模型改成生图模型，同时增加一个测试按钮，在用户配置好url、
  apikey以及生图模式后，可以调用测试按钮，测试生图模型是否能用」。上一轮（issue
  #135）设置的图像模型本身即为图像生成接口（Gemini generateContent 图像输出 /
  OpenAI images 接口），本期将设置页命名纠正为「生图模型」，并补上配置可用性验证：
  - 后端 `api/settings.py`：新增 `POST /api/settings/image-model-test` 测试端点——
    接收表单提交的 provider（生图模式）/ base_url / api_key / model，用
    `ImageModelClient.generate` 真实调用一次生图接口（轻量测试 prompt，60s
    超时，verify_ssl 跟随全局设置）；api_key 掩码/留空、url/model 留空按 name
    回退已保存配置（与 image_models 保存同模式）；生图成功返回 ok=true + 生成
    张数/mime，缺配置/接口报错/网络异常均返回 ok=false + 原因，不抛 500（与
    webhook-test 同容错策略）；
  - 后端 `image_models.py` / `config.py` / `config.example.yaml`：文案统一
    「识图模型」→「生图模型」（错误提示、注释、示例配置）；
  - 前端 `components/ImageModelsCard.jsx`：卡片标题与全部文案改为「生图模型」，
    「模型类型」字段改名「生图模式」；新增测试按钮——列表行「测试」（只提交
    name + provider，按已保存配置测试）与编辑表单内「测试配置」（用当前表单
    值，未保存也可测），测试中/成功/失败均有状态提示；
  - 前端 `providers.jsx` / `pages/Settings.jsx`：注释文案同步改为「生图模型」；
  - 测试：`backend/tests/test_api_settings.py` 新增 `TestImageModelTestEndpoint`
    （成功/缺生图模式/掩码回退已保存配置/接口报错/未知 provider/verify_ssl 跟随
    全局），`backend/tests/test_image_models.py` 文案同步；
    `frontend/tests/settings-image-models-card.test.mjs` 新增测试按钮与结果提示
    断言、文案改为「生图模型」。
- **任务完成时调用 webhook 进行消息推送，可在设置页配置（issue #136）**：
  需求「任务完成时，添加调用webhook来进行消息推送，可以在设置页面配置webhook地址」，
  配置选项：webhook 地址、Content-Type、Authorization、POST 结构体（可使用全局
  模板中的占位符，请求的时候自动填充）。任务成功收尾（打 bot-done 标签）后按配置
  POST 推送，推送为尽力而为——失败仅记日志，不阻塞任务收尾（与网页通知同容错策略）。
  实现：
  - 后端 `config.py`：`Settings` 新增 `webhook_enabled / webhook_url /
    webhook_content_type（默认 application/json）/ webhook_authorization /
    webhook_body_template` 五个字段 + `update_webhook` 落盘（authorization
    掩码值/空串 = 保持现有凭据，与 sso.client_secret 同模式）；新增内置默认
    POST 结构体模板 `DEFAULT_WEBHOOK_TEMPLATE`（body_template 留空时归一使用）；
    `KNOWN_FIELDS` 增加 `webhook` 段；
  - 后端 `webhook_push.py`（新模块）：`WebhookPusher`——复用提示词模版占位符
    机制（`templates.PLACEHOLDERS`：repo_name / issue_title / issue_body /
    issue_url / gitlab_url / project_id / issue_iid / project_path /
    project_path_encoded / gitlab_host）渲染 POST 结构体；`send_task_succeeded`
    任务成功推送（未启用/未配置地址不发送）、`send_test` 测试推送；请求带
    Content-Type / Authorization 头，verify 跟随全局 verify_ssl，15s 超时，
    非 2xx 与网络异常抛 `WebhookPushError`；
  - 后端 `executor.py`：`ClaudeExecutor` 注入 `WebhookPusher`，`_finish_succeeded`
    成功收尾时调用 `_push_webhook_succeeded`——推送前拉取 issue 完整信息
    （正文/链接供占位符渲染，失败降级用任务记录数据 + 链接拼接兜底），
    推送成功/失败均记任务日志，异常不阻塞收尾；
  - 后端 `api/settings.py`：`GET /api/settings` 返回 `webhook` 段
    （authorization 只返回掩码）；`PUT` 支持 webhook 段更新（`_validate_webhook`：
    enabled 布尔 / url 须 http(s) 开头 / content_type 空白归一）；新增
    `POST /api/settings/webhook-test` 测试推送端点（未配置地址 / 非 2xx /
    网络异常均返回 ok=false + 原因，不抛 500）；
  - 前端 `pages/Settings.jsx`：新增「消息推送 Webhook」卡片（网页通知卡片之后）——
    启用开关、地址、Content-Type、Authorization（密码输入框，留空 = 保持现有
    凭据）、POST 结构体（textarea + 全局模板占位符说明，从
    templates.placeholders 动态展示）、「发送测试推送」按钮（调用
    webhook-test 端点展示结果）；跟随全局「保存」提交 webhook 段；
  - 文档：`README.md` 关键配置表、`backend/config.example.yaml` 新增
    webhook 段注释示例；
  - **测试**：后端 `test_webhook_push.py`（新）19 用例（配置默认值 / 占位符
    变量构建与链接兜底 / 模板渲染 / httpx mock 发送链路：成功、非 2xx、网络
    异常、未配置地址 / 未启用与无地址不发送 / 测试推送）+ `test_api_settings.py`
    新增 `TestWebhookSettings` 10 用例（GET 默认值 / PUT 持久化与掩码保持 /
    URL 与类型校验 / content_type 归一 / webhook-test 未配置、成功、非 2xx、
    异常容错）+ 前端 `settings-webhook-card.test.mjs`（新）7 用例（卡片挂载与
    位置 / 字段齐全 / 保存走 webhook 段与 authorization 留空保持 / 占位符说明 /
    测试推送端点）；后端 1175 + 前端 600 全量测试通过。

- **设置页新增「识图模型」配置，内置 Gemini Nano Banana Pro 与 GPT Image 2 两个接口（issue #135）**：
  需求「设置页面，增加配置识图模型的设置，目前先实现 gemini 的 nano banana pro 以及
  gpt image 2 的接口」。参照 AI API 供应商配置（issue #46）模式实现——本期交付
  「配置存储 + 调用接口封装」，为后续 AI 功能消费做准备，不接入具体业务。
  实现：
  - 后端 `config.py`：`Settings` 新增 `image_models` 列表（每项
    `{name, provider, base_url, api_key, model, enabled}`，与 ai_providers
    同构，api_key 支持 `${ENV}` 引用落盘 config.yaml）+ `update_image_models`
    整体替换落盘；
  - 后端 `api/settings.py`：`GET /api/settings` 返回 `image_models`
    （api_key 只返回掩码）、`PUT` 校验（name 必填且不重复 / base_url 须
    http(s) 开头 / provider 缺省归一 custom / enabled 布尔 / 掩码或留空 =
    保持现有），新增 `_validate_image_models`；
  - 后端 `image_models.py`（新模块）：统一 `ImageModelClient.generate()`
    调用封装——`gemini_nano_banana`（默认模型 `gemini-3-pro-image`，走
    `generateContent`，支持文本 prompt + base64 inline_data 图片输入、
    输出 inlineData 解码）与 `openai_gpt_image`（默认模型 `gpt-image-2`，
    走 images/generations JSON 与 images/edits multipart）；含超时/网络/
    非 2xx/缺 key/缺图像结果等错误诊断；`find_enabled` / `client_from_config`
    工具函数；
  - 前端 `providers.jsx`：新增 `IMAGE_MODEL_PRESETS`（gemini_nano_banana /
    openai_gpt_image / custom，默认 base_url 与 model 自动填充，logo 复用
    gemini / openai 品牌图标）；
  - 前端 `components/ImageModelsCard.jsx`（新组件）：设置页「识图模型」
    卡片，增删改查 + 独立保存按钮（只提交 image_models 段）+ API Key
    掩码回填；`pages/Settings.jsx` 在 AI 供应商卡片之后挂载；
  - 文档：`README.md` 目录结构与关键配置表、`backend/config.example.yaml`
    新增 image_models 段注释示例；
  - **测试**：后端 `test_api_settings.py` 新增 `TestImageModelsSettings`
    13 用例（GET 空列表 / PUT 持久化与掩码 / 掩码与留空不覆盖 / 整体替换 /
    清空 / name 必填与唯一 / base_url 校验 / provider 归一 / enabled 布尔 /
    `${ENV}` 引用）+ `test_image_models.py` 16 用例（两 provider 请求构造
    URL/认证头/请求体、参考图片输入、非 2xx 与缺图像响应报错、缺 key/空
    prompt 拦截、预设与工具函数）；前端新增
    `settings-image-models-card.test.mjs` 7 用例（卡片挂载与位置、增删改
    表单、保存走 image_models 段、预设与 logo）；后端 1146 + 前端 594 全量
    测试通过。

- **概览页开放 Issue 板块：每个仓库卡片右上角新增「对账」按钮（issue #134）**：
  需求「概览页面中的开放issue组件，每个仓库的右上角添加一个对账按钮，点击后对账该仓库issue」。
  复用仓库页对账接口 `POST /api/repos/{repo_id}/reconcile`（issue #17）：点击后同步扫描该
  仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入任务队列，并以小字展示
  对账结果（入队 N 个 = 发现待处理；0 个 = 无需处理；仓库停用显示后端 note；失败显示错误）。
  实现：
  - 前端 `Overview.jsx`：仓库卡片头右上角新增「对账」按钮（`↻ 对账`，请求中 `↻ 对账中…`
    并禁用防重复点击，与仓库页对账按钮一致为低危操作无需确认），与「添加 Issue」按钮
    并排成组（新增 `.issue-repo-actions` 操作组容器整体推右）；新增 `reconcileRepo`
    回调与 `ReconcileResult` 结果展示组件（入队 >0 / 无需处理 / 停用 note / 失败错误
    四种形态）；
  - 前端 `styles.css`：新增 `.issue-repo-actions`（margin-left:auto 推右、inline-flex
    并排、gap 用间距 token）、`.reconcile-btn`（white-space:nowrap 防换行）、
    `.reconcile-result`（卡片头下方间距）；「添加 Issue」按钮推右职责移交操作组容器；
  - 后端：无改动（复用 issue #17 的 `POST /api/repos/{id}/reconcile` 接口）；
  - 文档：`README.md` API 一览补充 `POST /api/repos/{id}/reconcile` 行；
  - **测试**：前端新增 `overview-repo-reconcile.test.mjs` 10 用例（按钮渲染与操作组
    结构、点击调对账接口参数正确、请求中禁用防重复点击、入队 >0/=0 两种成功结果、
    停用 note、失败错误、低危无需确认、源码与样式断言）；前端 587 + 后端 1117 全量
    测试通过。

- **概览页新增「灵感」板块：开放 Issue 下方、CI/CD 流水线上方按仓库随手记录新功能灵感（issue #131）**：
  需求「概览页面，在开放issue下方，ci\cd流水线上方增加灵感页面，可以在这里按仓库随手记录下来
  关于对应仓库的一些新功能的灵感，这些灵感只保存在项目的数据库，不要提交到issue上」。灵感是
  用户对对应仓库新功能的随手笔记，与 GitLab issue 完全隔离——只保存在 Botler 本地 SQLite
  数据库，不调用任何 GitLab API、不创建/修改 issue。
  实现：
  - 后端 `database.py`：新增 `inspirations` 表（repo_id / content / created_at / updated_at），
    v8 迁移（`PRAGMA user_version` 7 → 8，旧库启动自动建表）；新增
    `list_inspirations` / `get_inspiration` / `create_inspiration` /
    `update_inspiration`（刷新 updated_at）/ `delete_inspiration` 方法；
  - 后端 `api/inspirations.py`（新模块，注册进 /api 路由）：
    `GET /api/inspirations/overview`（聚合所有未软删除仓库 + 各自灵感，仓库按优先级排序、
    灵感按 updated_at 降序，无灵感的仓库也返回供前端展示空状态与添加表单）、
    `POST /api/inspirations`（创建，repo_id 须指向存在且未软删除的仓库，内容去首尾空白
    后非空且 ≤ 5000 字）、`PUT /api/inspirations/{id}`（更新内容）、
    `DELETE /api/inspirations/{id}`（删除）；
  - 前端 `Overview.jsx`：新增 `inspirations-section`（源码位置在 issues-section 与
    pipelines-section 之间），每仓库一张卡：灵感列表（内容 / 更新时间 / 编辑 / 删除）+
    底部「随手记录」表单；本地增删改成功后立即刷新列表，15 秒轮询兜底多标签页并发；
    新增 `INSPIRATION_POLL_MS = 15000` 常量与 `loadInspirations` /
    `submitNewInspiration` / `saveInspiration` / `deleteInspiration`；
  - 前端 `styles.css`：新增 `.inspirations-section` 系列样式（网格/卡片/列表/表单，
    与开放 Issue 板块视觉语言一致，含 hover 轻抬升动效）；
  - 文档：`README.md` API 一览补充 4 个灵感接口；
  - **测试**：后端 `test_api_inspirations.py` 20 用例（overview 聚合与排序、软删除仓库
    过滤、创建/更新/删除、空内容/纯空白/超长/仓库不存在/记录不存在等边界）；
    `test_database_migrate.py` 新增 v8 迁移与灵感 CRUD 4 用例；
    前端 `overview-inspirations.test.mjs` 12 用例（板块顺序、渲染、增删改交互、空状态、
    失败兜底）；`apple-hig` / `overview-issue-task` 既有用例适配三板块结构；
    后端 1108 + 前端 577 全量测试通过。

### Fixed

- **dsh 引擎（deepseek-harness SDK）任务详情页事件流同一语句出现两次（issue #144）**：
  用户反馈「使用 deepseek harness sdk 作为执行引擎时候，任务详情页里的事件流，同样
  的语句都是出现两次」。根因：SDK 对同一 assistant 输出会先后发射两类通知——
  `assistant/chunk`（text-delta / reasoning-delta 流式增量）与 `assistant/message`
  （由增量拼成的完整 content 块）；`dsh_runner._format_assistant_content` 把两类
  都转成 `stream_delta` / `thinking` 事件行，导致事件流（SSE 实时 + 日志回放）里
  每条语句都出现两次（任务 #242 日志实测：143 条唯一文本中 31 条精确成对重复，
  其余为增量片段 + 完整句的成对重复）。
  实现：
  - 后端 `botler/dsh_runner.py`：`_format_assistant_content` 仅保留 chunks 未覆盖
    的 `tool_use` 块（转 `tool_start`），`text` / `thinking` / `reasoning` 完整块
    不再产事件行——流式阶段（chunk 增量）已实时展示过同一内容，完整块重复发布是
    冗余；`format_dsh_notification` docstring 同步说明（issue #144）；
  - 测试：`test_dsh_runner.py` 新增复现用例
    `test_message_after_chunk_delta_not_duplicated`（chunk 增量 + 完整块同内容
    时，完整块不产行）；原 `test_text_block_becomes_stream_delta` /
    `test_thinking_block_becomes_thinking` / `test_reasoning_block_becomes_thinking`
    / `test_multi_block_keeps_order` 适配新语义（message 文本/思考块不产行，仅
    tool_use 转 tool_start）；
  - 后端全量 pytest 1264 passed、前端 node --test 645 passed。

- **「界面显示」卡片取消勾选「显示未启用项目」后没有保存按钮，无法保存设置（issue #142 反馈轮）**：
  用户反馈「取消勾选后没有保存按钮，无法保存设置」。现状与差距：issue #142 新增的
  `ui.show_disabled_repos` 开关位于「界面显示」卡片，而设置页全局「保存」按钮在上方
  「任务调度」卡片内——用户滚动到「界面显示」卡片修改开关后找不到保存入口，误以为
  无法保存（与 issue #27 SSO 卡片 / issue #141 Webhook 卡片同型问题）。
  实现：
  - 前端 `pages/Settings.jsx`：「界面显示」卡片内新增独立「保存界面显示配置」按钮与
    成功提示（uiSaved 2 秒自动消失），绑定新增 `saveUi`——只提交 ui 段
    （`PUT /api/settings {ui: ...}`，后端支持部分更新，与 `saveSso` / `saveWebhook`
    同模式），不影响 worker/claude/notifications 等其他设置；ui 段统一由新增
    `buildUiPatch` 构建（timezone + show_disabled_repos，全局 save 与卡片内 saveUi
    共用，行为一致）；保存成功后同步更新页面显示时区（与全局保存一致，无需刷新）；
    卡片说明文字由「点击上方『保存』」改为「点击下方『保存界面显示配置』」，不再
    误导用户去其他卡片找保存按钮；全局「保存」按钮与既有 ui 保存链路保持不变；
  - **测试**：新增 `frontend/tests/settings-ui-save-button.test.mjs` 5 用例
    （卡片内含独立保存按钮且绑定 saveUi / saveUi 只提交 ui 段不携带 worker 等
    其他字段 / buildUiPatch 同时携带 timezone 与 show_disabled_repos / 说明文字
    不再指向「上方保存」/ 全局保存按钮仍存在）；既有
    `settings-show-disabled-repos.test.mjs` 适配 buildUiPatch 结构；
    前端 645 + 后端 1263 全量测试通过。

- **消息推送 Webhook 设置没有保存按钮，无法保存设置（issue #141）**：
  需求「消息推送 Webhook设置没有保存按钮，无法保存设置」。现状与差距：设置页
  全局「保存」按钮位于上方「任务调度」卡片，而「消息推送 Webhook」卡片在页面
  下方，卡片内只有表单字段与「发送测试推送」按钮——用户滚动到 Webhook 卡片
  修改配置后找不到保存入口，误以为无法保存（与 issue #27 SSO 卡片同型问题）。
  实现：
  - 前端 `pages/Settings.jsx`：Webhook 卡片内新增独立「保存 Webhook 配置」
    按钮与成功提示（webhookSaved 2 秒自动消失），绑定新增 `saveWebhook`——
    只提交 webhook 段（`PUT /api/settings {webhook: ...}`，后端支持部分更新，
    与 `saveSso` 同模式），不影响 worker/claude/notifications 等其他设置；
    authorization 留空 = 保持现有凭据（复用 buildWebhookPatch，与全局 save 共用）；
    卡片说明文字由「点击上方『保存』」改为「点击下方『保存 Webhook 配置』」，
    不再误导用户去其他卡片找保存按钮；全局「保存」按钮与既有 webhook 保存链路
    保持不变；
  - **测试**：新增 `frontend/tests/settings-webhook-save-button.test.mjs`
    5 用例（卡片内含独立保存按钮且绑定 saveWebhook / saveWebhook 只提交
    webhook 段不携带 worker 等其他字段 / authorization 留空不覆盖现有凭据 /
    说明文字不再指向「上方保存」/ 全局保存按钮仍存在）；
    前端 616 + 后端 1200 全量测试通过。

- **显示 gitlab token 隔离：设置页 owner gitlab token 所有 Agent 均不可使用（issue #130）**：
  需求「显示gitlab token隔离，设置页面里的owner gitlab token，所有agent都不可以实现，
  已经在系统架构上实现隔离，避免agent错误调用；owner gitlab token只允许在概览页面上
  编辑issue、添加issue、关闭issue、在issue添加评论以及回复issue评论的时候使用，其他
  场景都不得使用；agent无论如何都不能使用owner gitlab token，他只能使用自己仓库的
  认证token来进行issue编辑」。现状与差距：owner token（issue #87）已掩码展示、git
  推送凭据走 bot token 不含 owner，但 agent 会话环境 `GITLAB_TOKEN` 仍把 owner
  token 作为最高优先级注入——agent 能拿到并错误调用 owner token，与"所有 Agent
  不可使用"矛盾；概览页 issue 编辑操作也未优先使用 owner token。
  实现：
  - 后端 `executor.py`：`_build_env` 会话 `GITLAB_TOKEN` 注入改为
    remote url 内嵌 token（仓库自己的认证 token）→ 全局 bot token，**绝不注入
    owner token**（owner 只允许在概览页 issue 编辑操作时由平台使用）；git 推送
    凭据仍走 `GIT_ASKPASS` 的 bot token，不受影响；
  - 后端 `api/issues.py`：新增 `_owner_client`（按 token 值缓存重建）与
    `_issue_edit_call`（owner 优先、owner 401/403 回退 per-repo → 全局）；
    概览页 5 处 issue 编辑操作（关闭 issue、编辑标签、添加评论、回复评论、
    添加 issue）统一优先 owner token，符合"只允许在概览页面上编辑 issue 时
    使用"；form-meta（只读查询）保持原链路；
  - 前端 `Settings.jsx`：Owner GitLab Token 卡片标题新增「已隔离 · Agent
    不可用」徽章（`h2 .badge` 对齐样式），说明区明确隔离规则——所有 Agent
    均不可使用、允许使用范围（仅概览页编辑/添加/关闭 issue、添加/回复评论）、
    Agent 只能使用自己仓库的认证 token 编辑 issue；
  - 文档：`config.example.yaml` owner_token 注释补充隔离说明；
  - **测试**：后端 `test_owner_token.py` 原"会话 GITLAB_TOKEN 优先 owner"
    用例改为"绝不注入 owner token"；`test_api_issues.py` 新增 7 用例（关闭/
    标签/评论/回复/添加 issue 优先 owner、未配置 owner 沿用原链路、owner
    401 回退原链路）；前端 `settings-owner-token.test.mjs` 新增 2 用例（隔离
    徽章显示、隔离规则说明），`cardSource` 正则兼容 h2 内徽章；后端 1084 +
    前端 565 全量测试通过。
- **概览页 issue 编辑必须使用 owner token：修复未配置时静默以 code01 身份发布（issue #132）**：
  需求「我在概览页面的最新回复，为什么是以code01的用户回复的，我不是在 issue #130里说明了，
  概览页面的所有issue编辑都要使用owner token吗，帮我诊断并修复」。诊断：概览页 5 处 issue
  编辑操作（关闭 issue、编辑标签、添加评论、回复评论、添加 issue）虽已「优先」owner token
  （issue #130），但部署环境 `gitlab.owner_token` **未配置**时 `_issue_edit_call` 会静默
  回退 per-repo/全局 bot token——用户经概览页发布的评论/回复以 code01（bot）身份发出
  （实测复现：issue #78 最新回复「如果将hermes 的集成方式从http api改成hermes agent sdk集成…」
  即因此以 code01 发布）。同时发现 issue #130 合规缺口：executor/reconciler 的任务生命周期
  评论与终态标签补打仍传 `prefer_owner=True`，一旦配置 owner token，会把「🤖 Botler」机器人
  消息以 owner 身份发布，同样违背「owner token 只允许概览页使用、agent 无论如何都不能使用」。
  修复：
  - 后端 `api/issues.py`：`_issue_edit_call` 改为**强制 owner token**——未配置
    `gitlab.owner_token` 返回 400 明确提示先到设置页配置，owner 401/403 返回 502 提示
    更新 token，**不再静默回退 bot token**（杜绝再次以 code01 身份发布）；
  - 后端 `executor.py`：移除 `_owner_gitlab_client` 与 `_call_with_fallback` 的
    `prefer_owner` 机制，7 处任务生命周期评论/打标签调用固定走「全局 bot token →
    remote 内嵌 token」链路（agent 绝不使用 owner token）；
  - 后端 `reconciler.py`：移除 `_owner_client` 与 `prefer_owner` 机制，终态标签补打
    固定走 bot 身份（对账非概览页操作）；
  - 前端 `Overview.jsx`：启动时检测 owner token 配置状态，未配置时开放 Issue 板块
    顶部显示醒目警示横幅（提示概览页 issue 编辑必须使用 owner token、操作会被拦截、
    不会以 code01 身份发布）；`styles.css` 新增 `.alert-warning` 样式；
  - 文档：`config.example.yaml` 与 `docs/GitLab-Owner-Token-申请教程.md` 更新（未配置
    owner token 时概览页编辑被拦截，不再回退 bot token；任务侧绝不使用 owner token）；
  - **测试**：后端 `test_api_issues.py` 原「未配置 owner 沿用原链路 / owner 401 回退」
    用例改为「未配置 owner 400 拦截、owner 401 502 拦截」（关闭/标签/评论/回复/添加
    issue 全覆盖，新增 8 用例），编辑类既有用例统一走 `client_edit` 夹具（已配置
    owner）；`test_owner_token.py` executor/reconciler 用例改为「绝不使用 owner token」
    （bot 身份固定，全局 401 回退 remote）；后端 1110 + 前端 577 全量测试通过。



### Fixed

- **dsh 引擎（deepseek-harness SDK）提示词未持久化、聊天记录不显示提示词（issue #146）**：
  需求「使用deepseek harness sdk作为执行引擎的时候，提示词未持久化，聊天记录中也没有显示
  提示词」。根因：`GET /api/tasks/{id}/execution` 的 `prompt`（「查看提示词」按钮数据源，
  issue #90）与 `transcript`（聊天记录，issue #20）此前只从 claude 会话 jsonl
  （`claude_session_id` 定位 `~/.claude/projects/*/<sid>.jsonl`）解析；dsh 引擎会话 id 落库在
  `dsh_session_id`、渲染后的提示词无处落库，SDK 会话文件又是 runtime 内部格式无法像 jsonl
  那样解析——dsh 任务详情页「查看提示词」固定显示「提示词未持久化，见执行日志」、聊天记录
  显示「暂无聊天记录（会话尚未开始或会话文件不可读）」。修复：参照 hermes 引擎
  `hermes_history` 落库模式，新增任务字段 `dsh_transcript`，由执行侧把提示词与消息落库，
  execution 接口读取返回。
  实现：
  - 后端 `database.py`：tasks 表新增 `dsh_transcript TEXT` 列（迁移链 v8→v9，旧库自动
    ALTER 补列），`_TASK_FIELDS` 白名单加入 `dsh_transcript`；
  - 后端 `executor.py`：`_run_dsh_once` 在 runner 启动前先把渲染后的完整提示词落库
    （运行中「查看提示词」即可用，不等执行结束）；`_on_line` 回调从事件行累积聊天消息
    ——`stream_delta` 流式文本收口为 `assistant` 消息（连续增量合并为一条回复，与 claude
    jsonl 写完完整行才落盘一致）、`tool_start` 转 `tool` 消息（含工具名与 input）、
    `thinking`/`status`/`raw` 不进聊天记录（事件流 SSE 已实时呈现，与 claude transcript
    只保留 text 对齐）；每次事件行后落库（运行中实时可见），停止/超时/正常结束均收口
    落库；新增 `_persist_dsh_transcript`（消息超 `_TRANSCRIPT_MAX_MESSAGES` 条时保留首条
    提示词与最近 N-1 条并置 truncated，与 parse_transcript 截断语义一致，落库失败不阻塞
    任务收尾）与 `_dsh_resume_messages`（断点续跑保留上次会话历史，追加本次恢复引导语为
    新 user 消息，与 SDK 会话内的真实输入一致）；
  - 后端 `api/tasks.py`：`task_execution` 的 `session_id` 回退取 `dsh_session_id`；
    `claude_session_id` 缺失时从 `dsh_transcript` 读取 `prompt` / `transcript` /
    `transcript_truncated`（非 JSON 脏数据容错返回空，不 500）；
  - 前端零改动：`TaskDetail.jsx` 的「查看提示词」与「聊天记录」消费的
    `execution` 接口字段契约不变；
  - **测试**：后端 `test_executor_dsh.py` 新增 `TestDshTranscript` 8 用例（fresh 落库
    提示词与 user/assistant 消息、流式增量合并、tool_start 工具消息、thinking/status
    不入列表、停止路径收口落库、断点续跑追加历史、runner 启动前提示词已落库、垃圾输出
    容错）；`test_api_tasks.py` 新增 3 用例（dsh_transcript 返回 prompt/transcript/
    session_id、非 JSON 脏数据空列表、truncated 标记透传）；`test_database_migrate.py`
    新增 3 用例（旧库迁移补列、新库建表含列、set_task_status 白名单写入）；同步更新
    `test_user_version_marker`（v8→v9）与 inspirations 迁移断言；后端 1294 全量测试通过（pytest 全量收集 1294 项全部通过）。

- **配置 Owner GitLab Token 后概览页评论/回复仍报「owner token 失效（403）」（issue #133）**：
  需求「我配置了gitlab owner token，但是回复评论、添加评论的时候报错，概览页 issue 编辑
  owner token 失效（403）：请在设置页更新 Owner GitLab Token 后重试」。诊断（部署日志
  实测复现）：设置页保存 owner token 时**不做任何校验**，用户误将只勾了 `read_api` 等
  只读 scope 的 PAT 保存进 config.yaml；GitLab REST 写操作（添加/回复评论、添加/关闭
  issue、编辑标签）要求 token 具备 `api` scope，只读 token 提交写操作返回 403，响应体
  `{"error":"insufficient_scope","error_description":"The request requires higher
  privileges than provided by the access token."}`——用户反复重新保存同一只读 token，
  概览页编辑持续 403 且错误提示笼统，无法定位根因。修复：
  - 后端 `api/settings.py`：PUT /api/settings 保存**真实** owner token（掩码/空串 =
    保持现有，跳过）前调用 GitLab `/personal_access_tokens/self` 校验有效性 + scopes：
    401 → 「token 无效或已过期」400 拒绝；403 → 「缺少 api scope」400 拒绝；404（旧版
    GitLab < 15.7 无 self 端点）→ 降级只校验 token 有效性；scopes 不含 `api` → 400
    拒绝并列出当前 scopes、明确指引重新生成勾选 api；校验失败一律**不落盘**；
  - 后端 `gitlab_client.py`：新增 `get_personal_access_token_self()`（self 端点封装）；
  - 后端 `api/issues.py`：`_issue_edit_call` 对 owner 403 且 GitLab 返回
    `insufficient_scope` 的响应直接提示「token 缺少 api scope（只读 scope 无法写评论/
    编辑 issue），请在设置页重新保存勾选了 api scope 的 Owner GitLab Token」，其余
    401/403 保留原通用提示；
  - 文档：`docs/GitLab-Owner-Token-申请教程.md` 安全提示补充「保存时校验」，FAQ 新增
    「缺少 api scope / 403 权限不足」诊断条目；
  - **测试**：后端 `test_api_settings.py` 新增 `TestOwnerTokenSaveValidation` 5 用例
    （缺 api scope 拒绝且不落盘、有效 token 正常保存、401 无效拒绝、掩码/空串保持
    现有跳过校验、旧版 GitLab 降级放行）；`test_api_issues.py` 新增 2 用例
    （insufficient_scope 403 提示 api scope、通用 403 保留原提示）；`test_owner_token.py`
    既有保存用例补校验桩；后端 1117 全量测试通过。


- **dsh 引擎事件流极其冗长：一句话拆成好多个单独字（issue #122）**：
  需求「使用 deepseek harness sdk 执行引擎时，事件流会变得极其冗长，
  因为一句话会拆成好多个单独字」。诊断：dsh 引擎（deepseek-harness
  SDK）的 `assistant/chunk` 按 token/字粒度回调 `text-delta` /
  `reasoning-delta`，每个增量（中文场景往往是一个字）都被
  `DshRunner._on_notification` 转成一条独立 `stream_delta` / `thinking`
  事件行——一句话被拆成几十上百行，SSE 事件流、事件总线（队列上限
  1000，满丢最旧）与任务日志文件同步爆炸式增长，任务执行页逐字刷屏。
  修复（`dsh_runner.py`，仅 dsh 引擎路径）：
  - 新增 `_DeltaCoalescer` 增量合并缓冲：连续同类型增量（文本/思考）
    先拼进缓冲，遇异类事件（工具调用/状态/回合结束等）、类型切换或
    超过刷新间隔（0.5s，保持 UI 实时感）时一次性冲刷为一条事件行；
    worker 结束产结果行前冲刷尾部缓冲，增量文本不丢失；
  - 事件行协议不变（`stream_delta` / `thinking` 语义与字段一致），
    executor 结果判定（`_dsh_result`）、SSE 解析（
    `parse_hermes_event_line`）与日志回放零改动；结果行仍是输出最后
    一行（issue #119 的多行解析不受影响）。
  - **测试**：`test_dsh_runner.py` 新增 6 个用例——逐字 text-delta
    合并为一条完整句、reasoning-delta 合并、类型切换保序冲刷、非增量
    行先冲刷再直发（顺序保持）、结果行前尾部冲刷（文本不丢）、20 字
    增量事件行总量降至 2 行（修复前 20 行逐字碎行）；后端 1040 +
    前端 529 全量测试通过。


- **概览页 issue 右边栏「执行引擎」全部误显当前全局引擎（issue #120）**：
  需求「issue 现在显示的执行引擎全都是 deepseek harness sdk，但是一开始
  的 issue 都是 claude code 为执行引擎」。诊断：`IssueDrawer` 的「执行引擎」
  行（issue #118 引入）读取的是全局配置 `worker.engine`（issue #113），只
  表示「新领取任务用什么引擎」，不代表该 issue 实际由哪个引擎处理——全局
  引擎从 claude 切到 dsh 后，所有 issue（含历史由 Claude Code CLI 处理的）
  都显示 deepseek-harness SDK。修复：
  - **执行引擎按任务落库**（database v7 迁移）：`tasks` 表新增 `engine`
    列，`executor.run_task` 执行时把本次实际引擎写入任务；迁移自动回填
    存量任务——按断点续跑会话字段推断历史引擎（`dsh_session_id` →
    dsh，`hermes_history` → hermes，`claude_session_id` → claude）；
  - **detail 接口返回该 issue 实际引擎**：`GET /api/issues/{project_id}/
    {iid}/detail` 新增 `engine` 字段（回退链：任务落库 engine > 会话字段
    推断 > 全局 `worker.engine`）；`GET /api/tasks` 列表/详情同步透出
    `engine`；
  - **前端改为按 issue 展示**：`IssueDrawer` 不再单独拉取 `/api/settings`
    全局配置，改为从 detail 响应读取 `d.engine` 归一展示（拉取失败仍显示
    「—」兜底）。
  - **测试**：后端新增数据库迁移回填、detail 引擎回退链、tasks 透出、
    executor 落库共 13 个新用例；前端重写抽屉引擎用例 7 个（detail
    返回 dsh / 空值回退 claude / 失败「—」/ 未知原样 + 源码不再依赖
    /api/settings）；后端 1034 + 前端 516 全量测试通过。

- **dsh 引擎任务重复开发直至失败（任务 #198 #199 根因，issue #119）**：
  需求「使用 deepseek harness SDK 作为执行引擎开发的时候，为什么会重复
  开发任务，直至显示失败，但是 claude code 作为执行引擎就不会」。诊断
  （任务 #198 #199 日志）：dsh 引擎多次尝试均 `exit 0`、输出结果行
  `finish_reason: completed` 且 final_response 非空（agent 实际已完成
  开发），却仍被判失败并重试，每次重试重新开发（约 4 分钟/次），重试
  耗尽后任务显示失败。根因：`_run_dsh_once` 收集 DshRunner 的
  `on_line` 回调事件行（行尾无换行符）后以 `''.join(lines)` 拼接，
  `output` 整串无换行分隔——下游 `_last_json_object` 按行扫描只能
  `raw_decode` 出**首个**事件对象（如 `{"event": "raw", ...}`），
  拿不到末尾结果行的 `finish_reason` → `_dsh_result` 误判 `failed`
  → 触发重试；`_persist_dsh_session_id` 同样解析不到 `session_id`
  → 断点续跑失效 → 每次重试都是全新会话（重复开发任务）。claude
  （stream-json 逐行）与 hermes（NDJSON）输出天然换行分隔，故不受
  影响。修复（executor `_run_dsh_once`）：
  - 事件行拼接改为 `'\n'.join(lines)`（与日志落盘 `line + "\n"`
    一致），结果行可被 `_dsh_result` / `_persist_dsh_session_id` /
    `_extract_error` 正确解析：成功判定恢复、会话 id 正常落库、
    断点续跑生效，不再重复开发。
  - **测试**：`test_executor_dsh.py` 新增 1 用例（多行事件 + 结果行
    输出：`_dsh_result == "success"` 且 `dsh_session_id` 落库，
    修复前该用例复现 `failed` 误判）；后端 1021 + 前端 516 全量
    测试通过。

- **dsh 引擎任务全部运行失败（任务 #194 #195 根因，issue #115）**：
  需求「任务 #194 #195 使用 deepseek 执行引擎都运行失败了，请诊断
  原因并修复 bug」。诊断：任务切换 `worker.engine: dsh` 后执行，
  dsh runtime 启动正常、prompt 正常发出，但 DeepSeek API 因无 Key
  拒绝（401 AUTH）→ `turn/end` reason.kind=error → 判定失败、3 次
  重试耗尽。Key 缺失链：config.yaml 无 dsh 段（dsh.api_key 空）、
  部署机环境无 `DEEPSEEK_API_KEY`（.env 未配），而用户实际把
  DeepSeek key 配在设置页「AI 供应商」（`ai_providers`），该配置
  此前无任何消费者——用户在平台上配过 key 却完全不生效。修复：
  - **凭据解析链**（executor 新增 `_dsh_credentials`）：dsh 段显式
    配置 > `ai_providers` 中 `provider=deepseek` 且 enabled 的项 >
    环境变量 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`（SDK 默认读取，
    botler 不覆盖）；`_run_dsh_once` 按解析结果传 runner；
  - **失败诊断透传**（dsh_runner）：`turn/end` 非 completed 时把
    reason.error/failure.message 拼进状态行；`assistant/chunk` 的
    finish/error 块（LLM 失败细节所在）透传为「模型调用失败: …」
    状态行——此前两者均被丢弃，任务日志与失败详情只有「回合结束:
    error」，无法诊断 401 等具体原因；
  - **验证**：本地以生产 config 回退解析出 key 后真实跑 SDK 会话
    `finish_reason=completed`；假 key 场景输出含完整 AUTH 401 文本。
  - **测试**：`test_executor_dsh.py` 新增 8 用例（显式优先 / deepseek
    项回退 / 缺项互填 / disabled·非 deepseek 跳过 / 无源为 None /
    runner 透传与 None 透传），`test_dsh_runner.py` 新增 6 用例
    （turn/end error·failure 透传 / max-tokens 不附加 / assistant
    chunk finish error 转状态行 / 非 error 仍 raw）；前端 493 +
    后端 989 全量测试通过。

- **dsh 引擎事件透传对真实 SDK 事件结构覆盖不全（issue #115 第二轮）**：
  上轮修复按猜测的事件结构透传失败诊断，本轮在部署机以真实
  deepseek-harness SDK（0.1.0rc6）实测发现：`assistant/message` 的
  思考块类型是 `reasoning`（字段 `text`）而非 `thinking`——思考内容
  从未出现在任务日志 / SSE；`assistant/chunk` 承载流式增量
  （`block-start` / `reasoning-delta` / `text-delta` / `block-end` /
  `usage` / `finish`），此前仅透传 finish/error 块，`reasoning-delta`
  与 `text-delta` 全部落 raw——任务执行页在回合结束前无任何实时
  流式输出，失败诊断只能依赖回合末的贫瘠摘要。修复（dsh_runner）：
  - `assistant/message` 的 `reasoning` 块 → `thinking` 事件行
    （与既有 `thinking` 块同语义，前端折叠展示）；
  - `assistant/chunk` 的 `text-delta` → `stream_delta` 事件行、
    `reasoning-delta` → `thinking` 事件行（实时流式可见）；
  - 其余簿记块（block-start/block-end/usage/finish 正常结束）与
    未识别类型仍落 raw 供日志诊断，行为不变。
  - **验证**：部署机真实 SDK 端到端跑会话（生产 key 回退解析 +
    修复后代码）：输出含 19 个 thinking 增量与流式 stream_delta，
    `finish_reason=completed`；假 key 场景「模型调用失败: 401 AUTH」
    透传依然生效。
  - **测试**：`test_dsh_runner.py` 新增 3 用例（reasoning 块 →
    thinking / text-delta → stream_delta / reasoning-delta →
    thinking），前端 493 + 后端 992 全量测试通过。

### Added

- **概览页 issue 右边栏新增「添加评论」与「回复评论」（issue #125）**：
  需求「在概览页面弹出的issue的右边栏，实现可以添加评论、以及回复
  评论」。实现：
  - **后端**：新增 `POST /api/issues/{project_id}/{iid}/comments`
    （添加评论，复用 `GitLabClient.add_comment` 的 notes API）与
    `POST /api/issues/{project_id}/{iid}/comments/{note_id}/reply`
    （回复评论）两个端点；回复语义经实测确认——GitLab 的 notes API
    响应不含 `discussion_id`，回复需先 GET discussions 解析目标评论
    所在线程 id，再 POST discussions 带 `in_reply_to_discussion_id`
    追加（`GitLabClient.reply_to_note` 新增方法，note 不存在抛 404）；
    正文去空白后为空 → 400（GitLab 对空正文同样拒绝，提前校验）；
    成功后清空概览缓存（user_notes_count/updated_at 已变化）并返回
    精简评论对象（与 detail 的 notes 条目同结构）；
  - **前端**（`IssueDrawer`）：评论区块底部新增评论输入区（占位
    「写下你的评论…」，空内容/提交中禁用按钮），每条评论下「回复」
    按钮展开内联回复框（占位「回复 @作者…」，支持取消）；添加/回复
    成功后本地即时追加新评论、清空输入并叠加评论计数（快照 + 本次
    新增），无需重新拉取详情；失败保留输入内容可重试；详情加载中/
    失败时不显示输入区（避免对不可知目标发言）；
  - **测试**：后端新增 21 用例（评论/回复端点 14：成功/空正文 400/
    仓库不存在/未启用 404/GitLab 404/5xx→502/网络错误→502/清缓存，
    以及 `reply_to_note` 单元 7：线程解析/404/异常数据跳过/响应容错）；
    前端新增 9 用例（渲染态、添加评论成功/空输入/失败保留、回复
    成功/取消/失败保留、加载失败隐藏输入区）；后端 1077 + 前端 563
    全量测试通过。

- **使用 apple-design skill 优化全部页面（issue #124）**：
  需求「使用apple-design skill优化所有页面，禁止少优化一个页面」。按
  apple-design skill（Apple 流体交互/材质与深度/响应/排版/可及性原则）
  对全部 8 个页面（概览/仓库/任务/任务详情/模版/标记库/设置/登录）落地
  统一优化，全部收敛在共享设计系统 `styles.css`，无页面遗漏：
  - **材质与深度（Materials & depth）**：顶导航由实心不透明条改为半透明
    毛玻璃悬浮层——`@supports (backdrop-filter)` 下 `blur(20px)
    saturate(180%)` + 半透明 `--nav-bg-glass`（浅色 rgba(255,255,255,.78)
    / 深色 rgba(10,10,10,.72)），内容从其下方滚动；不支持时回退纯色
    `--nav-bg` 兜底；`prefers-reduced-transparency: reduce` 时回退纯色
    无模糊；`prefers-contrast: more` 时导航/卡片补明确边框、次级文字加深；
  - **动效（Motion）**：模态/抽屉/遮罩「材质化入场」——新增
    `@keyframes surface-in`（scale .97 + translateY 8px + 淡入，模拟材质
    到达而非纯 opacity 淡入）/ `drawer-in`（从右滑入，空间一致性）/
    `overlay-in`（遮罩先淡入）；`.modal`/`.login-card`/`.guide-content`
    用 `--dur`（200ms）+ `--ease-spring`，`.drawer` 用 240ms，全部落在
    150–300ms 区间且受既有 `prefers-reduced-motion` 全局降级保护；
    高频动画元素（spinner/阶段节点/模态/抽屉）补 `will-change` 合成层
    提示（帧级流畅，只动 transform/opacity）；
  - **响应（Response）**：按钮按下升级为 Apple 风格微缩放
    `translateY(1px) scale(0.98)`；issue-link / section-toggle /
    modal-close / folder-item / add-method / remote-option /
    label-choice 补齐 active 按下即时反馈；概览 issue 行、仓库行、标签行
    补 hover 过渡反馈（运行中高亮行 hover 保持蓝色弱底不被覆盖）；
    概览流水线/开放 issue 卡片 hover 轻抬升 + 阴影加深；
  - **排版（Typography）**：正文启用 `font-optical-sizing: auto`；
    新增字号相关字距 token `--tracking-display`（大标题负字距收紧）与
    `--tracking-caption`，h1 引用负字距；表格启用 `tabular-nums` 等宽
    数字，数字跳变不抖动；折叠标题（任务详情/模版页）hover 变主色平滑
    过渡；
  - **测试**：新增 `apple-design.test.mjs` 17 用例逐条验收（毛玻璃材质/
    token 双主题/减弱透明/增强对比度/三类入场动画时长区间/响应按下态/
    排版/页面覆盖清单 8 页无一遗漏），前端 551 + 后端 1056 全量测试
    通过。
  - **补充（第二轮，issue #124）**：逐页复查交互反馈覆盖后发现三处
    可交互元素反馈缺口，统一收敛共享设计系统补齐（无页面遗漏）：
    - 可关闭提示条 `.alert`（概览/任务/仓库/设置等页 onClick 点击关闭）
      补 hover 阴影加深 + active 按下微缩放，反馈平滑过渡
      （`--dur-fast` 150ms + `--ease-out`）；
    - 任务详情页「思考过程」折叠条 `summary` 补 hover 变主色（提示可展开）；
    - 标签多选胶囊 `.label-choice`（新增 issue 弹窗/右边栏标记编辑）补
      hover 微降透明度（与既有 active 0.8 呼应）。
    `apple-design.test.mjs` 同步新增 3 个用例验收，前端 554 全量测试通过。

- **设置页新增 dsh 引擎推理等级设置（issue #123）**：
  需求「deepseek harness sdk是可以设置推理等级的，在设置页面添加deepseek
  harness推理等级的设置」。deepseek-harness runtime 的 `llm-deepseek`
  adapter 支持 `reasoningEffort`（`off` / `high` / `max` 三档，SDK 默认
  `high`），但 SDK 的 `DeepSeekHarnessConfig` 未直接暴露该参数。实现：
  - **后端**：`Settings` 新增 `dsh.reasoning_effort`（空 = 不设置），
    GET/PUT `/api/settings` 透出与校验（白名单 `off` / `high` / `max`，
    空串允许，非法值 400 提前拦截）；
  - **推理等级注入**（`dsh_runner.py`）：`DshRunner` 新增
    `reasoning_effort` 参数，非空时基于内置默认 Cordis 组合（或自定义
    `cordis` 文件）行级注入 `llm-deepseek` 条目的
    `config.reasoningEffort`（默认组合含 `!!js` 标签无法用 PyYAML 加载，
    故采用行级文本编辑），按内容哈希缓存到系统临时目录
    `botler-dsh-cordis/`，executor 执行时把 `cfg.dsh_reasoning_effort`
    透传给 `DshRunner`；自定义 cordis 缺失 / 无 `llm-deepseek` 条目时
    任务以可读错误失败，不静默忽略；
  - **前端**：设置页新增「dsh 引擎」卡片，推理等级下拉
    （默认 / off / high / max，含语义说明），跟随全局「保存」提交；
  - **文档**：`config.example.yaml`、`README.md` 设置表、
    `docs/dsh-engine-deployment.md` 同步补充配置说明。
  - **测试**：后端新增 cordis 注入（默认组合注入 / 已有 config 替换 /
    已有 config 首键插入 / 缺条目报错 / 非法值报错）、`resolve_dsh_cordis`
    派生缓存（空 effort 原样返回 / 自定义与内置基底 / 缺失文件报错）、
    settings API（GET 默认值 / PUT 持久化 / 非法值 400）、DshRunner 透传
    派生 cordis；前端新增设置页 dsh 卡片源码 + 渲染/保存交互用例；
    后端与前端全量测试通过。

- **新增 UI 优化参考文档——同类开源项目调研与设计借鉴（issue #121）**：
  issue「界面优化讨论，现在界面布局和样式都不够美观，帮助找一些类似的
  开源项目，可以参考ui设计」为讨论型需求，本期不实施界面改造，调研结论
  落库 `docs/ui-design-reference.md`：
  - **现状分析**：Botler 已基于 Vercel Geist + Apple HIG（issue #110/#111）
    完成设计令牌与基础规范；对照同类项目后短板集中在「关键状态可视化 /
    信息密度 / 微交互 / 品牌感」四方面，基底无需推翻；
  - **项目调研**：按「Git/DevOps 平台、自动化 Bot 平台、监控仪表盘、
    后台管理、组件库/设计系统」五类梳理 28 个同类开源项目（Star/License/
    主页经 GitHub/Codeberg/GitLab API 核实，截至 2026-08-16），逐项给出
    可借鉴点；优先参考 GitLab Pajamas > Gitea > Uptime Kuma / Grafana >
    Plane / Huly > shadcn/ui / Ant Design；
  - **分页建议**：概览页关键数字条与状态可视化、任务页过滤/排序/批量
    操作、空状态/骨架屏、登录页品牌化、暗色令牌调校、DropdownMenu 等
    组件补充共 6 组改进建议；
  - **落地路线**：建议按模块拆分为后续独立 issue 逐项实施，每项遵守
    `docs/design-system.md` 规范并补前端测试。
  - 纯文档变更（README 同步补充文档入口），无代码/测试改动；本地全量
    测试通过（后端 1034 + 前端 529）。


- **概览页开放 Issue 板块展示正在运行任务、删除独立任务板块（issue #114）**：
  需求「概览页面删除，正在运行任务组件，现在开放issue组件页面也显示正在
  运行的任务」。实现：
  - **删除独立任务板块**：概览页移除「正在执行的任务」板块（任务卡片
    网格），页面仅保留「开放 Issue」与「CI/CD 流水线」两板块；
  - **任务信息整合进开放 issue 列表**：正在运行任务的信息（任务状态
    徽章 / 执行引擎 / 实时输出日志）随「开放 Issue」板块 running 组
    的对应 issue 项内展示——按 repo_id+issue_iid 匹配（与 #99/#101
    高亮置顶同规则），新增 `tasksForIssue` / `engineLabel` 纯函数，
    同一 issue 的多条任务记录逐一渲染任务块；任务轮询与 SSE 事件流
    数据流保持不变，实时输出自动滚动同步迁入 issue 项内日志元素
    （`.issue-task-log`），任务轮询错误并入开放 Issue 板块错误横幅；
  - **样式**：styles.css 删除任务板块样式（overview-grid / overview-
    card / tasks-section 等），新增 issue-row / issue-task 任务块样式
    （左侧竖条与运行中高亮呼应）；
  - **测试**：新增 overview-issue-task.test.mjs 14 用例（板块删除
    源码断言 / tasksForIssue 边界 / 任务块渲染 / SSE 实时输出 / 多任务
    / 跨仓库不误显 / 样式断言），适配 overview-page、overview-section-
    order、hig-layout、overview-columns-unified、apple-hig 5 个既有
    测试（三板块断言改为两板块）；后端 1034 + 前端 529 全量测试通过。

- **概览页 issue 右边栏展示任务执行引擎类型（issue #118）**：
  需求「概览页面弹出的issue右边栏，显示任务执行引擎的类型」——概览页
  打开 issue 详情右边栏时，KV 表格新增「执行引擎」行，展示当前任务
  执行引擎类型（`worker.engine`：claude / hermes / dsh，issue #113
  引入的全局配置，对新领取任务生效）：
  - **数据源**：抽屉打开时从 `GET /api/settings` 读取
    `worker.engine`，复用现有接口无需新增后端改动；展示文案与设置页
    「任务调度」卡片下拉选项一致（Claude Code CLI / hermes-agent /
    deepseek-harness SDK）；
  - **前端**：IssueDrawer 新增 `ENGINE_META` 映射与 `engineDisplay`
    纯函数——null=加载中、空值/纯空白回退默认 claude、未知值原样
    展示兜底不崩溃；拉取失败显示「—」不阻塞其余信息展示；
  - **测试**：前端新增 `overview-issue-drawer-engine.test.mjs` 7 用例
    （源码契约 / ENGINE_META 三引擎映射 / engineDisplay 边界 / 渲染
    回显 dsh / 未返回回退 claude / 失败「—」/ 未知值兜底），并适配
    `overview-issue-notes.test.mjs` 4 用例（新增 /api/settings 调用后
    detail 路径计数与路由）；前端 516 全量测试通过。

- **概览页 issue 右边栏新增失败任务「重试」按钮（issue #117）**：
  需求「概览页面的issue详情页面右边栏，如果是失败任务，增加一个重试
  按钮，点击之后重新执行任务」——概览页开放 issue 列表中带 `bot-failed`
  标签（且无 `bot-done`，与列表分组判定一致）的失败任务，打开详情
  右边栏后在右上角显示「重试」按钮，点击二次确认后重新执行该 issue
  对应的任务：
  - **后端**：新增 `POST /api/issues/{project_id}/{iid}/retry`——按
    project_id+iid 定位该 issue 的任务：已有活跃任务（排队/执行/重试
    中）→ 409 防重复执行；最近任务为 failed/interrupted → 复用任务
    记录重试（重置 queued 重新入队，与任务页手动重试一致，保留断点
    续跑会话、清除历史停止请求残留）；无任务记录或最近任务已终态成功
    → 新建任务入队（triggered_by=manual，记录 issue 标签/更新时间供
    调度器排序）；`database.find_latest_task` 按 id 倒序取最近一条任务；
    成功后清空概览缓存，前端刷新列表即可看到 issue 进入「运行中」组；
  - **前端**：IssueDrawer 右上角新增「重试」按钮（btn-primary，失败
    任务 + 非运行中 + 本次会话未重试才显示）——二次确认（复用自定义
    对话框）→ 调 issue 级重试接口；成功后按钮消失、显示「任务 #N 已
    重新入队」提示并通知父组件刷新开放列表；失败显示错误按钮保留可重
    试；请求中按钮禁用防重复点击；`isFailedTask` 纯函数判定（bot-done
    优先级高于 bot-failed）；Overview 将 running 标记传入抽屉，任务
    运行中（重试已在进行）时不显示重试按钮；
  - **测试**：后端新增 14 用例（issue 级重试 API 11 用例：复用失败/
    中断任务、新建任务、成功任务兜底新建、活跃冲突 409、仓库不存在/
    未启用 404、GitLab 404/5xx/网络错误映射、清空缓存；`find_latest_task`
    db 层 3 用例）；前端新增 overview-issue-drawer-retry.test.mjs 10
    用例（isFailedTask 纯函数边界、按钮条件渲染、确认/取消、成功/失败、
    请求中禁用、Overview 传参联动）。


  需求「将中断恢复的提示词也做成类似全局模板一样，可以由用户编辑
  修改的提示词」——中断恢复引导语此前硬编码在 executor.py，用户
  无法修改，现与全局默认模版（templates.default）同机制开放编辑：
  - **后端**：内置默认 `DEFAULT_RESUME_PROMPT` 迁入 config.py（与
    DEFAULT_TEMPLATE 并列），新增 `templates.resume` 配置键——
    缺失/空串归一为内置默认（中断恢复必须有引导语，不允许空模版）；
    `GET /api/settings` 的 templates 段返回 `resume`，`PUT` 支持
    写入（非字符串 400 拒绝，空白 = 移除自定义键恢复内置默认）；
    executor._resume_prompt 改从 config 读取（claude/hermes/dsh
    三引擎断点续跑统一入口，自定义对三引擎同时生效）；
  - **政策修正（issue #109）**：旧 RESUME_PROMPT 含「用 GitLab API
    关闭 issue」指令，与「Agent 永不主动关闭 Issue」矛盾，迁入时
    同步修正为「不要关闭该 issue——关闭动作留给用户确认后手动执行」；
  - **前端**：模版页新增「中断恢复模版」视图（与全局默认/仓库级
    并列切换），复用现有编辑器与占位符表（全部 11 个占位符可用），
    界面注明「留空保存即恢复内置默认」；
  - **测试**：后端新增 14 用例（settings API 读写/清空恢复默认/
    非字符串 400/部分更新不影响 default；config 加载兜底与空串归一/
    内置默认不含关闭指令/占位符齐备/update_resume_template 写盘清键；
    executor 渲染默认/自定义/清空回退），前端新增
    `templates-resume-page.test.mjs` 6 用例（加载读取/视图切换/保存
    API/恢复默认提示/后端 GET·PUT 契约）；前端 499 + 后端 1006
    全量测试通过。

- **设置页新增「任务执行引擎」设置项（issue #113）**：
  需求「设置页面增加一个设置项，用来切换后端用来编写代码的agent」——
  `worker.engine` 引擎切换逻辑（issue #47/#84）此前只能在 config.yaml
  手工修改，本次起设置页可直接切换，保存后对新领取的任务生效：
  - **后端**：`GET /api/settings` 的 worker 段返回 `engine`；`PUT`
    校验引擎名白名单（claude / hermes / dsh，strip + 小写归一，
    与 executor._engine 合法集合一致），非法值 400 拒绝不落盘；
  - **前端**：「任务调度」卡片新增引擎下拉设置项（三引擎 + 默认
    claude 回退），跟随全局「保存」写回 config.yaml；
  - **测试**：后端新增 9 用例（默认值 / 三引擎持久化 / 大小写归一 /
    非法值·非字符串·空串拒绝 / 部分更新不影响其他字段），前端新增
    `settings-engine.test.mjs` 5 用例（下拉渲染 / 回显与回退 / 保存
    提交）；前端 493 + 后端 976 全量测试通过。

- **pm2 部署自动安装 deepseek-harness SDK（issue #112 跟进）**：
  issue #112 首轮仅覆盖 Docker 镜像内置 SDK，用户反馈 pm2 部署实例
  仍缺该依赖，本次补齐 pm2 部署形态：
  - **deploy/install-dsh-sdk.sh**：新增一键安装脚本（单一事实来源）
    ——阿里镜像 + 显式全版本号（`DSH_INDEX_URL` 环境变量可覆盖）、
    幂等（已装目标版本跳过）、安装后 import 校验 fail fast、优先
    uv pip（CI venv 无 pip seed）回退 venv 内 pip；
  - **.gitlab-ci.yml**：`deploy_to_code01`（pm2 部署）主依赖安装后
    自动调用脚本装入 `backend/.venv`，失败即部署失败；全局变量新增
    `DSH_INDEX_URL`（默认阿里镜像，UI 可覆盖）；
  - **文档同步**：`docs/dsh-engine-deployment.md` 第 2.2 节改为
    pm2 CI 部署自动安装 + 手动部署一键脚本，故障排查表、
    `dsh_runner.py` 安装指引与 README 部署步骤同步（README 的
    「CI/CD 自动部署」过时描述一并修正为 pm2）；
  - **测试**：新增 `test_deploy_dsh_sdk.py` 14 用例静态校验部署产物
    防回退，适配 `test_dockerfile_dsh.py` 中「pm2 保留手动安装」用例。

- **Docker 部署内置 deepseek-harness SDK（issue #112）**：
  需求「后端部署增加安装deepseke harness依赖」——dsh 引擎 SDK
  （`deepseek-harness-sdk==0.1.0rc6`）原为可选依赖（issue #84：镜像
  不含、部署机手动安装），本次起 Docker 镜像构建期自动安装，容器
  内 `worker.engine: dsh` 开箱即用：
  - **Dockerfile**：runtime 阶段新增 SDK 安装 RUN——阿里镜像（清华
    源未同步 rc 预发布版）+ 显式全版本号；镜像源可用 `DSH_INDEX_URL`
    build arg 覆盖（内网代理场景）；构建期 import 校验，装不上直接
    构建失败（fail fast）；
  - **requirements.txt** 保持不声明 SDK：主依赖继续走清华源，避免
    rc 版解析失败阻塞全部依赖安装；
  - **deploy/verify-docker.sh**：--full 冒烟新增第 12 项校验——容器
    内 `/opt/venv` 可 `from deepseek_harness import DeepSeekHarness`；
  - **文档同步**：`docs/dsh-engine-deployment.md` 第 2 节按部署形态
    拆分（Docker 已内置无需手动 / pm2·systemd 手动安装），故障排查
    表与 README Docker 部署章节、`dsh_runner.py` 安装指引注释同步；
  - **测试**：新增 `test_dockerfile_dsh.py` 10 用例，静态校验部署
    产物防回退（版本号锁定 / 阿里镜像 / build arg 可覆盖 / venv
    路径 / 构建期 import 校验 / 冒烟脚本 / 文档同步）。

- **按 Apple HIG 布局原则优化全部页面（issue #111）**：
  需求「优化页面，根据下面的设计原则，帮我优化项目中的每一个页面」
  （Apple HIG — 布局：安全区 / 布局指南 / 分组 / 视觉层次 / 适配性），
  依据 Issue 正文「给 Agent 的实现检查清单」逐项落地：
  - **8pt 网格间距体系（检查清单 #2）**：新增 `--gutter` 内容外边距
    token（桌面 20px，对应 HIG 数值速查「大屏 20pt」；≤640px 窄视口
    回落 16px，对应「iOS 默认 16pt」）；`.content`/`.topnav`/`.card`
    统一引用；按钮（7px 14px→8px 16px）、提示条、导航链接、统计条、
    聊天消息、评论卡片、弹窗/抽屉/目录对话框、分页、标记库、仓库
    列表等全部布局间距（padding/margin/gap）由非网格裸值（20/14/10/
    7/6px 等）统一改用 `--space-*` token，全部落在 4px 网格。
  - **视觉层次统一（检查清单 #4）**：概览页「正在执行的任务」板块
    补齐 section 容器 + h2 标题（`.tasks-section`），与「开放 Issue」
    「CI/CD 流水线」两板块结构/字号/间距节奏完全对齐；三板块间距
    统一 `--space-5`。
  - **布局响应式补全（检查清单 #5）**：顶导航窄视口可横向滚动
    （导航项 `flex-shrink: 0` 不压缩、隐藏滚动条，不再溢出截断）；
    设置页/详情页 kv 表格窄视口（≤640px）标签列 180px→120px 降级，
    输入控件不被挤压；内容区/卡片外边距随视口回落。
  - **导航浮于内容之上（检查清单 #7）**：`.topnav` sticky 断言防回退，
    内容区底部 60px 留白保证滚动内容延伸到底部。
  - **全宽按钮检查（检查清单 #6）**：断言 `.btn` 系列无 `width: 100%`
    规则（按钮遵循外边距、从一侧嵌入），防回退。
  - 测试：前端新增 `hig-layout.test.mjs` 9 用例（--gutter token 与窄屏
    回落、内容容器 token 引用、控件间距 4px 网格断言、三板块 h2 层级
    与顺序渲染、顶导航横向滚动/sticky、kv 表格窄屏降级、无全宽按钮、
    底部留白），先复现失败再修复通过；适配 2 个既有测试的 CSS 解析
    （支持 var() token 引用，间距语义值不变）；全量测试
    （前端 488 + 后端 942）通过。

- **按 Apple HIG 设计原则重新优化全部页面（issue #110）**：
  需求「根据下面的苹果的ui设计原则，帮我重新优化项目中的每一个页面」——
  依据 Apple Human Interface Guidelines 八原则（目标感/能动性/责任感/
  熟悉感/灵活/简洁/匠心/愉悦感）与验收检查清单逐项落地：
  - **交互状态补全（熟悉感/愉悦感）**：`.btn` 系列新增 `:active`
    按下反馈微交互（主/危险变体各自加深），导航链接 hover/active
    双态；transition 时长由 0.1s 统一为 150ms ease-out 缓动
    （HIG 匠心 150–300ms 建议区间）。
  - **design token 统一（匠心）**：新增动效 token（`--dur`/
    `--dur-fast`/`--ease-out`）、4px/8px 网格间距 token（`--space-1~6`）、
    主色实底拆分（`--primary-strong`/`--on-primary`）；散落硬编码
    中性色（rgba 遮罩/发丝线/弱底、阶段图节点色）统一 token 化。
  - **对比度达 WCAG AA（灵活）**：语义色加深（`--ok` #12823d、
    `--warn` #b45309、`--err` #d13438，原色白底对比度仅 3.2~3.9:1
    不达标），浅色/深色主题下语义色与 muted 文字对比度均 ≥ 4.5:1。
  - **跟随系统深色模式（灵活）**：新增 `prefers-color-scheme: dark`
    令牌翻转块（Geist Dark 基调），全部组件零改动自动适配。
  - **减弱动画支持（灵活）**：新增 `prefers-reduced-motion: reduce`
    降级规则，动画/过渡时长降为 0.01ms。
  - **空状态与加载态设计（匠心/愉悦感）**：新增 `.empty-state`
    （图标 + 文案）与 `.spinner`/`.loading-hint`/`.app-loading`
    加载态；概览页三板块（开放 Issue/运行任务/流水线）、任务表格、
    仓库列表、标记库、目录选择、App/任务详情/设置页加载态全部
    由裸文本升级为设计态（文案文本保持不变，既有测试兼容）。
  - **无障碍（灵活）**：键盘焦点可见性补全（`.section-toggle`/
    `.issue-link`/`.add-method`/`.remote-option`/`.label-choice`/
    `.folder-hidden-toggle`/`.modal-close` 等 focus-visible 焦点环）；
    全部弹窗/抽屉关闭按钮补充 `aria-label`；顶部导航补充
    `aria-label="主导航"`；触控设备（`pointer: coarse`）按钮最小
    触控目标 44px。
  - 测试：前端新增 `apple-hig.test.mjs` 15 用例（active 按下态、
    spinner/空态渲染、token 化断言、浅色/深色语义色对比度计算、
    reduced-motion、pointer: coarse、focus-visible 覆盖、aria-label、
    深色模式令牌翻转），先复现失败再修复通过；全量测试
    （前端 479 + 后端 942）通过。

- **概览页 issue 右边栏新增标记编辑功能（issue #108）**：
  需求「概览页面，点击issue弹出的右边栏，增加issue的标记编辑功能，
  可以删除和添加标记」——抽屉「标签」行由只读展示升级为可编辑：
  新增「编辑标记」按钮，点击进入编辑态加载项目标记池（checkbox
  多选、当前标记预勾选、池外标记（组标签/已从标记库删除）仍可取消
  勾选移除），保存时 diff 出 add/remove 一次提交，成功后抽屉标记
  即时更新并刷新概览列表；失败保留编辑态可重试、取消不调接口。
  - 后端：`api/issues.py` 新增 `GET /api/issues/{project_id}/labels`
    （项目标记池，复用 `_form_meta_labels` 颜色归一化）与
    `PUT /api/issues/{project_id}/{iid}/labels`（add/remove 一次提交，
    复用 `GitLabClient.add_labels`，标记名归一化去重、全空 400、
    成功后清概览缓存并返回更新后标记）；remove 只含当前实际存在的
    标记，规避 GitLab remove_labels 对不存在标记返回 404 的行为。
  - 前端：`IssueDrawer.jsx` 标签行新增编辑态（复用 label-picker/
    label-choice 多选样式与抽屉错误横幅/重试模式），新增
    `onLabelsUpdated` 回调；`Overview.jsx` 传入列表刷新回调；
    `styles.css` 新增 `.labels-edit`/`.labels-edit-btn` 样式。
  - 测试：后端 `test_api_issues.py` 新增 TestIssueLabels 18 用例
    （标记池获取/空池/仓库不存在/未启用/GitLab 错误/网络错误、
    更新成功/仅添加/仅移除/全空 400/归一化去重/缓存清空/issue 404/
    5xx/网络错误）；前端新增 `overview-issue-drawer-labels-edit.test.mjs`
    11 用例（编辑按钮显示条件、池加载与预勾选、add/remove 参数、
    成功更新与回调、无变更不调接口、失败重试、加载失败重试、空池、
    取消还原、防重复提交），先复现失败再修复通过。

- **弹窗全面改用自定义对话框，不再使用浏览器原生 alert/confirm（issue #105）**：
  用户反馈「不要使用 alert 来弹出通知，自定义一个对话框」——前端新增
  页面内自定义对话框体系，替换全部 10 处浏览器原生 `window.confirm` /
  `confirm` 弹窗调用（删除备份/恢复备份/上传恢复/删除标签/删除仓库/
  删除供应商/清空模版覆盖/关闭 issue/停止所有任务/重试任务），原生
  弹窗零残留。对话框为统一风格的自定义组件：遮罩 + 居中面板，沿用
  现有 Modal 的样式体系与交互约定（× 按钮/点击遮罩/Esc 键关闭），
  支持确认形态（取消+确定，danger 参数控制确定按钮 btn-danger 危险
  样式）与提示形态（单确定按钮），消息多行换行保留（pre-line），
  同一时刻只显示一个对话框、连续调用自动排队依次弹出。
  - 前端：新增 `dialog.js`（confirmDialog/alertDialog Promise 化
    接口 + 模块级队列 + DialogHost 订阅 + 测试注入 installAutoAnswer）
    与 `components/DialogHost.jsx`（挂在 App 根部）；`App.jsx` 挂载
    宿主；`styles.css` 新增 `.modal.dialog` 窄宽与 `.dialog-message`
    换行样式；7 个调用文件改为 `await confirmDialog({...})`。
  - 测试：新增 `tests/dialog.test.mjs` 18 用例（confirm/alert 形态
    各关闭路径、danger 样式、空消息/空标题边界、多行消息、排队、
    重复点击、无宿主兜底、注入应答、队列清理、调用点零残留源码
    断言）；适配 7 个既有测试文件（window.confirm mock → dialog.js
    autoAnswer 注入，点击后推进微任务链）；「刷新」「对账」低危操作
    断言升级为同时校验不存在 window.confirm 与 confirmDialog 防回归。

### Fixed

- **修复 graph2plan 任务 issue 被「自动关闭」的根因：GitLab autoclose 机制 + 三层防护（issue #109）**：
  用户反馈「graph2plan 的 issue 运行时 agent 总是自己 close issue，但提示词已标明
  严禁」——排查会话记录发现 agent **从未调用关闭 API**：graph2plan 提交信息写成
  `fix: #24 …`，命中 GitLab 实例开启的 `autoclose_referenced_issues` 默认关闭模式，
  推送默认主分支时 issue 被 GitLab 系统自动关闭（closed_by 为 project bot），
  用户侧即表现为「agent 自己 close issue」（agent 会话中自查发现误关后曾手动
  reopen + 说明评论）。三层防护：
  - 平台兜底（治本）：`executor.py` 新增 `_restore_autoclosed_issue()`，任务成功
    收尾时检测 issue 状态——closed 且 closed_by 是本项目 project bot（autoclose
    特征）→ 自动 reopen + 补说明评论 + warn 日志；人工关闭不干预；任意步骤
    失败仅记 warn 不阻塞任务成功。`gitlab_client.py` 新增 `reopen_issue()`
    （与 `close_issue` 对称的 `state_event=reopen`）。
  - 模板阻断（根因）：`config.example.yaml` 与内置兜底模板 `config.py
    DEFAULT_TEMPLATE` 增加提交信息规范——严禁 `fix: #N` / `fixes #N` /
    `closes #N` / `resolves #N` 等 autoclose 触发模式，issue 引用一律写全角
    括号 `（issue #NN）`；同时移除旧示例模板中「curl state_event=close 关闭
    issue」的指令（与「Agent 永不主动关闭 Issue」政策矛盾）。
  - 文档同步：`docs/labels.md` 新增「提交信息规范（防 GitLab autoclose 自动
    关闭）」章节。
  - 测试：`test_executor_autoclose.py` 新增 7 用例（autoclose 恢复/opened 不
    干预/人工关闭不干预/无 closed_by 容错/他项目 bot 不误判/查询失败与 reopen
    失败不阻塞收尾）；`test_config_template.py` 更新旧断言（模板不得含关闭
    指令、必须含 autoclose 禁用模式）；`test_gitlab_client.py` 新增
    TestReopenIssue 2 用例，先复现失败再修复通过。

- **概览页开放 Issue、正在执行任务、CI/CD 流水线三板块列数不统一（issue #107）**：
  用户反馈「开放issue、正在执行任务、ci/cd流水线的列数统一，以开放
  issue 为标准，先有些是三列，有些又是 4 列」——开放 Issue 板块
  （issue #96）已改为自适应网格（auto-fit + minmax(280px, 1fr)），
  宽屏下 4 列一行，而正在执行任务、CI/CD 流水线两个板块仍是固定
  3 列（repeat(3, 1fr)），同一页面列数错落不一致。
  - 前端：`styles.css` 中 `.overview-grid` 与 `.pipelines-list`
    改为 `repeat(auto-fit, minmax(280px, 1fr))`，与 `.issues-list`
    列数标准完全一致（最小列宽/网格间距一致，宽屏同为 4 列一行，
    窄视口同步降列回退不溢出），并同步更新注释说明。
  - 测试：新增 `tests/overview-columns-unified.test.mjs` 6 用例
    （三板块均 auto-fit 断言、防固定 3 列回退、minmax 最小列宽与
    间距三板块一致、多视口列数模拟一致+窄视口降列不溢出、任务/流水线
    多卡片渲染不丢卡），先复现失败（4 断言失败）再修复通过；
    `overview-page.test.mjs` 中编码旧固定 3 列行为的断言同步适配为
    auto-fit 标准。

- **「添加 issue」只输入标题时后端未在发送 GitLab API 时填充描述（issue #103，用户反馈修正）**：
  前一轮实现只在**前端**做标题→描述联动与提交兜底，后端创建
  issue 的 GitLab API 请求在描述为空时不发送 description 字段，
  用户反馈「没有实现」——要求后端在发送 API 请求时将标题内容填充
  到描述字段。修复：`GitLabClient.create_issue` 请求体 description
  兜底为 `description or title`（描述 None/空串时填充标题、非空
  （用户手写）保持原样、纯空白字符串的 strip 仍由 API 层负责），
  无论从哪个入口调用客户端创建的 issue 描述恒不为空。
  - 后端：`gitlab_client.py` create_issue 请求体 description 兜底
    填充标题（`{"title": title, "description": description or title}`）。
  - 测试：`test_gitlab_client.py` 新增 TestCreateIssue 5 用例
    （None 兜底/空串兜底/非空不覆盖/与标题相同不变/assignee 与
    labels 转发回归），先复现失败再修复通过。

- **数据备份卡片加载失败永久卡「加载中…」+ 上传恢复首次选文件不触发（issue #104）**：
  前端补测 BackupManager 时发现两个缺陷：① 首次加载 `/api/backups`
  失败时 `!data` 分支只渲染「加载中…」，catch 里写入的 error 永远
  不可见，用户永久卡在加载态且无任何报错提示；② 「上传备份恢复」
  文件选择框 onChange 里 setFile 后立即调用 restoreUpload()，但
  restoreUpload 读的是当前渲染闭包里的旧 file 状态（首次为 null）——
  首次选文件不弹确认也不上传，第二次选择会误用上一次的文件。修复：
  ① 无数据分支区分 error 渲染 alert-error 并支持点击重试（error 为
  空时才显示「加载中…」）；② restoreUpload 改为显式接收 picked 文件
  参数（onChange 直接传入，不再依赖异步 setState 后的状态），移除
  仅用于传参的 file 状态。
  - 前端：`components/BackupManager.jsx` 无数据分支增加错误渲染与
    点击重试；restoreUpload(picked) 参数化。
  - 测试：前端新增 BackupManager 全量交互测试 13 用例（加载/空列表/
    保存配置/立即备份/下载/删除确认/恢复确认/上传恢复/busy 防重复/
    首次加载失败错误展示），其中「首次加载失败错误展示」「首次选
    文件确认上传」2 用例先复现失败再修复通过。

- **概览页竖屏平板浏览时开放 issue 区域仓库名显示不出来（issue #102）**：
  概览页「开放 Issue」仓库卡片头 `.issue-repo-head` 为不换行 flex，
  仓库名 `.issue-repo-name` 设了 `white-space: nowrap` +
  `text-overflow: ellipsis` + `overflow: hidden`（flex 子项
  overflow 非 visible 时自动最小尺寸为 0，可被压缩至 0），竖屏平板
  窄视口下卡片网格 auto-fit 降为 1 列（约 280px 宽），优先级
  badge、issue 计数、添加按钮占掉固定宽度后，仓库名被压缩至几乎
  不可见。修复：头部 `flex-wrap: wrap` 允许换行；仓库名
  `flex-basis: 100%` 独占首行 + `overflow-wrap: anywhere` 任意断行
  （深路径 group/subgroup/project 无空格时也能断行完整显示），任何
  视口宽度下仓库名均完整显示不再截断。
  - 前端：`styles.css` 修改 `.issue-repo-head`（新增
    `flex-wrap: wrap`）与 `.issue-repo-name`（`flex-basis: 100%`
    + `overflow-wrap: anywhere`，移除 nowrap/ellipsis/overflow 截断
    三件套）。
  - 测试：前端新增 5 用例（CSS 规则断言头部换行、仓库名无
    nowrap+ellipsis、overflow-wrap: anywhere、flex-basis 独占首行；
    渲染级长仓库名深路径完整渲染进 issue-repo-name 不丢字符、多个
    仓库各完整渲染）。

- **「添加 issue」界面标签颜色不显示（issue #100）**：
  GitLab labels API 实际返回的颜色带 `#` 前缀（实测
  `{"color": "#6699cc", "text_color": "#FFFFFF"}`），而后端校验只
  接受不带 `#` 的 6 位 hex——真实环境中「添加 issue」弹窗标签多选、
  概览页 issue 列表、issue 右边栏三处的标签胶囊全部被置为无色中性
  降级，GitLab 标签颜色从未正确同步到界面。修复：`api/issues.py`
  新增 `_normalize_hex`（可选 `#` 前缀 + 6 位 hex → 无 `#` 的 6 位
  hex），`_label_entry` 统一经其归一化——与前端 `label-pill` 自行拼
  `#` 的约定对齐，三处标签胶囊即正确着色；非法值（畸形 `#` 前缀、
  非 hex 字符、数字类型、注入尝试）仍置 None 中性降级，防样式注入
  校验不因兼容 `#` 前缀而放宽。前端零改动（拼 `#` 渲染逻辑本就
  就绪）。
  - 测试：后端新增 overview 带 `#` 前缀颜色归一化、非法 `#` 变体
    拒绝（`#12345`/`##123456`/`#GGGGGG`/`#12 345`/数字）、form-meta
    带 `#` 前缀归一化与非法降级 4 用例；前端新增弹窗标签胶囊显示
    GitLab 颜色、无色标签无内联样式（中性兜底）2 用例。

### Added

- **前端补充测试：fmtSize/登录页/目录选择器/数据备份卡片（issue #104）**：
  前端测试框架为 Node.js 内置 node:test 运行器（`node --test
  'tests/**/*.test.mjs'`，非 Vitest/Jest）+ react-test-renderer 渲染
  + vite ssrLoadModule 加载 JSX + node:test mock.method 做 API mock。
  此前 Login.jsx、FolderPicker.jsx、BackupManager.jsx 三个组件与
  fmtSize 纯函数无任何测试覆盖，本次补齐：fmtSize B/KB/MB 三档
  边界 8 用例；登录页 SSO 错误映射（login_failed/access_denied/
  未知透传）与登录跳转 7 用例；目录选择器打开加载/隐藏目录过滤/
  上级与路径跳转/空目录与错误提示/选择回调/ESC 与遮罩关闭 16 用例；
  数据备份卡片交互 13 用例。新增 tests/helpers/mock-router-login.jsx
  （useSearchParams 查询参数可注入，供登录页 error 分支渲染测试）。

- **「添加 issue」界面只输入标题时描述自动复制标题内容（issue #103）**：
  概览页「添加 Issue」弹窗中，用户只输入标题、描述留空时，描述直接
  复制标题内容：输入时实时联动（标题 onChange 检测描述为空或仍为
  上次自动复制的旧标题时同步填充，用户可见），提交时兜底（描述
  trim 后为空则用标题填充），保证最终创建的 issue 描述等于标题；
  描述非空（用户手写）时标题改动不覆盖。
  - 前端：`components/AddIssueModal.jsx` 标题输入联动逻辑 + 提交
    兜底（`description: trimmedDesc || trimmedTitle`，原 `|| null`
    改为兜底复制）。
  - 测试：前端新增 8 用例（输入联动：只输标题描述框自动复制、继续
    改标题描述跟随、描述已输入时改标题不覆盖；提交兜底：只输标题
    提交 description=标题、自动复制后手动清空描述提交仍兜底、标题
    与描述都输入提交保留用户输入；回归：标题为空仍被校验拦截）。

- **概览页正在运行的 issue 置顶展示（issue #101）**：
  概览页「开放 Issue」板块中，正在被 bot 执行（任务状态
  running/retrying，即 LIVE_STATUSES）的 issue 从原 bot 终态分组
  （bot-failed / bot-done / 其他）中移出，单独成「⚙️ 运行中」组
  置于该仓库 issue 列表最上方，优先于终态分组展示；任务结束从任务
  列表消失后，issue 自动回落原分组。复用 issue #99 的
  `runningIssueKeys` 匹配逻辑（按 `repo_id + issue_iid`），零新增
  接口。覆盖边界：runningKeys 缺失/null/非 Set、repoId 数字/字符串
  归一、running 与 bot-done/bot-failed 并存（运行中优先终态分组）、
  多个运行中 issue 保持原始相对顺序、跨仓库同 iid 不误置顶、
  100 条混合数据分组计数正确且总量不丢、置顶项保留 #99 高亮与
  「运行中」徽章、任务结束后分组还原。
  - 前端：`Overview.jsx` 的 `groupIssuesByBotLabel` 增加可选参数
    `runningKeys`/`repoId`（命中即归 running 组，缺省行为与 #80
    完全一致），`ISSUE_GROUPS` 置顶新增 running 组（标题
    「⚙️ 运行中」），渲染处传入运行键集合与仓库 id；板块说明文案
    同步注明置顶规则。
  - 测试：前端新增 12 用例（纯函数分组归类/相对顺序/边界防御/
    100 条计数 + 渲染置顶顺序/多运行中/跨仓库/回落），更新 #99 与
    #80 既有 3 处断言以对齐置顶分组行为。

- **概览页开放 issue 高亮正在运行的 issue（issue #99）**：
  概览页「开放 Issue」板块中，正在被 bot 执行（任务状态
  running/retrying，即概览页活跃任务定义 LIVE_STATUSES）的 issue
  列表项高亮——浅蓝背景 + 左侧蓝色竖条 + 标题旁「⚙️ 运行中」蓝色
  徽章（与任务状态徽章 status-running 同色系）。数据复用概览页已有
  的任务轮询（每 3 秒），按 `repo_id + issue_iid` 匹配，零新增接口；
  任务结束从任务列表消失后高亮自动消失，任务失败重试（retrying）
  期间持续高亮。覆盖边界：任务缺 repo_id/issue_iid、非活跃状态
  （queued/succeeded/failed）不误标、跨仓库同 iid 不误高亮（repo_id
  参与匹配）、数字/字符串类型归一、高亮与 bot 终态徽章及标签胶囊
  并存不破坏既有分组。
  - 前端：`Overview.jsx` 新增导出纯函数 `runningIssueKeys`（活跃任务
    → `repo_id:iid` 键集合，Set 去重、字段缺失防御），渲染时命中项
    加 `issue-item-running` 类与「运行中」徽章；`styles.css` 新增
    `.issue-item-running`（浅蓝背景 + 左侧 3px 蓝色竖条）与
    `.issue-status-running`（蓝色弱底徽章）样式；
  - 测试：前端新增 `overview-issue-running-highlight.test.mjs`
    16 用例（纯函数正常/边界/类型归一/去重/100 条混合、渲染高亮、
    跨仓库同 iid、retrying、空任务列表、全不匹配、与分组及标签胶囊
    并存）。
- **概览页 issue 右边栏展示评论与活动（issue #97）**：
  点击开放 issue 打开右边栏后，描述下方新增「评论」与「活动」两个
  区块——评论（其他参与者的发言：作者头像/姓名/时间 + Markdown 正文）
  与活动（GitLab 系统事件：分配/标签/状态变更等，纯文本 + 时间）按
  note 的 system 标志分区展示。抽屉打开时按需拉取详情、切换 issue
  自动重新拉取；覆盖边界：加载中占位、接口失败错误横幅 + 重试按钮、
  无评论/无活动空占位、旧缓存数据缺 project_id 时不发请求显示占位、
  异常 note 字段（缺作者/时间/正文）兜底不崩溃。
  - 后端：`gitlab_client.py` 新增 `list_issue_notes`（notes API
    升序分页拉取，limit 截断防大 issue 翻页打爆 API）；`api/issues.py`
    新增 `GET /api/issues/{project_id}/{iid}/detail` 薄路由——仓库
    定位与关闭接口共用 `_enabled_repo_by_project_id`（不存在/未启用
    → 404），客户端选择与聚合一致（per-repo token 优先，回退全局
    bot token），note 精简 id/body/system/author{name,username,
    avatar_url}/created_at（UTC 无后缀，前端 fmtTime 解析约定），
    错误映射 GitLab 404 → 404、其他错误与网络错误 → 502；
  - 前端：`IssueDrawer.jsx` 新增评论/活动区块（评论正文 Markdown
    渲染复用 issue #27 组件、头像复用列表 assignee 的 avatar-fallback
    兜底），`styles.css` 新增区块/评论卡片/活动行样式；README API
    清单同步补充 detail 端点；
  - 测试：后端 `test_api_issues.py` 新增 TestIssueDetail 10 用例
    （字段精简与时间转换、limit 传参、空 notes、404/502 错误映射、
    异常字段兜底、per-repo client 优先）；前端新增
    `overview-issue-notes.test.mjs` 11 用例（接口路径、评论渲染、
    活动分区、加载中/失败重试/空占位、缺 project_id 不发请求、
    切换 issue 重拉、作者回退与空正文兜底）；关闭按钮测试随抽屉
    新增详情拉取补 api.get mock。
- **概览页「添加 Issue」按钮恢复——分支未合并回归修复（issue #95）**：
  排查发现 issue #92 实现的「添加 Issue」按钮只推送到
  `feat/overview-add-issue` 分支、从未合并回 main（也未创建 MR），
  main 上 issue #80/#94 独立演进，导致概览页开放 issue 板块仓库卡片
  右上角的按钮在 main 上缺失。修复方式：将 `feat/overview-add-issue`
  分支合并回 main（含 issue #92 按钮弹窗与 issue #93 分配人下拉修复），
  按钮、后端 API、测试随 main 一并发布。
  防回归：`frontend/tests/overview-add-issue.test.mjs` 11 用例随合并
  进入 main，CI frontend:test job 持续守护按钮渲染与弹窗行为——按钮
  再次丢失会立即测试失败阻断流水线。
- **概览页 issue 右边栏「关闭 issue」按钮（issue #94）**：
  点击开放 issue 打开右边栏后，右上角新增「关闭 issue」危险操作按钮
  （btn-danger 样式）——点击先二次确认（window.confirm），确认后调用
  后端关闭 GitLab issue；成功后按钮消失、状态徽章即时变「已关闭」并
  通知父组件刷新开放 issue 列表（该 issue 从列表消失），失败展示错误
  信息、按钮保留可重试，请求进行中按钮禁用（「关闭中…」）防重复点击。
  已关闭（closed）或无 project_id 的旧缓存数据不显示按钮。
  - 后端：`api/issues.py` 新增 `POST /api/issues/{project_id}/{iid}/close`
    薄路由——按 GitLab project_id 匹配「已启用」仓库（不存在/未启用 →
    404），复用 `GitLabClient.close_issue`（state_event=close，GitLab
    幂等，重复关闭安全），客户端选择与聚合一致（per-repo token 优先，
    回退全局 bot token）；错误映射 GitLab 404 → 404「issue 不存在」、
    GitLab 其他错误与网络错误 → 502；成功后清空概览缓存，下一轮轮询
    立即反映关闭状态；overview 聚合的每条 issue 注入 project_id 字段
    （前端关闭按钮定位仓库用）；
  - 前端：`IssueDrawer.jsx` 新增关闭按钮（confirm 二次确认、closing
    禁用、closed 本地标记即时更新徽章）、`Overview.jsx` 传
    onIssueClosed 回调刷新列表、`styles.css` 新增 `.issue-drawer-error`
    错误提示样式；
  - 测试：后端 `test_api_issues.py` 新增 TestCloseIssue 9 用例（正常
    关闭、缓存清空、仓库不存在/未启用 404、GitLab 404/5xx、网络错误、
    幂等重复关闭、project_id 注入），2 个既有字段断言随新字段更新；
    前端新增 `overview-issue-close-button.test.mjs` 8 用例（按钮显隐、
    确认/取消、接口参数、成功状态与回调、失败重试、请求中禁用）。
- **概览页「添加 Issue」按钮：弹窗表单直连 GitLab 创建 issue（issue #92）**：
  开放 issue 板块每个仓库卡片右上角新增「添加 Issue」按钮，点击弹出
  表单：标题（必填）、描述（选填）、分配人（项目成员下拉，必填，默认
  选中 agent）、标签（仓库已有标签多选，必填，不可新建）；提交后调用
  GitLab API 在对应仓库创建 issue，成功后关闭弹窗并立即刷新列表。
  - 后端：`gitlab_client.py` 新增 `list_project_members`（members/all
    含继承成员，取 user_id 作为 assignee_ids 的用户 id——members API
    顶层 id 是成员关系 id 不可用）与 `create_issue`（标题/描述/分配人/
    标签拼装）；`api/issues.py` 新增 `GET /api/issues/form-meta/{repo_id}`
    （成员+标签元数据，二者为必填字段数据来源，任一查询失败 502 不降级）
    与 `POST /api/issues`（标题/分配人/标签必填校验、空白标签元素过滤，
    per-repo client 优先与 issue 查询一致，创建成功后清空 overview 缓存
    保证前端刷新立即生效）；
  - 前端：新增 `AddIssueModal.jsx`（表单交互、成员含 agent 时默认选中、
    Esc/遮罩/× 三种关闭方式）；`Overview.jsx` 卡片头右上角按钮 + 创建
    成功后关闭弹窗并重新拉取列表；`styles.css` 弹窗字段与标签多选样式；
  - 测试：后端 `test_api_issues.py` 新增 19 用例（成员精简 user_id、
    404/400/502、成员标签空列表、异常成员元素过滤、per-repo client 优先、
    三项必填校验、描述选填、缓存失效后重新拉取）；前端
    `overview-add-issue.test.mjs` 11 用例（按钮渲染、默认选中 agent、
    三项必填校验不调 POST、提交成功参数/弹窗关闭/列表刷新、失败保持
    弹窗、空标签/元数据失败/遮罩关闭边界）。

- **概览页开放 Issue 按 bot 终态标签分组 + 状态徽章（issue #80）**：
  开放 issue 板块区分 bot 处理状态——每个仓库卡片内 issue 按三组分组
  展示：`bot-failed`（处理失败）/ `bot-done`（已完成待确认）/ `其他`
  （两种标签都不带，组顺序按用户指定 failed → done → other），只渲染
  非空组、组标题带计数；带终态标签的 issue 在标题旁显示醒目状态徽章
  （绿=done、红=failed，复用任务状态徽章弱底语义色风格），同时从普通
  标签胶囊中过滤这两个标签，避免同一标签重复显示。判定优先级 bot-done
  高于 bot-failed（失败后重试成功时两标签并存，成功为最终态）。
  - 前端：`Overview.jsx` 新增导出 `BOT_STATUS_NAMES` / `BOT_STATUS_META` /
    `ISSUE_GROUPS` / `botStatusKey` / `groupIssuesByBotLabel` 纯函数，
    分组保持组内原始相对顺序（后端已按 updated_at 降序）；`styles.css`
    新增组标题行与状态徽章样式；
  - 测试：`frontend/tests/overview-issue-groups.test.mjs` 18 用例
    （三组归类、组顺序、双标签并存归 done、labels 缺失/null/非数组/
    元素缺 name、空数组、单元素、100 条混合不丢失、组标题顺序与计数、
    徽章渲染与位置、标签胶囊过滤、全普通/空组/空仓库/缺字段渲染）。

- **CI 静态代码分析矩阵：5 种免费工具、高危/中危中止流水线（issue #86）**：
  security 阶段由单一 bandit 扩展为 5 个并行 job，覆盖前后端「代码 SAST +
  依赖 CVE + 密钥泄露」三个维度，任一 job 发现高危/中危问题即失败阻断
  后续所有 stage；审计服务不可用（基础设施故障）时警告放行不阻断。
  - `security:bandit`（既有，issue #58）：后端 Python 代码 SAST，中危
    及以上阻断（SARIF 报告），行为不变；
  - `security:deps-python`（新增）：pip-audit 扫描 requirements.txt 依赖
    已知 CVE（PyPI 数据源失败自动切 OSV 重试）；pip-audit 输出不含
    severity，任何已公开漏洞保守按中危阻断，CI 变量 `PIP_AUDIT_IGNORE`
    （逗号分隔漏洞 ID）可人工豁免；
  - `security:deps-frontend`（新增）：npm audit 扫描 package-lock.json
    依赖 CVE（npmmirror 审计端点不可用自动切官方 registry），moderate
    及以上阻断，低危/Info 仅记录；`NPM_AUDIT_IGNORE`（逗号分隔包名）
    可豁免；
  - `security:semgrep`（新增）：semgrep 社区版一次扫描前后端代码
    （Python + JS/TS），规则内置仓库 `ci/semgrep-rules.yaml`（12 条，
    不依赖外网规则下载），ERROR/WARNING（高危/中危）阻断、INFO 仅
    记录；扫描跑两遍（--json 阻断判定 + 规则健康检查防假绿、
    --sarif 报告上传）；
  - `security:secrets`（新增）：gitleaks 全仓库硬编码密钥检测（100+
    内置规则），任何发现即阻断；扫描排除运行时数据目录（data/、
    workspace、node_modules 等，配置见 `ci/gitleaks.toml`）；二进制
    复用 runner 持久目录 `~/.local/bin/gitleaks`（已预置），缺失时
    自动下载；
  - 报告统一上传 GitLab Security 页面：bandit/semgrep/gitleaks 走
    `reports:sast`（SARIF），pip-audit/npm audit 经新增
    `ci/convert-audit-to-gitlab.py` 转为 GitLab 依赖扫描报告走
    `reports:dependency_scanning`（JSON 15.0.0 schema），均为
    `when: always` 上传（阻断失败时页面同样可见漏洞详情）；
  - 依赖漏洞修复（门禁上线时暴露的既有漏洞）：react-router-dom
    6.26.0 → 7.18.2（2 个 CVE：open redirect 与构造器注入）、
    vite 5.4.0 → 7.3.6 + @vitejs/plugin-react 4.3.1 → 5.2.0
    （esbuild dev server 任意请求读取，vite 高危传递依赖），
    audit 清零、前端测试 280 通过、构建通过；
  - 测试：`ci/test_convert_audit_to_gitlab.py` 14 用例（pip/npm 转换、
    阻断判定、豁免机制、服务不可用 exit 2、severity 归一化边界），
    CI 的 deps-python job 每次流水线回归执行；
  - 文档：`docs/静态分析扫描结果查看指南.md` 汇总 5 种工具的报告
    查看入口与误报豁免方式。

- **概览页 issue 详情右边栏（issue #85）**：点击开放 issue 不再直接跳转
  GitLab，改为打开右侧抽屉展示 issue 具体信息与正文；跳转统一走抽屉
  右上角「在 GitLab 中打开」按钮（web_url 新窗口）。
  - 后端 `/api/issues/overview`：issue 对象新增透传 description（Markdown
    正文原样，前端渲染）、author（精简为 name/username）、state、
    created_at（转 UTC 无后缀，与 updated_at 同规则），缺失时均为 None
    兜底，不影响旧版缓存数据；
  - 前端：新增 `components/IssueDrawer.jsx` 详情抽屉——状态徽章 / 作者 /
    创建与更新时间 / 标签胶囊 / 里程碑 / 负责人 / 评论数 + 正文 Markdown
    渲染（复用 issue #27 的 Markdown 组件，空正文显示「暂无描述」占位）；
    关闭方式：右上角 × / 点击遮罩 / Esc 键；列表项标题由 `<a>` 改为
    `<button>` 打开抽屉；样式复用 issue #70 drawer 模式加宽至 640px 容纳
    正文；
  - 测试：TDD 先行（红灯确认后实现），新增 18 用例（后端详情字段透传与
    缺失兜底 6，前端抽屉打开 / 跳转按钮 / 三种关闭方式 / 切换与幂等 /
    旧数据与空正文兜底 12），另更新行为变更断言 4 处（后端 2、前端 2）；
    后端全量 853 通过、前端 280 通过。

- **issue 标签优先级调度（issue #76，方案 C）**：同仓库队列内按 issue 标签
  权重排序派发，默认 bug 最优先，标签顺序可在设置页自定义。
  - 调度器：队内派发改按「标签权重 → issue 更新时间 → task_id」选任务
    （权重 = 任务标签在 `worker.issue_priority` 中首个命中的索引，未命中
    配置标签或无标签排最后）；派发时动态读配置，设置页修改后已排队任务
    即时生效；仓库优先级（issue #51）仍优先于队列内标签权重；
  - 数据层：tasks 表新增 `issue_labels`（JSON 数组）与 `issue_updated_at`
    （UTC 串，GitLab ISO8601 归一化）列（迁移 v6）；webhook 与对账两条
    入队路径均落库（标签以 API 最新状态为准）；
  - 配置：新增 `worker.issue_priority`（默认 `["bug","test","feature"]`），
    设置页「任务调度」卡片可编辑（逗号分隔输入框，保存写回 config.yaml），
    API 校验（非空字符串数组 / 标签名合法 / 无重复，非法回退默认）；
  - 文档：`docs/labels.md` 优先级判定、`docs/设计方案.md` §5.4、README
    架构图与配置表同步；
  - 测试：TDD 先行（红灯确认后实现），新增 38 用例（调度器标签权重排序
    12、webhook/对账标签落库 5、迁移与时间归一化 8、设置 API 8，前端
    设置页 5，另更新迁移版本断言 1 处）；后端全量 847 通过、前端 268
    通过。

- **执行引擎集成 deepseek-harness（issue #84）**：按 issue #74 方案B 以
  Python SDK 进程内调用方式接入第三执行引擎 `dsh`（deepseek-harness 官方
  `deepseek-harness-sdk`，stdio JSON-RPC 驱动捆绑运行时，无需 Node.js）。
  `worker.engine: dsh` 即可切换，与 claude / hermes 共享全部既有设施
  （工作区、超时、停止、日志、SSE 实时流、CI 流水线等待与收尾流程）。
  - 后端新增 `botler/dsh_runner.py`：封装 SDK 生命周期（惰性导入、worker
    线程跑 `harness.run()`、停止/超时经 `close()` 强制终止运行时——语义
    等价 SIGKILL 进程组），SDK 通知映射为 hermes 风格事件行（文本/思考/
    工具调用/回合结束/会话状态），输出协议与 hermes 对齐，SSE 解析与
    回放零改动复用；
  - 后端 `executor.py`：引擎白名单加 `dsh`，新增 `_run_dsh_once`（轮询
    停止/超时）、`_dsh_result` 结果判定（finish_reason=completed 且
    final_response 非空才算成功；max-tokens/error/未知 reason 一律按失败
    重试，不静默成功）；
  - 断点续跑：SDK 在 session_root 持久化会话，tasks 表新增
    `dsh_session_id` 列（迁移 v5），重试/重启以同一会话 id 接续对话；
  - 配置：config.yaml 新增 `dsh` 段（provider/model/max_tokens/
    session_root/cordis/runtime_bin/base_url/api_key），设置页 API 可写
    （api_key 掩码），DeepSeek Key 走环境变量 DEEPSEEK_API_KEY
    （botler 不管理，同 hermes 模式）；SDK 为可选依赖（不在
    requirements.txt），未安装时任务报错并附安装指引（阿里镜像 +
    全版本号，清华源未同步 rc 版）；
  - 环境检测：设置页新增 dsh 项（pip 包检测 + PyPI 最新版本查询）；
  - 文档：新增 `docs/dsh-engine-deployment.md` 部署指南，README 配置表
    同步；
  - 测试：TDD 先行（红灯确认后实现），新增 65 用例（dsh_runner 线程
    模型/停止/超时/事件映射 26、executor 分派/判定/落库/断点续跑 29、
    迁移与环境检测 10，含既有引擎回归保护；另更新迁移版本断言 1 处）；
    全量 806 通过；设置页 API 补 dsh 段（掩码返回 + 校验，另增 8 用例，
    全量 814 通过）。

### Changed

- **全页面响应式布局优化——宽屏单边留白从最多 351px 缩至恒 50px（issue #98）**：
  概览页等所有页面在 1920~2559 视口区间内容区恒为 1840px，视口越宽两侧
  留白越大（2K 屏窗口化视口 2542px 时单边 351px）；4K 屏内容区封顶
  2480px，单边留白 680px。修复方式：宽屏断点（≥1440px）从固定档
  （1920→1840、2560→2480）改为动态跟随视口
  `max(1440px, calc(100vw - 100px))`——≥1540px 视口单边留白恒为 50px，
  1440~1539px 取下限 1440px 平滑过渡无跳变；1440px 以下断点体系（与任务
  表格列隐藏联动，issue #70）保持不变。所有页面共用 `.content` 容器，
  一处改动全局生效。
  - 前端：`styles.css` 宽屏断点动态化并删除 1920/2560 固定档；
    `Tasks.jsx` `contentWidthAt` 同步为 `max(1440, 视口 − 100)`；
  - 测试：三个布局测试文件的断点提取函数支持动态断点形式；
    `overview-responsive-layout.test.mjs` 新增单边留白恒 50px 断言（含
    2542 用户场景、1440/1539/1540 断点边界、4000 超大视口防封顶回退）；
    `tasks-responsive-cols.test.mjs` 与 `tasks-table-fit-content.test.mjs`
    一致性断言视口列表同步扩充。

### Fixed

- **概览页「添加 Issue」弹窗分配人下拉为空（issue #93）**：
  现象为点击仓库卡片「添加 Issue」按钮后，分配人下拉列表为空，但
  GitLab 平台上该仓库有三个可分配成员——排查确认并非 token 权限问题
  （仓库内嵌 token 实测可正常拿到成员清单），而是字段兼容缺陷：后端
  `_trim_member` 硬依赖 members/all 返回项的 `user_id` 字段，而本实例
  GitLab 19.0.1 的 members/all 实际只返回顶层 `id`（成员关系 id，不可
  用作 assignee_ids）与 `username`/`name`，不含 `user_id`——所有成员
  被当作异常元素过滤，form-meta 返回空 members，前端下拉自然为空。
  - 后端 `api/issues.py`：`_trim_member` 放宽为 user_id 缺失但 username
    存在时保留条目（id 暂置 None）；`issue_form_meta` 对这类成员按
    username 调 `client.get_user_id_by_username`（issue #65 已有方法）
    查 /users 补齐真实用户 id，查不到（用户已删除等）的成员剔除——
    下拉不出现无法分配的条目；user_id 存在的成员不发额外查询；
  - 测试：TDD 先行（红灯复现真实 GitLab 成员返回形态），新增 3 用例
    （无 user_id 成员按 username 补齐、查不到时剔除、有 user_id 时零
    额外查询），并更新原有异常元素过滤用例语义；后端全量 886 通过、
    前端全量 313 通过。

- **CI security 门禁两处失效与 venv 安装目标缺陷（issue #86 收尾，
  issue #91 流水线诊断 #850 暴露）**：
  - 门禁失效：`.docs_only_skip` 规则尾条款 `when: always` 会绕过
    stage 失败传播——security 阶段因高危/中危漏洞失败时 build/deploy
    仍照常执行（#846 起 deploy 在 security failed 下仍 success）；
    改为 `when: on_success`（与 GitLab 默认行为一致）。另
    `deploy_to_code01` 的 `needs` 仅依赖 frontend:build，needs 会
    绕过 stage 顺序与失败传播，security 失败不阻止部署；needs 显式
    补全 5 个 security job（任一失败即阻止部署）；
  - venv 安装目标缺陷：uv 只自动发现 `VIRTUAL_ENV` / `.venv`，不会
    使用 `.venv-audit` / `.venv-semgrep` 自定义名——deps-python 的
    pip-audit/pytest 被装进主 `.venv`（随后 `.venv-audit` 报
    No module named pytest），semgrep 则直接报 No virtual environment
    found；改用 `uv pip install --python <venv>/bin/python` 显式指定
    + 安装后 import 验证（shell executor 工作区跨 job 持久，坏 venv
    残留会让 uv 误判已安装而跳过，验证失败自动重建重装）；
  - 测试：本地模拟验证 `--python` 显式安装与坏 venv 重建路径。

- **git clean 权限受限残留导致任务重试耗尽失败（issue #91）**：
  任务 #136（daymark 仓库 issue #7）连续 3 次失败，错误均为
  `git clean 失败 (exit 1): Permission denied`——用户曾以 root 身份在
  local_path 工作区跑过 flutter build，留下 root 属主的
  `linux/flutter/ephemeral_root_bak_20260812/.plugin_symlinks/` 残留
  （内含指向 `/root/.pub-cache` 的符号链接），executor 以普通用户执行
  `git clean -fd` 时删除其中条目受父目录写权限约束而 EACCES，整个
  clean 退出非零 → ExecutorError → 重试耗尽 → 任务失败。
  此类残留不影响 fetch / checkout / reset（只涉及 tracked 文件），
  不应拖垮整个任务。
  - 后端 `executor.py`：`prepare_workspace` 的 clean 步骤改走新增
    `_clean_untracked`——首次 `git clean -fd` 因 Permission denied 失败
    时，列出残留 untracked 条目并尝试 Python 层删除（`_force_remove`：
    删除失败先恢复条目与父目录权限再重试一次），复检 clean 仍权限失败
    则降级为警告继续执行（残留留给用户手动清理），非权限类失败保持
    原行为直接抛错；新增 `_on_rmtree_error` 供 rmtree 恢复权限后重试
    深层条目删除；
  - 测试：TDD 先行（红灯复现与任务 #136 完全一致的错误信息），新增
    3 用例（无写权限残留清理干净、root 属主删不掉时警告放行不阻塞、
    非权限错误仍抛 ExecutorError）；全量 864（后端）+ 284（前端）+
    14（ci 转换脚本）通过。

- **CI security 矩阵三个 job 自上线即失败（issue #91 流水线诊断发现）**：
  issue #86 的 security 门禁从未真实跑通（历史流水线 #846/#847 同样
  failed），两处缺陷：
  - semgrep / deps-python：`.backend_setup` 的 before_script 会
    `cd backend`，venv 实际创建在 `backend/.venv-*`，但脚本
    `cd "$CI_PROJECT_DIR"` 后仍用相对路径 `.venv-semgrep/bin/...` /
    `.venv-audit/bin/...` 引用 → No such file or directory（exit 127）；
    semgrep 的 SARIF 报告输出到仓库根目录，与 artifacts 期望的
    `backend/semgrep-report.sarif` 不符（失败时报告也传不上去）；
  - deps-python / deps-frontend：`set -e` 下转换脚本 exit 2（审计服务
    不可用）直接终止整个 job，「切备用数据源重试 → 仍失败警告放行」
    的循环逻辑从未执行（deps-frontend 日志证实：npmmirror 不可用后
    未尝试官方 registry 即 exit 2 失败）；
  - 修复：semgrep 步骤 3/4/5 统一引用 `backend/.venv-semgrep/...` 且
    SARIF 输出至 `backend/semgrep-report.sarif`；deps-python 步骤 4
    改用 `backend/.venv-audit/bin/python`；两处转换脚本调用改为
    `CONVERT_EXIT=0; ... || CONVERT_EXIT=$?`（|| 左侧失败不触发
    set -e，重试循环得以执行）。

- **任务详情聊天记录中用户提示词被截断、与全局模版不一致（issue #90）**：
  任务详情页聊天记录只显示到「推送后必须用」附近（#114 号任务复现），
  与全局模版比对不完整；根因是展示层两处缺陷——发送给 AI 的提示词本身
  完整（7066 字符，与会话文件首条 user 消息逐字节相等），但聊天记录
  API 对每条消息硬截断到 5000 字符且前端不渲染截断标记，用户无感知；
  「查看提示词」按钮则因后端从不返回 prompt 字段而永远只显示占位文案
  （根因分析存档见 `docs/任务提示词截断诊断.md`，本次实施其方案一）。
  - 后端 `executor.py`：`parse_transcript` 对首条可见 user 消息（渲染后的
    完整提示词）跳过 5000 字符文本截断（后续长消息仍截断并标记
    truncated）；消息数量超上限时截断窗口置顶保留首条 user 行，提示词
    不因只保留最近消息而消失；新增 `read_session_prompt` 读取会话文件
    首条 user 消息全文（提示词不落库，仅存在于会话 jsonl）；
  - 后端 `api/tasks.py`：`GET /tasks/{id}/execution` 响应新增 `prompt`
    字段（首条 user 消息全文，会话文件不可读时为 None）；
  - 前端 `TaskDetail.jsx`：「查看提示词」按钮改为展示 execution 懒加载
    的 prompt 全文（无 prompt 时回退占位文案）；`LiveMsg` 对被截断的
    消息（助手回复 / 工具结果 / 后续用户消息）渲染「内容过长，已截断」
    标记；聊天记录消息数量超上限时显示「仅显示首条提示词与最近消息」
    提示（不再丢弃 transcript_truncated）；
  - 测试：TDD 先行（红灯确认后实现），后端新增 7 用例（首条 user 消息
    不截断与数量截断置顶保留、read_session_prompt 全文/空消息/文件缺失、
    execution prompt 字段契约），前端新增 4 用例
    （`task-detail-prompt-truncation.test.mjs`：截断标记渲染、数量截断
    提示、查看提示词懒加载、无 prompt 回退占位）；全量 861（后端）+
    284（前端）通过。

- **任务手动重试后过几秒又变中断（issue #69）**：一键停止所有任务时
  executor 把 task_id 登记进 `_stop_requests` 内存集合后从未清除，任务
  被停止后用户手动重试，worker 领取任务时 `run_task` 开头的
  `_stop_requested` 检查命中残留的旧停止请求，任务被 `_finish_stopped`
  立即打回 interrupted——表现为「每次手动重试过几秒就变成中断状态」，
  只有平台重启（集合随内存清空）才能逃脱（生产日志 task_93/115 证实）。
  - 后端 `executor.py`：新增 `clear_stop_request`（加锁 discard，幂等）；
    `_finish_stopped` 落终态后消费清除停止请求（防止集合无限膨胀）；
  - 后端 `api/tasks.py`：`POST /tasks/{id}/retry` 重置成功后清除该任务
    的停止请求残留——手动重试即用户明确恢复执行，历史停止请求不再
    影响新一次执行；重试后再次一键停止仍正常生效；
  - 测试：TDD 先行（红灯确认后实现），新增 3 用例（重试清除残留请求、
    停止收尾消费请求、重试后再次停止回归保障）；全量 744（后端）+
    263（前端）通过。

### Added

- **bandit 扫描结果在 GitLab 页面可视化查看（issue #72）**：CI 的
  `security:bandit` job 此前只把扫描结果打在日志里，用户在页面无从
  查看；现扫描时同步输出 SARIF 报告（`-f sarif`，安装
  bandit-sarif-formatter 补齐格式支持）并经 `artifacts:reports:sast`
  上传 GitLab，可在「安全 → 漏洞报告」/流水线 Security 标签/MR 安全
  组件中可视化查看漏洞详情（CWE、文件行号、代码片段），并保留原
  阻断门禁语义（中/高危退出码非 0 阻断流水线）。
  - `.gitlab-ci.yml`：`security:bandit` 新增 SARIF 输出与
    `artifacts:reports:sast` 上传（`when: always`——门禁失败时报告
    同样上传，页面仍能看到被阻断的漏洞），报告同时加入
    `artifacts:paths` 保留原始文件下载（reports 类型文件默认不进
    可下载归档），扫描后从 SARIF 提取摘要打印到 job 日志；
  - 文档：新增 `docs/bandit-扫描结果查看指南.md`（三个查看入口、
    原始报告下载、常见问题），README 安全说明章节同步补充入口。

- **设置页新增 Owner GitLab Token 配置与申请教程（issue #87）**：设置页
  增加 owner gitlab token 设置，该 token 专门用来编辑 issue（写评论/
  打标签），严禁用于推送代码与处理流水线；页面内展示「如何申请一个只有
  处理 issue 权限的 token」教程（折叠区，后端读 docs/ 单一文档来源）。
  - 后端 `config.py`：Settings 新增 `gitlab_owner_token`（空串 = 未配置）；
    KNOWN_FIELDS 新增 `gitlab` 段；`update_gitlab` 掩码值/空串 = 保持现有
    （与 sso.client_secret 同模式，支持 ${ENV} 引用）；
  - 后端 `api/settings.py`：GET 返回 `owner_token_masked`（明文不外流）；
    PUT 支持 `gitlab` 段部分更新（类型校验 400）；新增
    `GET /api/settings/owner-token-guide` 教程端点（缺失 404 前端降级）；
  - 后端 `executor.py`：`_call_with_fallback` 新增 `prefer_owner` 参数 +
    `_owner_gitlab_client`（按 token 值缓存重建）；7 处 issue 编辑调用点
    （处理中/成功/失败/提问的评论与标签）传 `prefer_owner=True` 优先
    owner token，owner 401/403 回退原链路（全局 → remote）；会话
    `GITLAB_TOKEN` 注入优先级改为 owner > remote > 全局。git 推送凭据
    （`_askpass_script`）与流水线操作仍只用 bot token——从代码路径保证
    「严禁推送代码/处理流水线」；
  - 后端 `reconciler.py`：终态标签补打（编辑 issue）同样优先 owner
    token，owner 失效回退原链路；
  - 文档：新增 `docs/GitLab-Owner-Token-申请教程.md`（推荐 Reporter 角色
    低权限账号申请 PAT，从账号层面杜绝越权；含方案对比/安全提示/FAQ）；
    `config.example.yaml` gitlab 段新增 owner_token 注释示例；
  - 前端 `Settings.jsx`：新增「Owner GitLab Token（issue 编辑专用）」卡片
    （GitLab 凭据卡片之前）：密码输入框（掩码占位 + 留空 = 保持现有）、
    独立保存按钮（只提交 `{gitlab:{owner_token}}` 段）、用途边界说明、
    「查看 token 申请教程」折叠区（Markdown 渲染，与 SSO 指南同模式）；
  - 测试：TDD 先行（红灯确认后实现），后端新增 22 用例（API 掩码/保存/
    掩码回传保持/空串保持/非字符串 400/${ENV} 展开/教程端点与 404、
    executor 编辑优先 owner/401/403 回退/默认调用不受影响/未配置无影响/
    _build_env 三级优先级/askpass 不含 owner、成功与失败收尾 prefer 分布、
    reconciler 补打优先 owner 与回退）；前端新增 8 用例（卡片位置、密码
    输入框与保存按钮、提交段隔离、用途边界文案、教程折叠、渲染掩码占位、
    保存交互 PUT 载荷、教程展开渲染）；全量 741（后端）+ 263（前端）通过。

- **概览页开放 Issue 板块美化（issue #71）**：参考 GitLab issue 列表页
  重新设计——每条 issue 左列 `#iid` 灰显 + 标题链接，下方渲染彩色
  标签胶囊（颜色与 GitLab 项目标签一致）与里程碑胶囊，右列显示
  assignee 头像（无头像回退首字符占位）、最后更新时间与 💬 评论数。
  - 后端：`gitlab_client.py` 新增 `list_project_labels`（labels API）；
    `api/issues.py` 聚合时每仓库额外查一次项目标签建 name→color 映射，
    issue 精简字段扩展 labels（带 color/text_color）/milestone（title）/
    assignees（name/username/avatar_url）/user_notes_count；标签颜色
    校验为 6 位 hex 防样式注入，labels API 失败或颜色非法时降级无色
    胶囊（不中断整体、不进 errors——标签色只是视觉增强）；
  - 前端：Overview.jsx issue 项改为 GitLab 列表页两列布局（issue-main
    标签/里程碑胶囊 + issue-side 头像/时间/评论数），无颜色标签走
    中性灰胶囊样式；styles.css 新增 label-pill/milestone-chip/
    assignee-avatar/avatar-fallback 等样式；
  - 测试：TDD 先行（红灯确认后实现），后端新增 4 用例（标签颜色附加、
    labels API 故障降级、非法颜色兜底、美化字段透传）并更新 1 例；
    前端新增 5 用例（彩色胶囊样式、里程碑/评论数、assignee 头像、
    美化字段缺失兜底、无头像首字符占位）；全量 707（后端）+ 248
    （前端）通过。

- **任务页响应式列隐藏与「⋯」抽屉（issue #70）**：任务列表宽度不够时
  按优先级隐藏列（尝试→来源→创建时间→失败原因→用时），有列被隐藏时
  操作列最右侧出现「⋯」按钮，点击弹出右侧抽屉显示该任务全部数据。
  - 前端：Tasks.jsx 新增 `hiddenColumnsForWidth` 纯函数（按内容区
    断点与列宽计算需隐藏的列，窗口缩放实时重算）；被隐藏列保留 DOM
    仅加 `col-hidden` 类（display:none，th:nth-child 列宽规则索引
    不受影响），表格 min-width 同步缩减为剩余列宽总和；「⋯」按钮打开
    `TaskDrawer` 右侧抽屉（kv 表格列出全部 12 字段，缺失显示「—」）；
  - 样式：styles.css 新增 col-hidden 隐藏规则、drawer 抽屉样式，
    并新增 1000/1120/1280/1360 四档 --content-width 媒体查询断点，
    使列隐藏按优先级渐进生效、窄屏下不出现水平滚动条（视口 <851px
    全隐藏后仍装不下时保留 .table-wrap 横向滚动兜底）；
  - 测试：TDD 先行（红灯确认后实现），前端新增 11 用例（纯函数断点
    边界与异常输入、窄/宽视口渲染、⋯ 抽屉交互、字段缺失、SSR 无
    window 全显示、JS 列宽常量与 styles.css 列宽规则一致性防漂移）；
    全量 703（后端）+ 243（前端）通过。

- **概览页板块排序调整（issue #68）**：概览页三个板块自上而下调整为
  开放 Issue → 运行中任务 → CI/CD 流水线（修复前为运行中任务 →
  CI/CD 流水线 → 开放 Issue，开放 Issue 板块在最底部不醒目）。
  - 前端：Overview.jsx 中「开放 Issue」板块（issues-section）整体
    上移至页面顶部（h1 之下），运行中任务区居中、流水线板块保持
    最后；数据拉取与轮询逻辑不变，仅调整 JSX 展示顺序；
  - 测试：TDD 先行（红灯确认顺序不符后实现），前端新增 7 用例
    （源码级板块顺序断言 + 渲染级三板块顺序断言，覆盖无任务 /
    无流水线 / 无 issue / 接口全失败等空板块边界）；全量 703
    （后端）+ 232（前端）通过。

- **「等待用户决策」提问反馈到 issue（issue #67）**：无人值守执行中
  Claude 停在需要用户决策的提问节点（如 fix-bug 流程评估多方案后
  提问「请选择 A 或 B」）时，提问不再只留在终端输出里——平台检测到
  该信号后把提问原文贴到 issue 评论、打 `blocked` 标签（不在领取
  过滤标签中），任务判 failed（未完成），用户回复后经重新指派/
  对账扫描再次入队继续处理。
  - 检测：`DECISION_QUESTION_RE` 匹配选项型提问引导（请选择 A 或 B /
    请回复 1 或 2 / 请问……？等），只认最终回复结尾 400 字符内的
    信号（中途提到不算）；再与「查不到该 issue 的引用提交」（无实际
    交付）双重确认，避免误伤正常完成任务（完成汇报的礼貌收尾
    「请确认后关闭本 issue」不在信号之列）；有提交时不判等待决策，
    走既有流水线等待路径；
  - 收尾：新增 `_finish_asked`——任务 failed（error_message 说明等待
    用户决策）、评论包含提问原文（`_extract_question` 从最后一个提问
    信号所在行截取）、打 `blocked` + 移除 `in-progress`、网页通知；
    claude / hermes 两引擎共用（接入 `_await_task_pipeline` 的
    「无提交」分支，两引擎成功判定都经过该入口）；
  - 附带修复：`GitLabClient.add_labels` 支持 `remove` 参数（同一次
    PUT 移除标签），成功/失败/提问收尾打终态标签时一并移除
    `in-progress`（修复前收尾后 issue 上 `in-progress` 与
    `bot-done`/`bot-failed` 并存，如生产 issue #66）；
  - 测试：TDD 先行（红灯复现生产任务 #90 的缺陷），后端新增 5 用例
    （提问+无提交 → failed+评论+blocked；正常完成/有提交不误伤；
    提问信号正则）；全量 703（后端）+ 225（前端）通过。

- **概览页开放 issue 聚合板块（issue #64）**：概览页新增「开放 Issue」
  板块，聚合读取所有已启用仓库的开放（opened）issue，按仓库优先级
  排序（外层），仓库内按最后更新时间降序（内层）。
  - 后端：新增 `GET /api/issues/overview`（api/issues.py）——遍历
    `list_repos()`（已按 priority, id 排序、过滤软删除，issue #62）
    中的已启用仓库，复用 pipelines 模块的 per-repo client 缓存
    （issue #60：remote 内嵌 token 优先、无 token 回退全局 bot
    token），单仓库失败进 errors 不中断整体（HTTP 200），结果带
    10 秒 TTL 缓存；每仓库最多 100 条（limit 防大仓库翻页打爆
    GitLab API），issue 的 updated_at 统一转 UTC 无后缀（复用
    pipelines._commit_time_utc，与前端 fmtAgo 解析约定一致）；
  - 客户端扩展：GitLabClient.list_open_issues 新增 order_by/sort/
    limit 可选参数（服务端按 updated_at 降序 + 条数截断，_paged
    支持 limit 停止翻页），不传时行为与扩展前一致（reconciler
    等既有调用不受影响）；
  - 前端：Overview.jsx 新增板块（15 秒轮询，与流水线板块同频），
    按仓库分组卡片展示（仓库名 + 优先级徽章 + 每仓库 issue 数），
    组内每条 issue 渲染 #iid、标题链接（web_url 新窗口打开）与
    最后更新时间（fmtAgo 相对时间）；无仓库 / 全部仓库无开放
    issue 时显示空状态；单仓库查询失败明细照常展示不影响整体；
  - 测试：TDD 先行（红灯确认后实现），后端新增 24 用例（聚合排序
    两级 / 未启用与软删除过滤 / 失败兜底 / 缓存 TTL / limit 截断 /
    参数透传 / per-repo token），前端新增 8 用例；全量 699（后端）
    + 225（前端）通过。

- **概览页流水线按仓库使用各自 token（issue #60）**：概览页获取流水线
  状态的 token 不再统一用全局 bot token，而是从各仓库本地目录
  `git remote -v` 输出的 URL 中解析——每个仓库使用自己 remote url
  内嵌的 token，仓库间互不相同；仓库 URL 展示统一脱敏。
  - 后端：概览页对每个仓库解析本地目录（local_path 优先，否则
    workspace/&lt;仓库名&gt;，与执行工作区一致）的 remote url，按
    remote_name（缺省 origin）取内嵌 token 建 per-repo GitLabClient
    （host 取 remote url 的 host:port，verify_ssl 沿用全局配置）；
    remote 无 token / 本地目录不存在 / 非 git 仓库时回退全局 bot
    token（兼容旧仓库）；per-repo client 60 秒 TTL 缓存（避免每轮
    轮询重复跑 git 子进程与重建 httpx client，token 轮换 60 秒内
    自动生效）；per-repo client 网络异常（httpx.HTTPError）同样
    进 errors 列表不中断整体；
  - 脱敏：git_remote 新增 `parse_remote_url`（解析 user:token@host，
    支持 URL 编码、token 含 @/冒号等边界）与 `mask_url_token`
    （脱敏为 user:***\@，幂等）；`GET /api/repos`、`POST /api/repos`、
    `PUT /api/repos/{id}`、`POST /api/repos/discover` 响应中的
    URL 统一脱敏；DB 与 config.yaml 仍保存真实 URL（clone 需要）；
    update 回传掩码 url（含 \*）时忽略该字段，防止把掩码写回
    DB（与 sso client_secret 掩码模式一致）；
  - 测试：TDD 先行（新增用例红灯确认后实现），后端新增 32 用例
    （URL 解析 9 + 脱敏 7 + per-repo client 7 + API 集成 4 + 仓库
    API 脱敏 5），全量 640（后端）+ 217（前端）通过。

- **任务列表页新增刷新按钮（issue #59）**：工具栏新增「↻ 刷新」按钮，
  点击重新拉取任务列表，更新所有任务的显示状态。
  - 背景：页面仅在存在活跃任务时每 5s 自动轮询，全部任务结束后轮询
    停止，列表状态可能陈旧；手动刷新补上这一缺口；
  - 低危操作无需确认；请求中按钮禁用防重复点击（文案变「刷新…」）；
    保持当前筛选（状态/仓库/搜索）与页码不变；接口失败展示错误提示；
  - 测试：前端新增 7 用例（源码断言 / 渲染可用 / 重新拉取 / 筛选保持 /
    防重复点击 / 失败提示），后端无改动。

- **CI/CD 接入 bandit 安全扫描（issue #58）**：流水线最前置安全门禁，
  存在高危/中危漏洞时阻断整条流水线，放在所有阶段之前。
  - GitLab 侧：新增 `security` stage（stages 首位）+ `security:bandit`
    job——复用 `.backend_setup`/`.docs_only_skip` 公共配置，扫描
    `botler` 包与 `hermes_runner.py`，`--severity-level medium`
    （高危/中危即 job 失败，后续 build/deploy/sync 全部不运行），
    `allow_failure: false` 显式声明阻断语义；bandit 装进持久化
    .venv，版本不 pin（新规则本应发现新漏洞）；
  - GitHub 侧：新增 `bandit-scan` job（jobs 首位），
    `frontend-build`/`backend-test` 加 `needs: bandit-scan` 门禁——
    bandit 失败时两者跳过、workflow 失败，与 GitLab 语义一致；
  - 误报处理：database.py 4 处动态列名拼接加 `# nosec B608`
    （列名均来自模块级常量白名单 `_TASK_FIELDS`/`allowed`/固定元组，
    非外部输入，SQL 列名无法参数化）；
  - 验证：本地 bandit 实测 4 个 Medium 告警（B608）处理后清零，
    exit 0；注入高危/中危漏洞时 exit 非 0 阻断（见 issue 评论）。

- **仓库设置编辑与调度优先级（issue #51）**：仓库页面每项新增「设置」
  按钮，弹窗可重新编辑显示名称、启用状态与新增的优先级字段（URL /
  本地路径不在编辑范围，涉及 webhook 重注册风险高）；列表展示各仓库
  优先级并按优先级排序。
  - 优先级为整数 1~999，默认 100，数字越小越优先；多个仓库同时有
    排队任务时调度器优先派发优先级高（数字小）的仓库，相同优先级按
    任务提交时间（tasks.created_at）排序，同仓库内仍保持 FIFO；
  - 后端：repos 表新增 priority 列（v3 迁移，存量默认 100）、
    `POST/PUT /api/repos` 支持 priority（1~999 校验）、config.yaml
    同步落盘；scheduler 派发选择改为按 (priority, 队首任务提交时间,
    repo_id) 排序；
  - 前端：新增 RepoEditModal 弹窗组件（名称/启用/优先级），保存前
    前端校验（名称必填、优先级 1~999 整数），失败展示后端错误；
  - 测试：后端新增 22 用例（调度优先级 6 + 迁移/数据库 5 + API 7 +
    回归更新 4），前端新增 7 用例；全量 608（后端）+ 210（前端）通过。

- **任务执行过程实时输出（事件流，SSE 推送）**：像在终端跑 Claude Code
  一样，任务页面逐事件实时看到引擎输出——模型文本、thinking（可折叠）、
  工具调用与工具结果、执行结果，边跑边出；任务结束后可回放完整事件流。
  - 后端：claude 引擎切 `--output-format stream-json --verbose`（逐行
    NDJSON 实时 flush；init 行即拿到 session_id，比原 result 更早落库）；
    hermes runner 注册 hermes-agent 自带回调（thinking/tool_start/
    tool_complete/stream_delta/status），stdout 改为 NDJSON 事件流
    + 最后一行结果 JSON（旧单行协议向后兼容）；
  - 新增 `botler/events.py`：引擎输出行 → 归一化事件解析
    （status/thinking/text/tool/tool_result/result）+ 进程内事件总线
    （EventBus，executor 发布 → SSE 订阅，队列满丢最旧不阻塞读流）；
  - 新增 `GET /api/tasks/{id}/events`（SSE）：连接先回放日志文件已有
    事件（先订阅再回放，回放间隙的实时事件在队列积累无缝衔接），再实时
    推送总线事件，终态后 done 收尾；断线重连自动补齐且不重复（前端按
    seq 去重）；
  - 任务详情页「实时输出」区替换为事件流面板（@tanstack/react-virtual
    虚拟滚动，thinking 默认折叠）；聊天记录面板并行保留；概览页任务卡片
    实时输出改走 SSE（每活跃任务一个连接，最后 N 行自动滚，保留
    trimLogTail 截尾）；
  - 成功判定适配：stream-json 多行输出下按尾部 result 行判定（首个 JSON
    对象是 init 行，不能作为成功依据）；错误提取同样取 result 行；
  - 测试：后端新增 67 用例（test_events 32、test_executor_stream 8、
    test_hermes_runner_stream 5、test_api_events 7，另回归更新）+ 前端
    新增 6 用例（SSE 封装 3 + 详情页事件流 3）+ overview 测试更新为
    SSE 断言；全量 591（后端）+ 188（前端）通过。

- **任务页面增加翻页组件（issue #50）**：任务列表页此前固定只显示
  最近 50 条（「最多显示 50 条」），更早的历史任务无法在页面浏览。
  现利用后端已有 limit/offset 分页能力，页面底部新增翻页组件：
  上一页/页码数字/下一页 + 当前页信息（`第 X / Y 页`），每页 50 条；
  状态/仓库/搜索筛选变化自动重置回第 1 页，5s 自动刷新保持当前页；
  单页（total ≤ 50）与空列表不渲染翻页组件。
  - 后端 `database.py`：`count_tasks` 扩展 `repo_id`/`search` 过滤
    （与 `list_tasks` 过滤一致），`api/tasks.py` 的 `total` 随之跟随
    当前筛选——否则筛选后 total 偏大、总页数错误；
  - 前端 `Tasks.jsx`：新增 `page` 状态与 `offset` 请求参数、导出
    `pageNumbers` 页码窗口函数（≤7 页全显示，多页显示首尾 + 当前 ±1 +
    省略号）；`styles.css` 新增 `.pagination` 样式；
  - 测试：`test_api_tasks.py` 新增 4 用例（total 跟随 repo/search/
    组合筛选、无匹配为 0）+ `tasks-pagination.test.mjs` 12 用例
    （翻页渲染与禁用态、点击翻页 offset 正确、筛选重置、单页/空列表
    隐藏、pageNumbers 边界）。

- **本地环境检测增加 hermes 检测（issue #48）**：设置页「本地环境检测」
  卡片新增 Hermes Agent 项（`hermes --version` 检测安装与版本）。hermes 为
  git 安装的内部 agent，无 npm/GitHub 公开发布源，不查最新版本
  （前端显示 "—"）。
  - 后端 `environment.py`：TOOLS 清单新增 hermes 项；前端/API 无需改动
    （环境检测卡片通用渲染 tools 列表）。
  - 测试：`test_environment.py` 新增 TestHermesTool 6 用例（清单配置 /
    真实版本输出格式 "Hermes Agent v0.20.0 (2026.8.3)" 提取 / 安装与
    未安装检测 / 无发布源不发网络请求 / 整体检测包含 hermes）。

- **任务执行引擎支持 hermes-agent（issue #47）**：`worker.engine` 可切换
  `claude`（Claude Code CLI，默认，现网行为不变）/ `hermes`（部署机已装好的
  hermes-agent，直接调用，botler 不打包不管理 hermes 的 LLM 配置）。
  - 后端 `hermes_runner.py`（新增）：独立 runner 脚本，由 hermes venv 的
    python 运行（`hermes.command`/`args` 配置），stdin/stdout JSON 协议
    （prompt/history/session_id → final_response/messages/session_id/error），
    进程内 `run_agent.AIAgent(quiet_mode=True)` 执行一次任务；
  - `executor.py`：`_run_once` 按引擎分派——hermes 引擎经 `TERMINAL_CWD`
    在仓库工作区执行 terminal 命令、git 凭据沿用 `GIT_ASKPASS` 注入；
    子进程读循环抽取 `_drain_process_output` 两引擎共用；结果判定
    `_hermes_result`（success/unresolvable/failed，自认无法解决不重试）；
  - 断点续跑等价实现（Q3-B）：执行结束把会话历史落库
    `tasks.hermes_history`（新增列，轻量迁移），重试/重启恢复时作为
    `conversation_history` 传入并保留工作区；损坏数据自动降级全新会话；
  - 成功收尾共用 `_await_pipeline_and_finish_succeeded`（CI 流水线等待 +
    bot-done + commit 记录，与 claude 引擎一致）；
  - 设置页无 hermes 配置（配置只走 config.yaml 文件，hermes 的模型/Key
    在 hermes 侧 `~/.hermes` 配好）；新增部署文档
    `docs/hermes-engine-deployment.md`（挂载、配置、断点续跑、故障排查）。
  - 测试：`test_hermes_runner.py` 17 用例（协议解析边界 / AIAgent 调用契约
    / import 失败与 agent 异常降级）+ `test_executor_hermes.py` 22 用例
    （引擎分派与非法值回退 / 命令与环境构造 / 历史落库与恢复 / 损坏降级 /
    结果判定），runner 已在真实 hermes venv 冒烟验证；后端全量 521 passed +
    前端全量 167 passed。

### Changed

- **docs-only 提交跳过 CI/CD 流水线（issue #57）**：此前任何 main 分支
  push 都会触发完整流水线（前端构建 + 后端测试 + 部署），即使提交只改了
  文档（README / CHANGELOG / docs/ 等）也会白跑 5 分钟以上的 runner
  资源。现改为：仅文档变更的提交跳过构建/测试/部署，只保留 GitHub
  镜像同步（docs 变更也需及时镜像）。
  - GitLab（`.gitlab-ci.yml`）：新增公共规则 `.docs_only_skip`
    （frontend:build / backend:test / deploy_to_code01 三 job 复用）——
    rules 按顺序求值，第一条「代码/配置白名单」优先于第二条「docs 跳过」，
    保证代码提交通常伴随的 CHANGELOG.md 变更不会把混合提交误判为
    docs-only；docs-only 判定仅对 main 分支 push 生效，其他分支 /
    MR / tag / schedule 维持原行为；
  - GitLab（sync_to_github）：docs-only 提交仍同步镜像，但按
    `CI_COMMIT_BEFORE_SHA..CI_COMMIT_SHA` 的变更文件判断跳过
    workflow_dispatch 触发（GitHub push 事件已被下方白名单拦截，
    dispatch 是补充入口，docs-only 时一并跳过避免白跑）；
  - GitHub（`.github/workflows/ci.yml`）：push 触发增加 paths 白名单
    （backend/frontend/deploy/scripts 等代码路径），docs-only 的镜像
    push 不再触发 Actions，与 GitLab 侧使用同一套「代码文件」定义；
  - 验证：GitLab API `ci/lint` 通过；本地全量测试通过；流水线实测
    ——代码提交跑全套 success，后续 docs-only 提交流水线只跑 sync
    （build/deploy skipped）。

- **模版页面默认全部展开，折叠方式对齐任务详情页聊天记录（issue #56）**：
  提示词模版页此前（issue #55）默认折叠为 6 行小窗口，每次进入页面都要
  点「展开全部」才能看到完整模版。现改为默认全部展开：textarea 高度
  自适应内容行数完整展示、无内层滚动条（滚动交给页面最外层，与
  issue #52 交互原则一致）；折叠方式改成与任务详情页聊天记录一致的
  标题行切换——「模版内容（N 行）」标题行带 chevron ▾/▸ 可点击
  折叠/展开，折叠时编辑器与操作按钮整体隐藏（非截断小窗口），再次
  展开内容不丢失。
  - 前端 `Templates.jsx`：`expanded` 默认改为 `true`；移除
    「展开全部（N 行）/收起」按钮，新增 `section-toggle` 风格标题行
    （chevron + 行数提示，`aria-expanded` 同步），折叠态不渲染
    textarea 与操作按钮行；
  - 前端 `styles.css`：移除不再使用的
    `.input.textarea.template-collapsed` 截断样式；
  - 测试：重写 `templates-collapsible-editor.test.mjs` 4 用例（默认展开
    rows 自适应 / 标题行折叠隐藏编辑器与保存按钮、再展开内容不丢失 /
    空模版与单行模版边界 / styles.css 无 template-collapsed 残留）；
    前端全量 203 passed + 后端全量 591 passed + vite build 成功。

- **任务详情页滚动与折叠优化（issue #52）**：详情页只保留页面最外层的
  垂直滚动条；事件流、聊天记录、执行日志、claude 输出尾部四个区块取消
  各自内部的垂直滚动条、内容直线完整展示，标题改为可点击折叠/展开
  （默认展开，事件流/聊天记录/执行日志为带 chevron 的标题按钮，
  claude 输出尾部沿用「展开/收起」按钮）。
  - 前端 `TaskDetail.jsx`：事件流移除 `@tanstack/react-virtual`
    虚拟滚动改为全量渲染（与「完整展示」需求一致，长事件流不再受
    420px 内部滚动窗限制）；新增 `SectionToggle` 折叠标题组件与
    `showEvents/showChat/showLogs` 状态；提示词与 claude 输出尾部
    加 `log-view-flat` 类取消内部滚动（概览页卡片不受影响）；
  - 前端 `styles.css`：`.event-list`/`.chat-list` 移除固定高度与
    `overflow-y`，`.log-list` 仅保留超长行横向滚动，新增
    `.section-toggle` 系列样式；清理不再使用的 `.live-log` 死样式；
  - 依赖：`@tanstack/react-virtual` 不再使用，已从 package.json 移除；
  - 测试：新增 `task-detail-collapsible-sections.test.mjs` 6 用例
    （三区块标题按钮默认展开 / 折叠隐藏后再次点击恢复 / claude 输出
    尾部按钮切换 / 120 条事件全量渲染无截断）；前端全量 194 passed +
    后端全量 591 passed。

### Fixed

- **概览页开放 Issue 长标题溢出遮挡评论数与时间（issue #81）**：概览页
  开放 Issue 列表的标题链接 `.issue-link` 是 inline `<a>`，inline 元素上
  `overflow:hidden` 与 `text-overflow:ellipsis` 不生效，长标题直接横向
  溢出容器，遮挡右侧的评论数与最后更新时间。
  - 前端 `styles.css`：`.issue-link` 显式声明 `display:block` 使省略号
    截断生效（保留 overflow/ellipsis/nowrap）；`.issue-main` 补
    `flex:1` 占据右侧元信息外的全部宽度（保留 `min-width:0`），
    右侧 `.issue-side` 保持 `flex-shrink:0` 不被压缩；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增
    `overview-issue-overflow.test.mjs` 2 用例（源码断言 issue-link
    块级 display + 溢出三件套、issue-main flex:1 + min-width:0 与
    issue-side flex-shrink:0）；前端全量 255 + 后端全量 719 通过。

- **任务完成后任务报告没有输出到 issue 评论（issue #79）**：任务成功
  收尾只由平台打 bot-done 标签，结果评论依赖任务会话内 Claude 按模板
  自行留言；全局 bot token 失效期间 Claude 侧 API 401 失败，任务
  succeeded、bot-done 已打，但 issue 上没有任何报告评论。现改为平台
  兜底：成功收尾时检查最后一条非系统评论的作者——是 bot 本人（含
  remote token 账号，即 Claude 已用兜底 token 留过）则跳过，否则从
  执行输出提取结果摘要（claude 的 result / hermes 的 final_response，
  超长按 3000 字符截断）写一条完成报告评论（含提交 sha 与确认提示）；
  检查/写评论失败均不阻塞任务成功（仅记 warn，与打标签一致）。
  - 后端 `executor.py`：`_finish_succeeded` 新增 `_leave_success_comment`
    平台兜底评论（防重集合 = 配置 bot_id + 调用客户端的 /user id，
    经 `_call_with_fallback` 走 per-repo remote token 兜底）；新增
    `_success_summary` 提取两引擎结果摘要；`_build_env` 注入会话
    `GITLAB_TOKEN` 时优先取仓库 remote url 内嵌 token（新增
    `_task_gitlab_token`，remote 无 token / 解析失败回退全局 token），
    使全局 token 失效期间 Claude 会话内的 API（读 issue/写评论）仍可用；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增 9 用例（无人评论
    时平台写报告评论、bot 已评论跳过、remote token 账号已评论跳过、
    写评论失败不阻塞成功、hermes final_response 摘要、条件终态不评论、
    remote token 注入 GITLAB_TOKEN / 无 token 回退全局 / 无 remote
    回退全局），并更新受影响测试的 mock；后端全量 719 + 前端全量
    253 通过。

- **添加本地仓库报 401「无法识别项目: token 无效或已过期」（issue #77）**：
  本地文件夹方式添加仓库时，识别项目与注册 webhook 一律走平台全局
  bot token——全局 token 失效期间，即使本地仓库 remote url 里内嵌了
  有效 token（用户 git pull/push 正常），添加仓库仍直接报 401。
  现与 executor / reconciler 的 per-repo 兜底模式对齐：识别项目遇
  401/403 且 remote URL 内嵌 token 时，用该 token 与 remote host 构建
  临时 client 重试；webhook 注册同理（识别已兜底则复用同一 client，
  全局注册遇 401/403 也兜底重试）。remote 无内嵌 token 或重试仍失败
  时保持原有错误信息（非认证类错误不兜底，原样抛出）。
  - 后端 `git_remote.py`：新增 `build_client_from_url`（从单个 remote
    URL 解析内嵌 token 构建 GitLabClient，添加场景无仓库行可用时的
    兜底入口，`webhook_base_url` 透传保证回调地址始终是平台地址）；
  - 后端 `api/repos.py`：`add_repo` 保存原始 remote URL（识别成功后
    url 会被替换为 API 返回的干净 url，兜底解析 token 必须用原始值）、
    webhook 回调地址计算提前；识别与 webhook 注册两个 401/403 点均
    按上述逻辑兜底；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增 3 用例
    （全局 401 时用 remote token 识别成功且 webhook 注册同 token、
    remote 无 token 时保持 400 原错误、识别成功但注册 401 时单独
    兜底）；后端全量 710 + 前端全量 253 通过。

- **仓库管理页添加仓库方式选项顺序调整（issue #73）**：此前「添加仓库」
  表单中「GitLab URL / project_id」排在第一个选项、「本地文件夹（读取
  git remote）」排在第二个，而默认选中的方式是本地文件夹——视觉顺序
  与选中态脱节，用户容易误选。现调整为：本地文件夹（读取 git remote）
  为第一个选项（默认选中，展开本地路径输入框与 remote 选择），
  GitLab URL / project_id 为第二个选项。
  - 前端 `Repos.jsx`：交换两个 `add-method` radio 选项的渲染顺序，
    默认方式仍为 `'local'`，与第一个选项一致；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增
    `repos-add-method-order.test.mjs` 5 用例（源码级选项顺序 /
    渲染级选项文本顺序 / 第一个选项默认选中、第二个未选中 /
    默认展开本地表单不渲染 URL 输入框 / 交互切换后表单随之切换）；
    前端全量 253 + 后端全量 707 通过。

- **全局 token 失效后任务领取/留评论全 401：executor 与 webhook 补上
  remote token 兜底（issue #65 补充）**：全局 bot token 被撤销期间，
  executor 全部 GitLab 操作（任务领取 get_issue、「处理中」评论、
  失败评论、bot-done/bot-failed 标签、流水线等待、提交查询）只走
  全局 client——任务 1 秒内即失败（生产任务 #88/#89），issue 上
  收不到任何评论；webhook 的 issue 查询与最后发言人查询 401 同样
  直接拒绝入队，事件只能等对账兜底补入队（实时性受损）。现与对账
  对齐：401/403 时按仓库 remote url 内嵌 token 构建 per-repo client
  重试一次，remote 无可用 token 或重试仍失败才报错（非认证类错误
  不兜底，原样抛出）。
  - 后端 `executor.py`：新增 `_call_with_fallback`（镜像对账
    `_call_with_fallback` 模式），应用到 get_issue、领取评论、
    `_issue_state`、`_await_task_pipeline`/`_wait_pipeline_for_commit`
    （流水线探测与轮询）、`_finish_succeeded`（bot-done 标签）、
    `_finish_failed`（失败评论 + bot-failed 标签，且评论失败不再
    阻断打标签）、`_record_commit`（提交查询）；
  - 后端 `webhook.py`：新增 `_repo_client`（按仓库 remote 解析兜底
    客户端）与 `_call_with_fallback`，issue 查询与最后发言人查询
    遇 401/403 时用 remote token 重试；`_repo_bot_ids` 复用
    `_repo_client`；
  - 测试：TDD 先行（复现测试红灯：run_task 领取 401 任务立即失败、
    `_finish_failed`/`_finish_succeeded` 401 评论标签落空、webhook
    401 拒绝入队），新增用例 8（executor 6 + webhook 2），后端全量
    675（+8）+ 前端 217 通过。

- **对账扫描为 0：全局 token 失效后 bot 身份漂移漏扫新 issue（issue #65）**：
  全局 bot token 被撤销后，对账降级以仓库 remote 内嵌 token 的账号
  （project access token 账号，如 project_123_bot / code01）作为 bot
  身份，而用户把新 issue 分配给 @agent——两者 id 不一致，
  `assignee_id` 过滤后扫描为 0，且 API 正常返回（无任何权限报错），
  新 issue 被静默漏扫。现改为 remote URL userinfo 的用户名（如 agent）
  也作为 bot 身份候选：对账以「remote token 账号 + remote 用户名对应
  账号」身份集合分别扫描、按 iid 去重合并；webhook 全局身份获取失败
  （401）时不再直接 500，同样按仓库 remote 身份集合判定 assignee。
  - 后端 `git_remote.py`：新增 `build_repo_client_with_username`
    （返回 (client, remote_username)，`build_repo_client` 改为其薄
    封装，概览页调用不变）；
  - 后端 `gitlab_client.py`：新增 `get_user_id_by_username`（按用户名
    查用户 id，用户不存在返回 None）；
  - 后端 `reconciler.py`：`_reconcile_repo` 全局 bot 身份不可用时，
    以身份集合（remote token 账号 + remote URL 用户名对应账号）分别
    扫描合并；「最后发言人是 bot」判定改用身份集合；
  - 后端 `webhook.py`：`get_bot_id` 401 时降级为 `_repo_bot_ids`
    （仓库 remote 身份集合），assignee 与最后发言人判定同样用集合，
    不再抛 500；
  - 测试：TDD 先行（复现测试红灯：身份漂移场景 scanned=0 无报错、
    webhook 直接抛 401），新增用例 6（对账扫到 remote 用户名账号
    assignee 的 issue / 用户名查无此人只扫 token 账号 / 多身份重复
    assignee 去重 / webhook 全局身份失效降级入队 / 身份不可用拒绝 /
    assignee 不匹配不误领取），更新 issue #63 用例 6 处
    （`build_repo_client` monkeypatch 改为新函数）；后端全量
    667（+6）通过，前端 217 全量通过。

- **仓库对账遇 token 失效时用 remote url 内嵌 token 兜底（issue #63）**：
  对账（定时兜底 + 仓库页「对账」按钮）完全依赖全局 bot token；全局
  token 失效（401）后 `get_bot_id` 失败即整体放弃，各仓库 remote url
  中明明有可用 token 也不尝试。现改为对账遇 401/403 时，从该仓库
  本地目录的 `git remote -v` URL 提取内嵌 token 构建 per-repo
  GitLabClient 重试（与 issue #60 概览页同一机制）。
  - 后端 `git_remote.py`：新增公共函数 `build_repo_client`（原
    `api/pipelines.py` 私有逻辑提取），概览页与对账共用；工作区根
    目录常量化 `_WORKSPACE_ROOT`（backend/botler/workspace，与
    executor 一致）；
  - 后端 `api/pipelines.py`：`_repo_client` 改用公共函数，删除
    本地重复实现；
  - 后端 `reconciler.py`：全局 bot 身份获取失败不再整体放弃对账，
    降级为逐仓库兜底；新增 `_call_with_fallback`（GitLab 调用遇
    401/403 且当前仍是全局 client 时，用 remote token client 重试
    一次，已兜底过不重复兜底）与 `_reconcile_repo`（单仓库扫描：
    `list_open_issues` / `last_note_author_id` 与终态标签补打的
    `get_issue` / `add_labels` 全部走 fallback；全局 bot 身份不可用
    时以 remote token 账号的 user id 作为该仓库 bot 身份；remote
    无可用 token 则记错跳过，行为不变）；
  - 测试：TDD 先行（复现测试红灯：全局 401 时对账直接失败），新增
    用例 6（全局 401/403 兜底补入队 / 无 remote token 记错 /
    全局 bot 身份不可用改用 remote token 账号身份 / 补打标签 401
    兜底 / 兜底也失败报错），更新 1 处（workspace 目录断言随
    `_WORKSPACE_ROOT` 提取调整）；后端全量 661（+6）通过，前端
    217 全量通过。

- **页面上删除仓库后列表仍显示该仓库（issue #62）**：`DELETE
  /api/repos/{id}` 返回 200，但 `GET /api/repos` 仍返回已删除的仓库
  （以「已停用」身份残留，可被重新「启用」成 webhook 已注销、config
  已移除的状态分裂仓库）。根因：「删除」与「停用」共用 `enabled`
  字段软删除，仓库列表接口对软删除行无任何过滤。
  - 后端 `database.py`：repos 表新增 `deleted_at` 列（迁移 v4，
    user_version 3→4）；新增 `soft_delete_repo`（写 `deleted_at`
    标记 + enabled=0，行保留供任务历史解析仓库名）；`list_repos`
    与 `get_repo_by_project_id` 增加 `include_deleted` 参数默认
    过滤已删除行（仓库列表、概览流水线、对账全局生效）；`upsert_repo`
    冲突更新时清除 `deleted_at`（支持删除后重新添加同仓库），
    id 改为按唯一键反查（冲突更新路径 lastrowid 不可靠，重新添加
    路径暴露）；
  - 后端 `api/repos.py`：`delete_repo` 的 db 软删除改调
    `c.db.soft_delete_repo`，与「停用」（enabled=False、行仍可见
    可重新启用）区分；
  - 后端 `api/tasks.py`：任务列表的仓库名映射改用
    `include_deleted=True`，已删除仓库的历史任务仍能显示仓库名；
  - 测试：TDD 先行（复现测试红灯：删除后列表仍含该仓库），新增
    用例 9（API 删除后列表不返回 / 停用仓库仍列表 / 删除后重新添加
    同仓库成功 + 迁移 v4 补列 + 软删除过滤/标记/反查/重新添加清标记），
    更新 3 处既有断言（deleted_at 标记、user_version 4）；后端全量
    655（+9）通过，前端 217 全量通过。

- **删除仓库报错 500（issue #61）**：仓库页删除仓库时后端 500，
  配置移除与软删除均未执行。根因：config 模块重构后
  `c.config.get()` 返回纯数据对象 `Settings`，而 `delete_repo`
  仍把它当 `ConfigManager` 调用其不存在的 `update_repos` 方法
  （`AttributeError`），该端点自重构后即无法删除任何仓库。
  - 后端 `config.py`：`ConfigManager` 新增领域方法 `remove_repo`
    （project_id）——重读磁盘、过滤掉指定仓库、落盘并刷新内存
    settings（与 `update_repos` 等 update_* 系列同一模式，避免
    覆盖并发的手动编辑）；
  - 后端 `api/repos.py`：`delete_repo` 改为调用
    `c.config.remove_repo(row["gitlab_project_id"])`，删除链路的
    config 移除逻辑内聚到 ConfigManager；
  - 测试：TDD 先行（复现测试红灯 `AttributeError` 与生产日志一致），
    新增 API 用例 3（删除成功含 webhook 注销/config 移除/db 软删除、
    404、webhook 注销失败不阻塞）+ config 层用例 3（落盘与内存刷新、
    缺失 id 幂等、保留并发手动编辑），后端全量 646（+6）通过。

- **全局模版取消内层的垂直滚动，改成折叠的方式（issue #55）**：提示词
  模版页的 textarea 此前固定 18 行，全局默认模版（issue-agent 完整提示词）
  远超 18 行，内容在 textarea 内部滚动查看（内层垂直滚动条）。现改为折叠
  方式：默认折叠为 6 行小窗口（overflow hidden 裁剪、无滚动条），按钮
  「展开全部（N 行）」展开后 textarea 高度自适应内容行数、完整展示，
  滚动交给页面最外层（与 issue #52 交互原则一致）；「收起」恢复小窗口，
  内容不丢失。
  - 前端 `Templates.jsx`：新增 `expanded` 状态（默认折叠）；textarea
    `rows` 折叠态 6 / 展开态内容行数 +1，折叠态追加
    `template-collapsed` class；保存按钮行新增「展开全部（N 行）/收起」
    切换按钮（`aria-expanded` 同步）；
  - 前端 `styles.css`：新增 `.input.textarea.template-collapsed`
    （`overflow: hidden; resize: none`）；
  - 测试：新增 `templates-collapsible-editor.test.mjs` 3 用例（展开态
    rows 自适应 40 行内容 / 默认折叠 + 展开收起往返切换内容不丢失 /
    折叠态样式 overflow hidden）；`tests/helpers/mock-router.jsx` 补
    `Navigate` 导出（issue #54 引入后 vite 依赖扫描报 missing export）；
    前端全量 202 passed + 后端全量 591 passed + vite build 成功。

- **默认页面改到概览页面（issue #54）**：打开平台默认落在仓库页，现改为
  默认进入概览页。根路径 `/` 重定向到 `/overview`（replace 导航，后退键
  不会卡在重定向环），仓库页迁至 `/repos`，顶部导航「仓库」链接同步指向
  `/repos`。
  - 前端 `App.jsx`：路由 `/` 由 `<Repos />` 改为
    `<Navigate to="/overview" replace />`，新增 `/repos` 路由挂载仓库页，
    「仓库」NavLink `to` 由 `/` 改为 `/repos`；
  - 测试：新增 `app-default-page.test.mjs` 2 用例（访问 `/` 渲染概览页且
    不渲染仓库页 / `/repos` 渲染仓库页）；`overview-page.test.mjs` 导航
    断言随仓库页迁址更新；前端全量 199 passed + 后端全量 591 passed。

- **任务页面两侧有空白时任务列表不再出现水平滚动条（issue #53）**：1440~
  1919px 视口下任务列表出现水平滚动条，页面左右两侧却还有大量空白。根因：
  border-box 下任务表格可用宽度 = `--content-width` − 80px（.content/.card
  左右 padding），而 `.table.tasks-table` min-width 为 1360px（12 列宽度
  总和，issue #37），需要 `--content-width ≥ 1440px` 才装得下；此前媒体
  查询 ≥1600px 才放宽到 1360px，导致 1440~1919px 视口下表格可用宽度仅
  1280px，必然滚动。
  - 前端 `styles.css`：媒体查询断点由 `min-width: 1600px`（1360px）提前为
    `min-width: 1440px`（1440px）——该区间表格恰好无滚动；≥1920px 保持
    1600px 封顶；视口 <1440px 本身装不下表格，保持 .table-wrap 横向滚动
    （issue #28/#37 既有行为不变）；
  - 测试：`tasks-table-fit-content.test.mjs` 3 用例（解析 styles.css 断点
    与 min-width，模拟 1440/1500/1600/1750/1919/1920/2560 视口断言表格
    可用宽度 ≥ min-width、窄视口滚动保留）。

- **任务「用时」改为完整处理周期动态计算（issue #49）**：任务页「用时」
  此前显示执行时长（started_at → finished_at），排队等待时间不计入，
  且终点不是 bot-done 打标时刻。现改为「系统接收到该问题的时间 →
  系统给 issue 打上 bot-done 标记的时间」的动态计算，不落库时长字段。
  - 后端 `executor.py`：`_finish_succeeded` 打 bot-done 标签成功后把
    `finished_at` 更新为打标时刻（`finished_at` 语义 = bot-done 打标
    时间）；打标失败保留收尾时刻兜底；
  - 前端 `Tasks.jsx` / `TaskDetail.jsx`：用时起点由 `started_at ||
    created_at` 改为固定 `created_at`（系统接收时间），详情页字段名
    「执行用时」→「处理用时」；
  - 测试：`test_task_duration.py`（finished_at 不早于打标时刻 / 打标失败
    兜底）+ `tasks-duration-calculation.test.mjs`（列表与详情渲染断言
    40 分钟完整周期、源码不依赖 started_at）。

- **任务「用时」仍显示不正确：存量 CST 时间戳数据迁移为 UTC**（issue #49
  第二轮）：用户以任务 #65 验证——该任务 14:38:41（本地）创建、14:48:10 打
  bot-done，18:20 查看时页面显示用时 8 小时（实际 9 分 29 秒）。根因：
  550e04f（issue #42）部署前旧版 executor 用 `time.strftime()`（无 gmtime）
  按容器本地 CST 写 `started_at`/`finished_at` 无时区后缀串，与 `created_at`
  （SQLite `datetime('now')` UTC）及前端「按 UTC 解析」契约不一致；第一轮只
  修了新数据写入，存量 CST 串按 UTC 解析偏移 +8 小时（终点落在未来）。
  - 后端 `database.py`：启动迁移新增 v2 步骤（`PRAGMA user_version` 版本化）
    `_fix_legacy_cst_timestamps`——以 `task_logs.ts`（恒为 UTC）为参照逐字段
    判定：串按 UTC 解析后与任一日志差 ≤ 10 分钟 → 已是 UTC 不动（H_UTC 优先，
    排队 8 小时以上的任务首条日志恰在 t-8h 附近不会被误减）；否则解析结果减
    8 小时与任一日志差 ≤ 10 分钟 → CST 串，改写为减 8 小时后的 UTC 串；均不
    命中（无日志等）→ 保守不动；幂等（修正后与日志直接吻合，重复执行不再
    命中）；
  - 生产数据实测：78 个任务 152 个字段迁移后与日志全部吻合（任务 #65
    finished_at 14:48:10 → 06:48:10，页面用时恢复 9 分钟；一键停止写 UTC 的
    #47 与 550e04f 后的 #66+ 均保持不动）；
  - 测试：新增 `test_database_legacy_cst.py` 7 用例（CST 修正 / UTC 不动 /
    一键停止 UTC 特例不动 / 排队 8 小时不误判 / 无日志保守不动 / 幂等 /
    user_version 标记）。

- **概览页 CI/CD 流水线阶段顺序反转（sync→deploy→build）**（issue #44）：
  概览页流水线区块的 stage 展示顺序与 `.gitlab-ci.yml` 定义顺序相反
  （执行 build→deploy→sync，显示 sync→deploy→build）。根因：GitLab
  `GET /projects/:id/pipelines/:pipeline_id/jobs` 默认按 job id 倒序
  返回且不响应 `sort` 参数（已对生产实例实证），后端按 API 返回顺序
  聚合 stage，导致顺序反转。
  - 后端 `pipelines.py`：`aggregate_stages` 聚合前先按 job id 升序排序
    （job id 为全局自增序列，同一 pipeline 内升序即 job 创建顺序，与
    stage 定义顺序一致），不再依赖 API 返回顺序；docstring 同步修正。
  - 测试：`test_api_pipelines.py` 新增
    `test_aggregate_reorders_reversed_api_jobs`（用生产流水线 #744 的
    真实倒序数据复现）；后端全量 458 passed + 前端全量 147 passed。

- **任务页面「用时」显示不正确（多 8 小时）**（issue #42）：
  生产任务 #64 创建于 13:52:43（本地），14:56 查看时页面显示用时 8 小时 24 分钟，
  实际执行仅约 24 分钟——恰好多 8 小时。根因：容器 TZ=Asia/Shanghai，
  tasks 表时间字段时区混合存储——`created_at` 由 SQLite `datetime('now')`
  写 UTC 串，`started_at`/`finished_at` 由 executor `time.strftime` 写本地
  CST 串；前端 `fmtDuration` 统一按 UTC 解析（api.js 契约），当 `started_at`
  为 NULL 回退 `created_at` 时 UTC/CST 串混算，时长多 8 小时。
  - 后端 `executor.py`：4 处 `time.strftime("%Y-%m-%d %H:%M:%S")` 改为
    `time.strftime(..., time.gmtime())`，started_at/finished_at 统一写 UTC，
    与 created_at 及前端解析契约一致。
  - 测试：新增 `tests/test_task_timestamps.py` 4 用例（TZ=Asia/Shanghai 下
    复现 + 验证 `_finish_failed`/`_finish_stopped`/`run_task` 全流程时间戳
    均为 UTC + 前端视角 created_at 与 finished_at 混算回归）。
  - 附带修复 flaky 测试：`test_executor.py::_shorten_ci_timeouts` 的等待
    窗口 20ms 太短，高负载机器上 `db.add_log` 真实写 SQLite（实测 27ms）
    即可耗尽窗口导致 `TestWaitPipelineForCommit` 误报 timeout——窗口放大
    到 1s（sleep 已 mock 为 no-op，不拖慢测试），「永不终态」用例的
    statuses 迭代器改 `itertools.repeat` 防耗尽；后端全量 457 passed +
    前端全量 147 passed。

- **任务在 CI 流水线运行中即显示已完成 + 完成后未给 issue 打终态标签**（issue #40）：
  生产任务 #63（issue #39 第二轮）于 13:31:45 被平台标记 succeeded，而其提交
  d08e104 触发的流水线 #737 的 backend:test 到 13:32:17、sync_to_github 到
  13:48:34 才结束——成功判定只检查 claude exit 0，不等待流水线终态；且该任务
  收尾打 bot-done 时（13:31:45）恰逢自身 push 的代码触发 deploy job 执行
  pm2 delete 重启平台，`PUT /issues/39` 未发出进程即被杀死，issue #39 至今无
  bot-done/bot-failed 标签，会被 webhook/对账当作新任务重复领取。
  - 后端 `executor.py`：成功收尾前新增流水线等待——`_await_task_pipeline` 用
    `find_commit_for_issue` 拿任务提交 sha（查不到提交即不等待，仓库无 CI
    不影响成功）；`_wait_pipeline_for_commit` 在探测窗口（默认 120s）内找
    sha 匹配的最新流水线（GitLab 收到 push 即创建流水线记录），命中后轮询
    至 success/failed/canceled/skipped 终态（总上限默认 1800s，轮询间隔
    默认 15s）；流水线 failed/canceled → 任务判失败（打 bot-failed + 留失败
    评论），超时未终态 → 判失败，success/skipped/无匹配 → 成功收尾（打
    bot-done）；等待期间用户一键停止 → interrupted。
  - 后端 `reconciler.py`：终态标签对账兜底——每轮对账回看每仓库最近 20 条
    succeeded/failed 任务，issue 仍 open 且无 bot-done/bot-failed 时按任务
    结果补打对应标签（幂等，GitLab 报错仅记日志下轮再试），覆盖「收尾打标签
    被部署重启打断」的窗口。
  - 后端 `gitlab_client.py`：新增模块级常量 `PIPELINE_TERMINAL_STATES` 与
    `get_pipeline`（单条流水线详情，轮询终态用）；`config.py` worker 配置
    新增 `ci_wait_detect_seconds` / `ci_wait_interval_seconds` /
    `ci_wait_timeout_seconds`（settings API 可写，默认 120/15/1800）。
  - 测试：后端 `test_executor.py` 新增 17 用例（流水线 success 才判成功/
    failed 与 canceled 判失败并打 bot-failed/skipped 视为成功/无匹配流水线
    不等待/超时判失败/等待中停止判 interrupted/`_wait_pipeline_for_commit`
    轮询细节 6 例）+ `test_reconciler.py` 新增 5 用例（succeeded 补打
    bot-done/failed 补打 bot-failed/已有终态标签跳过/issue 已关闭跳过/
    补打失败不影响补入队）；后端全量 449 passed + 前端全量 147 passed。

### Added

- **设置页增加 AI API 供应商配置**（issue #46）：
  设置页新增「AI API 供应商」卡片，可增删改 AI 供应商配置（名称 / 供应商
  类型 / Base URL / API Key / 默认模型 / 启用开关），为后续 AI 功能消费做
  准备（本期纯配置存储，不接入实际调用）。内置 11 个预设供应商
  （DeepSeek / OpenAI / Anthropic / Google Gemini / Moonshot / 通义千问 /
  智谱 GLM / 硅基流动 / Ollama / OpenRouter / 自定义），选择预设自动填充
  默认 Base URL 与模型，每个供应商显示各自品牌 logo（而非统一图标）。
  - 后端：`config.py` Settings 新增 `ai_providers` 列表（config.yaml 落盘，
    与 repos / custom_labels 同为整体替换模式）与 `update_ai_providers`；
    `api/settings.py` GET 返回 `ai_providers` 段（api_key 只返回掩码，明文
    不流转到界面），PUT 校验（name 必填且不重复 / base_url 须 http(s) 开头 /
    enabled 布尔），api_key 回传掩码值或留空 = 保持现有（按 name 匹配旧
    配置，与 sso.client_secret 同模式），config.yaml 中 api_key 支持
    `${ENV}` 引用（凭据不落明文）。
  - 前端：新增 `providers.jsx`（预设清单 + 各供应商品牌色内联 SVG logo，
    未知 key 回退 custom 通用图标）与 `components/AiProvidersCard.jsx`
    （列表展示 + 增删改表单 + 卡片内独立保存按钮，只提交 ai_providers 段）；
    `Settings.jsx` 在 SSO 卡片后挂载；styles.css 新增 provider-* 样式。
  - 测试：后端 `test_api_settings.py` 新增 11 用例（持久化 / 掩码不回传 /
    掩码与留空保持现有 / 整体替换 / 清空 / name 必填与去重 / base_url 校验 /
    非数组拒绝 / ${ENV} 引用展开），后端全量 482 passed；前端新增
    `settings-ai-providers-card.test.mjs` 8 用例（卡片挂载 / 表单字段 /
    PUT ai_providers 段 / 预设清单 / logo 差异化 / 样式类），前端全量
    167 passed。

- **概览页流水线卡片显示最近流水线对应提交的提交时间**（issue #43）：
  概览页 CI/CD 流水线区块每张仓库卡片在分支 · sha 下方显示最近一次流水线
  对应提交的提交时间（绝对时间，沿用页面时区配置）与距今多久（相对时间：
  刚刚 / X 分钟前 / X 小时前 / X 天前 / X 个月前 / X 年前）。
  - 后端：`gitlab_client.py` 新增 `get_commit`（单条提交详情）；`pipelines.py`
    每条结果新增 `commit_time` 字段（committed_date 转 UTC 无后缀时间串，
    与 executor 落库格式一致），commit 查询失败 / 不存在（force-push 后
    sha 失效）/ 缺字段时静默降级为 None、不进 errors（时间仅为展示增强
    信息，不影响卡片其余部分）；无流水线仓库不查 commit 避免无效 API 调用。
  - 前端：`api.js` 新增 `fmtAgo` 相对时间纯函数（60 秒内与未来时间按
    「刚刚」，30 天/365 天为月/年档边界，now 参数可注入供测试）；概览页
    卡片渲染 `pipeline-commit-time` 节点（绝对+相对时间，commit_time 为
    null 时不渲染）；`styles.css` 新增节点样式。
  - 测试：后端 `test_api_pipelines.py` 新增 13 用例（时区转换纯函数 7 例
    + API 正常/故障静默降级/404 静默降级/无流水线跳过查询/pipeline 缺
    sha/commit 缺 committed_date 6 例）；前端新增
    `pipeline-commit-time.test.mjs` 12 用例（fmtAgo 各档位与边界 9 例 +
    渲染 2 例 + 源码/样式断言 1 例）；后端全量 471 passed + 前端全量
    159 passed。

- **标记库内置默认标签 need-verify + 领取任务过滤**（issue #41）：标记库默认
  清单新增流程/状态标签 `need-verify`（黄色 `#ffcc00`，语义「需要人工验证，
  bot 不领取」）；bot 领取任务（webhook 入队与对账补入队两处）时跳过带
  need-verify 标签的 issue——用户给需人工验证的 issue 打上该标签后，bot
  不再自动领取处理（沿用「以 API 最新标签为准」的既有过滤路径）。
  - 后端：`labels.py` 默认清单新增 need-verify（内置 14 个）并新增统一常量
    `CLAIM_SKIP_LABELS`（bot-done / bot-failed / need-verify）供领取过滤
    引用；`webhook.py` 与 `reconciler.py` 的入队过滤改用该常量（终态标签
    对账补打仍只看 bot-done / bot-failed，不受影响）。
  - 文档/工具：`docs/labels.md` 与 `scripts/sync_labels.py` 同步新增
    need-verify（GitLab 项目侧标签需运行 sync 脚本同步）。
  - 测试：后端 `test_webhook.py` 新增 need-verify 拒绝入队 2 例、
    `test_reconciler.py` 新增对账跳过 2 例、`test_api_labels.py` 默认清单
    数量与包含断言更新；后端全量 453 passed + 前端全量 147 passed。

- **概览页 CI/CD 流水线状态展示未启用仓库**（issue #39 第二轮）：流水线区块由
  「所有启用仓库」扩展为「所有配置仓库」——未启用（enabled=false）的仓库同样
  查询并展示最新流水线状态，卡片标题旁显示灰色「未启用」徽章（`badge-muted`）
  以便区分；未启用仓库无流水线时显示「暂无流水线」占位，查询失败与启用仓库
  一致进 `errors` 列表。
  - 后端：`_collect` 移除 enabled 过滤，每条结果新增 `enabled` 字段透传前端。
  - 前端：`Overview.jsx` 卡片渲染未启用徽章，区块副标题改为「所有配置仓库的
    最新流水线」。
  - 测试：后端 `test_api_pipelines.py` 更新停用仓库用例为「未启用仓库也返回并
    查询」（enabled 字段透传断言），新增未启用仓库有流水线/查询失败 2 例；
    前端 `overview-pipelines.test.mjs` 新增未启用徽章渲染（启用仓库不显示、
    灰色样式类）与未启用仓库流水线卡片渲染 2 例。

- **概览页 CI/CD 流水线状态**（issue #39）：概览页面新增「CI/CD 流水线」区块，
  展示所有启用仓库的最新一次流水线状态——整体是否完成、成功还是失败、运行到
  哪个阶段、还有哪些阶段；每仓库一张卡片，参考 GitLab CI/CD 的阶段图以横向
  stage 节点条展示（绿=成功/红=失败/蓝=运行中带脉冲动画/灰=待运行），节点悬停
  显示 stage 名与状态，卡片链接跳转 GitLab pipeline 页面；无流水线仓库显示
  「暂无流水线」占位。
  - 前端：`Overview.jsx` 新增流水线区块，独立慢轮询（15 秒，任务轮询仍为
    3 秒）；导出 `PIPELINE_STATUS_META`（流水线状态徽章映射，复用 status-*
    样式类）与 `stageClass()`（stage 状态→节点样式类，未知状态兜底 pending）；
    `styles.css` 新增 pipeline 卡片/节点样式（3 列网格、运行中脉冲动画）。
  - 后端：新增 `GET /api/pipelines/overview`，遍历所有启用仓库（停用跳过），
    取最新流水线（`GitLabClient.get_latest_pipeline`，无流水线 pipeline=null）
    与全部 jobs（`list_pipeline_jobs`），按 stage 聚合状态（`aggregate_stages`：
    failed（allow_failure 的失败不计）> running > pending 系列 > canceled >
    success，manual/skipped 不影响；stage 顺序 = jobs 首次出现顺序即
    .gitlab-ci.yml 定义顺序）；多仓库场景下单仓库失败不中断整体（HTTP 200），
    失败明细进 `errors` 列表（与 issue #38 对账一致）；结果带 10 秒 TTL 内存
    缓存，避免轮询打爆 GitLab API。
  - 测试：后端 `tests/test_api_pipelines.py` 19 用例（stage 聚合规则 10 例含
    allow_failure/优先级/空列表 + API 正常/无流水线/部分与全部仓库失败/jobs
    查询失败/停用跳过/无仓库/缓存命中与过期）+ 前端
    `tests/overview-pipelines.test.mjs` 11 用例（轮询源码断言/状态映射纯函数/
    组件渲染含 stage 节点样式与 GitLab 链接/无流水线与 errors 兜底/样式）。
    后端全量 429 passed + 前端全量 145 passed。

- **一键对账所有仓库**（issue #38）：任务页面筛选行新增「对账所有仓库」按钮，
  点击后同步扫描全部启用仓库，把「assignee 是 bot 但任务表无活跃记录」的
  open issues 补入任务队列，并即时展示扫描数/补入队数。
  - 前端：`Tasks.jsx` 筛选行「停止所有任务」旁新增对账按钮（低危操作无需
    `confirm`，请求中禁用防重复点击）；调 `POST /api/tasks/reconcile-all` 后
    显示绿色成功提示（扫描 X 个 issue、补入队 Y 个任务）并刷新列表；部分仓库
    失败时展示失败明细，接口失败显示错误。
  - 后端：新增 `POST /api/tasks/reconcile-all`，同步执行全量对账扫描
    （复用 `Reconciler.reconcile_once()`，与仓库页单仓库对账 issue #17 一致）
    并直接返回 `{ok, scanned, enqueued, errors}`；多仓库场景下单个仓库失败
    不中断整体（HTTP 200），失败明细放入 `errors` 列表返回。
  - 测试：后端 `tests/test_task_reconcile.py` 8 用例（全量入队/无漏单/幂等/
    终态标签跳过/部分与全部仓库失败/停用仓库/无仓库）+ 前端
    `tests/tasks-reconcile-all-button.test.mjs` 7 用例（源码断言/按钮渲染/
    成功提示/部分失败明细/接口失败）。后端全量 410 passed + 前端全量
    134 passed + build 通过。

- **手动重试任务**（issue #36）：任务页面操作列新增「重试」按钮（仅失败/已中断
  任务显示），点击确认后任务重新入队执行——失败相关字段清空、尝试次数归零、
  来源标记「手动」，保留 claude 会话断点续跑（接续上次进度继续处理）。
  - 前端：`Tasks.jsx` 失败/中断任务行渲染「重试」按钮（`btn-gap-left` 与「执行」
    按钮分隔，请求中禁用防重复点击），点击需 `window.confirm` 确认防误触；调
    `POST /api/tasks/{id}/retry` 后显示绿色成功提示并刷新列表，失败显示错误；
    来源列支持「手动」展示。
  - 后端：新增 `POST /api/tasks/{task_id}/retry`（404 不存在 / 400 仅失败与
    中断可重试 / 409 同 issue 已有活跃任务 / 200 成功入队）；数据库
    `retry_task()` 条件更新重置失败字段（`triggered_by='manual'`，保留
    `claude_session_id` 与 `log_path`），同事务检查活跃任务去重防撞唯一索引，
    条件 UPDATE 兜底并发；成功后交调度器 `enqueue` 重新派发。
  - 测试：后端 `tests/test_task_retry.py` 12 用例（db 重置/拒绝/冲突/日志/
    重复重试 + API 契约/入队/404/400/409）+ 前端 `tests/tasks-retry-button.test.mjs`
    7 用例（源码断言/按钮渲染条件/确认流/成功失败提示/请求中禁用）。
    后端全量 402 passed + 前端全量 117 passed + build 通过。

- **一键停止所有任务**（issue #35）：任务页面新增「停止所有任务」危险按钮（显示当前
  活跃任务数），一键把排队/执行/重试中的任务全部标记为已中断（interrupted 终态），
  执行中的 claude 进程组被强制终止；被停止的任务不会在平台重启后自动恢复执行。
  - 前端：`Tasks.jsx` 筛选行新增 `btn-danger` 按钮（无活跃任务或请求中禁用），
    点击需 `window.confirm` 确认防误触；调 `POST /api/tasks/stop-all` 后显示
    绿色成功提示（已停止 N 个任务）并刷新列表，失败显示错误。
  - 后端：新增 `POST /api/tasks/stop-all`（返回 `{stopped, count}`）；调度器
    `stop_all()` 清空待派发队列并终止运行中任务；执行器维护运行中进程注册表 +
    停止请求集合（`request_stop` SIGKILL 进程组，`_run_once` 读循环轮询感知停止
    返回约定退出码 125，`run_task` 收到停止请求后不再执行/重试）；数据库
    `stop_active_tasks()` 统一落 interrupted 终态 + 错误信息 + warn 日志。
  - 测试：后端 `tests/test_task_stop.py` 11 用例（db 落库/调度清队列/进程终止/
    停止收尾/API 契约）+ 前端 `tests/stop-all-button.test.mjs` 8 用例（按钮
    禁用条件/确认流/成功失败提示，`tests/helpers/mock-router.jsx` mock
    react-router-dom 供 node --test 渲染）。
    后端全量 390 passed + 前端全量 110 passed + build 通过。

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

### Changed

- **版本与构建时间显示移入设置页底部 + 登录用户信息放导航栏最右**（issue #9 第二轮）：
  用户反馈调整：版本号与构建时间不再占用导航栏右侧，改为设置页面底部「版本信息」
  卡片展示；登录后用户名称与退出按钮位于导航栏最右侧。
  - 前端：`App.jsx` 导航栏移除 `VersionBadge`（及 import），`user-chip` 承接
    原版本徽标"推到最右"职责；`Settings.jsx` 页面底部新增「版本信息」卡片
    （复用 `VersionBadge` 组件，构建产物 `version.json` 注入，开发模式无该
    文件时静默隐藏）；`styles.css` 移除 `.version-badge` 的 `margin-left:auto`
    （导航栏专属布局不再适用），新增 `.settings-version .version-badge` 放大
    字号跟随正文字色、`.user-chip` 增加 `margin-left:auto`。
  - 测试：前端 `tests/version-badge-settings-page.test.mjs` 4 用例（导航栏
    无版本徽标/设置页渲染/导航栏末尾为用户信息/user-chip 推右样式）。
    后端全量 402 passed + 前端全量 121 passed + build 通过。

- **SSO 配置指南直接显示在设置页 + 提示文字优化**（issue #27 第六轮）：
  平台使用者看不到代码仓库里的本地文档 `docs/Synology-SSO-配置指南.md`，
  SSO 卡片说明此前指向该本地文件路径（部署环境中不存在）。改为设置页
  从后端拉取指南 Markdown 并渲染展示，文档内容与 `docs/` 单一来源同步；
  同时修正登录有效期默认值 7 → 30 天（用户第三轮已确认 30 天，历史实现偏差）。
  - 后端：`GET /api/settings/sso-guide` 读取 `docs/Synology-SSO-配置指南.md`
    返回 Markdown 原文（文件缺失 404 降级，SSO 启用时同受登录保护）；
    `config.py` `sso_session_days` 默认值 7 → 30。
  - 前端：新增 `components/Markdown.jsx` 轻量渲染器（标题/段落/围栏代码块/
    嵌套列表/表格/引用块/行内粗体、代码、链接，全部走 React 文本节点天然
    防 XSS，无第三方依赖）；设置页 SSO 卡片内新增「查看 SSO 配置指南」折叠区
    （默认收起，点击展开渲染文档）；卡片说明文字去掉本地 docs/ 路径指向、
    改为指向页面内指南；`session_days` 输入框 fallback 7 → 30。
  - 文档：`docs/Synology-SSO-配置指南.md` 同步默认有效期 30 天（两处）。
  - 测试：后端 `tests/test_api_sso_guide.py` 4 用例（内容契约/文件缺失 404/
    SSO 保护/默认 30 天）+ 前端 `tests/markdown.test.mjs` 11 用例（各语法/
    空值边界/XSS 转义/长文档混合）与 `tests/settings-sso-guide.test.mjs`
    4 用例（不指向本地路径/折叠区/默认 30/渲染交互）。后端全量 379 passed +
    前端全量 102 passed + build 通过。

### Fixed

- **任务列表三列换行：页面宽度足够时「创建时间/用时/操作」数据仍折成两行**（issue #37）：
  任务表格 12 列固定宽度分配失衡——创建时间 135px（内容 `2026-08-13 12:06:19`
  需约 147px）、用时 60px（`12 天 23 小时` 需约 105px）、操作 65px（「执行」+「重试」
  两按钮需约 136px）均放不下单行内容，浏览器在空格处折行；而标题（30%）与失败
  原因（22%）两列在宽视口下吃掉富余空间，形成「页面两侧有空白、三列却换行」。
  - 前端：`styles.css` 三列加宽至 165/120/145px 并新增 `white-space: nowrap`
    规则（含空格内容恒为一行）；标题/失败原因列由百分比改为固定 254/140px
    （长文本仍由 ellipsis 截断 + title 悬浮 + 详情弹窗补偿）；提交列 104→90px
    （8 字符 shortSha 富余让出）；任务表格新增 `min-width: 1360px`（= 12 列总和）——
    宽视口（≥1360px）表格撑满容器、富余空间按列宽比例分配不换行，窄视口保持
    `.table-wrap` 横向滚动（issue #28 既有行为）。
  - 测试：前端 `tests/tasks-time-duration-actions-nowrap.test.mjs` 6 用例（三列
    最小列宽/nowrap 规则/min-width 与列宽总和一致/总和 ≤1600px 宽视口不滚动回归
    保护）。后端全量 402 passed + 前端全量 127 passed + build 通过。

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
