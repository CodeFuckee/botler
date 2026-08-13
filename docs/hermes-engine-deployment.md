# hermes 引擎部署与接入指南（issue #47）

Botler 的任务执行引擎支持两种实现：**claude**（Claude Code CLI，默认）与
**hermes**（部署机已装好的 [hermes-agent](https://github.com/NousResearch/hermes-agent)）。
`hermes` 引擎经 botler 自带的 `backend/hermes_runner.py` 脚本进程内调用
`run_agent.AIAgent`（quiet_mode），hermes 的 terminal 工具通过 `TERMINAL_CWD`
在 botler 的仓库工作区执行命令，git 凭据与 claude 引擎共用同一套
`GIT_ASKPASS` 注入机制。

**引擎选择**：`config.yaml` 的 `worker.engine`（`claude` / `hermes`），
非法值回退 `claude`。hermes 的 LLM 配置（模型 / API Key）在 hermes 侧
`~/.hermes` 已配好，botler 不管理，也不在设置页提供 hermes 配置 UI。

## 1. 前置条件

部署机（运行 botler 的 NAS）上已安装 hermes-agent（源码 + venv，如
`~/.hermes/hermes-agent`），且：

- `~/.hermes/config.yaml` / `~/.hermes/.env` 的 LLM 供应商已配置完成
  （`hermes chat` 可正常对话即可）；
- hermes venv 的 python 可直接 `import run_agent`（`uv pip install -e .`
  安装后即满足，验证：
  `<venv>/bin/python -c "import run_agent; print(run_agent.__file__)"`）。

若尚未安装 hermes-agent，可参考 shipyard 仓库的
`docs/hermes-agent-deployment.md`（Docker / 源码两种方式）。

## 2. botler 侧配置

编辑部署机的 `data/backend/config.yaml`（docker compose 部署）或
`backend/config.yaml`：

```yaml
worker:
  engine: hermes                       # 切换为 hermes 引擎（默认 claude）

hermes:
  # 宿主机 hermes venv 的 python（容器内路径见下节挂载说明）
  command: /opt/hermes/hermes-agent/venv/bin/python
  # botler 自带 runner 脚本的容器内路径
  args: ["/app/backend/hermes_runner.py"]
```

切回 claude 引擎只需把 `worker.engine` 改回 `claude`，hermes 段可保留。

## 3. Docker 部署挂载（botler 容器访问宿主机 hermes）

`docker-compose.yml` 的 `services.botler.volumes` 增加：

```yaml
volumes:
  # ……既有挂载……
  # hermes：宿主机已装好的 hermes-agent（源码 + venv，只读挂载即可）
  - ${HERMES_DIR:-~/.hermes/hermes-agent}:/opt/hermes/hermes-agent:ro
  # hermes 数据目录（LLM 配置 / 会话状态；runner 会读写会话数据，需可写）
  - ${HERMES_DATA_DIR:-~/.hermes/data}:/opt/hermes/data
```

> venv 路径提示：容器内直接执行挂载的 venv python 二进制时，venv 内的
> shebang 指向宿主机原路径但不影响 `python script.py` 形式的调用；
> 若遇依赖缺失，可在容器启动命令中先执行
> `ln -sf /opt/hermes/hermes-agent/venv /opt/hermes/venv` 之类的路径
> 归一化，或把挂载点选为与宿主机相同的绝对路径
> （`- ~/.hermes:/root/.hermes` + `command: /root/.hermes/hermes-agent/venv/bin/python`）。

> 安全提示：hermes 自带 terminal 工具（可执行任意命令）。容器内挂载
> hermes 等同于把 botler 容器权限授予 hermes，请确保 hermes 仅服务
> botler 的工作区目录，且 LLM 供应商 Key 不外泄。

## 4. 断点续跑（conversation_history 落库）

hermes 引擎与 claude 引擎等价支持断点续跑：每次执行结束后 runner 输出
完整会话消息历史（`messages`）与会话 id，executor 落库到
`tasks.hermes_history`；重试或平台重启恢复时把历史作为
`conversation_history` 传入接续对话（工作区保留不清空），并复用上次
会话 id。落库数据损坏（非 JSON）时自动降级为全新会话。

## 5. 验证

1. 修改 `worker.engine: hermes` 后重启 botler（`docker compose restart botler`）；
2. 在 GitLab 把一个测试 issue 指派给 bot 账号，观察任务页日志出现
   「执行 hermes runner（工作区 …）」；
3. 任务完成后 issue 收到结果评论并打 `bot-done` 标签（与 claude 引擎
   相同的收尾流程，包括 CI 流水线等待）。

## 6. 故障排查

| 现象 | 排查 |
|------|------|
| 任务日志报「找不到 hermes 命令」 | `hermes.command` 的容器内路径不存在，检查挂载与 venv 路径 |
| runner 输出 error 字段含「无法导入 run_agent」 | hermes venv 未 `-e` 安装源码，或挂载目录不对 |
| 任务反复重试、日志尾部是 runner 的 error JSON | 用 `<venv>/bin/python -c "import run_agent"` 在容器内同路径验证 |
| hermes 不操作工作区文件 | 检查 `TERMINAL_CWD`（executor 自动注入工作区路径）与容器内路径可达性 |
