# Synology SSO 登录配置指南（issue #27）

Botler 支持接入群晖 **SSO Server**（DSM 套件）作为登录身份源：启用后访问 Botler
管理界面必须使用群晖账号登录（OIDC 协议，OAuth2 授权码模式）；未启用时保持开放访问。

```
浏览器 ──访问──► Botler ──302──► 群晖 SSO Server（登录页）
   ▲                                   │ 认证成功回调
   └─────────── 会话 cookie ◄──────────┘
```

## 一、群晖 SSO Server 侧配置

### 1. 准备 SSO Server

1. 在 DSM「套件中心」安装 **SSO Server** 套件。
2. 打开 SSO Server → **服务** 标签页：
   - 启用 **OIDC** 服务
   - 记录 **Well-known URL**（形如
     `https://<群晖地址>:<端口>/.well-known/openid-configuration`），稍后填入 Botler。
3. （可选）**常规设置** 中确认服务器 URL 可被 Botler 服务器访问。

### 2. 创建 OIDC 应用程序

1. 打开 SSO Server → **应用程序** 标签页 → 点击 **新增**。
2. 选择 **OIDC** → 下一步。
3. 填写：
   - **应用程序名称**：如 `Botler`
   - **Redirect URI（重定向 URI）**：Botler 的回调地址，必须与 Botler 侧填写的
     `redirect_uri` **完全一致**。推荐使用域名形式：
     - 若 Botler 的 `redirect_uri` 留空（自动生成），则为
       `https://<Botler地址>/api/auth/callback`（注意端口）
   - 可一次注册多个 Redirect URI（最多 10 个）
4. 确认完成。点击该应用 → **编辑**，复制以下信息，稍后填入 Botler 设置页：
   - **Application ID**（即 OIDC 的 Client ID）
   - **Application Secret**（即 Client Secret，仅此一次完整显示）

> 群晖 SSO Server 若使用自签名证书，Botler 侧需关闭证书校验（见下文
> `verify_ssl`），否则 OIDC 握手会因证书不受信任而失败。

## 二、Botler 侧配置

### 方式 A：Web 设置页（推荐）

1. 登录 Botler → **设置** → **Synology SSO 登录** 卡片。
2. 填写：
   - **Well-known URL**：群晖 SSO Server 中记录的地址
   - **Application ID**：上一步复制的 Application ID
   - **Application Secret**：上一步复制的 Application Secret（保存后只显示掩码，
     再次保存时留空即可保持现有值）
   - **Scope**：默认 `openid profile email` 即可
   - **登录有效期**：默认 30 天
   - **回调地址**：建议留空（自动按浏览器访问地址生成，`/api/auth/callback`）；
     若 Botler 有固定域名且群晖侧已注册，也可显式填写（必须与群晖侧一致）
   - **校验群晖证书**：群晖为自签名证书时取消勾选
3. 勾选 **启用 SSO**，点击 **保存**（已写回 config.yaml）。
4. 之后访问 Botler 即跳转群晖登录页；认证成功回跳并建立会话。

### 方式 B：直接编辑 config.yaml

```yaml
sso:
  enabled: true
  well_known_url: "https://nas.example.com:5001/.well-known/openid-configuration"
  client_id: "your-application-id"
  client_secret: "${SYNOLOGY_SSO_SECRET}"   # 建议用环境变量引用，不落明文
  scope: "openid profile email"
  session_days: 7
  redirect_uri: ""                           # 留空 = 自动生成
  verify_ssl: false                          # 群晖自签名证书时设 false
```

保存后重启服务生效（或等设置页保存触发重载）。

## 三、使用说明

- **登录**：未登录访问任何页面 → 自动跳转登录页 →「使用群晖账号登录」→
  群晖登录页认证 → 回跳 Botler。
- **退出**：页面右上角显示当前群晖账号（👤 用户名），点「退出」清除会话。
- **会话有效期**：默认 30 天，可在设置页调整（1~365 天）。会话为签名 cookie
  （HMAC-SHA256），密钥自动生成于 `backend/data/session_secret.key`
  （Docker 部署由 `docker-compose.yml` 挂载持久化，重建容器不丢登录态）。
- **开放范围**：启用 SSO 后，除登录流程自身与健康检查外，所有 `/api/*` 接口均需登录；
  GitLab webhook（`/webhook/gitlab`）不要求登录（由 GitLab 外部调用）。
- **登录用户范围**：任何通过群晖 SSO 认证的账号均可登录（如需限制特定用户/组，
  可在群晖 SSO Server 的 OIDC 应用/权限设置中控制）。

## 四、常见问题

| 现象 | 原因与处理 |
|---|---|
| 点击登录后页面提示「无法访问此网站」 | 群晖地址不可达：确认 Well-known URL 正确、端口开放、Botler 服务器能访问群晖 |
| 登录报错 `login_failed` | ① 群晖自签名证书 → 取消勾选「校验群晖证书」；② Redirect URI 与群晖侧注册不一致 → 核对两边地址完全一致（含端口与路径） |
| 登录成功后回到首页但马上又跳登录页 | 服务器时钟与签发时间不一致，或容器重建导致会话密钥丢失（确认 `data/backend/data` 卷已挂载） |
| 群晖侧配置改了但 Botler 不生效 | 设置页保存会写回 config.yaml 并重载；直接改文件则需重启服务 |

## 五、安全提示

- `client_secret` 建议通过 `.env` + `${ENV}` 引用，避免明文落入 config.yaml。
- 会话密钥文件（`backend/data/session_secret.key`）已加入 `.gitignore`，请勿提交。
- 启用 SSO 后建议通过 HTTPS 访问 Botler（避免会话 cookie 明文传输）。
