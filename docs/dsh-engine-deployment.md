# dsh 引擎部署与接入指南（issue #84）

Botler 的任务执行引擎支持三种实现：**claude**（Claude Code CLI，默认）、
**hermes**（部署机已装好的 hermes-agent）与 **dsh**（deepseek-harness
Python SDK，进程内调用）。`dsh` 引擎经 botler 自带的
`backend/botler/dsh_runner.py` 进程内调用 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
的官方 Python SDK（stdio JSON-RPC 驱动捆绑运行时，无需 Node.js），
模型侧工具（bash / 读写文件 / subagent 等）在 botler 的仓库工作区
执行，git 凭据与 claude / hermes 引擎共用同一套 `GIT_ASKPASS` 注入机制。

**引擎选择**：`config.yaml` 的 `worker.engine`（`claude` / `hermes` / `dsh`），
非法值回退 `claude`。DeepSeek 的 LLM 配置（API Key / Base URL）在部署机
环境变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 配好，botler 不管理。

> ⚠️ SDK 当前为 **developer preview**（`0.1.0rc6`）：官方明示会有兼容性
> 破坏变更，botler 锁定该版本，升级时需按本指南验证后重新回归。

## 1. 前置条件

- 部署机 Python ≥ 3.10（botler 后端 venv 本身即满足），Linux x64/arm64
  或 macOS 14+（arm64）；
- 可访问 DeepSeek API（或兼容端点），且已配置凭据（三选一，见下方
  「凭据解析链」）：
  ```bash
  export DEEPSEEK_API_KEY=sk-xxx        # 方式一：环境变量（SDK 默认读取）
  # export DEEPSEEK_BASE_URL=...        # 可选：OpenAI 兼容代理端点
  ```
  botler 以 pm2 管理时，写进 `data/backend/.env` 即可（load_dotenv 加载）。

### 凭据解析链（issue #115）

dsh 引擎的 API Key / Base URL 按以下优先级解析，命中即用：

1. **config.yaml 的 `dsh.api_key` / `dsh.base_url`** 显式配置；
2. **设置页「AI 供应商」中 `provider: deepseek` 且启用的项**
   （issue #115 起：用户在该处配过的 DeepSeek key 会被 dsh 引擎消费，
   此前配置了却不生效导致任务 401 全部失败）；
3. **环境变量 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`**（SDK 默认
   读取，botler 不覆盖）。

三处均未配置时任务会失败，任务日志/失败详情将显示具体的 API 错误
（如 `模型调用失败: Authentication Fails, ...`），可按此排查。

## 2. 安装 SDK

SDK 不在 `requirements.txt`（与 hermes 部署模式一致，主依赖继续走
清华源，避免 rc 版解析失败阻塞全部依赖安装），按部署形态处理：

### 2.1 Docker 部署（已内置，无需手动安装）

issue #112 起 `Dockerfile` 构建镜像时自动安装 SDK（阿里镜像 +
全版本号，镜像源可用 `DSH_INDEX_URL` build arg 覆盖），构建期还会
做 import 校验（装不上即构建失败）。CI 自动部署（`deploy_to_code01`）
与手动 `docker compose up -d --build` 均自带 SDK，容器内无需任何
额外步骤；`deploy/verify-docker.sh --full` 冒烟含 SDK 可导入校验。

### 2.2 pm2 / systemd 部署（pm2 CI 部署自动安装；手动部署一键脚本）

pm2 部署分两种形态：

- **CI 自动部署（`deploy_to_code01`）**：issue #112 起部署 job 在
  主依赖安装后自动调用 `deploy/install-dsh-sdk.sh` 装进后端 venv，
  无需任何手动操作（用户曾反馈 pm2 实例缺 SDK，此步保证开箱即用）；
- **手动部署（pm2 / systemd 直跑）**：在项目根目录执行一键脚本：

```bash
deploy/install-dsh-sdk.sh                 # 默认装进 backend/.venv
# 指定其他 venv：deploy/install-dsh-sdk.sh /path/to/venv
# 镜像源覆盖（内网代理）：DSH_INDEX_URL=... deploy/install-dsh-sdk.sh
```

脚本幂等（已装目标版本直接跳过），安装后自动做 import 校验
（装不上立即失败）；优先用 uv pip（CI venv 由 uv 创建、无 pip
seed），无 uv 时回退 venv 内 pip。

> 注意：清华 pip 镜像暂未同步 rc 版（仅有 `0.0.0.dev0` 占位），脚本
> 默认走阿里镜像（`DSH_INDEX_URL` 可覆盖）；rc 为预发布版本，安装
> 命令须显式写全版本号（脚本已内置 `deepseek-harness-sdk==0.1.0rc6`，
> 勿手写缩写版本号）。

验证安装：

```bash
backend/.venv/bin/python -c "from deepseek_harness import DeepSeekHarness; print('ok')"
```

未安装时 dsh 任务会失败并提示安装命令（任务日志可见），不影响
claude / hermes 引擎运行。

## 3. botler 侧配置

编辑部署机的 `backend/config.yaml`：

```yaml
worker:
  engine: dsh                        # 切换为 dsh 引擎（默认 claude）

