"""GitHub 平台适配器（issue #484）：GitHub REST API v3。

支持公共 GitHub 与 GitHub Enterprise（base url 可配，GHES 需带 /api/v3）。
流水线映射为 GitHub Actions workflow run；Webhook 为 repository webhooks。
"""

from __future__ import annotations

from typing import Any

from .base import ProviderError
from .domain import (
    Issue,
    IssueComment,
    Pipeline,
    PipelineJob,
    PipelineStatus,
    PLATFORM_GITHUB,
    Project,
    PullRequest,
    PullRequestState,
    Webhook,
    map_github_job_status,
    map_github_pipeline_status,
)
from .rest_base import RestProvider


class GitHubProvider(RestProvider):
    """GitHub 平台适配器。"""

    platform = PLATFORM_GITHUB
    display_name = "GitHub"

    def _build_headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ---- Issue ----

    def get_issue(self, project: Project | str, iid: int) -> Issue:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/issues/{iid}")
        assert isinstance(payload, dict)
        if payload.get("pull_request"):
            raise ProviderError(
                f"编号 {iid} 对应 Pull Request 而非 Issue", 404, self.platform)
        return Issue.from_github(payload)

    def list_open_issues(self, project: Project | str,
                         limit: int | None = None) -> list[Issue]:
        payloads = self._paged(
            f"{self._repo_path(project)}/issues",
            state="open", limit=limit)
        # GitHub issues API 混入 PR 条目（带 pull_request 键），领域层
        # Issue 不含 PR 语义，这里过滤掉
        return [Issue.from_github(p) for p in payloads if not p.get("pull_request")]

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
        return Issue.from_github(payload)

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
            changes["assignees"] = [assignee]
        if not changes:
            return self.get_issue(project, iid)
        payload = self._request(
            "PATCH", f"{self._repo_path(project)}/issues/{iid}", json=changes)
        assert isinstance(payload, dict)
        return Issue.from_github(payload)

    def add_comment(self, project: Project | str, iid: int,
                    body: str) -> IssueComment:
        payload = self._request(
            "POST", f"{self._repo_path(project)}/issues/{iid}/comments",
            json={"body": body})
        assert isinstance(payload, dict)
        return IssueComment.from_github(payload)

    def list_issue_notes(self, project: Project | str, iid: int,
                         limit: int | None = None) -> list[IssueComment]:
        payloads = self._paged(
            f"{self._repo_path(project)}/issues/{iid}/comments", limit=limit)
        return [IssueComment.from_github(p) for p in payloads]

    def add_labels(self, project: Project | str, iid: int,
                   labels: list[str]) -> None:
        if not labels:
            return
        # GitHub POST 追加标签；返回标签列表（调用方不关心结果）
        self._request(
            "POST", f"{self._repo_path(project)}/issues/{iid}/labels",
            json={"labels": list(labels)})

    # ---- Pull Request ----

    def get_pull_request(self, project: Project | str,
                         number: int) -> PullRequest:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/pulls/{number}")
        assert isinstance(payload, dict)
        return PullRequest.from_github(payload)

    def list_pull_requests(self, project: Project | str,
                           state: PullRequestState | None = None,
                           limit: int | None = None) -> list[PullRequest]:
        params: dict[str, Any] = {}
        if state is not None:
            # GitHub 无独立 merged 查询态：merged 在 closed 中；查询
            # merged 时拉全量后在内存过滤
            params["state"] = "closed" if state is PullRequestState.CLOSED else "all"
        payloads = self._paged(
            f"{self._repo_path(project)}/pulls", limit=limit, **params)
        prs = [PullRequest.from_github(p) for p in payloads]
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
        return PullRequest.from_github(payload)

    def merge_pull_request(self, project: Project | str,
                           number: int) -> PullRequest:
        self._request(
            "PUT", f"{self._repo_path(project)}/pulls/{number}/merge")
        # 合并成功响应不含完整 PR 对象，重新拉取最新状态返回
        return self.get_pull_request(project, number)

    # ---- 流水线（GitHub Actions）----

    def get_latest_pipeline(self, project: Project | str,
                            ref: str | None = None) -> Pipeline | None:
        params: dict[str, Any] = {"per_page": 1}
        if ref:
            params["branch"] = ref
        payload = self._request(
            "GET", f"{self._repo_path(project)}/actions/runs", params=params)
        runs = (payload.get("workflow_runs") or []) if isinstance(payload, dict) else []
        if not runs:
            return None
        return self._pipeline(runs[0])

    def list_pipelines(self, project: Project | str,
                       limit: int = 20) -> list[Pipeline]:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/actions/runs",
            params={"per_page": max(1, min(limit, 100))})
        runs = (payload.get("workflow_runs") or []) if isinstance(payload, dict) else []
        return [self._pipeline(run) for run in runs]

    @staticmethod
    def _pipeline(run: dict) -> Pipeline:
        status = map_github_pipeline_status(run.get("status"))
        conclusion = run.get("conclusion")
        if conclusion and status is not PipelineStatus.RUNNING:
            # 完成态以 conclusion 为准（success/failure/cancelled/skipped）
            status = map_github_pipeline_status(conclusion)
        return Pipeline(
            id=run.get("id"),
            status=status,
            ref=run.get("head_branch") or "",
            sha=run.get("head_sha") or "",
            web_url=run.get("html_url") or "",
            created_at=run.get("created_at") or "",
            raw=run,
        )

    def list_pipeline_jobs(self, project: Project | str,
                           pipeline_id: Any) -> list[PipelineJob]:
        payload = self._request(
            "GET", f"{self._repo_path(project)}/actions/runs/{pipeline_id}/jobs",
            params={"per_page": 100})
        jobs = (payload.get("jobs") or []) if isinstance(payload, dict) else []
        result: list[PipelineJob] = []
        for job in jobs:
            result.append(PipelineJob(
                id=job.get("id"),
                name=job.get("name") or "",
                stage="",
                status=map_github_job_status(
                    job.get("conclusion") or job.get("status")),
                web_url=job.get("html_url") or "",
                raw=job,
            ))
        return result

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
            "name": "web",
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


# 供 factory 使用：GitHubProvider 无别名需求，直接导出即可
__all__ = ["GitHubProvider"]
