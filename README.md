# Botler — GitLab AI Issue Bot 平台

> Bot + Butler，机器人管家

运行在服务器上的自动化平台：统一配置多个 GitLab 仓库，通过 webhook 实时监控 issue，
当 issue 被指派给 bot 账号时，自动调用 **Claude Code CLI（无头模式）** 处理并推送修复到 main，最后关闭 issue。
执行引擎可切换为 **hermes-agent**（部署机已装好时，见 [docs/hermes-engine-deployment.md](docs/hermes-engine-deployment.md)）
或 **deepseek-harness**（Python SDK 进程内调用，见 [docs/dsh-engine-deployment.md](docs/dsh-engine-deployment.md)）。

完整设计见 [`docs/设计方案.md`](docs/设计方案.md)。

## 工作原理

```
用户在 GitLab 把 issue 指派给 bot 账号
        │  webhook（issue 事件）
        ▼
Webhook 接收器 ──► 任务调度器（SQLite，同仓库串行/跨仓库并行）
        ▲                 │
        │                 ▼
对账兜底调度器 ◄──── Claude Code 执行器（干净工作区 + 模版渲染 + 超时/重试）
（每 5 分钟扫漏网 issue）   │
                          ├─► git push 到 main（Claude 自己执行）
                          └─► 调 GitLab API 关闭 issue（Claude 自己执行）
```

调度器派发顺序：仓库优先级（数字小先）→ 同仓库队列内按 issue 标签优先级
（默认 `bug` > `test` > `feature`，设置页可自定义），同优先级按 issue 更新时间升序。

> 💡 **断点续跑**（issue #8）：CI/CD 频繁重新部署时，执行中的任务被进程重启打断后
> 不会从头重跑——executor 持久化 claude 会话 id，重启恢复时用 `claude --resume`
> 接续上次会话且保留工作区改动，从上次中断处继续（会话文件丢失时自动降级全新会话）。
> hermes 引擎（issue #47）等价支持：会话消息历史落库 `tasks.hermes_history`，
> 恢复时作为 `conversation_history` 传入接续对话。
> dsh 引擎（issue #84）等价支持：SDK 在 session_root 持久化会话，会话 id
> 落库 `tasks.dsh_session_id`，恢复时以同一 id 接续对话。

## 目录结构

```
backend/
  botler/
    main.py          FastAPI 入口（静态托管前端）
    config.py        config.yaml 加载（${ENV} 展开）+ 写回
    database.py      SQLite 模型（repos / tasks / task_logs）
    gitlab_client.py GitLab REST API 封装（webhook 注册、issue 评论等）
    webhook.py       webhook 接收器（secret 校验 + assignee 判定 + 去重）
    scheduler.py     任务调度器（每仓库串行、跨仓库并行、按仓库优先级派发；同仓库队列内按 issue 标签优先级排序，默认 bug 最优先）
    executor.py      执行器（claude / hermes / dsh 三引擎，干净工作区 / 超时 / 重试 / 失败评论）
    hermes_runner.py hermes 引擎 runner 脚本（hermes venv 进程内调用 AIAgent，stdin/stdout JSON 协议）
    dsh_runner.py    dsh 引擎 runner（deepseek-harness SDK 进程内调用，线程运行 + 停止/超时关闭运行时）
    reconciler.py    对账兜底（APScheduler 定时扫描补漏）
    auth.py          Synology SSO（OIDC 客户端 / 签名会话 / API 保护中间件）
    api/             REST API（repos / tasks / settings / auth）
  config.example.yaml
  requirements.txt
frontend/            React (Vite) Web UI，构建产物由 FastAPI 托管
docs/                文档（设计方案 / Synology SSO 配置指南）
deploy/              pm2 与 systemd 配置
workspace/           仓库工作区（运行时生成）
logs/                任务执行日志（运行时生成）
```

## 快速开始（本地开发）

```bash
# 后端
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env          # 填入 GITLAB_BOT_TOKEN / WEBHOOK_SECRET / ANTHROPIC_*
.venv/bin/uvicorn botler.main:app --reload --port 8000

# 前端（另开终端）
cd frontend
npm install && npm run dev    # http://localhost:5173，/api 代理到 8000
```

## 部署（10.0.0.122，Ubuntu 24.04）

前置条件：

1. Node.js 18+，安装 Claude Code：`npm install -g @anthropic-ai/claude-code`
2. 配置 DeepSeek 兼容端点（写入 `backend/.env`）：
   ```
   ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
   ANTHROPIC_API_KEY=sk-xxxx
   ANTHROPIC_MODEL=deepseek-chat
   ```
   > ⚠️ 若服务器配了代理，`claude -p` 前需清理代理环境变量，否则报 TLS 证书错误。
