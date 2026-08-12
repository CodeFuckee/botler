# Botler — GitLab AI Issue Bot 平台

> Bot + Butler，机器人管家

运行在服务器上的自动化平台：统一配置多个 GitLab 仓库，通过 webhook 实时监控 issue，
当 issue 被指派给 bot 账号时，自动调用 **Claude Code CLI（无头模式）** 处理并推送修复到 main，最后关闭 issue。

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

> 💡 **断点续跑**（issue #8）：CI/CD 频繁重新部署时，执行中的任务被进程重启打断后
> 不会从头重跑——executor 持久化 claude 会话 id，重启恢复时用 `claude --resume`
> 接续上次会话且保留工作区改动，从上次中断处继续（会话文件丢失时自动降级全新会话）。

## 目录结构

```
backend/
  botler/
    main.py          FastAPI 入口（静态托管前端）
    config.py        config.yaml 加载（${ENV} 展开）+ 写回
    database.py      SQLite 模型（repos / tasks / task_logs）
    gitlab_client.py GitLab REST API 封装（webhook 注册、issue 评论等）
    webhook.py       webhook 接收器（secret 校验 + assignee 判定 + 去重）
    scheduler.py     任务调度器（每仓库 FIFO 串行、跨仓库并行）
    executor.py      Claude Code 执行器（干净工作区 / 超时 / 重试 / 失败评论）
    reconciler.py    对账兜底（APScheduler 定时扫描补漏）
    api/             REST API（repos / tasks / settings）
  config.example.yaml
  requirements.txt
frontend/            React (Vite) Web UI，构建产物由 FastAPI 托管
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

`backend/config.yaml` 是唯一事实来源，Web UI 是编辑它的外壳。凭据一律用 `${ENV_VAR}` 引用环境变量（`backend/.env`），不入库、不进日志、不进提示词。

关键配置（`config.example.yaml` 中有完整示例与注释）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `gitlab.bot_token` | — | bot PAT，经 `GITLAB_BOT_TOKEN` 环境变量引用 |
| `gitlab.webhook_secret` | — | webhook 校验 secret |
| `worker.max_concurrent_repos` | 3 | 跨仓库并行上限 |
| `worker.task_timeout_seconds` | 1800 | 单任务超时（30 分钟） |
| `worker.max_retries` | 2 | 失败重试次数（「无法解决」不重试） |
| `worker.reconcile_interval_seconds` | 300 | 对账兜底扫描间隔 |
| `claude.command` / `args` | `claude -p --output-format json` | 执行命令 |
| `browse.default_path` | 空（服务器用户主目录 `~`） | 目录选择对话框的初始定位目录；支持 `~` 展开，路径不存在时自动回退主目录 |
| `notifications.enabled` | true | 网页通知总开关（任务需交互 / issue 完成 / 队列空 / 无新任务，逐项可关） |

提示词模版支持变量占位符：`{repo_name}` `{issue_title}` `{issue_body}` `{issue_url}` `{gitlab_url}` `{project_id}` `{issue_iid}`。
全局默认模版 + 仓库级覆盖可在 Web UI「模版」页编辑。

## API 一览

```
GET    /api/health                    健康检查
GET    /api/repos                     仓库列表
POST   /api/repos                     添加仓库（自动识别 project_id + 注册 webhook）
GET    /api/repos/browse              浏览服务器目录（无 path 时初始定位到 browse.default_path，默认服务器用户主目录 ~）
POST   /api/repos/discover            读取本地文件夹的 git remote 列表
PUT    /api/repos/{id}                更新仓库（启用/停用/模版覆盖）
DELETE /api/repos/{id}                删除仓库
POST   /api/repos/{id}/test           测试连通性（token + 项目 + webhook）
GET/PUT /api/repos/{id}/template      仓库模版
GET/PUT /api/settings                 系统设置（写回 config.yaml）
POST   /api/settings/reconcile-now    手动触发对账
GET    /api/tasks                     任务列表（分页/过滤，含 commit_sha/commit_url）
GET    /api/tasks/{id}                任务详情（含日志、commit_sha/commit_url）
GET    /api/tasks/{id}/logs           任务日志
GET    /api/tasks/{id}/execution      实时执行（增量日志 + 聊天记录，issue #20）
GET    /api/notifications/events      通知事件增量拉取（游标 after，issue #21）
GET    /api/environment               本地环境检测（服务器上 agent/基础工具安装与版本，issue #22）
POST   /webhook/gitlab                GitLab webhook 入口
```

## 安全说明

- 凭据全部走环境变量，token 只通过子进程环境变量注入 Claude Code（不进提示词 transcript）
- webhook 用 `X-Gitlab-Token` 校验，防伪造请求
- 工作区每次执行前 `reset --hard` + `clean -fd`，不同仓库互相隔离
- 风险认知：main 不保护 + bot 直接推，单点失误可能破坏 main；
  缓解：同仓库串行、干净工作区、模版强调「自测通过才推」。个人自用可接受，如需保护请对 main 加 push 保护。
