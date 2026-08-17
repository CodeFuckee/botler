# Botler — GitLab AI Issue Bot 平台

> Bot + Butler，机器人管家

运行在服务器上的自动化平台：统一配置多个 GitLab 仓库，通过 webhook 实时监控 issue，
当 issue 被指派给 bot 账号时，自动调用 **Claude Code CLI（无头模式）** 处理并推送修复到 main，最后关闭 issue。
执行引擎可切换为 **hermes-agent**（部署机已装好时，见 [docs/hermes-engine-deployment.md](docs/hermes-engine-deployment.md)）
或 **deepseek-harness**（Python SDK 进程内调用，见 [docs/dsh-engine-deployment.md](docs/dsh-engine-deployment.md)）；
切换入口在 Web 设置页「任务调度」卡片的 `worker.engine` 设置项（issue #113）。

完整设计见 [`docs/设计方案.md`](docs/设计方案.md)；
UI 优化参考与同类开源项目调研见 [`docs/ui-design-reference.md`](docs/ui-design-reference.md)（issue #121 调研产出）。

## 工作原理

```
用户在 GitLab 把 issue 指派给 bot 账号
        │  webhook（issue 事件）
        ▼
Webhook 接收器 ──► 任务调度器（SQLite，同仓库串行/跨仓库并行）
        ▲                 │
        │                 ▼
对账兜底调度器 ◄──── Claude Code 执行器（自动切回默认主分支 + 干净工作区 + 模版渲染 + 超时/重试）
（每 5 分钟扫漏网 issue）   │
                          ├─► git push 到 main（Claude 自己执行）
                          └─► 调 GitLab API 关闭 issue（Claude 自己执行）
```

调度器派发顺序：仓库优先级（数字小先）→ 同仓库队列内按 issue 标签优先级
（默认 `bug` > `test` > `feature`，设置页可自定义），同优先级按 issue 创建时间升序
（创建时间越早的 issue 越先处理）。

> 💡 **定时暂停窗口**（issue #169）：可在设置页「任务调度」卡片配置暂停窗口
>（如 `09:00-12:00`、`14:00-18:00`，支持多窗口、跨天、星期与时区）。窗口内
> 调度器**停止开始新任务**（webhook / 对账仍照常入队），**已经开始执行的任务
> 可以继续执行**，**未开始执行的任务保留在队列中，等到窗口结束后自动开始
> 执行**；窗口状态实时计算，无需重启服务。

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
    executor.py      执行器（引擎分发走插件体系，任务开始自动切回默认主分支 + git pull（拉取冲突保留现场交由 agent 手工合并）/ 超时 / 重试 / 失败评论）
    plugins/         插件体系（issue #140）：base（PluginKind / PluginRegistry 注册表）/
                     executors（执行引擎插件 claude / hermes / dsh）/ models（大模型供应商
                     插件 gemini / openai）/ notifiers（任务消息通道插件 webhook / in_app）；
                     支持 worker.plugin_paths 外部加载新插件
    hermes_runner.py hermes 引擎 runner 脚本（hermes venv 进程内调用 AIAgent，stdin/stdout JSON 协议）
    dsh_runner.py    dsh 引擎 runner（deepseek-harness SDK 进程内调用，线程运行 + 停止/超时关闭运行时）
    reconciler.py    对账兜底（APScheduler 定时扫描补漏）
    image_models.py  生图模型调用接口封装（Gemini Nano Banana Pro / GPT Image 2，统一 ImageModelClient，issue #135/#137，含配置可用性测试端点）
    vision_models.py 识图模型调用接口封装（Gemini 视觉 / OpenAI 视觉 / 自定义 OpenAI 兼容视觉，统一 VisionModelClient，issue #152，含测试端点：上传图片调用模型描述图片；issue #163 起支持图片先哈希上传 MinIO、识图请求传 http URL；issue #164 起 OpenAI 兼容识图模型禁止 base64 内联，未启用 MinIO 时明确报错引导）
    minio_client.py  MinIO 对象存储客户端（issue #163：识图图片 SHA-256 哈希命名上传，返回 http URL，配合 vision_models 使用；issue #164：默认桶 public、不存在自动创建并自动设为公开只读）
    chat_models.py   灵感 AI 对话模型调用封装（issue #166：复用设置页「AI API 供应商」配置，支持 OpenAI 兼容 chat/completions / Gemini generateContent / Anthropic messages 三种协议，统一 ChatModelClient）
    auth.py          Synology SSO（OIDC 客户端 / 签名会话 / API 保护中间件）
    api/             REST API（repos / tasks / settings / auth）
  config.example.yaml
  requirements.txt
