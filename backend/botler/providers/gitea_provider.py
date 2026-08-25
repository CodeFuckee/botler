"""Gitea 平台适配器（issue #484）：Gitea API v1，面向私有化部署用户。

与 GitHub 共用 REST 基类（issue / pull request / webhook 端点一致），
差异点：
- API 前缀 /api/v1，鉴权头 ``Authorization: token <token>``；
- 标签操作用标签 id（先按名称解析/创建）；
- 流水线以「最近提交状态（commit status）」聚合表示（Gitea Actions 或
  外部 CI 写入的 status 均可覆盖），无子任务列表。
"""

from __future__ import annotations

from typing import Any

from .base import ProviderError, project_ref
from .domain import (
    Issue,
    IssueComment,
    Pipeline,
    PipelineJob,
    PLATFORM_GITEA,
    Project,
    PullRequest,
    PullRequestState,
    Webhook,
    map_gitea_status_state,
)
from .rest_base import RestProvider


class GiteaProvider(RestProvider):
    """Gitea 平台适配器。"""

    platform = PLATFORM_GITEA
    display_name = "Gitea"

    api_prefix = "/api/v1"

    def _build_headers(self, token: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            # Gitea 鉴权：Authorization: token <PAT>（与 GitHub Bearer 不同）
            headers["Authorization"] = f"token {token}"
        return headers

    # ---- Issue ----

    def get_issue(self, project: Project | str, iid: int) -> Issue:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/issues/{iid}")
        assert isinstance(payload, dict)
        if payload.get("pull_request"):
            raise ProviderError(
                f"编号 {iid} 对应 Pull Request 而非 Issue", 404, self.platform)
        return Issue.from_gitea(payload)

    def list_open_issues(self, project: Project | str,
                         limit: int | None = None) -> list[Issue]:
        payloads = self._paged(
            f"{self._repo_path(project)}/issues",
            state="open", type="issues", limit=limit)
        return [Issue.from_gitea(p) for p in payloads if not p.get("pull_request")]

    def create_issue(self, project: Project | str, title: str,
                     description: str | None = None,
                     labels: list[str] | None = None) -> Issue:
        if not title or not title.strip():
            raise ProviderError("Issue 标题不能为空", 400, self.platform)
        body: dict[str, Any] = {"title": title}
        if description:
            body["body"] = description
        if labels:
            body["labels"] = list(labels)
        payload = self._request(
            "POST", f"{self._repo_path(project)}/issues", json=body)
        assert isinstance(payload, dict)
        return Issue.from_gitea(payload)

    def update_issue(self, project: Project | str, iid: int, *,
                     title: str | None = None,
                     description: str | None = None,
                     state: str | None = None,
                     assignee: str | None = None) -> Issue:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["body"] = description
        if state is not None:
            normalized = state.strip().lower()
            if normalized not in ("open", "closed"):
                raise ProviderError(
                    f"不支持的 Issue 状态: {state}（仅支持 open/closed）",
                    400, self.platform)
            changes["state"] = normalized
        if assignee is not None:
            changes["assignee"] = assignee
        if not changes:
            return self.get_issue(project, iid)
        payload = self._request(
            "PATCH", f"{self._repo_path(project)}/issues/{iid}", json=changes)
        assert isinstance(payload, dict)
        return Issue.from_gitea(payload)

    def add_comment(self, project: Project | str, iid: int,
                    body: str) -> IssueComment:
        payload = self._request(
            "POST", f"{self._repo_path(project)}/issues/{iid}/comments",
            json={"body": body})
        assert isinstance(payload, dict)
        return IssueComment.from_gitea(payload)

    def list_issue_notes(self, project: Project | str, iid: int,
                         limit: int | None = None) -> list[IssueComment]:
        payloads = self._paged(
            f"{self._repo_path(project)}/issues/{iid}/comments", limit=limit)
        return [IssueComment.from_gitea(p) for p in payloads]

    def _label_ids(self, project: Project | str, labels: list[str]) -> list[int]:
        """标签名 → Gitea 标签 id（Gitea issue 加标签 API 只接受 id）。

        不存在的标签先创建（GET labels 比对，POST labels 补齐），
        保证「给 issue 打任意标签名」的通用语义与 GitLab 一致。
        """
        repo = self._repo_path(project)
        existing = self._paged(f"{repo}/labels")
        by_name = {item.get("name"): item.get("id") for item in existing
                   if isinstance(item, dict) and item.get("id") is not None}
        ids: list[int] = []
        for name in labels:
            label_id = by_name.get(name)
            if label_id is not None:
                ids.append(int(label_id))
                continue
            created = self._request(
                "POST", f"{repo}/labels",
                json={"name": name, "color": "#6699cc"})
            if isinstance(created, dict) and created.get("id") is not None:
                ids.append(int(created["id"]))
        return ids

    def add_labels(self, project: Project | str, iid: int,
                   labels: list[str]) -> None:
        if not labels:
            return
        ids = self._label_ids(project, labels)
        if ids:
            self._request(
                "POST", f"{self._repo_path(project)}/issues/{iid}/labels",
                json={"labels": ids})

    # ---- Pull Request ----

    def get_pull_request(self, project: Project | str,
                         number: int) -> PullRequest:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/pulls/{number}")
        assert isinstance(payload, dict)
        return PullRequest.from_gitea(payload)

    def list_pull_requests(self, project: Project | str,
                           state: PullRequestState | None = None,
                           limit: int | None = None) -> list[PullRequest]:
        params: dict[str, Any] = {}
        if state is not None:
            # Gitea 无独立 merged 查询态：merged 在 closed 中；查询
            # merged 时拉全量后在内存过滤
            params["state"] = "closed" if state is PullRequestState.CLOSED else "all"
        payloads = self._paged(
            f"{self._repo_path(project)}/pulls", limit=limit, **params)
        prs = [PullRequest.from_gitea(p) for p in payloads]
        if state is PullRequestState.MERGED:
            prs = [p for p in prs if p.state is PullRequestState.MERGED]
        return prs

    def create_pull_request(self, project: Project | str, *,
                            source_branch: str, target_branch: str,
                            title: str,
                            description: str | None = None) -> PullRequest:
        if not title or not title.strip():
            raise ProviderError("Pull Request 标题不能为空", 400, self.platform)
        if not source_branch or not target_branch:
            raise ProviderError(
                "Pull Request 源分支与目标分支不能为空", 400, self.platform)
        body: dict[str, Any] = {
            "title": title,
            "head": source_branch,
            "base": target_branch,
        }
        if description:
            body["body"] = description
        payload = self._request(
            "POST", f"{self._repo_path(project)}/pulls", json=body)
        assert isinstance(payload, dict)
        return PullRequest.from_gitea(payload)

    def merge_pull_request(self, project: Project | str,
                           number: int) -> PullRequest:
        # Gitea 合并走 POST（成功返回 204），随后重新拉取最新状态
        self._request(
            "POST", f"{self._repo_path(project)}/pulls/{number}/merge")
        return self.get_pull_request(project, number)

    # ---- 流水线（commit status 聚合）----

    def get_latest_pipeline(self, project: Project | str,
                            ref: str | None = None) -> Pipeline | None:
        statuses = self._list_statuses(project, ref)
        return self._pipeline(statuses[0]) if statuses else None

    def list_pipelines(self, project: Project | str,
                       limit: int = 20) -> list[Pipeline]:
        statuses = self._list_statuses(project, ref=None)
        return [self._pipeline(s) for s in statuses[:limit]]

    def _list_statuses(self, project: Project | str,
                       ref: str | None) -> list[dict]:
        """拉取最近提交状态列表；ref 为空时取仓库默认分支。"""
        repo = self._repo_path(project)
        if not ref:
            project_obj = self.resolve_project(project_ref(project))
            ref = project_obj.default_branch or "main"
        payloads = self._paged(
            f"{repo}/commits/{ref}/statuses")
        return [p for p in payloads if isinstance(p, dict)]

    @staticmethod
    def _pipeline(status: dict) -> Pipeline:
        return Pipeline(
            id=status.get("id"),
            status=map_gitea_status_state(status.get("state")),
            ref="",
            sha=status.get("sha") or "",
            web_url=status.get("target_url") or "",
            created_at=status.get("created_at") or "",
            raw=status,
        )

    def list_pipeline_jobs(self, project: Project | str,
                           pipeline_id: Any) -> list[PipelineJob]:
        # Gitea commit status 为扁平结构，无子任务列表，返回空列表
        return []

    # ---- Webhook ----

    def list_webhooks(self, project: Project | str) -> list[Webhook]:
        payloads = self._paged(f"{self._repo_path(project)}/hooks")
        return [
            Webhook(
                id=hook.get("id"),
                url=(hook.get("config") or {}).get("url") or "",
                events=list(hook.get("events") or []),
                raw=hook,
            )
            for hook in payloads
        ]

    def register_webhook(self, project: Project | str, url: str,
                         secret: str | None = None,
                         events: list[str] | None = None) -> Webhook:
        config: dict[str, Any] = {"url": url, "content_type": "json"}
        if secret:
            config["secret"] = secret
        body: dict[str, Any] = {
            "type": "gitea",
            "active": True,
            "events": events or ["issues", "pull_request"],
            "config": config,
        }
        payload = self._request(
            "POST", f"{self._repo_path(project)}/hooks", json=body)
        assert isinstance(payload, dict)
        return Webhook(
            id=payload.get("id"),
            url=(payload.get("config") or {}).get("url") or "",
            events=list(payload.get("events") or []),
            raw=payload,
        )

    def unregister_webhook(self, project: Project | str,
                           hook_id: Any) -> None:
        self._request(
            "DELETE", f"{self._repo_path(project)}/hooks/{hook_id}")


__all__ = ["GiteaProvider"]
