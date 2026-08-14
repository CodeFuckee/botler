# GitLab Owner Token 申请教程

Owner Token 是 Botler 专门用来**编辑 issue**（写评论、打标签、改状态）的
个人访问令牌（Personal Access Token，PAT）。Botler **绝不会**用它推送代码
或操作流水线——git 推送与流水线操作仍使用 bot 账号的 token。

因为要保证「无法推送代码、无法操作流水线」，推荐用**低权限账号**（仓库
Reporter 角色）申请 token，从账号权限层面就杜绝越权使用。

## 方案一（推荐）：低权限账号的 PAT

用 GitLab 账号权限本身保证「只有处理 issue 的权限」：

1. 在 GitLab 上准备一个专用账号（或使用你已有的低权限账号）；
2. 由项目 Owner 把该账号加入目标项目，角色选 **Reporter**：
   - Reporter 可以查看 issue、写评论、打标签 —— 足够编辑 issue；
   - Reporter **不能**推送代码（需要 Developer 及以上）；
   - Reporter **不能**取消/重试/删除流水线（需要 Maintainer 及以上）；
3. 用该账号登录 GitLab，进入 **用户设置 → Access Tokens → Add new token**；
4. 填写：
   - **Token name**：例如 `botler-owner-token`
   - **Expiration date**：建议设置到期时间（如 90 天后），到期前记得续期
   - **Scopes**：勾选 `api`（GitLab 没有仅 issue 的 scope，`api` 是
     编辑 issue 所需的最小可用 scope；账号角色已限制其权限范围）
5. 点击 **Create personal access token**，**立即复制**生成的 token
   （格式 `glpat-xxxx`，离开页面后不再显示）；
6. 回到 Botler 设置页，把 token 粘贴到「Owner GitLab Token」输入框保存。

## 方案二：Owner 本人主账号的 PAT

如果不方便准备低权限账号，也可以用你的主账号（Owner/Maintainer）申请：

1. 登录 GitLab，进入 **用户设置 → Access Tokens → Add new token**；
2. 填写 Token name、Expiration date，Scopes 勾选 `api`；
3. 创建并复制 token，粘贴到 Botler 设置页保存。

> ⚠️ 注意：主账号的 PAT 权限较大（可以推送代码、操作流水线）。
> Botler 的代码路径保证不会把它用于 git 推送与流水线操作，
> 但请妥善保管，**不要**把这个 token 用于 git remote 或其他工具，
> 以免它出现在推送凭据中。

## 安全提示

- Owner Token 只在 Botler 服务器上以明文写入 `backend/config.yaml`
  （与 bot token 相同；也可用 `${ENV_VAR}` 引用环境变量，见
  `config.example.yaml` 注释）；
- 网页上只显示掩码（`glpa****xxxx`），明文不会回传到浏览器；
- 输入框**留空保存 = 保持现有 token**；要清除 token 请直接编辑
  `config.yaml` 删除 `gitlab.owner_token` 后重启；
- token 失效（401）或权限不足（403）时，Botler 会自动回退到原有链路
  （bot token / 仓库 remote 内嵌 token），不会中断任务处理；
- 到期后请按上述步骤重新申请并更新设置页的 token。

## 常见问题

**Q：为什么没有「仅 issue 权限」的 scope 可选？**
GitLab 的 PAT scope 最细粒度是 `read_api`（只读）与 `api`（完整 API）。
「只处理 issue」的权限只能通过**账号角色**（Reporter）来实现，这也是
推荐方案一的原因。

**Q：不配置 Owner Token 会怎样？**
Botler 照常工作，编辑 issue 仍使用 bot token（或仓库 remote 内嵌 token）。
配置 Owner Token 后，issue 上的评论与标签操作会以你的身份执行。