frontend/            React (Vite) Web UI，构建产物由 FastAPI 托管
frontend/e2e/        Playwright 浏览器级 E2E（issue #212）：tests/ 用例、
                     fixtures/ mock 夹具、support/ 浏览器级 mock API、
                     scripts/ 服务编排与种子数据库、backend-config.yaml
docs/                文档（设计方案 / UI 优化参考 / Synology SSO 配置指南）
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
> 前端「添加 Issue」弹窗的标题输入框右侧提供语音输入按钮（🎤，issue #165）：
> 使用浏览器原生 Web Speech API（`SpeechRecognition`）将语音实时转文字填入
> 标题，无需后端接口；支持 Chrome / Edge / Safari，Firefox 等不支持时会给出
> 中文提示，识别中再次点击按钮可停止。

> 概览页「灵感」板块每条灵感提供「💬 对话」按钮（issue #166）：打开面板即可
> 围绕该灵感与 AI agent 多轮探讨（完善想法、补充边界场景、评估可行性、给出
> 分步落地建议），对话历史保存到本地数据库。对话模型复用设置页「AI API
> 供应商」（ai_providers）配置的文本对话模型——取列表第一个启用且 API Key
> 非空的项（支持 DeepSeek / OpenAI / Gemini / Anthropic 等，未配置时给出
> 中文提示引导到设置页）；AI 回复失败时输入保留可重试。

## 测试

```bash
# 后端单元测试（pytest，API 单测 + 执行器 + 数据库迁移等）
cd backend && .venv/bin/python -m pytest tests/ -q

# 前端单元测试（node --test：源码静态断言 + react-test-renderer 渲染断言）
cd frontend && npm test

# 覆盖率（issue #210）：pytest-cov 统计后端、c8（v8 原生）统计前端
cd backend && .venv/bin/python -m pytest tests/ -q --cov=botler --cov-report=term-missing
cd frontend && npm run test:coverage
```
覆盖率说明（issue #210）：
- **后端**：`pytest-cov` 按 `--cov=botler` 统计全部后端模块，CI 上传
  `coverage.xml`（Cobertura）至 GitLab coverage_report，MR 页面显示覆盖率
  对比；总覆盖率阈值 **70%**（`--cov-fail-under=70`），低于阈值流水线阻断；
- **前端**：`c8` 统计 `src/` 源码，CI 上传 `coverage/cobertura-coverage.xml`；
  行/语句 **70%**、分支 **60%**、函数 **50%** 阈值（`--check-coverage`），
  低于阈值流水线阻断；
- **徽章**：项目主页覆盖率徽章解析后端 pytest-cov 日志中的 `TOTAL` 行
  （`test_coverage_regex`），CI 成功后在
  「项目 → 设置 → 通用 → 徽章」可查看/引用 `badges/main/coverage.svg`。

