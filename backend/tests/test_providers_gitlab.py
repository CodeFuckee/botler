"""GitLabProvider 适配器测试（issue #484）。

用桩 GitLabClient（替换 _request / _paged）验证：通用接口 → GitLab API
调用路径的映射、领域模型转换、异常转换（GitLabError → ProviderError）、
边界输入（空标题 / 非法状态 / 未知项目等）。
"""


import pytest

from botler.gitlab_client import GitLabError
from botler.providers import (
    GitLabProvider,
    IssueState,
    PipelineStatus,
    Project,
    ProviderError,
    PullRequest,
    PullRequestState,
)

ISSUE_PAYLOAD = {
    "id": 1, "iid": 7, "title": "标题", "description": "正文",
    "state": "opened", "labels": "feature,bug",
    "author": {"id": 3, "username": "agent", "name": "agent"},
    "web_url": "https://gitlab.example.com/ckd/botler/-/issues/7",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}
MR_PAYLOAD = {
    "id": 21, "iid": 8, "title": "MR 标题", "state": "opened",
    "source_branch": "feature/x", "target_branch": "main",
    "web_url": "https://gitlab.example.com/ckd/botler/-/merge_requests/8",
}
PIPELINE_PAYLOAD = {
    "id": 302, "status": "running", "ref": "main", "sha": "abc123",
    "web_url": "https://gitlab.example.com/ckd/botler/-/pipelines/302",
    "created_at": "2026-01-01T00:00:00Z",
}


class StubClient:
    """记录调用参数并按脚本返回的桩 GitLabClient。"""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple] = []

    def _answer(self, key: str):
        value = self.responses.get(key, [])
        if isinstance(value, list):
            return list(value)  # 每次调用返回副本
        return value

    def test_connection(self):
        self.calls.append(("test_connection",))
        if self.responses.get("conn_error"):
            raise GitLabError("token 无效（401）", 401)
        return {"id": 11, "username": "bot"}

    def resolve_project(self, ref):
        self.calls.append(("resolve_project", ref))
        if ref == "unknown":
            raise GitLabError("项目不存在（404）: /projects/unknown", 404)
        return {"id": 42, "name": "botler", "path_with_namespace": "ckd/botler",
                "web_url": "https://gitlab.example.com/ckd/botler",
                "default_branch": "main"}

    def get_issue(self, pid, iid):
        self.calls.append(("get_issue", pid, iid))
        if iid == 999:
            raise GitLabError("资源不存在（404）: /projects/42/issues/999", 404)
        return ISSUE_PAYLOAD

    def list_open_issues(self, pid, limit=None):
        self.calls.append(("list_open_issues", pid, limit))
        return [ISSUE_PAYLOAD]

    def create_issue(self, pid, title, description=None, labels=None):
        self.calls.append(("create_issue", pid, title, description, labels))
        return {**ISSUE_PAYLOAD, "iid": 100, "title": title}

    def update_issue(self, pid, iid, **changes):
        self.calls.append(("update_issue", pid, iid, changes))
        return {**ISSUE_PAYLOAD, "iid": iid}

    def add_comment(self, pid, iid, body):
        self.calls.append(("add_comment", pid, iid, body))
        return {"id": 51, "body": body, "system": False,
                "author": {"id": 3, "username": "agent"}}

    def list_issue_notes(self, pid, iid, limit=None):
        self.calls.append(("list_issue_notes", pid, iid, limit))
        return [{"id": 51, "body": "评论", "system": False,
                 "author": {"id": 3, "username": "agent"}}]

    def add_labels(self, pid, iid, labels, remove=None):
        self.calls.append(("add_labels", pid, iid, labels))
        return ISSUE_PAYLOAD

    def get_user_id_by_username(self, username):
        self.calls.append(("get_user_id_by_username", username))
        return 5 if username == "agent" else None

    def get_merge_request(self, pid, mr_iid):
        self.calls.append(("get_merge_request", pid, mr_iid))
        if mr_iid == 999:
            raise GitLabError("资源不存在（404）", 404)
        return MR_PAYLOAD

    def list_merge_requests(self, pid, state=None, limit=None):
        self.calls.append(("list_merge_requests", pid, state, limit))
        return [MR_PAYLOAD]

    def create_merge_request(self, pid, source, target, title, description=None):
        self.calls.append(("create_merge_request", pid, source, target, title, description))
        return {**MR_PAYLOAD, "iid": 200, "title": title,
                "source_branch": source, "target_branch": target}

    def merge_merge_request(self, pid, mr_iid):
        self.calls.append(("merge_merge_request", pid, mr_iid))
        return {**MR_PAYLOAD, "iid": mr_iid, "state": "merged",
                "merged_at": "2026-01-01T00:00:00Z"}

    def get_latest_pipeline(self, pid):
        self.calls.append(("get_latest_pipeline", pid))
        return PIPELINE_PAYLOAD

    def list_pipelines(self, pid, limit=200):
        self.calls.append(("list_pipelines", pid, limit))
        return [PIPELINE_PAYLOAD]

    def list_pipeline_jobs(self, pid, pipeline_id):
        self.calls.append(("list_pipeline_jobs", pid, pipeline_id))
        return [{"id": 901, "name": "backend:test", "stage": "build",
                 "status": "success", "web_url": "https://gitlab.example.com/jobs/901"}]

    def list_webhooks(self, pid):
        self.calls.append(("list_webhooks", pid))
        return [{"id": 1, "url": "https://app.example.com/webhook/gitlab",
                 "issues_events": True, "push_events": False}]

    def register_webhook(self, pid, secret):
        self.calls.append(("register_webhook", pid, secret))
        return {"id": 2, "url": "https://app.example.com/webhook/gitlab",
                "issues_events": True}

    def delete_webhook(self, pid, hook_id):
        self.calls.append(("delete_webhook", pid, hook_id))


