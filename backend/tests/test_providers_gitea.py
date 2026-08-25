"""GiteaProvider 适配器测试（issue #484）。

验证 Gitea 平台差异：API 前缀 /api/v1、鉴权头 token、标签名→id 解析、
commit status 聚合流水线、合并走 POST 等；其余 issue/PR/webhook 行为
与 GitHub 共用基类。
"""

import json

import httpx
import pytest

from botler.providers import (
    GiteaProvider,
    IssueState,
    PipelineStatus,
    ProviderError,
    PullRequestState,
)

GITEA_ISSUE = {
    "id": 11, "number": 5, "title": "gitea 标题", "body": "正文",
    "state": "open",
    "labels": [{"name": "bug"}, {"name": "enhancement"}],
    "user": {"login": "user1", "id": 1},
    "html_url": "https://gitea.example.com/ckd/botler/issues/5",
    "created_at": "2026-01-01T00:00:00Z",
}
GITEA_PR = {
    "id": 31, "number": 10, "title": "PR 标题", "state": "open",
    "head": {"ref": "feature/y"}, "base": {"ref": "main"},
    "user": {"login": "user1"},
    "html_url": "https://gitea.example.com/ckd/botler/pulls/10",
    "created_at": "2026-01-01T00:00:00Z",
}
GITEA_STATUS = {
    "id": 401, "state": "success", "sha": "abc123",
    "context": "ci/backend", "target_url": "https://gitea.example.com/status/401",
    "created_at": "2026-01-01T00:00:00Z",
}
GITEA_HOOK = {
    "id": 601, "events": ["issues", "pull_request"],
    "config": {"url": "https://app.example.com/webhook", "content_type": "json"},
}

API_PREFIX = "/api/v1"


class MockAPI:
    """按「去前缀后的路径」返回脚本数据的 MockTransport 处理器。"""

    def __init__(self, routes: dict | None = None, fail: int | None = None):
        self.routes = {self._lookup("/" + k.lstrip("/")): v
                       for k, v in (routes or {}).items()}
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def _lookup(self, path: str) -> str:
        """去掉 /api/v1 前缀，routes 键与业务路径一致。"""
        return path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(self.fail, text=f"error {self.fail}")
        data = self.routes.get(self._lookup(request.url.path))
        if data is None:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, json=data)


def make_provider(mock: MockAPI) -> GiteaProvider:
    provider = GiteaProvider("https://gitea.example.com", "gitea_token")
    provider._http = httpx.Client(
        transport=httpx.MockTransport(mock.handler),
        base_url="https://gitea.example.com/api/v1",
        headers=provider._build_headers("gitea_token"),
    )
    return provider


class TestGiteaCore:
    def test_platform_metadata(self):
        assert GiteaProvider.platform == "gitea"
        assert GiteaProvider.display_name == "Gitea"

    def test_connection_uses_token_auth(self):
        mock = MockAPI({"/user": {"id": 1, "login": "user1"}})
        provider = make_provider(mock)
        assert provider.test_connection() is True
        assert mock.requests[0].url.path == "/api/v1/user"
        assert mock.requests[0].headers["authorization"] == "token gitea_token"

    def test_resolve_project(self):
        mock = MockAPI({"/repos/ckd/botler": {
            "full_name": "ckd/botler", "default_branch": "master",
            "html_url": "https://gitea.example.com/ckd/botler"}})
        provider = make_provider(mock)
        project = provider.resolve_project("ckd/botler")
        assert project.id == "ckd/botler"
        assert project.default_branch == "master"
        # Gitea API 前缀正确拼入 URL
        assert mock.requests[0].url.path == "/api/v1/repos/ckd/botler"

    def test_connection_403(self):
        mock = MockAPI(fail=403)
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.test_connection()
        assert exc.value.status_code == 403
        assert exc.value.platform == "gitea"


