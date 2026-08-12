# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### Added

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