def make_provider(client: StubClient | None = None) -> tuple[GitLabProvider, StubClient]:
    stub = client or StubClient()
    provider = GitLabProvider(
        url="https://gitlab.example.com", token="glpat-test",
        client=stub)  # type: ignore[arg-type]
    return provider, stub


class TestGitLabProviderCore:
    def test_platform_metadata(self):
        provider, _ = make_provider()
        assert provider.platform == "gitlab"
        assert provider.display_name == "GitLab"

    def test_test_connection_ok(self):
        provider, stub = make_provider()
        assert provider.test_connection() is True
        assert stub.calls[0][0] == "test_connection"

    def test_test_connection_failure_raises_provider_error(self):
        provider, _ = make_provider(StubClient({"conn_error": True}))
        with pytest.raises(ProviderError) as exc:
            provider.test_connection()
        assert exc.value.status_code == 401
        assert exc.value.platform == "gitlab"

    def test_resolve_project(self):
        provider, stub = make_provider()
        project = provider.resolve_project("ckd/botler")
        assert project.id == "42"
        assert project.path == "ckd/botler"
        assert stub.calls[0] == ("resolve_project", "ckd/botler")

    def test_resolve_project_404(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.resolve_project("unknown")
        assert exc.value.status_code == 404


class TestGitLabProviderIssues:
    def test_get_issue_maps_domain(self):
        provider, stub = make_provider()
        issue = provider.get_issue("42", 7)
        assert issue.iid == 7
        assert issue.title == "标题"
        assert issue.state is IssueState.OPEN
        assert issue.labels == ["feature", "bug"]
        assert stub.calls[0] == ("get_issue", 42, 7)

    def test_get_issue_accepts_project_object(self):
        provider, _ = make_provider()
        project = Project(id="42", path="ckd/botler", raw={"id": 42})
        issue = provider.get_issue(project, 7)
        assert issue.iid == 7

    def test_get_issue_404_converted(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("42", 999)
        assert exc.value.status_code == 404

    def test_list_open_issues(self):
        provider, stub = make_provider()
        issues = provider.list_open_issues("42")
        assert len(issues) == 1
        assert issues[0].iid == 7
        assert stub.calls[0][0] == "list_open_issues"

    def test_create_issue_empty_title_rejected(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.create_issue("42", "   ")
        assert exc.value.status_code == 400

    def test_create_issue(self):
        provider, stub = make_provider()
        issue = provider.create_issue("42", "新标题", "新正文", ["feature"])
        assert issue.iid == 100
        assert issue.title == "新标题"
        assert stub.calls[0] == ("create_issue", 42, "新标题", "新正文", ["feature"])

    def test_update_issue_close(self):
        provider, stub = make_provider()
        issue = provider.update_issue("42", 7, state="closed")
        assert issue.iid == 7
        assert stub.calls[0] == ("update_issue", 42, 7, {"state_event": "close"})

    def test_update_issue_open(self):
        provider, stub = make_provider()
        provider.update_issue("42", 7, state="open")
        assert stub.calls[0] == ("update_issue", 42, 7, {"state_event": "reopen"})

    def test_update_issue_invalid_state(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.update_issue("42", 7, state="half-open")
        assert exc.value.status_code == 400

    def test_update_issue_assignee(self):
        provider, stub = make_provider()
        provider.update_issue("42", 7, assignee="agent")
        assert stub.calls[0] == ("get_user_id_by_username", "agent")
        assert stub.calls[1] == ("update_issue", 42, 7, {"assignee_ids": [5]})

    def test_update_issue_unknown_assignee_clears(self):
        provider, stub = make_provider()
        provider.update_issue("42", 7, assignee="ghost")
        assert stub.calls[1] == ("update_issue", 42, 7, {"assignee_ids": []})

    def test_update_issue_no_changes_returns_current(self):
        provider, stub = make_provider()
        issue = provider.update_issue("42", 7)
        assert issue.iid == 7
        # 无字段变更时只调用 get_issue，不触发写请求
        assert stub.calls[0][0] == "get_issue"
        assert len(stub.calls) == 1

    def test_add_comment(self):
        provider, stub = make_provider()
        comment = provider.add_comment("42", 7, "评论正文")
        assert comment.body == "评论正文"
        assert stub.calls[0] == ("add_comment", 42, 7, "评论正文")

    def test_list_issue_notes(self):
        provider, _ = make_provider()
        comments = provider.list_issue_notes("42", 7)
        assert len(comments) == 1
        assert comments[0].body == "评论"

    def test_add_labels(self):
        provider, stub = make_provider()
        provider.add_labels("42", 7, ["feature", "docs"])
        assert stub.calls[0] == ("add_labels", 42, 7, ["feature", "docs"])

    def test_add_labels_empty_noop(self):
        provider, stub = make_provider()
        provider.add_labels("42", 7, [])
        assert stub.calls == []


class TestGitLabProviderPullRequests:
    def test_get_pull_request_maps_to_pull_request_model(self):
        provider, stub = make_provider()
        pr = provider.get_pull_request("42", 8)
        assert isinstance(pr, PullRequest)  # 领域层只有 PullRequest，无 MergeRequest
        assert pr.number == 8
        assert pr.state is PullRequestState.OPEN
        assert pr.source_branch == "feature/x"
        assert stub.calls[0] == ("get_merge_request", 42, 8)

    def test_get_pull_request_404(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.get_pull_request("42", 999)
        assert exc.value.status_code == 404

    def test_list_pull_requests_open(self):
        provider, stub = make_provider()
        prs = provider.list_pull_requests("42", state=PullRequestState.OPEN)
        assert len(prs) == 1
        # GitLab MR API 用 opened（领域层 open 归一为 opened）
        assert stub.calls[0] == ("list_merge_requests", 42, "opened", None)

    def test_list_pull_requests_no_state(self):
        provider, stub = make_provider()
        prs = provider.list_pull_requests("42")
        assert len(prs) == 1
        assert stub.calls[0] == ("list_merge_requests", 42, None, None)

    def test_create_pull_request_empty_title(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.create_pull_request("42", source_branch="f",
                                        target_branch="main", title="")
        assert exc.value.status_code == 400

    def test_create_pull_request_missing_branch(self):
        provider, _ = make_provider()
        with pytest.raises(ProviderError) as exc:
            provider.create_pull_request("42", source_branch="",
                                        target_branch="main", title="标题")
        assert exc.value.status_code == 400

    def test_create_pull_request(self):
        provider, stub = make_provider()
        pr = provider.create_pull_request(
            "42", source_branch="feature/x", target_branch="main",
            title="新增功能", description="说明")
        assert pr.number == 200
        assert stub.calls[0] == ("create_merge_request", 42, "feature/x",
                                 "main", "新增功能", "说明")

    def test_merge_pull_request(self):
        provider, stub = make_provider()
        pr = provider.merge_pull_request("42", 8)
        assert pr.state is PullRequestState.MERGED
        assert stub.calls[0] == ("merge_merge_request", 42, 8)


class TestGitLabProviderPipelines:
    def test_get_latest_pipeline(self):
        provider, stub = make_provider()
        pipeline = provider.get_latest_pipeline("42")
        assert pipeline is not None
        assert pipeline.id == 302
        assert pipeline.status is PipelineStatus.RUNNING
        assert pipeline.ref == "main"
        assert stub.calls[0] == ("get_latest_pipeline", 42)

    def test_list_pipelines(self):
        provider, stub = make_provider()
        pipelines = provider.list_pipelines("42", limit=5)
        assert len(pipelines) == 1
        assert stub.calls[0] == ("list_pipelines", 42, 5)

    def test_list_pipeline_jobs(self):
        provider, _ = make_provider()
        jobs = provider.list_pipeline_jobs("42", 302)
        assert len(jobs) == 1
        assert jobs[0].name == "backend:test"
        assert jobs[0].status is PipelineStatus.SUCCESS


class TestGitLabProviderWebhooks:
    def test_list_webhooks(self):
        provider, stub = make_provider()
        hooks = provider.list_webhooks("42")
        assert len(hooks) == 1
        assert hooks[0].id == 1
        assert "issues" in hooks[0].events
        assert stub.calls[0] == ("list_webhooks", 42)

    def test_register_webhook(self):
        provider, stub = make_provider()
        hook = provider.register_webhook("42", "https://app.example.com/webhook",
                                        secret="s3cr3t")
        assert hook.id == 2
        assert stub.calls[0] == ("register_webhook", 42, "s3cr3t")

    def test_unregister_webhook(self):
        provider, stub = make_provider()
        provider.unregister_webhook("42", 7)
        assert stub.calls[0] == ("delete_webhook", 42, 7)


class TestGitLabProviderErrorConversion:
    def test_gitlab_error_preserves_status_and_message(self):
        class FailClient(StubClient):
            def get_issue(self, pid, iid):
                raise GitLabError("GitLab API 错误 500: boom", 500)

        provider, _ = make_provider(FailClient())
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("42", 1)
        assert exc.value.status_code == 500
        assert "boom" in exc.value.message
        assert exc.value.platform == "gitlab"

    def test_platform_classvar_consistency(self):
        assert GitLabProvider.platform == "gitlab"