3. GitLab 网络互通：10.0.0.122 能访问 `home.chenkaidi.top:509`，且 GitLab 能反访 10.0.0.122 的 webhook 端口（同一 ZeroTier 网络应互通）。
4. bot 账号 PAT（scope `api` + `write_repository`），并加入各目标仓库（角色 Maintainer，webhook 注册需要）。

部署步骤：

```bash
git clone <platform-repo> && cd botler
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml && cp .env.example .env   # 填入凭据
cd ../frontend && npm install && npm run build               # 构建 Web UI

# 启动（三选一）
pm2 start deploy/botler.config.cjs && pm2 save && pm2 startup
# 或
sudo cp deploy/botler.service /etc/systemd/system/ && sudo systemctl enable --now botler
# 或 Docker（见下方「Docker 部署」）
```

> 💡 **CI/CD 自动部署**：推送 main 分支后，GitLab CI 自动执行 Docker 部署
> （`deploy_to_code01`：构建镜像 → compose 启动 → 健康检查），无需手动操作。
> 部署前会自动停止旧 pm2 服务，凭据优先用服务器上 `backend/.env`（缺失时用
> CI 变量生成）。

冒烟测试：浏览器打开 `http://10.0.0.122:8000` → 添加仓库（自动注册 webhook）→
在 GitLab 建一个测试 issue 指派给 bot → 观察任务列表，验证代码推上 main、issue 自动关闭。

## Docker 部署

镜像已包含全部运行时（前端构建产物、Python 依赖、`git`、`claude` CLI），只需挂载配置与数据。

前置条件：Docker 20.10+ / docker compose v2。

```bash
# 1. 准备配置（数据统一放在 data/ 目录下，容器重建不丢；目录不存在会自动创建）
mkdir -p data/backend
cp backend/config.example.yaml data/backend/config.yaml
cp backend/.env.example data/backend/.env   # 填入 GITLAB_BOT_TOKEN / WEBHOOK_SECRET / ANTHROPIC_*
touch data/backend/botler.db                # SQLite 库（首次运行自动建表）

# 2. 构建并启动
docker compose up -d --build
# 国内无法访问 Docker Hub 时，用环境变量覆盖基础镜像：
#   NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine \
#   RUNTIME_IMAGE=docker.m.daocloud.io/library/node:20-bookworm-slim \
#   docker compose up -d --build

# 3. 验证
docker compose ps                          # 状态 healthy
curl http://localhost:8000/api/health      # {"ok":true,...}
./deploy/verify-docker.sh --full           # 11 项冒烟检查（临时数据目录，不碰真实数据）
```

数据持久化（全部卷挂载，容器重建不丢失）：`data/backend/config.yaml`（Web UI 设置页会写回）、
`data/backend/.env`（只读）、`data/backend/botler.db`、`data/workspace/`、`data/logs/`、
`data/claude-home/`（claude 会话文件 `~/.claude`，断点续跑依赖——容器重建后
`--resume` 才能找到上次会话；pm2 部署下在宿主 HOME 天然持久，无需此挂载）。
容器时区固定为 **Asia/Shanghai**（compose `TZ` 环境变量 + 镜像内 tzdata）。
CI 部署（`deploy_to_code01`）固定数据目录为绝对路径 **`/home/ckd/codes/botler/data`**
（显式 export `BOTLER_DATA_DIR`，不随 gitlab-runner 构建目录漂移，构建目录可能被清理）。

