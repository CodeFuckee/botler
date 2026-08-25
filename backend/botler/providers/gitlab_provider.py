"""GitLab 平台适配器（issue #484）。

包装既有 ``GitLabClient``（复用其重试 / 限速 / 日志脱敏等健壮性），
对外暴露统一的 ``Provider`` 通用接口。领域层只出现 ``PullRequest``
（``ChangeRequest``），GitLab 的 ``MergeRequest`` 概念仅存在于本模块
内部（方法名与注释），不渗透到核心业务逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from ..gitlab_client import GitLabClient, GitLabError
from .base import Provider, ProviderError
from .domain import (
    Issue,
    IssueComment,
    Pipeline,
    PipelineJob,
    PLATFORM_GITLAB,
    Project,
    PullRequest,
    PullRequestState,
    Webhook,
    map_gitlab_pipeline_status,
)

logger = logging.getLogger(__name__)


class GitLabProvider(Provider):
    """GitLab 平台适配器：包装 GitLabClient，映射为通用领域模型。"""

    platform = PLATFORM_GITLAB
    display_name = "GitLab"

    def __init__(self, url: str = "", token: str | None = None,
                 verify_ssl: bool = True,
                 client: GitLabClient | None = None):
        super().__init__(url, token, verify_ssl)
        # 允许注入既有 GitLabClient（main.py 全局实例），复用其限速 /
        # 重试配置；未注入时按 url/token 新建
        self._client = client or GitLabClient(
            url, token or "", verify_ssl=verify_ssl)

    # ---- 异常转换 ----

    def _wrap(self, exc: GitLabError) -> ProviderError:
        return ProviderError(
            exc.args[0] if exc.args else "GitLab 请求失败",
            status_code=exc.status_code,
            platform=self.platform,
        )

    def _project_id(self, project: Project | str) -> int:
        """把通用 project 参数归一为 GitLab 数字项目 id。

        Project 对象携带 raw 中的原始数字 id；纯字符串 ref 支持数字
        id / path / URL（交给 GitLabClient.resolve_project 解析）。
        """
        if isinstance(project, Project):
            raw_id = (project.raw or {}).get("id")
            if isinstance(raw_id, int):
                return raw_id
            # 演示/测试场景构造的 Project 可能只有字符串 id
            value = project.id
        else:
            value = project
        if str(value).isdigit():
            return int(value)
        resolved = self.resolve_project(str(value))
        raw_id = (resolved.raw or {}).get("id")
        return int(raw_id) if isinstance(raw_id, int) else int(resolved.id)

    # ---- 认证与项目 ----

    def test_connection(self) -> bool:
        try:
            self._client.test_connection()
            return True
        except GitLabError as exc:
            raise self._wrap(exc) from exc

    def resolve_project(self, ref: str) -> Project:
        try:
            payload = self._client.resolve_project(ref)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return Project.from_gitlab(payload)

    # ---- Issue ----

    def get_issue(self, project: Project | str, iid: int) -> Issue:
        try:
            payload = self._client.get_issue(self._project_id(project), iid)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return Issue.from_gitlab(payload)

    def list_open_issues(self, project: Project | str,
                         limit: int | None = None) -> list[Issue]:
        try:
            payloads = self._client.list_open_issues(
                self._project_id(project), limit=limit)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return [Issue.from_gitlab(p) for p in payloads]

    def create_issue(self, project: Project | str, title: str,
                     description: str | None = None,
                     labels: list[str] | None = None) -> Issue:
        if not title or not title.strip():
            raise ProviderError("Issue 标题不能为空", 400, self.platform)
        try:
            payload = self._client.create_issue(
                self._project_id(project), title,
                description=description, labels=labels)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return Issue.from_gitlab(payload)

    def update_issue(self, project: Project | str, iid: int, *,
                     title: str | None = None,
                     description: str | None = None,
                     state: str | None = None,
                     assignee: str | None = None) -> Issue:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        if state is not None:
            normalized = state.strip().lower()
            if normalized == "open":
                changes["state_event"] = "reopen"
            elif normalized == "closed":
                changes["state_event"] = "close"
            else:
                raise ProviderError(
                    f"不支持的 Issue 状态: {state}（仅支持 open/closed）",
                    400, self.platform)
        if assignee is not None:
            user_id = self._client.get_user_id_by_username(assignee)
            changes["assignee_ids"] = [user_id] if user_id is not None else []
        if not changes:
            # 无字段变更时直接返回当前状态，避免空请求
            return self.get_issue(project, iid)
        try:
            payload = self._client.update_issue(
                self._project_id(project), iid, **changes)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return Issue.from_gitlab(payload)

    def add_comment(self, project: Project | str, iid: int,
                    body: str) -> IssueComment:
        try:
            payload = self._client.add_comment(
                self._project_id(project), iid, body)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return IssueComment.from_gitlab(payload)

    def list_issue_notes(self, project: Project | str, iid: int,
                         limit: int | None = None) -> list[IssueComment]:
        try:
            payloads = self._client.list_issue_notes(
                self._project_id(project), iid, limit=limit)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return [IssueComment.from_gitlab(p) for p in payloads]

    def add_labels(self, project: Project | str, iid: int,
                   labels: list[str]) -> None:
        if not labels:
            return
        try:
            self._client.add_labels(
                self._project_id(project), iid, list(labels))
        except GitLabError as exc:
            raise self._wrap(exc) from exc

    # ---- Pull Request / ChangeRequest（GitLab MergeRequest 映射）----

    def _pull_request(self, payload: dict) -> PullRequest:
        return PullRequest.from_gitlab(payload)

    def get_pull_request(self, project: Project | str,
                         number: int) -> PullRequest:
        try:
            payload = self._client.get_merge_request(
                self._project_id(project), number)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return self._pull_request(payload)

    def list_pull_requests(self, project: Project | str,
                           state: PullRequestState | None = None,
                           limit: int | None = None) -> list[PullRequest]:
        gitlab_state: str | None = None
        if state is not None:
            # 通用状态 → GitLab MR state（GitLab 用 opened 而非 open；
            # merged 可直接查询，与通用枚举一致）
            gitlab_state = {
                PullRequestState.OPEN: "opened",
                PullRequestState.CLOSED: "closed",
                PullRequestState.MERGED: "merged",
            }.get(state, "all")
        try:
            payloads = self._client.list_merge_requests(
                self._project_id(project), state=gitlab_state, limit=limit)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return [self._pull_request(p) for p in payloads]

    def create_pull_request(self, project: Project | str, *,
                            source_branch: str, target_branch: str,
                            title: str,
                            description: str | None = None) -> PullRequest:
        if not title or not title.strip():
            raise ProviderError("Pull Request 标题不能为空", 400, self.platform)
        if not source_branch or not target_branch:
            raise ProviderError(
                "Pull Request 源分支与目标分支不能为空", 400, self.platform)
        try:
            payload = self._client.create_merge_request(
                self._project_id(project), source_branch, target_branch,
                title, description=description)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return self._pull_request(payload)

    def merge_pull_request(self, project: Project | str,
                           number: int) -> PullRequest:
        try:
            payload = self._client.merge_merge_request(
                self._project_id(project), number)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return self._pull_request(payload)

    # ---- 流水线 ----

    def get_latest_pipeline(self, project: Project | str,
                            ref: str | None = None) -> Pipeline | None:
        try:
            payload = self._client.get_latest_pipeline(
                self._project_id(project))
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        if not payload:
            return None
        return self._pipeline(payload)

    def list_pipelines(self, project: Project | str,
                       limit: int = 20) -> list[Pipeline]:
        try:
            payloads = self._client.list_pipelines(
                self._project_id(project), limit=limit)
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return [self._pipeline(p) for p in payloads]

    @staticmethod
    def _pipeline(payload: dict) -> Pipeline:
        return Pipeline(
            id=payload.get("id"),
            status=map_gitlab_pipeline_status(payload.get("status")),
            ref=payload.get("ref") or "",
            sha=payload.get("sha") or "",
            web_url=payload.get("web_url") or "",
            created_at=payload.get("created_at") or "",
            raw=payload,
        )

    def list_pipeline_jobs(self, project: Project | str,
                           pipeline_id: Any) -> list[PipelineJob]:
        try:
            payloads = self._client.list_pipeline_jobs(
                self._project_id(project), int(pipeline_id))
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        jobs: list[PipelineJob] = []
        for p in payloads:
            jobs.append(PipelineJob(
                id=p.get("id"),
                name=p.get("name") or "",
                stage=p.get("stage") or "",
                status=map_gitlab_pipeline_status(p.get("status")),
                web_url=p.get("web_url") or "",
                raw=p,
            ))
        return jobs

    # ---- Webhook ----

    def list_webhooks(self, project: Project | str) -> list[Webhook]:
        try:
            payloads = self._client.list_webhooks(
                self._project_id(project))
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        # GitLab hook 事件字段名 → 通用事件名（领域层不暴露 GitLab 专属名）
        event_names = {
            "issues_events": "issues",
            "push_events": "push",
            "merge_requests_events": "pull_request",
            "note_events": "note",
            "pipeline_events": "pipeline",
            "tag_push_events": "tag_push",
        }
        return [
            Webhook(
                id=hook.get("id"),
                url=hook.get("url") or "",
                events=[name for gitlab_field, name in event_names.items()
                        if hook.get(gitlab_field)],
                raw=hook,
            )
            for hook in payloads
        ]

    def register_webhook(self, project: Project | str, url: str,
                         secret: str | None = None,
                         events: list[str] | None = None) -> Webhook:
        # GitLabClient.register_webhook 使用内部 webhook 回调地址与固定
        # issues 事件；url 参数被忽略（保持既有语义），events 暂不开放
        # （GitLab hook 事件集固定为 issue 事件）
        try:
            payload = self._client.register_webhook(
                self._project_id(project), secret or "")
        except GitLabError as exc:
            raise self._wrap(exc) from exc
        return Webhook(
            id=payload.get("id"),
            url=payload.get("url") or "",
            events=["issues"],
            raw=payload,
        )

    def unregister_webhook(self, project: Project | str,
                           hook_id: Any) -> None:
        try:
            self._client.delete_webhook(
                self._project_id(project), int(hook_id))
        except GitLabError as exc:
            raise self._wrap(exc) from exc