class TestGiteaIssues:
    def test_get_issue(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5": GITEA_ISSUE})
        provider = make_provider(mock)
        issue = provider.get_issue("ckd/botler", 5)
        assert issue.iid == 5
        assert issue.state is IssueState.OPEN
        assert issue.labels == ["bug", "enhancement"]

    def test_get_issue_pr_rejected(self):
        mock = MockAPI({"/repos/ckd/botler/issues/10":
                        {**GITEA_PR, "pull_request": {"url": "x"}}})
        provider = make_provider(mock)
        with pytest.raises(ProviderError) as exc:
            provider.get_issue("ckd/botler", 10)
        assert exc.value.status_code == 404

    def test_list_open_issues(self):
        mock = MockAPI({"/repos/ckd/botler/issues": [GITEA_ISSUE]})
        provider = make_provider(mock)
        issues = provider.list_open_issues("ckd/botler")
        assert len(issues) == 1
        assert mock.requests[0].url.params["state"] == "open"
        assert mock.requests[0].url.params["type"] == "issues"

    def test_create_issue(self):
        mock = MockAPI({"/repos/ckd/botler/issues": {**GITEA_ISSUE, "number": 6}})
        provider = make_provider(mock)
        issue = provider.create_issue("ckd/botler", "新标题", "正文", ["feature"])
        assert issue.iid == 6
        body = json.loads(mock.requests[0].content)
        assert body == {"title": "新标题", "body": "正文", "labels": ["feature"]}

    def test_create_issue_empty_title(self):
        provider = make_provider(MockAPI({}))
        with pytest.raises(ProviderError) as exc:
            provider.create_issue("ckd/botler", "")
        assert exc.value.status_code == 400

    def test_update_issue_state(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5": {**GITEA_ISSUE, "state": "closed"}})
        provider = make_provider(mock)
        issue = provider.update_issue("ckd/botler", 5, state="closed")
        assert issue.state is IssueState.CLOSED
        assert json.loads(mock.requests[0].content) == {"state": "closed"}

    def test_list_issue_notes(self):
        mock = MockAPI({"/repos/ckd/botler/issues/5/comments": [
            {"id": 51, "body": "一", "user": {"login": "a"}},
        ]})
        provider = make_provider(mock)
        comments = provider.list_issue_notes("ckd/botler", 5)
        assert len(comments) == 1
        assert comments[0].body == "一"

    def test_add_labels_resolves_ids(self):
        mock = MockAPI({
            "/repos/ckd/botler/labels": [{"id": 1, "name": "bug"},
                                         {"id": 2, "name": "docs"}],
            "/repos/ckd/botler/issues/5/labels": [{"id": 1}],
        })
        provider = make_provider(mock)
        provider.add_labels("ckd/botler", 5, ["bug", "docs"])
        assert mock.requests[0].url.path == "/api/v1/repos/ckd/botler/labels"
        last = mock.requests[-1]
        assert last.url.path == "/api/v1/repos/ckd/botler/issues/5/labels"
        assert json.loads(last.content) == {"labels": [1, 2]}

    def test_add_labels_creates_missing(self):
        created_label = {"id": 9, "name": "new-label"}

        class LabelMock(MockAPI):
            def handler(self, request):
                self.requests.append(request)
                path = self._lookup(request.url.path)
                if request.method == "POST" and path == "/repos/ckd/botler/labels":
                    return httpx.Response(201, json=created_label)
                if path == "/repos/ckd/botler/labels":
                    return httpx.Response(200, json=[{"id": 1, "name": "bug"}])
                if path == "/repos/ckd/botler/issues/5/labels":
                    return httpx.Response(200, json=[{"id": 9}])
                return httpx.Response(404)

        mock = LabelMock()
        provider = make_provider(mock)
        provider.add_labels("ckd/botler", 5, ["bug", "new-label"])
        post_bodies = [
            json.loads(r.content) for r in mock.requests
            if r.method == "POST"
            and r.url.path == "/api/v1/repos/ckd/botler/labels"
        ]
        assert post_bodies == [{"name": "new-label", "color": "#6699cc"}]
        final = [r for r in mock.requests
                 if r.url.path.endswith("/issues/5/labels")]
        # bug → id 1（已存在）、new-label → 创建后 id 9
        assert json.loads(final[0].content) == {"labels": [1, 9]}

    def test_add_labels_empty_noop(self):
        mock = MockAPI({})
        provider = make_provider(mock)
        provider.add_labels("ckd/botler", 5, [])
        assert mock.requests == []


class TestGiteaPullRequests:
    def test_get_pull_request(self):
        mock = MockAPI({"/repos/ckd/botler/pulls/10": GITEA_PR})
        provider = make_provider(mock)
        pr = provider.get_pull_request("ckd/botler", 10)
        assert pr.number == 10
        assert pr.state is PullRequestState.OPEN
        assert pr.source_branch == "feature/y"

    def test_list_pull_requests_closed(self):
        mock = MockAPI({"/repos/ckd/botler/pulls": [GITEA_PR]})
        provider = make_provider(mock)
        provider.list_pull_requests("ckd/botler", state=PullRequestState.CLOSED)
        assert mock.requests[0].url.params["state"] == "closed"

    def test_create_pull_request(self):
        mock = MockAPI({"/repos/ckd/botler/pulls": {**GITEA_PR, "number": 11}})
        provider = make_provider(mock)
        pr = provider.create_pull_request(
            "ckd/botler", source_branch="feature/y", target_branch="main",
            title="标题")
        assert pr.number == 11
        body = json.loads(mock.requests[0].content)
        assert body == {"title": "标题", "head": "feature/y", "base": "main"}

    def test_merge_pull_request_uses_post_then_refetch(self):
        merged = {**GITEA_PR, "state": "merged",
                  "merged_at": "2026-01-01T00:00:00Z"}
        mock = MockAPI({"/repos/ckd/botler/pulls/10": merged})
        provider = make_provider(mock)

        class MergeMock(MockAPI):
            def handler(self, request):
                self.requests.append(request)
                path = self._lookup(request.url.path)
                if path == "/repos/ckd/botler/pulls/10/merge":
                    return httpx.Response(204)
                if path == "/repos/ckd/botler/pulls/10":
                    return httpx.Response(200, json=merged)
                return httpx.Response(404)

        mock = MergeMock()
        provider = make_provider(mock)
        pr = provider.merge_pull_request("ckd/botler", 10)
        assert pr.number == 10
        assert mock.requests[0].method == "POST"
        assert mock.requests[0].url.path == "/api/v1/repos/ckd/botler/pulls/10/merge"
        assert mock.requests[1].url.path == "/api/v1/repos/ckd/botler/pulls/10"


class TestGiteaPipelines:
    def test_get_latest_pipeline_via_commit_status(self):
        mock = MockAPI({
            "/repos/ckd/botler": {"full_name": "ckd/botler",
                                  "default_branch": "main"},
            "/repos/ckd/botler/commits/main/statuses": [GITEA_STATUS],
        })
        provider = make_provider(mock)
        pipeline = provider.get_latest_pipeline("ckd/botler")
        assert pipeline is not None
        assert pipeline.id == 401
        assert pipeline.status is PipelineStatus.SUCCESS
        assert pipeline.sha == "abc123"
        assert pipeline.web_url == "https://gitea.example.com/status/401"

    def test_get_latest_pipeline_with_explicit_ref(self):
        mock = MockAPI({"/repos/ckd/botler/commits/dev/statuses": [GITEA_STATUS]})
        provider = make_provider(mock)
        pipeline = provider.get_latest_pipeline("ckd/botler", ref="dev")
        assert pipeline is not None
        assert mock.requests[0].url.path == "/api/v1/repos/ckd/botler/commits/dev/statuses"

    def test_get_latest_pipeline_none_when_no_status(self):
        mock = MockAPI({
            "/repos/ckd/botler": {"full_name": "ckd/botler", "default_branch": "main"},
            "/repos/ckd/botler/commits/main/statuses": [],
        })
        provider = make_provider(mock)
        assert provider.get_latest_pipeline("ckd/botler") is None

    def test_list_pipeline_jobs_empty(self):
        provider = make_provider(MockAPI({}))
        assert provider.list_pipeline_jobs("ckd/botler", 401) == []


class TestGiteaWebhooks:
    def test_register_webhook(self):
        mock = MockAPI({"/repos/ckd/botler/hooks": GITEA_HOOK})
        provider = make_provider(mock)
        hook = provider.register_webhook("ckd/botler", "https://app.example.com/w")
        assert hook.id == 601
        body = json.loads(mock.requests[0].content)
        assert body["type"] == "gitea"
        assert body["config"]["url"] == "https://app.example.com/w"

    def test_unregister_webhook(self):
        class DelMock(MockAPI):
            def handler(self, request):
                self.requests.append(request)
                return httpx.Response(204)

        mock = DelMock()
        provider = make_provider(mock)
        provider.unregister_webhook("ckd/botler", 601)
        assert mock.requests[0].method == "DELETE"
        assert mock.requests[0].url.path == "/api/v1/repos/ckd/botler/hooks/601"
