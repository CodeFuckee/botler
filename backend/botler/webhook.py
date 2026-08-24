"""GitLab webhook 事件接收与任务创建。

- 校验 X-Gitlab-Token
- 仅处理 issues 事件且与 assignee 相关（assignee changed / opened）
- 判定 assignee 是否包含 bot 账号
- 去重入队（scheduler.enqueue 保证同一 issue 只有一条活跃任务）
"""

from __future__ import annotations

import hmac
import logging

from .config import ConfigManager
from .database import Database, normalize_issue_created_at, normalize_issue_updated_at
from .events import global_bus
from .gitlab_client import GitLabClient, GitLabError
from .git_remote import build_repo_client_with_username
from .labels import CLAIM_SKIP_LABELS
from .scheduler import TaskScheduler
from .metrics import inc_webhook_received

logger = logging.getLogger(__name__)

# GitLab issue 事件 action 中与「指派」相关的
ASSIGNEE_ACTIONS = {"assignee", "open", "opened", "reopen", "update"}

# 触发入队的 action（设计方案：assignee changed / opened 为主；update 保守处理）
ENQUEUE_ACTIONS = {"assignee", "open", "opened"}


class WebhookError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class WebhookHandler:
    def __init__(self, config: ConfigManager, db: Database,
                 gitlab: GitLabClient, scheduler: TaskScheduler):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        self.scheduler = scheduler

    def handle(self, body: dict, header_token: str | None) -> dict:
        """处理 webhook 请求，返回响应体。校验失败抛 WebhookError。

        指标（issue #208）：每次接收计数一次，按结果分类——accepted
        （入队/去重跳过）/ rejected（事件无关/未指派/标签过滤等）/
        error（校验失败抛 WebhookError），三类之和即 webhook 接收总数。
        """
        try:
            result = self._handle_impl(body, header_token)
        except WebhookError:
            inc_webhook_received("error")
            raise
        inc_webhook_received("accepted" if result.get("accepted") else "rejected")
        return result

    def _handle_impl(self, body: dict, header_token: str | None) -> dict:
        """处理 webhook 请求（原 handle 主体），返回响应体。校验失败抛 WebhookError。"""
        cfg = self.config.get()

        # 1. secret 校验（防伪造）
        expected = cfg.webhook_secret
        if not expected:
            raise WebhookError("webhook secret 未配置，拒绝处理", 500)
        if not header_token or not hmac.compare_digest(header_token, expected):
            logger.warning("webhook secret 校验失败")
            raise WebhookError("webhook secret 校验失败", 401)

        # 2. 事件过滤
        event = body.get("object_kind")
        if event != "issue":
            return {"accepted": False, "reason": f"忽略非 issue 事件: {event}"}

        attrs = body.get("object_attributes") or {}
        action = attrs.get("action")
        if action not in ENQUEUE_ACTIONS:
            return {"accepted": False, "reason": f"忽略 action: {action}"}

        project_id = body.get("project", {}).get("id") or attrs.get("project_id")
        issue_iid = attrs.get("iid")
        if not project_id or not issue_iid:
            raise WebhookError("事件缺少 project_id / issue_iid")

        # 2.5 维护模式（issue #241）：开启后新事件不派发新任务。默认
        # （maintenance_hold_events=True）照常建任务入队，由调度器拦截派发
        # （恢复后自动执行）；False = 直接不建任务（事件忽略，不消耗
        # GitLab API 查询）。
        if cfg.maintenance_mode and not cfg.maintenance_hold_events:
            logger.info("维护模式开启且不保留事件，忽略 webhook 事件 %s#%s",
                        project_id, issue_iid)
            return {"accepted": False, "reason": "维护模式开启，忽略新事件"}

        # 3. assignee 判定：事件快照可能不可靠，一律以 API 最新状态为准
        #    （顺带取最新标签供终态过滤，见步骤 4）
        issue = body.get("issue") or {}
        bot_id = cfg.bot_id
        try:
            if bot_id is None:
                bot_id = self.gitlab.get_bot_id()
        except GitLabError as e:
            # issue #65：全局 token 失效时 bot 身份不可用，不整体拒绝——
            # 后续按仓库 remote 身份集合判定 assignee（与对账兜底对齐）
            logger.warning("webhook 获取全局 bot 身份失败（%s），将按仓库 remote 身份判定", e)
            bot_id = None
        try:
            current = self._call_with_fallback(
                project_id, cfg, lambda c: c.get_issue(project_id, issue_iid))
        except GitLabError as e:
            logger.warning("webhook 查询 issue %s#%s 失败: %s", project_id, issue_iid, e)
            current = None

        if current is None:
            return {"accepted": False, "reason": "查询 issue 失败，拒绝入队"}

        cur_assignees = [a.get("id") for a in (current.get("assignees") or [])]
        bot_ids = [bot_id] if bot_id is not None else self._repo_bot_ids(project_id, cfg)
        if not (set(cur_assignees) & set(bot_ids)):
            return {"accepted": False, "reason": "issue 未指派给 bot 账号"}

        # 4. 领取过滤标签（issue #30 / #41）：bot-done（完成待用户确认）/
        #    bot-failed（失败待人工介入）/ need-verify（用户标记需人工验证，
        #    bot 不领取）的 issue 不入队——用户重新指派也不再重复处理。
        #    以 API 最新标签为准（事件快照 labels 格式不可靠）
        cur_labels = current.get("labels") or []
        for label in CLAIM_SKIP_LABELS:
            if label in cur_labels:
                logger.info("webhook 忽略已打 %s 的 issue %s#%s",
                            label, project_id, issue_iid)
                return {"accepted": False, "reason": f"issue 已打 {label} 标签，跳过"}

        # 5. 最后发言人过滤（issue #34）：最后一条非系统评论是 bot 本人时
        #    不重复领取——bot 提问/处理完留评论后，用户仅重新指派（无新回复）
        #    也会触发 webhook，此时应等用户回复。用户回复后（或新任务无评论）
        #    再领取。
        try:
            last_author = self._call_with_fallback(
                project_id, cfg, lambda c: c.last_note_author_id(project_id, issue_iid))
        except GitLabError as e:
            logger.warning("webhook 查询 issue %s#%s 评论失败: %s", project_id, issue_iid, e)
            return {"accepted": False, "reason": "查询 issue 评论失败，拒绝入队"}
        if last_author is not None and last_author in bot_ids:
            logger.info("webhook 忽略最后发言人为 bot 的 issue %s#%s", project_id, issue_iid)
            return {"accepted": False, "reason": "最后一个发言人是 bot，等待用户回复，跳过"}

        # 6. 入队（去重由 scheduler 保证）
        repo = self.db.get_repo_by_project_id(project_id)
        if repo is None:
            logger.info("webhook 来自未注册仓库 project=%s，忽略", project_id)
            return {"accepted": False, "reason": "仓库未在平台注册"}
        if not repo["enabled"]:
            return {"accepted": False, "reason": "仓库已停用"}

        title = issue.get("title") or attrs.get("title") or f"issue #{issue_iid}"
        # issue #76 + #234：入队时记录 issue 标签、更新时间与创建时间，
        # 调度器按配置的标签优先级排序派发（同权重按创建时间升序，创建
        # 早的 issue 先处理；标签以 API 最新状态为准，与步骤 4 一致）
        task_id = self.db.create_task(
            repo["id"], project_id, issue_iid, title, triggered_by="webhook",
            issue_labels=cur_labels,
            issue_updated_at=normalize_issue_updated_at(current.get("updated_at")),
            issue_created_at=normalize_issue_created_at(current.get("created_at")))
        # issue #478：webhook 收到的 issue 事件说明开放 issue 数据已变化
        # （assignee / 标签 / 评论），广播 issue 事件供前端事件驱动刷新
        global_bus.publish({"type": "issue"})
        if task_id is None:
            return {"accepted": True, "reason": "已有活跃任务，跳过（去重）"}
        self.scheduler.enqueue(task_id)
        logger.info("webhook 入队: 任务 %s (%s#%s)", task_id, project_id, issue_iid)
        return {"accepted": True, "task_id": task_id}

    def _repo_client(self, project_id: int, cfg):
        """按仓库 remote 解析 per-repo 兜底客户端（issue #63）。

        仓库未注册 / remote 无可用 token / 本地目录不存在时返回 None。
        """
        repo = self.db.get_repo_by_project_id(project_id)
        if repo is None:
            return None
        fallback, _ = build_repo_client_with_username(repo, cfg.verify_ssl)
        return fallback

    def _call_with_fallback(self, project_id: int, cfg, call):
        """用全局 client 执行 call(client)；遇 401/403（全局 token 失效）
        时用仓库 remote url 内嵌 token 构建 per-repo client 重试一次。

        issue #65 补充：此前 webhook 的 issue 查询与最后发言人查询 401
        直接拒绝入队——全局 token 被撤销期间 webhook 事件全部丢弃，
        只能等对账兜底补入队（实时性受损）。remote 无可用 token 或
        重试仍失败时抛 GitLabError。
        """
        try:
            return call(self.gitlab)
        except GitLabError as e:
            if e.status_code not in (401, 403):
                raise
            fallback = self._repo_client(project_id, cfg)
            if fallback is None:
                raise
            logger.info("webhook 项目 %s：全局 token 失效（%s），"
                        "改用 remote url 内嵌 token 重试", project_id, e)
            return call(fallback)

    def _repo_bot_ids(self, project_id: int, cfg) -> list[int]:
        """全局 bot 身份不可用时，按仓库 remote 解析候选身份（issue #65）。

        remote token 账号 + remote URL 用户名的对应账号（如 agent）一并
        纳入；仓库未注册 / remote 无 token / 解析失败时返回空列表。
        """
        fallback = self._repo_client(project_id, cfg)
        if fallback is None:
            return []
        repo = self.db.get_repo_by_project_id(project_id)
        _, username = build_repo_client_with_username(repo, cfg.verify_ssl)
        try:
            ids = [fallback.get_bot_id()]
        except GitLabError as e:
            logger.warning("webhook 解析 remote token 身份失败: %s", e)
            ids = []
        if username:
            try:
                uid = fallback.get_user_id_by_username(username)
            except GitLabError as e:
                logger.warning("webhook 按用户名 %s 解析 bot 身份失败: %s", username, e)
                uid = None
            if uid and uid not in ids:
                ids.append(uid)
        return ids
