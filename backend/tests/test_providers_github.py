"""GitHubProvider 适配器测试（issue #484）。

用 httpx.MockTransport 拦截请求，验证：URL 路径与参数、鉴权头
（Authorization: Bearer）、分页、领域模型映射、错误转换与边界输入。
"""

import json

import httpx
import pytest

from botler.providers import (
    GitHubProvider,
    IssueState,
    PipelineStatus,
    ProviderError,
    PullRequestState,
)

GITHUB_ISSUE = {
    "id": 11, "number": 5, "title": "gh 标题", "body": "gh 正文",
    "state": "open",
    "labels": [{"name": "bug"}, {"name": "enhancement"}],
    "user": {"login": "octocat", "id": 1},
    "html_url": "https://github.com/ckd/botler/issues/5",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}
GITHUB_PR = {
    "id": 31, "number": 10, "title": "PR 标题", "state": "open",
    "head": {"ref": "feature/y"}, "base": {"ref": "main"},
    "user": {"login": "octocat"},
    "html_url": "https://github.com/ckd/botler/pull/10",
    "created_at": "2026-01-01T00:00:00Z",
}
GITHUB_RUN = {
    "id": 401, "name": "CI", "status": "completed", "conclusion": "success",
    "head_branch": "main", "head_sha": "abc123",
    "html_url": "https://github.com/ckd/botler/actions/runs/401",
    "created_at": "2026-01-01T00:00:00Z",
}
GITHUB_JOB = {
    "id": 501, "name": "backend:test", "status": "completed",
    "conclusion": "success", "html_url": "https://github.com/.../job/501",
}
GITHUB_HOOK = {
    "id": 601, "events": ["issues", "pull_request"],
    "config": {"url": "https://app.example.com/webhook", "content_type": "json"},
}


class MockAPI:
    """按请求路径返回脚本数据的 MockTransport 处理器。"""

    def __init__(self, routes: dict | None = None, fail: int | None = None,
                 no_content_paths: set[str] | None = None):
        self.routes = routes or {}
        self.fail = fail  # 非空时所有请求返回该状态码
        self.no_content = no_content_paths or set()  # 这些路径返回 204
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(self.fail, text=f"error {self.fail}")
        if request.url.path in self.no_content:
            return httpx.Response(204)
        data = self.routes.get(request.url.path)
        if data is None:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, json=data)


def make_provider(mock: MockAPI) -> GitHubProvider:
    provider = GitHubProvider("https://api.github.com", "ghp_test")
    provider._http = httpx.Client(
        transport=httpx.MockTransport(mock.handler),
        base_url="https://api.github.com",
        headers=provider._build_headers("ghp_test"),
    )
    return provider


