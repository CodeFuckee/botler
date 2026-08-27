# 远程项目与 ZCode 引擎部署指南

Botler 支持把「代码位于**其他服务器**上的项目」接入平台：仓库页以
「远程服务器（SSH）」方式添加项目，botler 经 SSH 在远程主机上完成
工作区准备（fetch / 切默认分支 / reset / clean / pull --rebase）并
拉起执行引擎（建议 **zcode**），事件流实时回传 botler UI，
所有交互（触发 / 观测 / 停止 / 重试 / 结果写回 issue）均在 botler
完成。

两条能力相互独立，可单独启用：

1. **zcode 执行引擎**（本机）：ZCode CLI 无头模式，与 claude 引擎同构；
2. **远程项目（SSH）**：任意 CLI 引擎（zcode，预留 claude）在远程主机执行。

> 常驻 Agent 服务（远程主机部署 botler-agent，HTTP+SSE 通道）为后续
> 演进计划，当前版本为 SSH 直连方案。

## 1. zcode 本机引擎

### 前置条件

- botler 部署机已安装 ZCode CLI 且在 PATH 中（`zcode --version` 可执行）；
- 认证继承部署机环境变量（与 claude 引擎同模式，pm2 部署写入
  `data/backend/.env`）。

### 启用

```yaml
worker:
  engine: zcode          # 全局切换；或仓库级 repos[].engine: zcode 覆盖
zcode:
  command: zcode         # CLI 命令（PATH 中改名/自定义包装时改这里）
  args: ["-p", "--output-format", "stream-json", "--verbose"]
```

- 无人值守语义与 claude 引擎一致：executor 自动追加
  `--dangerously-skip-permissions`，stream-json 逐行输出驱动实时事件流，
  会话 id 落 `tasks.zcode_session_id`，重试 / 重启后 `--resume` 断点续跑；
- 健康探测：设置页「任务调度」卡片引擎徽章（`zcode --version`），
  探测失败自动按 `worker.fallback_engines` 降级；
- 设置页「任务调度」下拉与 issue 右边栏「执行引擎」行均已含 zcode。

## 2. 远程项目（SSH）

### 远程主机前置条件

- botler 主机 → 远程主机 **SSH 密钥免密登录**（`BatchMode=yes`，禁止
  交互密码；建议为 botler 专设受限账号）；
- 远程主机已安装 `git` 与 zcode CLI（或其他 CLI 引擎），且能访问
  GitLab（clone/fetch/push 与 API）；
- 项目目录为完整 git 仓库（存在 `.git`），remote 指向 GitLab。

### 第一步：配置远程服务器（设置页）

「设置 → 执行引擎 → 远程服务器」：添加主机（名称 / 地址 / 端口 /
用户 / 私钥路径 / 附加 ssh -o 选项）→ 保存 → 测试（验证 SSH 连通 +
远端 zcode 可用）。也可直接编辑 config.yaml：

```yaml
remotes:
  - name: build-server
    host: 192.168.1.20
    port: 22
    user: bot
    key_path: ~/.ssh/botler_ed25519
    extra_options: []
```

### 第二步：添加远程项目（仓库页）

「仓库管理 → 添加仓库 → 远程服务器（SSH）」：选择远程服务器 → 填
项目绝对路径（如 `/srv/apps/my-repo`）→ 读取 remote → 选中指向
GitLab 的 remote → 添加。botler 经 SSH 在远端运行
`git -C <path> remote -v` 识别 GitLab 项目，其后 webhook 注册、
issue 回写与普通仓库完全一致。

建议把该仓库的「执行引擎」（仓库编辑弹窗）设为 `zcode`——引擎在
远程主机上运行，本机无需安装。

### 执行链路

- 工作区准备：botler 经 SSH 在远端执行 fetch → 解析默认分支
  （`ls-remote --symref` 服务端权威）→ 补齐跟踪引用 → checkout -B →
  reset --hard → clean -fd → pull --rebase；pull 冲突保留现场交
  agent 手工合并（与本地流程语义一致）；
- 引擎执行：远端 `cd <path> && env GITLAB_TOKEN=… GIT_ASKPASS=…
  <引擎> -p …`，prompt 经 stdin 传输；stdout 行流回本地，日志脱敏
  落盘 + SSE 实时事件流 + 会话 id/用量落库；
- 停止：杀本地 ssh 进程组断开会话，并尽力远程 `pkill` 任务标记进程；
- 预检：SSH 连通（含时延）、远端 `.git` 存在、远端磁盘剩余空间。

### 安全与边界

- token 经 SSH 远端 env 注入，远端 `ps` 可短暂见到（与计划一致；
  后续常驻 Agent 方案会改为 env 文件 0600 下发）；
- `StrictHostKeyChecking=accept-new`：首连自动记录 host key，已知
  主机变更仍拒绝（防 MITM）；
- SSH 断连 → 任务按失败分类处理，重试 + `--resume` 续跑兜底；远端
  askpass 脚本保留（0700，token 轮换后下次任务自动刷新）；
- 远程项目暂不支持 MCP 工具注入（`.mcp.json` 需写远端，后续版本）；
- `remote_path` 必须为绝对路径，且与 `local_path`（本机目录）互斥。

## 3. 故障排查

| 现象 | 排查 |
|---|---|
| 添加仓库报「远程服务器不存在」 | 设置页 remotes 未保存该 name，先配置再保存 |
| 「读取远程仓库 remote 失败」 | 远端路径非 git 仓库 / SSH 账号无权访问，手动 `ssh <host> git -C <path> remote -v` 验证 |
| 任务预检失败「SSH 连接失败」 | 密钥免密未配置；`ssh -o BatchMode=yes <host> echo ok` 复现 |
| 任务预检失败「Permission denied (publickey)」 | key_path 错误或未加入 agent；设置页「测试」按钮复现 |
| 远程 zcode 探测失败 | 远端未装 zcode 或不在登录 shell PATH（非交互 ssh 不加载 rc 文件时可用 zcode.command 填绝对路径） |
| 引擎徽章 fail（本机 zcode） | 本机未安装 zcode；仅远程使用时忽略本机徽章 |
