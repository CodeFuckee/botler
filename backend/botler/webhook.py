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
from .database import Database
from .gitlab_client import GitLabClient, GitLabError
from .scheduler import TaskScheduler

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
        """处理 webhook 请求，返回响应体。校验失败抛 WebhookError。"""
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

        # 3. assignee 判定：事件快照可能不可靠，一律以 API 最新状态为准
        #    （顺带取最新标签供终态过滤，见步骤 4）
        issue = body.get("issue") or {}
        bot_id = cfg.bot_id or self.gitlab.get_bot_id()
        try:
            current = self.gitlab.get_issue(project_id, issue_iid)
        except GitLabError as e:
            logger.warning("webhook 查询 issue %s#%s 失败: %s", project_id, issue_iid, e)
            current = None

        if current is None:
            return {"accepted": False, "reason": "查询 issue 失败，拒绝入队"}

        cur_assignees = [a.get("id") for a in (current.get("assignees") or [])]
        if bot_id not in cur_assignees:
            return {"accepted": False, "reason": "issue 未指派给 bot 账号"}

        # 4. 终态标签过滤（issue #30）：bot-done（完成待用户确认）/ bot-failed
        #    （失败待人工介入）的 issue 不入队——用户重新指派也不再重复处理。
        #    以 API 最新标签为准（事件快照 labels 格式不可靠）
        cur_labels = current.get("labels") or []
        for label in ("bot-done", "bot-failed"):
            if label in cur_labels:
                logger.info("webhook 忽略已打 %s 的 issue %s#%s",
                            label, project_id, issue_iid)
                return {"accepted": False, "reason": f"issue 已打 {label} 标签，跳过"}

        # 5. 最后发言人过滤（issue #34）：最后一条非系统评论是 bot 本人时
        #    不重复领取——bot 提问/处理完留评论后，用户仅重新指派（无新回复）
        #    也会触发 webhook，此时应等用户回复。用户回复后（或新任务无评论）
        #    再领取。
        try:
            last_author = self.gitlab.last_note_author_id(project_id, issue_iid)
        except GitLabError as e:
            logger.warning("webhook 查询 issue %s#%s 评论失败: %s", project_id, issue_iid, e)
            return {"accepted": False, "reason": "查询 issue 评论失败，拒绝入队"}
        if last_author is not None and last_author == bot_id:
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
        task_id = self.db.create_task(
            repo["id"], project_id, issue_iid, title, triggered_by="webhook")
        if task_id is None:
            return {"accepted": True, "reason": "已有活跃任务，跳过（去重）"}
        self.scheduler.enqueue(task_id)
        logger.info("webhook 入队: 任务 %s (%s#%s)", task_id, project_id, issue_iid)
        return {"accepted": True, "task_id": task_id}
