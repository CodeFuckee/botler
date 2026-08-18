# 标记库规范（Labels）

> 统一 chenkaidi 名下全部仓库（botler / shipyard / patio / graph2plan / daymark / glimmer）的 issue 标签，
> 保证机器人（GitLab Issue Agent）与人工协作时标签语义一致。
> 批量同步工具：`scripts/sync_labels.py`（创建/更新，不删除旧标签）。

## 类型标签（表达"这是什么工作"）

| 标签 | 颜色 | 说明 |
|---|---|---|
| `bug` | `#d9534f` | 缺陷修复 |
| `feature` | `#009966` | 新功能 |
| `optimize` | `#cd5b45` | 性能 / 体验优化 |
| `ui` | `#cc338b` | 界面相关改动 |
| `docs` | `#428bca` | 文档 |
| `test` | `#8e44ad` | 补充测试 |
| `gitlab-ci` | `#f0ad4e` | CI/CD 配置相关（统一命名，旧仓库的 `ci` 不再使用） |
| `chore` | `#95a5a6` | 杂务（依赖升级、重构、构建等，对应 commit 前缀 `chore:`） |

## 流程 / 状态标签（表达"进展到哪一步"）

| 标签 | 颜色 | 说明 |
|---|---|---|
| `in-progress` | `#6699cc` | 处理中（bot 领取 issue 时自动添加） |
| `review` | `#ff9800` | 待人工审查确认 |
| `need-verify` | `#ffcc00` | 需要人工验证，bot 不领取（由人工标记） |
| `blocked` | `#607d8b` | 等待补充信息 / 被阻塞 |
| `bot-done` | `#6699cc` | bot 已完成开发，待用户确认后关闭（**由 bot 添加，禁止 bot 移除**） |
| `bot-failed` | `#6699cc` | bot 处理失败，需人工介入 |

## 使用约定

1. **一个 issue 至少一个类型标签 + 至多一个流程标签**：类型标签描述工作性质，流程标签描述当前进展（通常为 `in-progress` 或 `blocked`；`need-verify` 由人工标记、bot 领取任务时跳过；`bot-done` / `bot-failed` 由 bot 在收尾时打）。
2. **优先级判定**（GitLab Issue Agent）：默认 `bug` > `test` > `feature`（同仓库队列内，bug 最优先）；顺序可在平台设置页「任务调度 → issue 标签优先级」自定义，未列出的标签排在最后；同优先级按 issue 更新时间升序处理。
3. **禁止删除**：`bot-done` / `bot-failed` 由 bot 流程管理，人工不要移除；旧标签（如 `ci`）保留不删，仅新工作统一使用本规范标签。
4. 新增标签需先更新本文件，再运行 `scripts/sync_labels.py` 同步到全部仓库。

## 提交信息规范（防 GitLab autoclose 自动关闭）

GitLab 实例开启了 `autoclose_referenced_issues`：提交信息命中默认关闭模式
（`fix: #N` / `fixes #N` / `closes #N` / `resolves #N` 等「关闭关键词 + #编号」）
且推送到默认主分支时，**issue 会被 GitLab 系统自动关闭**（closed_by 显示为该
项目的 project bot，非任何真人用户），用户侧表现为「agent 自己 close issue」。
graph2plan 仓库曾因提交信息 `fix: #24 …` 反复触发（issue #109）。

- **禁止**在提交信息中使用上述关闭模式引用 issue；
- issue 引用一律写全角括号形式：`（issue #NN）`；
- 若 issue 已被 autoclose 误关：重新打开（`state_event=reopen`）并补评论说明，
  关闭操作仍保留人工执行（botler 平台已在任务收尾时自动检测并恢复）。

## 同步方法

```bash
# 需 GITLAB_TOKEN 环境变量（自托管实例自签名证书时自动跳过校验）
python3 scripts/sync_labels.py --host <host:port> --namespace <用户名或群组路径>
```

脚本枚举命名空间下全部项目，对每个项目创建缺失标签、更新已存在标签的颜色/描述；**不删除任何已有标签**。

平台「添加仓库」时也会自动在目标 GitLab 项目补齐缺失的默认标签（issue #157）：
只创建缺失的，已存在的不覆盖（与同步脚本的「更新已存在标签」行为不同，避免覆盖用户自定义的颜色/描述）。

## 平台内一键同步（标记库页，issue #307）

Botler 平台「标记库」页（Web UI）为每个**默认标签**提供「同步到所有仓库」
按钮：点击后自动把该标签同步到**已添加的全部仓库，包括启用和未启用的**
（软删除的仓库除外），用于新规范/新标签在存量仓库上一键补齐。

- 接口：`POST /api/labels/{name}/sync`（name 为默认标签名，非默认标签 400 拒绝）；
- 同步语义与「添加仓库时补齐默认标签」（issue #157）一致：目标项目**缺失才创建**、
  已存在的不覆盖（保留用户已有的颜色/描述）；
- 身份：per-repo client（仓库 remote URL 内嵌 token）优先，无 token 回退全局 bot token；
- 单仓库失败为尽力而为：不中断其余仓库，失败明细随响应 `failed` 返回，前端提示展示。