dsh:
  provider: deepseek-official        # provider 路由（SDK 默认组合注册）
  model: deepseek-v4-flash           # 模型 id
  max_tokens: null                   # 可选：单请求输出 token 上限（null=provider 默认）
  session_root: ""                   # 可选：会话持久化目录（断点续跑数据）；
                                     #   留空 = SDK 默认（环境变量 DSH_SESSION_ROOT）
  cordis: ""                         # 可选：自定义 Cordis 配置路径（需保留
                                     #   @deepseek-ai/dsh-sdk-jsonrpc-server 项）
  runtime_bin: ""                    # 可选：自定义 runtime 二进制路径
  base_url: ""                       # 可选：DeepSeek 兼容端点（默认环境 DEEPSEEK_BASE_URL）
  api_key: ""                        # 可选：默认环境 DEEPSEEK_API_KEY；支持 ${ENV} 引用
```

切回 claude 引擎只需把 `worker.engine` 改回 `claude`，dsh 段可保留。
dsh 段同样可通过设置页 API 写回（`PUT /api/settings` 的 dsh 段），
`api_key` 回显掩码。

## 4. 停止 / 超时语义（与子进程引擎的差异）

claude / hermes 引擎由 botler spawn 子进程（可 SIGKILL 进程组）；dsh 的
SDK 在 botler 进程内运行，停止与超时通过「关闭运行时」实现：任务停止
或超时时 botler 终止 SDK 运行时子进程（等价 SIGKILL 语义），已完成回合
的会话数据已增量落盘（`session_root`），下次可断点续跑恢复。

## 5. 断点续跑

dsh 引擎与 claude / hermes 引擎等价支持断点续跑：SDK 以 JSONL + checkpoint
在 `session_root` 持久化会话，每次执行结束后 executor 把会话 id 落库到
`tasks.dsh_session_id`；重试或平台重启恢复时以同一会话 id 接续对话
（工作区保留不清空），从上次中断处继续而非从头重跑。

## 6. 验证

1. 修改 `worker.engine: dsh` 后重启 botler；
2. 在 GitLab 把一个测试 issue 指派给 bot 账号，观察任务页日志出现
   「执行 dsh 引擎（工作区 …）」；
3. 任务完成后 issue 收到结果评论并打 `bot-done` 标签（与 claude 引擎
   相同的收尾流程，包括 CI 流水线等待）。

## 7. 故障排查

| 现象 | 排查 |
|------|------|
| 任务日志报「dsh 引擎需要 deepseek-harness-sdk，未安装」 | Docker 部署：重新构建镜像（issue #112 起镜像已内置 SDK）；pm2 CI 部署：重跑流水线（issue #112 起 deploy job 自动安装）；手动 pm2/systemd 部署：执行 `deploy/install-dsh-sdk.sh`（阿里镜像 + 全版本号已内置） |
| 结果行 `finish_reason: error` | DeepSeek API 调用失败：先看任务日志/失败详情中的具体错误（issue #115 起 `turn/end` 与 `assistant/chunk` 的 error/failure message 已透传，如 401 AUTH / 400 模型名 / 网络超时），再按「凭据解析链」检查 `dsh.api_key` / 设置页 AI 供应商 deepseek 项 / `DEEPSEEK_API_KEY` 与 `DEEPSEEK_BASE_URL`、网络连通性 |
| 结果行 `finish_reason: max-tokens` | 输出超上限被截断：调大 `dsh.max_tokens` 或简化任务 |
| 任务反复重试、日志尾部结果行含 error 字段 | 查看结果行 error 文本（含异常类型与消息）与事件行定位 |
| 设置页「本地环境检测」dsh 未安装 | SDK 未装或不在 botler 的 venv：Docker 部署重建镜像；pm2 CI 部署重跑流水线；手动 pm2/systemd 部署执行 `deploy/install-dsh-sdk.sh`（检测项为 pip 包检测，不走 PATH） |
| dsh 不操作工作区文件 | 检查 `dsh.session_root` 目录可写性与工作区路径（executor 自动以仓库工作区为 cwd） |