# 浏览器级 E2E（Playwright，issue #212）：一键起真实后端 + 前端构建产物 + 真浏览器
# 前置：backend/.venv 就绪、frontend dist 已构建（脚本缺失时自动构建）
bash frontend/e2e/scripts/start-servers.sh                 # 跑全部 E2E
bash frontend/e2e/scripts/start-servers.sh tests/overview.spec.js   # 跑单个文件
```

### E2E 架构（issue #212）

- **链路**：真实浏览器（Chromium）→ vite preview（前端构建产物，SPA 路由，`/api`
  经 `preview.proxy` 代理）→ uvicorn（真实 FastAPI 后端，独立端口 8011 避开生产
  8000，`BOTLER_CONFIG` / `BOTLER_DB` 指向临时目录）；
- **mock API 模式**：只拦截依赖真实 GitLab 的接口（`issues/overview`、
  `pipelines/overview`、`issues/form-meta`、`POST issues`，见
  `e2e/support/mock-api.js`），其余接口（settings / tasks / 灵感 / 通知 / auth /
  SSE 事件流）走真实后端——前后端契约与 SSE 实时交互真实验证、零真实 GitLab 依赖、
  数据完全确定（种子数据库 + mock 夹具）；
- **覆盖链路**：概览页加载与 issue 展示 / 添加 Issue 弹窗提交（含必填校验）/ 设置页
  保存配置（重载验证持久化）/ 任务详情 SSE 事件流（后端回放种子执行日志逐事件渲染）；
- **防 flaky**：`playwright.config.js` 配置 `retries: 2` + trace 保留；CI
  `.gitlab-ci.yml` 新增 `e2e` stage（build 之后、deploy 之前，E2E 未通过不部署）。

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
deploy/install-dsh-sdk.sh                # 安装 dsh 引擎 SDK（可选依赖，issue #112）
deploy/install-minio.sh                # 安装 MinIO Server 二进制（对象存储服务，issue #160）
deploy/nginx-minio-public.conf        # nginx 代理 MinIO public 桶配置（识图模型图片访问，issue #164）

# 启动（三选一）
pm2 start deploy/botler.config.cjs && pm2 save && pm2 startup
#   pm2 配置含 botler + botler-minio 两个 app（MinIO 随 botler 一并托管，
#   数据目录 $BOTLER_DATA_DIR/minio/data，控制台 http://10.0.0.122:9001）
# 或
sudo cp deploy/botler.service /etc/systemd/system/ && sudo systemctl enable --now botler
# 或 Docker（见下方「Docker 部署」）
```

> 💡 **CI/CD 自动部署**：推送 main 分支后，GitLab CI 自动执行 pm2 部署
> （`deploy_to_code01`：安装依赖 → 停止旧服务 → pm2 启动 → 健康检查），
> 无需手动操作。dsh 引擎 SDK 与 MinIO Server 二进制均在部署 job 内自动
> 安装（issue #112 / #160）；MinIO 凭据（`MINIO_ROOT_USER` /
> `MINIO_ROOT_PASSWORD`）自动写入 `data/backend/.env`（缺失时用 CI 变量
> 或默认值），pm2 与 docker compose 两种部署形态同源保持一致。
> 部署前会自动停止旧 pm2 服务，凭据优先用服务器上 `backend/.env`（缺失时用
> CI 变量生成）。

冒烟测试：浏览器打开 `http://10.0.0.122:8000` → 添加仓库（自动注册 webhook）→
在 GitLab 建一个测试 issue 指派给 bot → 观察任务列表，验证代码推上 main、issue 自动关闭。

## Docker 部署

镜像已包含全部运行时（前端构建产物、Python 依赖、`git`、`claude` CLI、dsh 引擎
deepseek-harness SDK（issue #112，构建期自动安装，`worker.engine: dsh` 开箱即用），
只需挂载配置与数据。

前置条件：Docker 20.10+ / docker compose v2。

```bash
# 1. 准备配置（数据统一放在 data/ 目录下，容器重建不丢；目录不存在会自动创建）
mkdir -p data/backend
cp backend/config.example.yaml data/backend/config.yaml
cp backend/.env.example data/backend/.env   # 填入 GITLAB_BOT_TOKEN / WEBHOOK_SECRET / ANTHROPIC_*
touch data/backend/botler.db                # SQLite 库（首次运行自动建表）

# 2. 构建并启动
docker compose up -d --build
# 国内无法访问 Docker Hub 时，用环境变量覆盖基础镜像 / MinIO 镜像：
#   NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine \
#   RUNTIME_IMAGE=docker.m.daocloud.io/library/node:20-bookworm-slim \
#   MINIO_IMAGE=docker.m.daocloud.io/minio/minio:RELEASE.2025-04-22T22-12-26Z \
#   docker compose up -d --build

# 3. 验证
docker compose ps                          # 状态 healthy（botler + minio）
curl http://localhost:8000/api/health      # {"ok":true,...}
curl http://localhost:9000/minio/health/live   # MinIO 健康检查（issue #160）
./deploy/verify-docker.sh --full           # 冒烟检查（临时数据目录，不碰真实数据，含 MinIO）
```

