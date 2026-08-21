# hermes 引擎部署与接入指南（issue #171：hermes agent SDK 进程内集成）

Botler 的任务执行引擎支持三种实现：**claude**（Claude Code CLI，默认）、
**hermes**（[hermes-agent](https://github.com/NousResearch/hermes-agent)
SDK，进程内调用 `run_agent.AIAgent`）与 **dsh**（deepseek-harness SDK）。

> **集成方式变更（issue #171）**：hermes 引擎从「子进程 + 部署机独立
> hermes venv（`hermes.command` / `hermes.args` 配置 + Docker 挂载独立
> venv）」改为「**hermes agent SDK 进程内集成**」——hermes-agent 以源码
> editable 安装进 **botler 自身 venv**，`HermesSdkRunner`
> （`backend/botler/hermes_sdk_runner.py`）在 botler 进程内 worker 线程
> 调用 `run_agent.AIAgent`（对齐 dsh 引擎的 SDK 集成方式，issue #84）。
> 人工停止经 `AIAgent.interrupt()` 跨线程中断（语义等价旧模式的
> SIGKILL 进程组）。输出协议不变（事件行 + 结果行），SSE 实时输出 /
> 断点续跑（`hermes_history` 落库）与 claude/dsh 引擎共享的设施全部复用。
> 旧的 `hermes.command` / `hermes.args` 配置键已移除，不再需要挂载
> hermes venv 或配置 venv python 路径。

**引擎选择**：`config.yaml` 的 `worker.engine`（`claude` / `hermes` / `dsh`），
非法值回退 `claude`。hermes 的 LLM 配置（模型 / API Key）在 hermes 侧
`~/.hermes` 已配好，botler 不管理，也不在设置页提供 hermes 配置 UI。

## 1. 前置条件

部署机（运行 botler 的 NAS）上已安装 hermes-agent 源码（如
`~/.hermes/hermes-agent`），且：

- `~/.hermes/config.yaml` / `~/.hermes/.env` 的 LLM 供应商已配置完成
  （`hermes chat` 可正常对话即可）；
- hermes-agent 源码目录含 `pyproject.toml`（git clone 或源码分发均可）。

若尚未安装 hermes-agent，可参考 shipyard 仓库的
`docs/hermes-agent-deployment.md`（Docker / 源码两种方式）。

## 2. SDK 安装（hermes-agent → botler venv）

hermes-agent 以源码分发（PyPI 无 wheel，setup.py 禁 wheel 构建），只能
**editable 安装**进 botler 自身 venv：

### pm2 / systemd 部署

```bash
bash deploy/install-hermes-agent.sh            # 默认装进 backend/.venv
# 源码不在默认位置时覆盖：
HERMES_SOURCE_DIR=/path/to/hermes-agent bash deploy/install-hermes-agent.sh
```

脚本行为：幂等（`run_agent` 已可导入直接跳过）、优先 venv 内 pip
（`--ignore-requires-python`，适配 Python 3.14——上游 pyproject 的
requires-python `<3.14` 封顶已过时，cp314 wheel 实测可用）、无 pip 时
回退 uv（CI venv 由 uv 创建）、安装后 `find_spec("run_agent")` 校验
（装不上立即失败，fail fast）。CI pm2 部署（`deploy_to_code01`）在主依赖
安装后自动调用本脚本，无需手动操作。

### Docker 部署

镜像构建期无法访问宿主机 hermes-agent 源码，SDK 安装放在**容器启动时**
由 `docker-entrypoint.sh` 幂等执行：检测到挂载的源码目录
（默认 `/opt/hermes/hermes-agent`，可用 `HERMES_SOURCE_DIR` 覆盖）即
`pip install -e` 进 `/opt/venv`；未挂载则跳过并告警（hermes 引擎运行时
按 SDK 未安装提示，不影响其他引擎）。

`docker-compose.yml` 增加（示例，已注释在 compose 文件内）：

```yaml
    environment:
      HERMES_SOURCE_DIR: /opt/hermes/hermes-agent   # entrypoint 安装源
      HERMES_HOME: /root/.hermes                    # hermes 数据目录（可选）
    volumes:
      # hermes-agent 源码（只读，entrypoint editable 安装）
      - ${HERMES_DIR:-~/.hermes/hermes-agent}:/opt/hermes/hermes-agent:ro
      # hermes 数据目录（LLM 配置 / 会话状态；runner 进程内读写，需可写）
      - ${HERMES_DATA_DIR:-~/.hermes}:/root/.hermes
```

> `HERMES_HOME`：hermes SDK 按 `HERMES_HOME` 环境变量 → `~/.hermes` 的
> 顺序解析数据目录（LLM 配置 / 会话 / skills）。pm2 部署在宿主机直接跑
> 默认即 `~/.hermes`，无需配置；容器部署把宿主 `~/.hermes` 挂到容器
> `/root/.hermes` 即可（与旧模式的 hermes 数据挂载一致）。

## 3. botler 侧配置

```yaml
worker:
  engine: hermes                       # 切换为 hermes 引擎（默认 claude）

hermes: {}                             # 无配置项（LLM 配置在 hermes 侧）
```

切回 claude 引擎只需把 `worker.engine` 改回 `claude`，hermes 段可保留。

## 4. 断点续跑（conversation_history 落库）

hermes 引擎与 claude 引擎等价支持断点续跑：每次执行结束后 runner 输出
完整会话消息历史（`messages`）与会话 id，executor 落库到
`tasks.hermes_history`；重试或平台重启恢复时把历史作为
`conversation_history` 传入接续对话（工作区保留不清空），并复用上次
会话 id。落库数据损坏（非 JSON）时自动降级为全新会话。

## 5. 验证

1. 安装 SDK 后重启 botler（pm2 `pm2 restart botler` / Docker
   `docker compose restart botler`）；
2. 在 GitLab 把一个测试 issue 指派给 bot 账号，观察任务页日志出现
   「执行 hermes 引擎（工作区 …）」；
3. 任务完成后 issue 收到结果评论并打 `bot-done` 标签（与 claude 引擎
   相同的收尾流程，包括 CI 流水线等待）。

## 6. 故障排查

| 现象 | 排查 |
|------|------|
| 任务日志报「hermes 引擎需要 hermes-agent SDK，未安装」 | 未执行 install-hermes-agent.sh（pm2）或未挂载源码（Docker），按 §2 安装后重启 |
| 安装脚本报「hermes-agent 源码不存在」 | `HERMES_SOURCE_DIR` 指向不对，或部署机未安装 hermes-agent |
| 任务反复重试、日志尾部是 runner 的 error JSON | 用 `<venv>/bin/python -c "import run_agent"` 验证 SDK 可导入；LLM 配置是否在 hermes 侧 `~/.hermes` 就绪 |
| hermes 不操作工作区文件 | 检查 `register_task_env_overrides` / `TERMINAL_CWD`（HermesSdkRunner 自动按任务 id 注册工作区 cwd），与容器内路径可达性 |
| 人工停止不生效（任务卡住） | `AIAgent.interrupt()` 依赖 conversation loop 的中断检查点与工具级中断信号；极端卡死的非中断点工具调用会等到工具自身超时（与 dsh 的 close() 强制终止不同，属 SDK 进程内模式的已知边界） |
