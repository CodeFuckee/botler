"""LocalDemoProvider 测试（issue #484）：内存演示平台。

覆盖：播种数据、Issue/PR/流水线/Webhook 全量 CRUD、边界输入
（空标题、未知 id、非法状态）、幂等（重复合并/重复标签）、并发创建。
"""

import threading

import pytest

from botler.providers import (
    IssueState,
    LocalDemoProvider,
    PipelineStatus,
    ProviderError,
    PullRequestState,
)


@pytest.fixture
def demo() -> LocalDemoProvider:
    return LocalDemoProvider()  # 默认播种


class TestLocalDemoCore:
    def test_platform_metadata(self):
        assert LocalDemoProvider.platform == "local_demo"
        assert LocalDemoProvider.display_name == "本地演示（LocalDemo）"

    def test_connection_always_ok(self):
        assert LocalDemoProvider().test_connection() is True

    def test_resolve_project_by_key(self):
        demo = LocalDemoProvider()
        project = demo.resolve_project("demo")
        assert project.id == "demo"
        assert project.path == "demo/demo-project"
        assert project.default_branch == "main"

    def test_resolve_project_by_path(self):
        project = LocalDemoProvider().resolve_project("demo/demo-project")
        assert project.id == "demo"

    def test_resolve_project_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().resolve_project("nope")
        assert exc.value.status_code == 404

    def test_reset_reseeds(self):
        demo = LocalDemoProvider()
        demo.create_issue("demo", "新增")
        demo.reset()
        assert demo.list_open_issues("demo")[0].iid == 1  # 回到播种状态

    def test_new_instance_is_isolated(self):
        a, b = LocalDemoProvider(), LocalDemoProvider()
        a.create_issue("demo", "只在 a 中")
        assert len(a.list_open_issues("demo")) == 2
        assert len(b.list_open_issues("demo")) == 1


class TestLocalDemoIssues:
    def test_seeded_open_issue(self):
        issues = LocalDemoProvider().list_open_issues("demo")
        assert [i.iid for i in issues] == [1]
        assert issues[0].title == "示例 Issue：演示本地 Provider"
        assert issues[0].labels == ["feature", "demo"]

    def test_get_issue(self):
        issue = LocalDemoProvider().get_issue("demo", 1)
        assert issue.iid == 1
        assert issue.state is IssueState.OPEN

    def test_get_issue_closed_seed(self):
        issue = LocalDemoProvider().get_issue("demo", 2)
        assert issue.state is IssueState.CLOSED

    def test_get_issue_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().get_issue("demo", 999)
        assert exc.value.status_code == 404

    def test_create_issue(self):
        demo = LocalDemoProvider()
        issue = demo.create_issue("demo", "新功能", "描述", ["feature"])
        assert issue.iid == 3
        assert issue.title == "新功能"
        assert issue.labels == ["feature"]
        # 创建后 open issues 计数 +1
        assert len(demo.list_open_issues("demo")) == 2

    def test_create_issue_blank_title_rejected(self):
        demo = LocalDemoProvider()
        for bad in ("", "   ", None):
            with pytest.raises(ProviderError) as exc:
                demo.create_issue("demo", bad)  # type: ignore[arg-type]
            assert exc.value.status_code == 400

    def test_create_issue_duplicate_labels_deduped(self):
        issue = LocalDemoProvider().create_issue(
            "demo", "标题", labels=["feature", "feature", "docs"])
        assert issue.labels == ["feature", "docs"]

    def test_update_issue_fields(self):
        demo = LocalDemoProvider()
        issue = demo.update_issue("demo", 1, title="新标题", description="新正文",
                                  state="closed")
        assert issue.title == "新标题"
        assert issue.description == "新正文"
        assert issue.state is IssueState.CLOSED
        assert demo.get_issue("demo", 1).state is IssueState.CLOSED

    def test_update_issue_reopen(self):
        demo = LocalDemoProvider()
        demo.update_issue("demo", 2, state="open")
        assert demo.get_issue("demo", 2).state is IssueState.OPEN

    def test_update_issue_invalid_state(self):
        demo = LocalDemoProvider()
        with pytest.raises(ProviderError) as exc:
            demo.update_issue("demo", 1, state="half")
        assert exc.value.status_code == 400

    def test_update_issue_blank_title_rejected(self):
        demo = LocalDemoProvider()
        with pytest.raises(ProviderError) as exc:
            demo.update_issue("demo", 1, title="  ")
        assert exc.value.status_code == 400

    def test_update_issue_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().update_issue("demo", 999, title="x")
        assert exc.value.status_code == 404

    def test_add_comment_and_list(self):
        demo = LocalDemoProvider()
        comment = demo.add_comment("demo", 1, "新评论")
        assert comment.body == "新评论"
        notes = demo.list_issue_notes("demo", 1)
        assert len(notes) == 2  # 播种 1 条 + 新增 1 条
        assert notes[-1].body == "新评论"

    def test_add_comment_on_unknown_issue_404(self):
        demo = LocalDemoProvider()
        with pytest.raises(ProviderError) as exc:
            demo.add_comment("demo", 999, "x")
        assert exc.value.status_code == 404

    def test_list_issue_notes_limit_takes_latest(self):
        demo = LocalDemoProvider()
        for i in range(3):
            demo.add_comment("demo", 1, f"评论{i}")
        notes = demo.list_issue_notes("demo", 1, limit=2)
        assert len(notes) == 2
        assert notes[-1].body == "评论2"

    def test_add_labels_dedupe(self):
        demo = LocalDemoProvider()
        demo.add_labels("demo", 1, ["feature", "new-tag"])
        demo.add_labels("demo", 1, ["new-tag", "again"])
        assert demo.get_issue("demo", 1).labels == ["feature", "demo",
                                                    "new-tag", "again"]

    def test_add_labels_empty_noop(self):
        demo = LocalDemoProvider()
        demo.add_labels("demo", 1, [])
        assert demo.get_issue("demo", 1).labels == ["feature", "demo"]