数据持久化（全部卷挂载，容器重建不丢失）：`data/backend/config.yaml`（Web UI 设置页会写回）、
`data/backend/.env`（只读）、`data/backend/botler.db`、`data/workspace/`、`data/logs/`、
`data/claude-home/`（claude 会话文件 `~/.claude`，断点续跑依赖——容器重建后
`--resume` 才能找到上次会话；pm2 部署下在宿主 HOME 天然持久，无需此挂载）。
**MinIO 对象存储（issue #160）**：compose 启动的 `minio` 服务数据挂在
`data/minio/data`（与 botler 数据同根目录，容器重建不丢失）；根凭据默认
`minioadmin/minioadmin`，生产环境务必用环境变量覆盖（见下表）。
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
| `MINIO_IMAGE` | `minio/minio:RELEASE.2025-04-22T22-12-26Z` | MinIO 镜像（国内镜像源覆盖，如 `docker.m.daocloud.io/minio/minio`） |
| `MINIO_API_PORT` / `MINIO_CONSOLE_PORT` | `9000` / `9001` | MinIO API / console 端口映射（pm2 形态固定 9000/9001） |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` / `minioadmin` | MinIO 根凭据（pm2 部署同款，CI 自动写入 `data/backend/.env`，生产环境务必覆盖） |

自定义镜像：`docker build -t botler:latest .`（见 Dockerfile 头部注释）。
停止：`docker compose down`（数据保留）；删除数据：`docker compose down -v`。

## 插件体系（issue #140）

平台把三类能力统一为**插件**，注册进全局插件注册表（`botler.plugins.PluginRegistry`），
调用方只面向注册表编程；新增能力无需改动核心模块：

| 插件分类 | 能力 | 内置插件 |
|---|---|---|
| `executor` | 任务执行引擎（`worker.engine` 选择） | `claude`（Claude Code CLI）/ `hermes`（hermes-agent）/ `dsh`（deepseek-harness SDK） |
| `model_provider` | 大模型 API 供应商（生图模型 provider 选择） | `gemini_nano_banana`（Gemini generateContent）/ `openai_gpt_image`（OpenAI images API） |
| `notifier` | 任务消息发送通道（任务收尾自动分发） | `webhook`（外部 HTTP 推送，issue #136）/ `in_app`（网页通知，issue #21） |

- **向后兼容**：现有 `config.yaml` 全部配置字段与默认行为不变，存量部署零迁移；
- **外部扩展**：`worker.plugin_paths` 声明 Python 模块路径（模块内调用
  `botler.plugins.register_plugin` 注册），启动时加载，可新增引擎 / 供应商 / 发送通道；
- **统一容错**：任一通道失败仅记日志，绝不阻塞任务收尾。

**插件管理页（issue #145）**：顶部导航「插件」入口（`/plugins`）集中管理
所有插件的安装、卸载与设置——
- *安装*：输入外部插件模块路径（每行一个，即 `worker.plugin_paths` 扩展点），
  后端校验（文件存在 / 模块可加载 / 至少注册一个插件 / 与已安装插件无冲突）
  通过后写入配置并热加载，无需重启即生效；
- *卸载*：仅外部插件可卸载（配置与注册表同时移除，内置插件带「内置」徽章）；
- *设置*：默认执行引擎（executor 插件，复用 `worker.engine`，与设置页
  「任务调度」卡片同源）+ 外部插件重新加载；列表同时展示模型供应商插件的
  默认接口 / 模型预设。

完整设计见 [`docs/插件体系设计方案.md`](docs/插件体系设计方案.md)（接口定义 / 迁移清单 / 测试计划 / 演进方向）。

## 配置说明

`backend/config.yaml` 是唯一事实来源，Web UI 是编辑它的外壳。直接编辑 config.yaml 的修改会被运行中的进程自动感知（检测文件变化后重载，无需重启；issue #25），且后续 Web UI 保存设置不会覆盖手动编辑的内容。凭据一律用 `${ENV_VAR}` 引用环境变量（`backend/.env`），不入库、不进日志、不进提示词。

设置页设置项较多时，左侧导航栏按功能分组整理全部设置项（issue #139）——分组：外部服务接入（Synology SSO 登录 / AI API 供应商 / 生图模型）、系统设置（任务调度 / 界面显示 / 网页通知 / 消息推送 Webhook）、执行引擎（Claude Code / dsh 引擎）、运维与数据（本地环境检测 / 数据备份）、账号与安全（Owner GitLab Token / GitLab 凭据）、关于（版本信息）。导航栏支持**关键词搜索设置项**（名称与关键字命中，含中英文别名）与**分组折叠/展开**（可「全部收起 / 全部展开」），点击子项平滑滚动到页面相应设置区块并高亮；导航面板可**整体折叠**成 44px 窄栏（仅保留「展开侧边栏」入口，issue #168），折叠后内容区占满全宽、最大化编辑空间，折叠偏好本地持久化（刷新保持）；窄视口（≤860px）自动回落单栏，导航置于页面顶部。

关键配置（`config.example.yaml` 中有完整示例与注释）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `gitlab.bot_token` | — | bot PAT，经 `GITLAB_BOT_TOKEN` 环境变量引用 |
| `gitlab.webhook_secret` | — | webhook 校验 secret |
| `worker.max_concurrent_repos` | 3 | 跨仓库并行上限 |
| `repos[].priority` | 100 | 仓库调度优先级（1~999 整数，数字越小越优先；多个仓库同时有排队任务时按优先级派发，同优先级按任务提交时间排序） |
| `worker.issue_priority` | `["bug","test","feature"]` | issue 标签处理优先级（同仓库队列内按此顺序选任务派发，越靠前越先处理；未列出的标签排在最后，同优先级按 issue 创建时间升序（创建早的 issue 先处理）；设置页「任务调度」卡片可修改） |
| `worker.task_timeout_seconds` | 1800 | 单任务超时（30 分钟） |
| `worker.max_retries` | 2 | 失败重试次数（「无法解决」不重试） |
| `worker.reconcile_interval_seconds` | 300 | 对账兜底扫描间隔 |
| `worker.engine` | `claude` | 任务执行引擎（插件体系，issue #140）：`claude`（Claude Code CLI）/ `hermes`（部署机已装好的 hermes-agent）/ `dsh`（deepseek-harness SDK）；引擎名对应执行引擎插件，非法值回退 `claude`（issue #47/#84）；设置页「任务调度」卡片可切换（issue #113） |
| `worker.pause_windows` | `[]` | 定时暂停窗口（issue #169）：窗口串数组（`HH:MM-HH:MM`，24 小时制，支持跨天如 `22:00-02:00`）。窗口内停止开始新任务，已经开始执行的任务可以继续执行，未开始执行的任务等到窗口结束后自动开始执行；空数组 = 不启用（默认）。设置页「任务调度」卡片可编辑 |
| `worker.pause_weekdays` | `[]` | 定时暂停窗口生效星期（0=周一 … 6=周日）；空 = 每天都生效（issue #169） |
| `worker.pause_timezone` | 空 | 定时暂停窗口判断所用时区（IANA 名，如 `Asia/Shanghai`）；空 = 服务器本地时区（issue #169） |
| `worker.plugin_paths` | `[]` | 外部插件加载（issue #140）：Python 模块路径列表，应用启动时逐个加载注册进插件体系（新增执行引擎 / 大模型供应商 / 消息发送通道）；模块内调用 `botler.plugins.register_plugin` 完成登记，加载失败仅记日志不阻塞启动 |
| `claude.command` / `args` | `claude -p --output-format stream-json --verbose` | claude 引擎执行命令（stream-json 逐行实时输出，任务页面逐事件查看执行过程） |
| `hermes.command` / `args` | — | hermes 引擎执行命令（部署机 hermes venv 的 python + `backend/hermes_runner.py`），部署见 `docs/hermes-engine-deployment.md` |
| `dsh.provider` / `model` | `deepseek-official` / `deepseek-v4-flash` | dsh 引擎运行参数（provider 路由 / 模型 id），Key 走环境变量 `DEEPSEEK_API_KEY` |
| `dsh.max_tokens` / `session_root` / `cordis` / `runtime_bin` / `base_url` / `api_key` | — | dsh 引擎可选参数（输出上限 / 会话持久化目录 / 自定义 Cordis 配置 / 自定义 runtime / 兼容端点），部署见 `docs/dsh-engine-deployment.md` |
| `dsh.reasoning_effort` | 空（SDK 默认 high） | dsh 引擎推理等级（issue #123）：`off` / `high` / `max`，设置页「dsh 引擎」卡片可选；设置后自动派生 Cordis 注入 SDK 运行时 |
| `browse.default_path` | 空（服务器用户主目录 `~`） | 目录选择对话框的初始定位目录；支持 `~` 展开，路径不存在时自动回退主目录 |
| `ai_providers[]` | 空列表 | AI API 供应商配置（设置页「AI API 供应商」卡片增删改查，issue #46）：每项 `{name, provider, base_url, api_key, model, enabled}`，内置 DeepSeek / OpenAI / Anthropic / Gemini / Moonshot / 通义千问 / 智谱 / 硅基流动 / Ollama / OpenRouter 预设；api_key 落盘 config.yaml（支持 `${ENV}` 引用），API 只返回掩码。dsh 引擎未配 api_key 时回退 provider=deepseek 且启用的项（issue #115）；概览页「DeepSeek 账户余额」（issue #138）同样按此链解析 Key（dsh 段 > AI 供应商 deepseek 项 > 环境变量 `DEEPSEEK_API_KEY`）代调 `GET https://api.deepseek.com/user/balance` 展示余额 |
| `image_models[]` | 空列表 | 生图模型配置（设置页「生图模型」卡片增删改查，issue #135/#137）：每项 `{name, provider, base_url, api_key, model, enabled}`，内置 Gemini Nano Banana Pro（默认模型 `gemini-3-pro-image`，generateContent 接口）与 GPT Image 2（默认模型 `gpt-image-2`，OpenAI images 接口）两个预设；api_key 落盘 config.yaml（支持 `${ENV}` 引用），API 只返回掩码；后端 `image_models.py` 提供统一调用封装（自定义 base_url 且不等于预设默认时视为完整请求地址直接使用，不再拼接接口路径；留空或等于预设默认按官方接口拼接），设置页测试按钮走 `POST /api/settings/image-model-test` 真实调用一次生图接口验证配置可用；OpenAI 兼容接口若返回 SSE 流（`text/event-stream`，多行 `data: {json}` 事件逐步上报进度、最终 `status: "succeeded"` 且 `results[].url` 为生成图片地址）自动按事件解析并下载图片返回，任务失败时展示 `failure_reason` / `error` 原因（issue #151） |
| `ui.show_disabled_repos` | true | 灵感 / CI/CD 页面是否显示未启用项目（issue #142）：`true` = 显示（未启用仓库带「未启用」徽章）；`false` = 两个板块只展示已启用仓库（后端接口直接过滤，未启用仓库不再发起 GitLab 流水线查询）；设置页「界面显示」卡片可切换，保存后立即生效 |
| `notifications.enabled` | true | 网页通知总开关（任务需交互 / issue 完成 / 队列空 / 无新任务，逐项可关） |
| `webhook.enabled` | false | Webhook 消息推送总开关（issue #136）：任务完成（成功收尾）时调用 webhook 推送消息；设置页「消息推送 Webhook」卡片可配置，卡片内提供独立「保存 Webhook 配置」按钮（issue #141），也可用上方「任务调度」卡片全局「保存」 |
| `webhook.url` / `content_type` / `authorization` | — | webhook 地址（POST 目标，须 http(s):// 开头）/ Content-Type 请求头（默认 `application/json`）/ Authorization 请求头（可选，支持 `${ENV}` 引用） |
| `webhook.body_template` | 内置默认 JSON 模板 | POST 结构体模板，可使用全局模板占位符（`{repo_name}` `{issue_title}` `{issue_body}` `{issue_url}` `{gitlab_url}` `{project_id}` `{issue_iid}` `{project_path}` `{project_path_encoded}` `{gitlab_host}`），请求时自动填充；留空 = 内置默认模板 |
| `sso.enabled` | false | Synology SSO 登录总开关：启用后访问 Web UI 需用群晖账号登录（issue #27） |
| `sso.well_known_url` / `client_id` / `client_secret` | — | 群晖 SSO Server 的 OIDC 接入参数（Well-known URL / Application ID / Secret） |
| `sso.session_days` | 7 | 登录有效期（天，1~365） |
| `sso.redirect_uri` | 空（自动生成） | 回调地址，须与群晖侧注册一致 |
| `sso.verify_ssl` | true | 群晖为自签名证书时设 false |
| `minio.enabled` | false | MinIO 对象存储开关（issue #163/#164）：启用后识图模型调用时用户上传的图片先计算 SHA-256 哈希、以哈希值为对象名上传 MinIO，识图请求传 http URL 而非 base64（图片 base64 可达数十万字符，网关/模型对请求体大小敏感；阿里云百炼等兼容网关直接拒绝 data: URL）。**issue #164 起 OpenAI 兼容识图模型（openai_vision / custom）禁止 base64 内联：未启用/配置不完整时识图测试明确报错引导启用 MinIO，不再静默回退**（Gemini 官方接口仅支持 base64 inline_data，保持内联） |
| `minio.endpoint` / `secure` / `verify_ssl` | `127.0.0.1:9000` / false / true | MinIO API 地址（host:port）/ 是否 https / 证书校验（自签证书设 false） |
| `minio.access_key` / `secret_key` | 回退环境变量 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO 访问凭据（与部署写入 `data/backend/.env` 的凭据同源；支持 `${ENV}` 引用） |
| `minio.bucket` | `public` | 识图图片对象桶（默认 `public`，不存在自动创建，并自动设为公开只读——匿名 `s3:GetObject`，识图模型可匿名取图） |
| `minio.public_base_url` | 空 | 识图模型取图的 http(s) 前缀，对象 URL = `public_base_url/bucket/<sha256 哈希>`；**识图模型必须能访问该地址**（建议用 nginx 代理 MinIO 桶，配置见 `deploy/nginx-minio-public.conf`，填 nginx 代理地址如 `https://home.chenkaidi.top:509/minio-public`，无需暴露 9000 端口；自建网关走内网地址；外部模型供应商需公网可达） |