class TestGitHubCore:
    def test_platform_metadata(self):
        assert GitHubProvider.platform == "github"
        assert GitHubProvider.display_name == "GitHub"

    def test_test_connection_ok(self):
        mock = MockAPI({"/user": {"id": 1, "login": "octocat"}})
        provider = make_provider(mock)
        assert provider.test_connection() is True
        assert mock.requests[0].headers["authorization"] == "Bearer ghp_test"

    def test_test_connection_401(self):
        mock = MockAPI(fail=401)
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.test_connection()
        assert exc.value.status_code == 401
        assert exc.value.platform == "github"

    def test_resolve_project_owner_repo(self):
        mock = MockAPI({"/repos/ckd/botler": {
            "id": 1, "name": "botler", "full_name": "ckd/botler",
            "html_url": "https://github.com/ckd/botler",
            "default_branch": "main"}})
        provider = make_provider(mock)
        project = provider.resolve_project("ckd/botler")
        assert project.id == "ckd/botler"
        assert project.path == "ckd/botler"
        assert project.default_branch == "main"

    def test_resolve_project_url_form(self):
        mock = MockAPI({"/repos/ckd/botler": {"full_name": "ckd/botler"}})
        provider = make_provider(mock)
        project = provider.resolve_project("https://github.com/ckd/botler.git")
        assert project.id == "ckd/botler"
        assert mock.requests[0].url.path == "/repos/ckd/botler"

    def test_resolve_project_scp_like(self):
        mock = MockAPI({"/repos/ckd/botler": {"full_name": "ckd/botler"}})
        provider = make_provider(mock)
        project = provider.resolve_project("git@github.com:ckd/botler.git")
        assert project.id == "ckd/botler"

    def test_resolve_project_invalid_ref(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.resolve_project("onlyone")
        assert exc.value.status_code == 400

    def test_api_error_429(self):
        mock = MockAPI(fail=429)
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("ckd/botler", 1)
        assert exc.value.status_code == 429


class TestGitHubIssues:
    def test_get_issue(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5": GITHUB_ISSUE})
        provider = make_provider(mock)
        issue = provider.get_issue("ckd/botler", 5)
        assert issue.iid == 5
        assert issue.state is IssueState.OPEN
        assert issue.labels == ["bug", "enhancement"]
        assert issue.author is not None and issue.author.username == "octocat"

    def test_get_issue_pr_number_rejected(self):
        # GitHub 的 issue API 对 PR 编号返回带 pull_request 键的对象
        mock = MockAPI({"/repos/ckd/botler/issues/10":
                        {**GITHUB_PR, "pull_request": {"url": "x"}}})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("ckd/botler", 10)
        assert exc.value.status_code == 404

    def test_get_issue_404(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("ckd/botler", 999)
        assert exc.value.status_code == 404

    def test_list_open_issues_filters_prs(self):
        mock = MockAPI({"/repos/ckd/botler/issues": [
            GITHUB_ISSUE,
            {**GITHUB_PR, "pull_request": {"url": "x"}},
        ]})
        provider = make_provider(mock)
        issues = provider.list_open_issues("ckd/botler")
        assert len(issues) == 1
        assert issues[0].iid == 5
        # 请求应带 state=open
        assert mock.requests[0].url.params["state"] == "open"

    def test_create_issue_empty_title(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.create_issue("ckd/botler", "  ")
        assert exc.value.status_code == 400

    def test_create_issue(self):
        mock = MockAPI({"/repos/ckd/botler/issues": {**GITHUB_ISSUE, "number": 6}})
        provider = make_provider(mock)
        issue = provider.create_issue("ckd/botler", "新标题", "正文", ["feature"])
        assert issue.iid == 6
        body = json.loads(mock.requests[0].content)
        assert body == {"title": "新标题", "body": "正文", "labels": ["feature"]}

    def test_update_issue_state(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5": {**GITHUB_ISSUE, "state": "closed"}})
        provider = make_provider(mock)
        issue = provider.update_issue("ckd/botler", 5, state="closed")
        assert issue.state is IssueState.CLOSED
        assert mock.requests[0].method == "PATCH"
        assert json.loads(mock.requests[0].content) == {"state": "closed"}

    def test_update_issue_invalid_state(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.update_issue("ckd/botler", 5, state="semi")
        assert exc.value.status_code == 400

    def test_update_issue_no_changes(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5": GITHUB_ISSUE})
        provider = make_provider(mock)
        issue = provider.update_issue("ckd/botler", 5)
        assert issue.iid == 5
        assert mock.requests[0].method == "GET"

    def test_add_comment(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5/comments": {
            "id": 51, "body": "评论", "user": {"login": "octocat"}}})
        provider = make_provider(mock)
        comment = provider.add_comment("ckd/botler", 5, "评论")
        assert comment.body == "评论"
        assert json.loads(mock.requests[0].content) == {"body": "评论"}

    def test_list_issue_notes(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5/comments": [
            {"id": 51, "body": "一", "user": {"login": "a"}},
            {"id": 52, "body": "二", "user": {"login": "b"}},
        ]})
        provider = make_provider(mock)
        comments = provider.list_issue_notes("ckd/botler", 5)
        assert len(comments) == 2
        assert comments[1].body == "二"

    def test_add_labels(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5/labels": GITHUB_ISSUE["labels"]})
        provider = make_provider(mock)
        provider.add_labels("ckd/botler", 5, ["bug", "docs"])
        assert json.loads(mock.requests[0].content) == {"labels": ["bug", "docs"]}

    def test_add_labels_empty_noop(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        provider.add_labels("ckd/botler", 5, [])
        assert mock.requests == []


class TestGitHubPullRequests:
    def test_get_pull_request(self):
        mock = MockAPI({"/repos/ckd/botler/pulls/10": GITHUB_PR})
        provider = make_provider(mock)
        pr = provider.get_pull_request("ckd/botler", 10)
        assert pr.number == 10
        assert pr.state is PullRequestState.OPEN
        assert pr.source_branch == "feature/y"
        assert pr.target_branch == "main"

    def test_list_pull_requests_closed(self):
        mock = MockAPI({"/repos/ckd/botler/pulls": [GITHUB_PR]})
        provider = make_provider(mock)
        provider.list_pull_requests("ckd/botler", state=PullRequestState.CLOSED)
        # GitHub 无 merged 查询态：closed 语义用 state=closed
        assert mock.requests[0].url.params["state"] == "closed"

    def test_list_pull_requests_all_by_default(self):
        mock = MockAPI({"/repos/ckd/botler/pulls": [GITHUB_PR]})
        provider = make_provider(mock)
        provider.list_pull_requests("ckd/botler")
        # 不指定状态时不传 state 参数（GitHub 默认 all）
        assert "state" not in mock.requests[0].url.params

    def test_create_pull_request(self):
        mock = MockAPI({"/repos/ckd/botler/pulls": {**GITHUB_PR, "number": 11}})
        provider = make_provider(mock)
        pr = provider.create_pull_request(
            "ckd/botler", source_branch="feature/y", target_branch="main",
            title="标题", description="说明")
        assert pr.number == 11
        body = json.loads(mock.requests[0].content)
        assert body == {"title": "标题", "head": "feature/y",
                        "base": "main", "body": "说明"}

    def test_create_pull_request_empty_title(self):
        provider = make_provider(MockAPI({}))
        with pytest.raises(ProviderError) as exc:
            provider.create_pull_request("ckd/botler", source_branch="f",
                                        target_branch="main", title="")
        assert exc.value.status_code == 400

    def test_create_pull_request_missing_branch(self):
        provider = make_provider(MockAPI({}))
        with pytest.raises(ProviderError) as exc:
            provider.create_pull_request("ckd/botler", source_branch="",
                                        target_branch="main", title="t")
        assert exc.value.status_code == 400

    def test_merge_pull_request(self):
        merged = {**GITHUB_PR, "state": "closed", "merged": True,
                  "merged_at": "2026-01-01T00:00:00Z"}
        mock = MockAPI({
            "/repos/ckd/botler/pulls/10/merge": {"merged": True, "sha": "x"},
            "/repos/ckd/botler/pulls/10": merged,
        })
        provider = make_provider(mock)
        pr = provider.merge_pull_request("ckd/botler", 10)
        assert pr.state is PullRequestState.MERGED
        assert mock.requests[0].method == "PUT"
        # 合并后重新拉取最新状态
        assert mock.requests[1].method == "GET"


class TestGitHubPipelines:
    def test_get_latest_pipeline(self):
        mock = MockAPI({"/repos/ckd/botler/actions/runs":
                        {"total_count": 1, "workflow_runs": [GITHUB_RUN]}})
        provider = make_provider(mock)
        pipeline = provider.get_latest_pipeline("ckd/botler")
        assert pipeline is not None
        assert pipeline.id == 401
        assert pipeline.status is PipelineStatus.SUCCESS
        assert pipeline.ref == "main"
        assert mock.requests[0].url.params["per_page"] == "1"

    def test_get_latest_pipeline_none_when_empty(self):
        mock = MockAPI({"/repos/ckd/botler/actions/runs":
                        {"total_count": 0, "workflow_runs": []}})
        provider = make_provider(mock)
        assert provider.get_latest_pipeline("ckd/botler") is None

    def test_get_latest_pipeline_with_ref(self):
        mock = MockAPI({"/repos/ckd/botler/actions/runs":
                        {"workflow_runs": [GITHUB_RUN]}})
        provider = make_provider(mock)
        provider.get_latest_pipeline("ckd/botler", ref="main")
        assert mock.requests[0].url.params["branch"] == "main"

    def test_list_pipelines(self):
        mock = MockAPI({"/repos/ckd/botler/actions/runs":
                        {"workflow_runs": [GITHUB_RUN]}})
        provider = make_provider(mock)
        pipelines = provider.list_pipelines("ckd/botler", limit=5)
        assert len(pipelines) == 1
        assert mock.requests[0].url.params["per_page"] == "5"

    def test_list_pipeline_jobs(self):
        mock = MockAPI({"/repos/ckd/botler/actions/runs/401/jobs":
                        {"total_count": 1, "jobs": [GITHUB_JOB]}})
        provider = make_provider(mock)
        jobs = provider.list_pipeline_jobs("ckd/botler", 401)
        assert len(jobs) == 1
        assert jobs[0].name == "backend:test"
        assert jobs[0].status is PipelineStatus.SUCCESS


class TestGitHubWebhooks:
    def test_list_webhooks(self):
        mock = MockAPI({"/repos/ckd/botler/hooks": [GITHUB_HOOK]})
        provider = make_provider(mock)
        hooks = provider.list_webhooks("ckd/botler")
        assert len(hooks) == 1
        assert hooks[0].id == 601
        assert hooks[0].url == "https://app.example.com/webhook"
        assert hooks[0].events == ["issues", "pull_request"]

    def test_register_webhook(self):
        mock = MockAPI({"/repos/ckd/botler/hooks": GITHUB_HOOK})
        provider = make_provider(mock)
        hook = provider.register_webhook(
            "ckd/botler", "https://app.example.com/webhook",
            secret="s3cr3t", events=["issues"])
        assert hook.id == 601
        body = json.loads(mock.requests[0].content)
        assert body["name"] == "web"
        assert body["events"] == ["issues"]
        assert body["config"] == {"url": "https://app.example.com/webhook",
                                  "content_type": "json", "secret": "s3cr3t"}

    def test_unregister_webhook_204(self):
        # GitHub 注销成功返回 204 无内容
        mock = MockAPI(no_content_paths={"/repos/ckd/botler/hooks/601"})
        provider = make_provider(mock)
        provider.unregister_webhook("ckd/botler", 601)
        assert mock.requests[0].method == "DELETE"
        assert mock.requests[0].url.path == "/repos/ckd/botler/hooks/601"

    def test_unregister_webhook_404(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.unregister_webhook("ckd/botler", 999)
        assert exc.value.status_code == 404


class TestGitHubBoundaries:
    def test_malformed_json_returns_none(self):
        class Weird(MockAPI):
            def handler(self, request):
                self.requests.append(request)
                return httpx.Response(200, text="not-json")

        provider = make_provider(Weird())
        assert provider.get_latest_pipeline("ckd/botler") is None