class TestLocalDemoPullRequests:
    def test_seeded_prs(self):
        demo = LocalDemoProvider()
        prs = demo.list_pull_requests("demo")
        states = {p.number: p.state for p in prs}
        assert states[1] is PullRequestState.OPEN
        assert states[2] is PullRequestState.MERGED

    def test_get_pull_request(self):
        pr = LocalDemoProvider().get_pull_request("demo", 1)
        assert pr.source_branch == "feature/demo"
        assert pr.target_branch == "main"

    def test_get_pull_request_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().get_pull_request("demo", 999)
        assert exc.value.status_code == 404

    def test_list_pull_requests_open_filter(self):
        prs = LocalDemoProvider().list_pull_requests(
            "demo", state=PullRequestState.OPEN)
        assert [p.number for p in prs] == [1]

    def test_list_pull_requests_closed_includes_merged(self):
        prs = LocalDemoProvider().list_pull_requests(
            "demo", state=PullRequestState.CLOSED)
        assert [p.number for p in prs] == [2]

    def test_create_pull_request(self):
        demo = LocalDemoProvider()
        pr = demo.create_pull_request(
            "demo", source_branch="feature/new", target_branch="main",
            title="新 PR", description="说明")
        assert pr.number == 3
        assert pr.state is PullRequestState.OPEN
        assert demo.get_pull_request("demo", 3).title == "新 PR"

    def test_create_pull_request_boundaries(self):
        demo = LocalDemoProvider()
        with pytest.raises(ProviderError) as exc:
            demo.create_pull_request("demo", source_branch="f",
                                    target_branch="main", title="")
        assert exc.value.status_code == 400
        with pytest.raises(ProviderError) as exc:
            demo.create_pull_request("demo", source_branch="",
                                    target_branch="main", title="t")
        assert exc.value.status_code == 400

    def test_merge_pull_request(self):
        demo = LocalDemoProvider()
        pr = demo.merge_pull_request("demo", 1)
        assert pr.state is PullRequestState.MERGED
        assert demo.get_pull_request("demo", 1).merged

    def test_merge_pull_request_idempotent(self):
        demo = LocalDemoProvider()
        demo.merge_pull_request("demo", 2)
        pr = demo.merge_pull_request("demo", 2)  # 已合并再次合并
        assert pr.state is PullRequestState.MERGED

    def test_merge_pull_request_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().merge_pull_request("demo", 999)
        assert exc.value.status_code == 404


class TestLocalDemoPipelines:
    def test_get_latest_pipeline(self):
        pipeline = LocalDemoProvider().get_latest_pipeline("demo")
        assert pipeline is not None
        assert pipeline.status is PipelineStatus.FAILED  # 播种最新一条为 failed
        assert pipeline.id == 302

    def test_get_latest_pipeline_by_ref(self):
        pipeline = LocalDemoProvider().get_latest_pipeline("demo", ref="main")
        assert pipeline is not None and pipeline.id == 302
        pipeline = LocalDemoProvider().get_latest_pipeline("demo", ref="nope")
        assert pipeline is None

    def test_list_pipelines(self):
        pipelines = LocalDemoProvider().list_pipelines("demo")
        assert [p.id for p in pipelines] == [302, 301]
        assert pipelines[0].status is PipelineStatus.FAILED
        assert pipelines[1].status is PipelineStatus.SUCCESS

    def test_list_pipeline_jobs(self):
        demo = LocalDemoProvider()
        jobs = demo.list_pipeline_jobs("demo", 301)
        assert len(jobs) == 2
        assert jobs[0].name == "backend:test"
        assert jobs[0].status is PipelineStatus.SUCCESS

    def test_list_pipeline_jobs_unknown_pipeline_empty(self):
        assert LocalDemoProvider().list_pipeline_jobs("demo", 999) == []


class TestLocalDemoWebhooks:
    def test_seeded_webhook(self):
        hooks = LocalDemoProvider().list_webhooks("demo")
        assert len(hooks) == 1
        assert hooks[0].id == 1

    def test_register_webhook(self):
        demo = LocalDemoProvider()
        hook = demo.register_webhook("demo", "https://app.example.com/w",
                                     secret="s", events=["issues"])
        assert hook.id == 2
        hooks = demo.list_webhooks("demo")
        assert len(hooks) == 2

    def test_unregister_webhook(self):
        demo = LocalDemoProvider()
        demo.unregister_webhook("demo", 1)
        assert demo.list_webhooks("demo") == []

    def test_unregister_webhook_unknown_404(self):
        with pytest.raises(ProviderError) as exc:
            LocalDemoProvider().unregister_webhook("demo", 999)
        assert exc.value.status_code == 404


class TestLocalDemoConcurrency:
    def test_concurrent_issue_creation_no_duplicate_iid(self):
        demo = LocalDemoProvider()
        errors: list[Exception] = []
        results: list[int] = []

        def worker() -> None:
            try:
                issue = demo.create_issue("demo", "并发任务")
                results.append(issue.iid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 20
        assert len(set(results)) == 20  # iid 唯一（锁保护计数器）
        assert demo.get_issue("demo", max(results)) is not None

    def test_concurrent_comments(self):
        demo = LocalDemoProvider()
        threads = [threading.Thread(
            target=lambda: demo.add_comment("demo", 1, "并发评论"))
            for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(demo.list_issue_notes("demo", 1)) == 11  # 播种 1 + 10