可调参数：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BOTLER_HTTP_PORT` | `8000` | 宿主机映射端口 |
| `BOTLER_DATA_DIR` | `./data` | 数据目录前缀（换目录时 config.yaml / botler.db 需一并迁移） |
| `NODE_IMAGE` / `RUNTIME_IMAGE` | 官方镜像 | 构建基础镜像覆盖（国内镜像源） |
| `GIT_CREDENTIALS_FILE` | `/dev/null` | 容器内 git 凭据文件（挂载为 `/root/.git-credentials`，执行器 clone/push 仓库依赖；CI 部署自动用宿主凭据或 `GITLAB_BOT_TOKEN` 生成，手动部署可指向任意格式的 `.git-credentials` 文件） |

自定义镜像：`docker build -t botler:latest .`（见 Dockerfile 头部注释）。
停止：`docker compose down`（数据保留）；删除数据：`docker compose down -v`。

## 配置说明

`backend/config.yaml` 是唯一事实来源，Web UI 是编辑它的外壳。直接编辑 config.yaml 的修改会被运行中的进程自动感知（检测文件变化后重载，无需重启；issue #25），且后续 Web UI 保存设置不会覆盖手动编辑的内容。凭据一律用 `${ENV_VAR}` 引用环境变量（`backend/.env`），不入库、不进日志、不进提示词。

关键配置（`config.example.yaml` 中有完整示例与注释）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `gitlab.bot_token` | — | bot PAT，经 `GITLAB_BOT_TOKEN` 环境变量引用 |
| `gitlab.webhook_secret` | — | webhook 校验 secret |
| `worker.max_concurrent_repos` | 3 | 跨仓库并行上限 |
| `repos[].priority` | 100 | 仓库调度优先级（1~999 整数，数字越小越优先；多个仓库同时有排队任务时按优先级派发，同优先级按任务提交时间排序） |
| `worker.issue_priority` | `["bug","test","feature"]` | issue 标签处理优先级（同仓库队列内按此顺序选任务派发，越靠前越先处理；未列出的标签排在最后，同优先级按 issue 更新时间升序；设置页「任务调度」卡片可修改） |
| `worker.task_timeout_seconds` | 1800 | 单任务超时（30 分钟） |
| `worker.max_retries` | 2 | 失败重试次数（「无法解决」不重试） |
| `worker.reconcile_interval_seconds` | 300 | 对账兜底扫描间隔 |
| `worker.engine` | `claude` | 任务执行引擎：`claude`（Claude Code CLI）/ `hermes`（部署机已装好的 hermes-agent）/ `dsh`（deepseek-harness SDK）；非法值回退 `claude`（issue #47/#84） |
| `claude.command` / `args` | `claude -p --output-format stream-json --verbose` | claude 引擎执行命令（stream-json 逐行实时输出，任务页面逐事件查看执行过程） |
| `hermes.command` / `args` | — | hermes 引擎执行命令（部署机 hermes venv 的 python + `backend/hermes_runner.py`），部署见 `docs/hermes-engine-deployment.md` |
| `dsh.provider` / `model` | `deepseek-official` / `deepseek-v4-flash` | dsh 引擎运行参数（provider 路由 / 模型 id），Key 走环境变量 `DEEPSEEK_API_KEY` |
| `dsh.max_tokens` / `session_root` / `cordis` / `runtime_bin` / `base_url` / `api_key` | — | dsh 引擎可选参数（输出上限 / 会话持久化目录 / 自定义 Cordis 配置 / 自定义 runtime / 兼容端点），部署见 `docs/dsh-engine-deployment.md` |
| `browse.default_path` | 空（服务器用户主目录 `~`） | 目录选择对话框的初始定位目录；支持 `~` 展开，路径不存在时自动回退主目录 |
| `notifications.enabled` | true | 网页通知总开关（任务需交互 / issue 完成 / 队列空 / 无新任务，逐项可关） |
| `sso.enabled` | false | Synology SSO 登录总开关：启用后访问 Web UI 需用群晖账号登录（issue #27） |
| `sso.well_known_url` / `client_id` / `client_secret` | — | 群晖 SSO Server 的 OIDC 接入参数（Well-known URL / Application ID / Secret） |
| `sso.session_days` | 7 | 登录有效期（天，1~365） |
| `sso.redirect_uri` | 空（自动生成） | 回调地址，须与群晖侧注册一致 |
| `sso.verify_ssl` | true | 群晖为自签名证书时设 false |

提示词模版支持变量占位符：`{repo_name}` `{issue_title}` `{issue_body}` `{issue_url}` `{gitlab_url}` `{project_id}` `{issue_iid}`。
全局默认模版 + 仓库级覆盖可在 Web UI「模版」页编辑。

## API 一览

```
GET    /api/health                    健康检查
GET    /api/repos                     仓库列表
POST   /api/repos                     添加仓库（自动识别 project_id + 注册 webhook；priority 1~999 缺省 100）
GET    /api/repos/browse              浏览服务器目录（无 path 时初始定位到 browse.default_path，默认服务器用户主目录 ~）
POST   /api/repos/discover            读取本地文件夹的 git remote 列表
PUT    /api/repos/{id}                更新仓库（名称/启用/优先级/模版覆盖）
DELETE /api/repos/{id}                删除仓库
POST   /api/repos/{id}/test           测试连通性（token + 项目 + webhook）
GET/PUT /api/repos/{id}/template      仓库模版
GET/PUT /api/settings                 系统设置（写回 config.yaml）
POST   /api/settings/reconcile-now    手动触发对账
GET    /api/tasks                     任务列表（分页/过滤，含 commit_sha/commit_url）
GET    /api/tasks/{id}                任务详情（含日志、commit_sha/commit_url）
GET    /api/tasks/{id}/logs           任务日志
GET    /api/tasks/{id}/execution      实时执行（增量日志 + 聊天记录，issue #20）
GET    /api/tasks/{id}/events         任务事件流（SSE 推送：thinking/文本/工具调用/结果逐事件实时可见；终态任务连接后回放历史事件，见实时输出功能）
GET    /api/issues/overview           已启用仓库开放 issue 聚合（10s 缓存，issue #64）
POST   /api/issues/{project_id}/{iid}/close   关闭指定 issue（概览页右边栏「关闭 issue」按钮，issue #94）
GET    /api/issues/{project_id}/{iid}/detail  issue 评论与活动详情（评论/系统活动分区，最多 100 条，issue #97）
GET    /api/notifications/events      通知事件增量拉取（游标 after，issue #21）
GET    /api/environment               本地环境检测（服务器上 agent/基础工具安装与版本，issue #22）
GET    /api/auth/status               登录状态探测（SSO 是否启用 + 当前用户，issue #27）
GET    /api/auth/login                跳转群晖 SSO 登录页（302）
GET    /api/auth/callback             OIDC 回调（换 token 建会话，302 回首页）
POST   /api/auth/logout               退出登录
GET    /api/auth/me                   当前登录用户
GET    /api/issues/overview           概览页开放 issue 聚合（已启用仓库，10 秒 TTL 缓存，issue #64）
GET    /api/issues/form-meta/{id}     添加 issue 表单元数据：项目成员（含继承，members/all）+ 项目标签（issue #92）
POST   /api/issues                    在指定仓库创建 issue（标题/分配人/标签必填、描述选填，描述为空时发送 GitLab API 自动填充标题；成功后清缓存，issue #92/#103）
GET    /api/issues/{project_id}/labels      项目标记池（概览页右边栏「编辑标记」多选数据源，颜色归一化，issue #108）
PUT    /api/issues/{project_id}/{iid}/labels  更新 issue 标记（add/remove 一次提交加删标记；成功后清缓存并返回更新后标记列表，issue #108）
GET    /api/issues/{project_id}/{iid}/detail  issue 评论与活动详情（评论/系统活动分区，最多 100 条，issue #97）
POST   /webhook/gitlab                GitLab webhook 入口
```

## Synology SSO 登录

可接入群晖 **SSO Server**（OIDC）作为登录身份源（issue #27）：设置页「Synology
SSO 登录」卡片填写 Well-known URL / Application ID / Secret 并启用后，访问管理
界面需使用群晖账号登录（未启用时保持开放访问）。

群晖侧创建 OIDC 应用的完整步骤、Botler 侧配置与常见问题见
[`docs/Synology-SSO-配置指南.md`](docs/Synology-SSO-配置指南.md)。

## 安全说明

- 凭据全部走环境变量，token 只通过子进程环境变量注入 Claude Code（不进提示词 transcript）
- webhook 用 `X-Gitlab-Token` 校验，防伪造请求
- 工作区每次执行前 `reset --hard` + `clean -fd`，不同仓库互相隔离
- SSO 启用后 `/api/*` 除登录流程与健康检查外均需登录（会话为签名 cookie，
  密钥自动生成于 `backend/data/session_secret.key`，已 gitignore；Docker 部署
  由 compose 挂载持久化）
- 风险认知：main 不保护 + bot 直接推，单点失误可能破坏 main；
  缓解：同仓库串行、干净工作区、模版强调「自测通过才推」。个人自用可接受，如需保护请对 main 加 push 保护。
- CI 的 security 阶段（issue #86）用 5 种免费开源工具做静态代码分析
  （高危/中危漏洞阻断部署）：bandit（后端代码 SAST）、pip-audit
  （后端依赖 CVE）、npm audit（前端依赖 CVE）、semgrep（前后端代码
  SAST）、gitleaks（密钥泄露检测），结果上传 GitLab Security 页面，
  查看方式与误报豁免详见
  [`docs/静态分析扫描结果查看指南.md`](docs/静态分析扫描结果查看指南.md)。

## License

[MIT](LICENSE) © 2026 chenkaidi