提示词模版支持变量占位符：`{repo_name}` `{issue_title}` `{issue_body}` `{issue_url}` `{gitlab_url}` `{project_id}` `{issue_iid}`。
全局默认模版 + 仓库级覆盖可在 Web UI「模版」页编辑。
中断恢复模版（平台重启/中断后恢复会话的引导语，claude/hermes/dsh 三引擎通用）同机制可编辑：留空保存即恢复内置默认（issue #116）。

## API 一览

```
GET    /api/health                    健康检查
GET    /api/repos                     仓库列表
POST   /api/repos                     添加仓库（自动识别 project_id + 注册 webhook + 在目标 GitLab 项目补齐标记库缺失的默认标签（issue #157）；priority 1~999 缺省 100，Web UI 添加仓库表单可填写调度优先级，issue #161）
GET    /api/repos/browse              浏览服务器目录（无 path 时初始定位到 browse.default_path，默认服务器用户主目录 ~）
POST   /api/repos/discover            读取本地文件夹的 git remote 列表
PUT    /api/repos/{id}                更新仓库（名称/启用/优先级/模版覆盖）
DELETE /api/repos/{id}                删除仓库
POST   /api/repos/{id}/test           测试连通性（token + 项目 + webhook）
POST   /api/repos/{id}/reconcile     立即扫描该仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入队列（仓库页与概览页「对账」按钮，issue #17/#134）
POST   /api/repos/{id}/remote-user   读取仓库 remote url 获取仓库用户（remote url userinfo 用户名，如 https://user:token@host/... 的 user；读取顺序：local_path 的 git remote → workspace 克隆 → 存储 url；结果落库并作为灵感「添加 Issue」的默认分配人，issue #153）
GET/PUT /api/repos/{id}/template      仓库模版
GET/PUT /api/settings                 系统设置（写回 config.yaml；worker.engine 为全局默认执行引擎，issue #113）
GET    /api/plugins                    插件列表（按分类分组，含内置/外部来源与供应商预设；插件管理页数据源，issue #145）
POST   /api/plugins/install            安装外部插件模块（校验后写入 worker.plugin_paths 并热加载；失败不落盘，issue #145）
POST   /api/plugins/uninstall          卸载外部插件（配置与注册表同时移除；内置插件不可卸载，issue #145）
POST   /api/plugins/reload             按 worker.plugin_paths 清空并重载外部插件（issue #145）
PUT    /api/plugins/settings           插件设置：默认执行引擎（executor 插件，复用 worker.engine，issue #145）
POST   /api/settings/reconcile-now    手动触发对账
GET    /api/settings/deepseek-balance  DeepSeek 账户余额（概览页余额卡片数据源：设置里配置了 deepseek api 时后端代调 user/balance 接口返回余额，API Key 明文不外发，issue #138）
GET    /api/tasks                     任务列表（分页/过滤，含 commit_sha/commit_url）
GET    /api/tasks/{id}                任务详情（含日志、commit_sha/commit_url）
GET    /api/tasks/{id}/logs           任务日志
GET    /api/tasks/{id}/execution      实时执行（增量日志 + 聊天记录，issue #20）
GET    /api/tasks/{id}/events         任务事件流（SSE 推送：thinking/文本/工具调用/结果逐事件实时可见；终态任务连接后回放历史事件，见实时输出功能）
GET    /api/issues/overview           已启用仓库开放 issue 聚合（10s 缓存，issue #64）
POST   /api/issues/{project_id}/{iid}/close   关闭指定 issue（概览页右边栏「关闭 issue」按钮，issue #94）
POST   /api/issues/{project_id}/{iid}/retry   重新执行 issue 对应的任务（概览页右边栏「重试」按钮：复用最近失败/中断任务或新建任务入队，issue #117）
GET    /api/issues/{project_id}/{iid}/detail  issue 评论与活动详情（评论/系统活动分区，最多 100 条，issue #97；含 engine 字段——该 issue 最近任务实际使用的执行引擎，issue #120）
GET    /api/issues/{project_id}/{iid}/tasks 该 issue 的全部任务执行记录（id 倒序最新在前，同 issue 多条任务记录全部返回；概览页右边栏「查看执行的详情」数据源，issue #167）
POST   /api/issues/{project_id}/{iid}/comments   添加 issue 评论（概览页右边栏「添加评论」，正文必填，成功后清缓存并返回精简评论，issue #125）
POST   /api/issues/{project_id}/{iid}/comments/{note_id}/reply   回复 issue 某条评论（概览页右边栏「回复评论」，后端经 discussions API 解析评论所在线程后追加回复，issue #125）
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
GET    /api/issues/{project_id}/{iid}/detail  issue 评论与活动详情（评论/系统活动分区，最多 100 条，issue #97；含 engine 字段——该 issue 最近任务实际使用的执行引擎，issue #120）
GET    /api/inspirations/overview      概览页灵感聚合：所有未软删除仓库 + 各自灵感（仓库按优先级排序，灵感按 updated_at 降序，issue #131）
POST   /api/inspirations              记录一条灵感（repo_id + content 必填；内容去首尾空白后非空且 ≤ 5000 字；默认仅存本地数据库，issue #131）
PUT    /api/inspirations/{id}         更新灵感内容（刷新 updated_at，issue #131）
DELETE /api/inspirations/{id}         删除灵感（issue #131）
POST   /api/inspirations/{id}/add-issue  将灵感一键提交为 GitLab issue（issue #143/#153/#162）：灵感内容同时作为标题与描述，默认标签 feature + ui，分配人 = 仓库用户（仓库设置页读取 remote url 得到的用户名，按项目成员解析为 GitLab 用户 id；未配置/解析失败则不指定分配人）；写操作必须配置 owner token，创建成功后清概览缓存并从灵感列表删除该灵感（issue #162，失败保留可重试）
GET    /api/inspirations/{id}/messages   返回灵感与 AI agent 的对话历史（按时间升序；issue #166）
POST   /api/inspirations/{id}/messages   向 AI agent 发送一条消息并返回回复（issue #166）：用户消息 + AI 回复成对保存到本地数据库，对话模型复用设置页「AI API 供应商」第一个启用且 API Key 非空的项（未配置返回 400 引导设置）；AI 调用失败返回 502 并回滚已保存的用户消息（对话历史保持成对完整，前端保留输入可重试）
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
