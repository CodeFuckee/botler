# CI/CD 流水线 docs-only 跳过机制（issue #57）

> 更新日期: 2026-08-14

## 1. 目的

main 分支 push 若仅变更文档（`*.md` / `docs/` / `LICENSE` 等），不再
执行完整流水线（前端构建 + 后端测试 + 部署），避免文档提交白跑 5 分钟
以上的 runner 资源。docs 变更仍需及时镜像到 GitHub，因此仅保留
`sync_to_github` 同步 job。

## 2. GitLab 侧（.gitlab-ci.yml）

公共规则 `.docs_only_skip`（`frontend:build` / `backend:test` /
`deploy_to_code01` 三 job 复用），rules 按顺序求值：

1. **代码/配置白名单命中 → 正常执行**（`when: always`）：变更文件匹配
   `backend/**`、`frontend/**`、`deploy/**`、`scripts/**`、`data/**`、
   `.github/**`、根目录 Dockerfile/docker-compose.yml/.dockerignore/
   .gitignore，或扩展名 `.py .js .jsx .mjs .ts .tsx .css .html .json
   .yaml .yml .toml .sh` 中的任意一个。
2. **仅文档变更 → 跳过**（`when: never`）：变更文件全部为 `**/*.md`、
   `docs/**` 或 `LICENSE`。
3. **其余场景 → 维持原行为**（`when: always`）：其他分支 / MR / tag /
   schedule 等不受影响。

> ⚠️ 规则顺序不可颠倒：代码提交通常伴随 CHANGELOG.md 变更，若先判定
> 文档会把混合提交误判为 docs-only 跳过。

`sync_to_github` 不受跳过规则影响（docs 也需镜像），但其内的
workflow_dispatch 触发按 `CI_COMMIT_BEFORE_SHA..CI_COMMIT_SHA` 的变更
文件做同样的白名单判定：docs-only 时跳过触发（GitHub push 事件已被
下方白名单拦截，dispatch 是补充入口，一并跳过避免白跑）。`BEFORE_SHA`
全零（force push / 新分支）或 diff 失败时保守视为含代码变更。

## 3. GitHub 侧（.github/workflows/ci.yml）

push 触发增加 `paths` 白名单（与 GitLab 同一套「代码文件」定义）：
至少一个变更文件匹配白名单才运行 workflow。docs-only 的镜像 push 无
匹配 → 不运行 Actions；docs+代码混合 push → 代码文件匹配 → 照常运行。
`workflow_dispatch` 手动入口保留。

## 4. 验证方法

1. 推送纯文档提交（如修改 README / docs/）→ GitLab 流水线只跑
   `sync_to_github`（其余 job 显示 skipped），sync 日志出现
   「⏭️ docs-only 提交，跳过 GitHub Actions 触发」；
2. 推送代码提交（含 CHANGELOG）→ 全套流水线正常执行；
3. `ci/lint` API 校验配置合法。
