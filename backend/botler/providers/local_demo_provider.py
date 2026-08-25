"""本地演示平台适配器（issue #484）：纯内存、零网络依赖。

用途：
- 零依赖演示（无 GitLab/GitHub 账号即可体验 Provider 统一接口）；
- 开发与单元测试（不依赖外部服务、不产生真实 API 调用）。

数据保存在进程内 dict（线程安全），默认播种一个演示项目
``demo/demo-project``（含打开/关闭的 Issue、打开/已合并的 PullRequest、
成功/失败的流水线与评论）。调用语义与真实平台一致：不存在的资源抛
``ProviderError(404)``，非法入参抛 ``ProviderError(400)``。
"""

from __future__ import annotations

import threading
from typing import Any

from .base import Provider, ProviderError, project_ref
from .domain import (
    Issue,
    IssueComment,
    IssueState,
    Pipeline,
    PipelineJob,
    PipelineStatus,
    PLATFORM_LOCAL_DEMO,
    Project,
    PullRequest,
    PullRequestState,
    Webhook,
)

# 演示项目默认值
_DEMO_KEY = "demo"
_DEMO_PATH = "demo/demo-project"
_DEMO_NAME = "演示项目"
_DEMO_DEFAULT_BRANCH = "main"


class LocalDemoProvider(Provider):
    """内存演示 Provider：实现完整 Provider 接口，无任何外部依赖。"""

    platform = PLATFORM_LOCAL_DEMO
    display_name = "本地演示（LocalDemo）"

    def __init__(self, url: str = "local://demo", token: str | None = None,
                 verify_ssl: bool = True, seed: bool = True):
        super().__init__(url, token, verify_ssl)
        self._lock = threading.RLock()
        # key → 项目状态（issues/prs/pipelines/webhooks 等全部内存化）
        self._projects: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, int] = {}
        if seed:
            self._seed()

    # ---- 内部数据结构 ----

    def _seed(self) -> None:
        """播种演示项目数据（幂等：已存在则跳过）。"""
        with self._lock:
            if _DEMO_KEY in self._projects:
                return
            self._projects[_DEMO_KEY] = {
                "key": _DEMO_KEY,
                "name": _DEMO_NAME,
                "path": _DEMO_PATH,
                "web_url": "local://demo/demo-project",
                "default_branch": _DEMO_DEFAULT_BRANCH,
                "issues": {},      # iid → dict
                "comments": {},    # iid → list[dict]
                "prs": {},         # number → dict
                "pipelines": [],   # list[dict]（新→旧）
                "jobs": {},        # pipeline_id → list[dict]
                "webhooks": {},    # hook_id → dict
            }
            # 计数器初值 = 已播种最大 id，保证首个新对象 id 连续
            self._counters = {
                "issue": 2, "pr": 2, "comment": 1001,
                "pipeline": 302, "job": 903, "hook": 1,
            }
            proj = self._projects[_DEMO_KEY]
            proj["issues"] = {
                1: {"id": 101, "iid": 1, "title": "示例 Issue：演示本地 Provider",
                    "description": "这是 LocalDemoProvider 播种的打开 Issue",
                    "state": "open", "labels": ["feature", "demo"],
                    "author": "demo-user", "assignees": ["demo-user"],
                    "web_url": "local://demo/demo-project/-/issues/1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z"},
                2: {"id": 102, "iid": 2, "title": "已关闭的示例 Issue",
                    "description": "播种数据里的关闭 Issue",
                    "state": "closed", "labels": ["docs"],
                    "author": "demo-user", "assignees": [],
                    "web_url": "local://demo/demo-project/-/issues/2",
                    "created_at": "2026-01-02T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z"},
            }
            proj["comments"] = {
                1: [{"id": 1001, "body": "演示评论：欢迎体验本地 Provider",
                     "author": "demo-user", "created_at": "2026-01-01T00:00:01Z"}],
            }
            proj["prs"] = {
                1: {"id": 201, "number": 1, "title": "示例 Pull Request",
                    "state": "open", "source_branch": "feature/demo",
                    "target_branch": "main", "description": "演示 PR",
                    "author": "demo-user", "web_url": "local://demo/pulls/1",
                    "merged": False, "created_at": "2026-01-01T00:00:00Z"},
                2: {"id": 202, "number": 2, "title": "已合并的 Pull Request",
                    "state": "merged", "source_branch": "feature/done",
                    "target_branch": "main", "description": "演示已合并 PR",
                    "author": "demo-user", "web_url": "local://demo/pulls/2",
                    "merged": True, "created_at": "2026-01-01T00:00:00Z"},
            }
            proj["pipelines"] = [
                {"id": 302, "status": "failed", "ref": "main", "sha": "abc123",
                 "web_url": "local://demo/pipelines/302",
                 "created_at": "2026-01-02T00:00:00Z"},
                {"id": 301, "status": "success", "ref": "main", "sha": "abc122",
                 "web_url": "local://demo/pipelines/301",
                 "created_at": "2026-01-01T00:00:00Z"},
            ]
            proj["jobs"] = {
                301: [{"id": 901, "name": "backend:test", "stage": "build",
                       "status": "success", "web_url": "local://demo/jobs/901"},
                      {"id": 902, "name": "backend:mypy", "stage": "build",
                       "status": "success", "web_url": "local://demo/jobs/902"}],
                302: [{"id": 903, "name": "backend:test", "stage": "build",
                       "status": "failed", "web_url": "local://demo/jobs/903"}],
            }
            proj["webhooks"] = {
                1: {"id": 1, "url": "local://demo/hook/1", "events": ["issues"]},
            }

    def reset(self) -> None:
        """清空并重新播种（测试用）。"""
        with self._lock:
            self._projects.clear()
            self._counters.clear()
            self._seed()

    def _project(self, project: Project | str) -> dict[str, Any]:
        """归一 project 参数并取项目状态；不存在抛 404。"""
        ref = project_ref(project)
        key = ref
        if "/" in ref:
            # 支持 owner/repo 形态：映射回 key（演示项目 path 唯一）
            for candidate in self._projects.values():
                if candidate["path"] == ref:
                    return candidate
            raise ProviderError(f"演示项目不存在: {ref}", 404, self.platform)
        proj = self._projects.get(key)
        if proj is None:
            raise ProviderError(f"演示项目不存在: {ref}", 404, self.platform)
        return proj

    def _next(self, name: str) -> int:
        self._counters[name] = self._counters.get(name, 0) + 1
        return self._counters[name]

    def _issue_payload(self, proj: dict[str, Any], iid: int) -> dict:
        issue = proj["issues"].get(iid)
        if issue is None:
            raise ProviderError(f"Issue 不存在: {iid}", 404, self.platform)
        return issue

    # ---- 认证与项目 ----

    def test_connection(self) -> bool:
        return True

    def resolve_project(self, ref: str) -> Project:
        proj = self._project(ref)
        return Project(
            id=proj["key"],
            name=proj["name"],
            path=proj["path"],
            web_url=proj["web_url"],
            default_branch=proj["default_branch"],
            raw=dict(proj),
        )

    # ---- Issue ----

    def get_issue(self, project: Project | str, iid: int) -> Issue:
        with self._lock:
            proj = self._project(project)
            issue = self._issue_payload(proj, iid)
            return self._to_issue(issue)

    def list_open_issues(self, project: Project | str,
                         limit: int | None = None) -> list[Issue]:
        with self._lock:
            proj = self._project(project)
            items = [self._to_issue(i) for i in proj["issues"].values()
                     if i["state"] == "open"]
            items.sort(key=lambda i: i.iid)
            return items[:limit] if limit is not None else items

    def create_issue(self, project: Project | str, title: str,
                     description: str | None = None,
                     labels: list[str] | None = None) -> Issue:
        if not title or not title.strip():
            raise ProviderError("Issue 标题不能为空", 400, self.platform)
        with self._lock:
            proj = self._project(project)
            iid = self._next("issue")
            proj["issues"][iid] = {
                "id": iid + 100,
                "iid": iid,
                "title": title.strip(),
                "description": description or "",
                "state": "open",
                "labels": list(dict.fromkeys(labels or [])),
                "author": "demo-user",
                "assignees": [],
                "web_url": f"local://demo/{proj['path']}/-/issues/{iid}",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            return self._to_issue(proj["issues"][iid])

    def update_issue(self, project: Project | str, iid: int, *,
                     title: str | None = None,
                     description: str | None = None,
                     state: str | None = None,
                     assignee: str | None = None) -> Issue:
        with self._lock:
            proj = self._project(project)
            issue = self._issue_payload(proj, iid)
            if title is not None:
                if not title.strip():
                    raise ProviderError("Issue 标题不能为空", 400, self.platform)
                issue["title"] = title.strip()
            if description is not None:
                issue["description"] = description
            if state is not None:
                normalized = state.strip().lower()
                if normalized not in ("open", "closed"):
                    raise ProviderError(
                        f"不支持的 Issue 状态: {state}（仅支持 open/closed）",
                        400, self.platform)
                issue["state"] = normalized
            if assignee is not None:
                issue["assignees"] = [assignee] if assignee.strip() else []
            issue["updated_at"] = "2026-01-01T00:00:00Z"
            return self._to_issue(issue)

    def add_comment(self, project: Project | str, iid: int,
                    body: str) -> IssueComment:
        with self._lock:
            proj = self._project(project)
            self._issue_payload(proj, iid)
            cid = self._next("comment")
            comment = {
                "id": cid,
                "body": body,
                "author": "demo-user",
                "created_at": "2026-01-01T00:00:00Z",
            }
            proj["comments"].setdefault(iid, []).append(comment)
            return self._to_comment(comment)

    def list_issue_notes(self, project: Project | str, iid: int,
                         limit: int | None = None) -> list[IssueComment]:
        with self._lock:
            proj = self._project(project)
            self._issue_payload(proj, iid)
            comments = [self._to_comment(c)
                        for c in proj["comments"].get(iid, [])]
            if limit is not None:
                comments = comments[-limit:]
            return comments

    def add_labels(self, project: Project | str, iid: int,
                   labels: list[str]) -> None:
        if not labels:
            return
        with self._lock:
            proj = self._project(project)
            issue = self._issue_payload(proj, iid)
            existing = list(issue["labels"])
            for label in labels:
                if label not in existing:
                    existing.append(label)
            issue["labels"] = existing

    # ---- Pull Request / ChangeRequest ----

    def get_pull_request(self, project: Project | str,
                         number: int) -> PullRequest:
        with self._lock:
            proj = self._project(project)
            pr = proj["prs"].get(number)
            if pr is None:
                raise ProviderError(f"Pull Request 不存在: {number}", 404, self.platform)
            return self._to_pr(pr)

    def list_pull_requests(self, project: Project | str,
                           state: PullRequestState | None = None,
                           limit: int | None = None) -> list[PullRequest]:
        with self._lock:
            proj = self._project(project)
            items = list(proj["prs"].values())
            if state is not None:
                if state is PullRequestState.CLOSED:
                    # 通用「已关闭」语义包含 merged（merged 属于非打开状态）
                    items = [p for p in items if p["state"] in ("closed", "merged")]
                else:
                    items = [p for p in items if p["state"] == state.value]
            prs = [self._to_pr(p) for p in items]
            prs.sort(key=lambda p: p.number)
            return prs[:limit] if limit is not None else prs

    def create_pull_request(self, project: Project | str, *,
                            source_branch: str, target_branch: str,
                            title: str,
                            description: str | None = None) -> PullRequest:
        if not title or not title.strip():
            raise ProviderError("Pull Request 标题不能为空", 400, self.platform)
        if not source_branch or not target_branch:
            raise ProviderError(
                "Pull Request 源分支与目标分支不能为空", 400, self.platform)
        with self._lock:
            proj = self._project(project)
            number = self._next("pr")
            proj["prs"][number] = {
                "id": number + 200,
                "number": number,
                "title": title.strip(),
                "state": "open",
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": description or "",
                "author": "demo-user",
                "web_url": f"local://demo/{proj['path']}/pulls/{number}",
                "merged": False,
                "created_at": "2026-01-01T00:00:00Z",
            }
            return self._to_pr(proj["prs"][number])

    def merge_pull_request(self, project: Project | str,
                           number: int) -> PullRequest:
        with self._lock:
            proj = self._project(project)
            pr = proj["prs"].get(number)
            if pr is None:
                raise ProviderError(f"Pull Request 不存在: {number}", 404, self.platform)
            if pr["state"] == "merged":
                return self._to_pr(pr)
            pr["state"] = "merged"
            pr["merged"] = True
            return self._to_pr(pr)

    # ---- 流水线 ----

    def get_latest_pipeline(self, project: Project | str,
                            ref: str | None = None) -> Pipeline | None:
        with self._lock:
            proj = self._project(project)
            pipelines = proj["pipelines"]
            if ref:
                pipelines = [p for p in pipelines if p["ref"] == ref]
            if not pipelines:
                return None
            return self._to_pipeline(pipelines[0])

    def list_pipelines(self, project: Project | str,
                       limit: int = 20) -> list[Pipeline]:
        with self._lock:
            proj = self._project(project)
            return [self._to_pipeline(p) for p in proj["pipelines"][:limit]]

    def list_pipeline_jobs(self, project: Project | str,
                           pipeline_id: Any) -> list[PipelineJob]:
        with self._lock:
            proj = self._project(project)
            jobs = proj["jobs"].get(int(pipeline_id), [])
            return [
                PipelineJob(
                    id=job.get("id"),
                    name=job.get("name") or "",
                    stage=job.get("stage") or "",
                    status=self._pipeline_status(job.get("status")),
                    web_url=job.get("web_url") or "",
                    raw=job,
                )
                for job in jobs
            ]

    # ---- Webhook ----

    def list_webhooks(self, project: Project | str) -> list[Webhook]:
        with self._lock:
            proj = self._project(project)
            return [
                Webhook(id=h.get("id"), url=h.get("url") or "",
                        events=list(h.get("events") or []), raw=h)
                for h in proj["webhooks"].values()
            ]

    def register_webhook(self, project: Project | str, url: str,
                         secret: str | None = None,
                         events: list[str] | None = None) -> Webhook:
        with self._lock:
            proj = self._project(project)
            hook_id = self._next("hook")
            proj["webhooks"][hook_id] = {
                "id": hook_id,
                "url": url,
                "events": list(events or ["issues"]),
            }
            hook = proj["webhooks"][hook_id]
            return Webhook(id=hook["id"], url=hook["url"],
                           events=list(hook["events"]), raw=hook)

    def unregister_webhook(self, project: Project | str,
                           hook_id: Any) -> None:
        with self._lock:
            proj = self._project(project)
            if int(hook_id) not in proj["webhooks"]:
                raise ProviderError(
                    f"Webhook 不存在: {hook_id}", 404, self.platform)
            del proj["webhooks"][int(hook_id)]

    # ---- 领域模型转换 ----

    @staticmethod
    def _to_issue(issue: dict) -> Issue:
        return Issue(
            id=issue.get("id"),
            iid=issue.get("iid", 0),
            title=issue.get("title", ""),
            description=issue.get("description", ""),
            state=(IssueState.OPEN if issue.get("state") != "closed"
                   else IssueState.CLOSED),
            labels=list(issue.get("labels") or []),
            author=None,
            assignees=[],
            web_url=issue.get("web_url", ""),
            created_at=issue.get("created_at", ""),
            updated_at=issue.get("updated_at", ""),
            raw=issue,
        )

    @staticmethod
    def _to_pr(pr: dict) -> PullRequest:
        state = {
            "open": PullRequestState.OPEN,
            "closed": PullRequestState.CLOSED,
            "merged": PullRequestState.MERGED,
        }.get(pr.get("state") or "", PullRequestState.OPEN)
        return PullRequest(
            id=pr.get("id"),
            number=pr.get("number", 0),
            title=pr.get("title", ""),
            state=state,
            source_branch=pr.get("source_branch", ""),
            target_branch=pr.get("target_branch", ""),
            description=pr.get("description", ""),
            author=None,
            web_url=pr.get("web_url", ""),
            merged=bool(pr.get("merged")),
            created_at=pr.get("created_at", ""),
            raw=pr,
        )

    @staticmethod
    def _to_comment(comment: dict) -> IssueComment:
        return IssueComment(
            id=comment.get("id"),
            body=comment.get("body", ""),
            author=None,
            created_at=comment.get("created_at", ""),
            system=False,
            raw=comment,
        )

    def _to_pipeline(self, pipeline: dict) -> Pipeline:
        return Pipeline(
            id=pipeline.get("id"),
            status=self._pipeline_status(pipeline.get("status")),
            ref=pipeline.get("ref", ""),
            sha=pipeline.get("sha", ""),
            web_url=pipeline.get("web_url", ""),
            created_at=pipeline.get("created_at", ""),
            raw=pipeline,
        )

    @staticmethod
    def _pipeline_status(value: Any) -> PipelineStatus:
        mapping = {
            "success": PipelineStatus.SUCCESS,
            "failed": PipelineStatus.FAILED,
            "failure": PipelineStatus.FAILED,
            "running": PipelineStatus.RUNNING,
            "pending": PipelineStatus.PENDING,
            "canceled": PipelineStatus.CANCELED,
            "cancelled": PipelineStatus.CANCELED,
            "skipped": PipelineStatus.SKIPPED,
        }
        return mapping.get(str(value).lower(), PipelineStatus.UNKNOWN)


__all__ = ["LocalDemoProvider"]
