# Web 终端集成设计（issue #183）

> 目标：Botler 内置 Web 终端，用户在浏览器内直接使用终端，**无需再打开系统终端**。
> 方案：后端 terminado + Tornado（独立进程）、前端 xterm.js（多标签 / 快捷键 /
> 复制粘贴）、部署 Nginx 统一入口代理独立终端服务进程、安全隔离。

## 1. 需求

1. 后端：`terminado` + `Tornado` 提供标准 WebSocket 终端服务，与 Botler 主后端
   通过「共享用户验证」（同一 HMAC 会话密钥签发的短时效 token）认证；
2. 前端：`xterm.js` 多标签、快捷键、复制粘贴，直连 WebSocket（等价 AttachAddon
   的协议适配层，见 §4 说明）；
3. 部署：统一入口（主后端反向代理开箱即用 / Nginx 可选直连），代理到**独立
   终端服务进程**，安全隔离。

## 2. 架构

```
浏览器（xterm.js 多标签）
   │  ws(s)://<host>/api/terminal/ws/<name>?token=…   （与主后端同源）
   ▼
Botler 主后端（FastAPI，:8000）
   │  ① POST /api/terminal/token → 短时效 token（SSO 会话保护）
   │  ② /api/terminal/ws/* 、/api/terminal/health → 反向代理
   ▼
独立终端服务进程（Tornado + terminado，默认 127.0.0.1:8765，安全隔离）
   │  握手校验 token（同一会话密钥，botler.auth.verify_terminal_token）
   ▼
PTY 会话（每个标签一个，NamedTermManager 按 name 隔离）
```

- **独立进程**：终端服务不内嵌在 FastAPI 里，单独进程（pm2 `botler-terminal` /
  docker compose `terminal`），崩溃不影响主后端，主后端重启不影响在线终端；
- **安全隔离**：终端服务默认只监听 `127.0.0.1`（docker 形态不映射宿主端口），
  外部一律经主后端反向代理 / Nginx 统一入口访问；
- **共享用户验证**：token 由主后端签发（SSO 启用时强制登录），终端服务用
  同一份 `backend/data/session_secret.key`（HMAC-SHA256）校验，签发后默认
  60 秒过期（`BOTLER_TERM_TOKEN_TTL` 可调）。

## 3. 认证与 token

- `POST /api/terminal/token`（受 `SsoGuardMiddleware` 保护）：
  - SSO 启用：必须登录，token 携带登录用户（sub/username/name/email）；
  - SSO 未启用：主后端本就开放，签发 `local` 用户 token（行为一致）；
- token 结构：`base64url(JSON{typ:"term", sub, username, name, email, exp}).HMAC-SHA256`，
  与会话 cookie 同构但以 `typ:"term"` 声明隔离——**会话 cookie 不能当终端
  token 用、终端 token 也不能当会话 cookie 用**；
- WebSocket 握手：`/api/terminal/ws/<name>?token=…` → 终端服务校验失败以
  close code `4001` 拒绝，不创建 PTY。

## 4. 协议（terminado 标准 JSON 协议）

```
客户端 → 服务端：["stdin", 文本] / ["set_size", rows, cols]
服务端 → 客户端：["setup", {}] / ["stdout", 文本] / ["disconnect", 码]
```

- 多标签：每个标签一个独立终端名（`终端 1`、`终端 2`…），terminado
  `NamedTermManager.get_terminal(name)` 按 name 隔离 PTY 会话；
- 前端适配层（`src/terminal/attach.js`）：`@xterm/addon-attach` 只收发原始
  文本、无法表达 resize，与 terminado 的 JSON 协议不兼容，故提供**等价
  AttachAddon 语义**的适配层：`onData → ["stdin",…]`、`onResize →
  ["set_size",…]`、`onmessage → ["stdout",…] → term.write`、连接状态回调。

## 5. 前端

- 入口：顶部导航「终端」（`/terminal`），路由与页面 `src/pages/Terminal.jsx`；
- 多标签：标签栏（`终端 1`…）+「新建」按钮 + 每标签关闭按钮，上限 8 个
  （`src/terminal/tabs.js`）；
- 快捷键：`Alt+T` 新建、`Alt+W` 关闭当前标签（避开浏览器保留快捷键
  Ctrl+Shift+T/Ctrl+W），`Ctrl+Shift+C` 复制、`Ctrl+Shift+V` 粘贴（xterm 原生）；
- 终端渲染：`src/terminal/TerminalView.jsx`（xterm.js + FitAddon + 适配层 +
  WebSocket，动态导入 xterm，测试环境无 DOM 时跳过）。

## 6. 部署

### 6.1 pm2（CI 自动部署，your-server.example.com）

- `deploy/botler.config.cjs` 新增 `botler-terminal` 进程（
  `backend/terminal_service.py`，监听 `127.0.0.1:8765`）；
- CI `deploy_to_code01` 同步停止/启动该进程，并在健康检查阶段新增
  `/api/terminal/health` 探活（失败即部署失败）；
- 会话密钥：与主后端共用 `backend/data/session_secret.key`（同一工作目录，
  auth.py 懒生成持久化）。

### 6.2 docker compose

- `docker-compose.yml` 新增 `terminal` 服务（同镜像，命令跑
  `backend/terminal_service.py`），与 botler 共享 `data/backend/data` 卷
  （会话密钥），`BOTLER_TERM_UPSTREAM=http://terminal:8765` 注入 botler；
- 不映射宿主端口（安全隔离），compose 网络内部可达。

### 6.3 Nginx 统一入口（可选参考）

- `deploy/nginx-terminal.conf`：`/` → FastAPI、`/terminal/` → 终端服务进程
  （WebSocket 升级头）；无 nginx 也开箱即用（主后端已内置反向代理）。

### 6.4 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BOTLER_TERM_PORT` | `8765` | 终端服务监听端口 |
| `BOTLER_TERM_BIND` | `127.0.0.1` | 终端服务监听地址（安全隔离） |
| `BOTLER_TERM_SHELL` | `$SHELL` → `/bin/bash` | 终端 shell 命令（可带参数） |
| `BOTLER_TERM_MAX_TERMINALS` | `64` | 终端会话上限 |
| `BOTLER_TERM_TOKEN_TTL` | `60` | token 有效期（秒） |
| `BOTLER_TERM_UPSTREAM` | `http://127.0.0.1:8765` | 主后端反向代理上游 |

## 7. 安全

- 终端服务不对外暴露端口（127.0.0.1 / compose 内网），认证依赖短时效 token；
- token 与会话 cookie 以 `typ` 声明隔离，防跨用途复用；
- WebSocket 经主后端同源代理，SSO 场景未登录无法获取 token（401）；
- 终端可执行任意 shell 命令——与「登录用户可执行操作」的授权边界一致
  （SSO 用户 / local 开放模式），部署方按需配合 Nginx 网络策略收紧。

## 8. 测试

- 后端：`tests/test_terminal_token.py`（token 签发/校验/篡改/过期/隔离）、
  `tests/test_terminal_service.py`（Tornado WS 认证与 PTY 回显、多标签隔离）、
  `tests/test_api_terminal.py`（token 端点 SSO 开关、健康/WS 反向代理）；
- 前端：`tests/terminal-protocol.test.mjs`（协议编解码/地址构造/快捷键/适配层）、
  `tests/terminal-page.test.mjs`（导航入口/路由/样式/新建关闭标签/token 失败提示）。
