"""Provider 适配层通用领域模型测试（issue #484）。

覆盖：跨平台 payload → 通用模型的映射（GitLab / GitHub / Gitea）、
状态归一（Issue / PullRequest / Pipeline）、边界输入（缺失字段、
None、空串、未知状态）与 ChangeRequest 别名语义。
"""

from botler.providers.domain import (
    ChangeRequest,
    Comment,
    Issue,
    IssueComment,
    IssueState,
    PipelineStatus,
    PLATFORM_GITEA,
    PLATFORM_GITHUB,
    PLATFORM_GITLAB,
    PLATFORM_LOCAL_DEMO,
    Project,
    PullRequest,
    PullRequestState,
    SUPPORTED_PLATFORMS,
    map_gitea_status_state,
    map_github_job_status,
    map_github_pipeline_status,
    map_gitlab_pipeline_status,
)


# ---- 平台标识 ----

class TestPlatformConstants:
    def test_supported_platforms_contains_four(self):
        assert SUPPORTED_PLATFORMS == (
            PLATFORM_GITLAB, PLATFORM_GITHUB, PLATFORM_GITEA, PLATFORM_LOCAL_DEMO)

    def test_platform_values_lowercase(self):
        for p in SUPPORTED_PLATFORMS:
            assert p == p.lower()
            assert p.strip()


# ---- Project 映射 ----

class TestProjectMapping:
    def test_from_gitlab(self):
        proj = Project.from_gitlab({
            "id": 42, "name": "botler", "path_with_namespace": "ckd/botler",
            "web_url": "https://gitlab.example.com/ckd/botler",
            "default_branch": "main",
        })
        assert proj.id == "42"
        assert proj.path == "ckd/botler"
        assert proj.web_url.startswith("https://")
        assert proj.default_branch == "main"
        assert proj.raw["id"] == 42

    def test_from_gitlab_missing_fields_defaults(self):
        proj = Project.from_gitlab({})
        assert proj.id == ""
        assert proj.name == ""
        assert proj.default_branch == ""
        assert proj.raw == {}

    def test_from_github_like(self):
        proj = Project.from_github_like({
            "name": "botler", "full_name": "ckd/botler",
            "html_url": "https://github.com/ckd/botler",
            "default_branch": "master",
        }, "ckd/botler")
        assert proj.id == "ckd/botler"
        assert proj.path == "ckd/botler"
        assert proj.default_branch == "master"


# ---- Issue 映射 ----

class TestIssueMapping:
    def test_from_gitlab_open(self):
        issue = Issue.from_gitlab({
            "id": 1, "iid": 7, "title": "标题", "description": "正文",
            "state": "opened", "labels": "feature,bug",
            "author": {"id": 3, "username": "agent", "name": "agent"},
            "assignees": [{"id": 4, "username": "user1"}],
            "web_url": "https://gitlab.example.com/-/issues/7",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        })
        assert issue.iid == 7
        assert issue.state is IssueState.OPEN
        assert issue.labels == ["feature", "bug"]
        assert issue.author is not None and issue.author.username == "agent"
        assert len(issue.assignees) == 1

    def test_from_gitlab_closed_and_missing(self):
        issue = Issue.from_gitlab({"state": "closed", "iid": "3"})
        assert issue.state is IssueState.CLOSED
        assert issue.iid == 3
        assert issue.labels == []
        assert issue.author is None

    def test_from_gitlab_unknown_state_defaults_open(self):
        issue = Issue.from_gitlab({"state": "weird", "iid": 1})
        assert issue.state is IssueState.OPEN

    def test_from_github_open_labels_objects(self):
        issue = Issue.from_github({
            "id": 11, "number": 5, "title": "gh 标题", "body": "gh 正文",
            "state": "open",
            "labels": [{"name": "bug"}, {"name": "enhancement"}],
            "user": {"login": "octocat", "id": 1},
            "html_url": "https://github.com/ckd/botler/issues/5",
            "created_at": "2026-01-01T00:00:00Z",
        })
        assert issue.iid == 5
        assert issue.state is IssueState.OPEN
        assert issue.labels == ["bug", "enhancement"]
        assert issue.author is not None and issue.author.username == "octocat"

    def test_from_github_closed(self):
        issue = Issue.from_github({"number": 9, "state": "closed"})
        assert issue.state is IssueState.CLOSED

    def test_from_github_empty_labels_and_null_user(self):
        issue = Issue.from_github({"number": 1, "state": "open",
                                   "labels": None, "user": None})
        assert issue.labels == []
        assert issue.author is None

    def test_from_gitea_same_as_github_shape(self):
        issue = Issue.from_gitea({"number": 2, "state": "closed",
                                  "labels": [{"name": "docs"}]})
        assert issue.iid == 2
        assert issue.state is IssueState.CLOSED
        assert issue.labels == ["docs"]


# ---- PullRequest / ChangeRequest 映射 ----

class TestPullRequestMapping:
    def test_from_gitlab_opened(self):
        pr = PullRequest.from_gitlab({
            "id": 21, "iid": 8, "title": "MR 标题", "state": "opened",
            "source_branch": "feature/x", "target_branch": "main",
            "author": {"id": 3, "username": "agent"},
            "web_url": "https://gitlab.example.com/-/merge_requests/8",
        })
        assert pr.number == 8
        assert pr.state is PullRequestState.OPEN
        assert pr.source_branch == "feature/x"
        assert pr.target_branch == "main"
        assert not pr.merged

    def test_from_gitlab_merged(self):
        pr = PullRequest.from_gitlab({"state": "merged", "iid": 2})
        assert pr.state is PullRequestState.MERGED
        assert pr.merged

    def test_from_gitlab_closed_with_merged_at(self):
        pr = PullRequest.from_gitlab({"state": "closed", "iid": 3,
                                      "merged_at": "2026-01-01T00:00:00Z"})
        assert pr.state is PullRequestState.CLOSED
        assert pr.merged  # merged_at 存在即视为已合并

    def test_from_github_open(self):
        pr = PullRequest.from_github({
            "id": 31, "number": 10, "title": "PR 标题", "state": "open",
            "head": {"ref": "feature/y"}, "base": {"ref": "main"},
            "user": {"login": "octocat"},
            "html_url": "https://github.com/ckd/botler/pull/10",
        })
        assert pr.number == 10
        assert pr.state is PullRequestState.OPEN
        assert pr.source_branch == "feature/y"
        assert not pr.merged

    def test_from_github_merged_by_flag(self):
        pr = PullRequest.from_github({"number": 11, "state": "closed",
                                      "merged": True})
        assert pr.state is PullRequestState.MERGED
        assert pr.merged

    def test_from_github_merged_by_merged_at(self):
        pr = PullRequest.from_github({"number": 12, "state": "closed",
                                      "merged_at": "2026-01-01T00:00:00Z"})
        assert pr.state is PullRequestState.MERGED

    def test_from_gitea_merged_state_direct(self):
        # Gitea 的 state 可直接为 merged（GitHub 不会）
        pr = PullRequest.from_gitea({"number": 13, "state": "merged"})
        assert pr.state is PullRequestState.MERGED
        assert pr.merged

    def test_change_request_is_pull_request_alias(self):
        # issue #484：领域层同时提供 PullRequest / ChangeRequest 两个名称
        assert ChangeRequest is PullRequest
        pr = PullRequest.from_gitlab({"state": "opened", "iid": 1})
        assert isinstance(pr, ChangeRequest)


# ---- Comment 映射 ----

class TestCommentMapping:
    def test_from_gitlab(self):
        comment = IssueComment.from_gitlab({
            "id": 51, "body": "评论内容", "system": False,
            "author": {"id": 3, "username": "agent"},
            "created_at": "2026-01-01T00:00:00Z",
        })
        assert comment.id == 51
        assert comment.body == "评论内容"
        assert not comment.system
        assert comment.author is not None

    def test_from_gitlab_system_note(self):
        comment = IssueComment.from_gitlab({"id": 52, "body": "assigned",
                                            "system": True})
        assert comment.system

    def test_from_github(self):
        comment = IssueComment.from_github({
            "id": 53, "body": "gh 评论", "user": {"login": "octocat"},
        })
        assert comment.body == "gh 评论"
        assert comment.author is not None and comment.author.username == "octocat"
        assert not comment.system

    def test_comment_alias(self):
        assert Comment is IssueComment


# ---- 流水线状态映射 ----

class TestPipelineStatusMapping:
    def test_gitlab_all_states(self):
        assert map_gitlab_pipeline_status("success") is PipelineStatus.SUCCESS
        assert map_gitlab_pipeline_status("failed") is PipelineStatus.FAILED
        assert map_gitlab_pipeline_status("running") is PipelineStatus.RUNNING
        assert map_gitlab_pipeline_status("pending") is PipelineStatus.PENDING
        assert map_gitlab_pipeline_status("canceled") is PipelineStatus.CANCELED
        assert map_gitlab_pipeline_status("skipped") is PipelineStatus.SKIPPED

    def test_gitlab_unknown_status_defaults_unknown(self):
        assert map_gitlab_pipeline_status("totally-new-state") is PipelineStatus.UNKNOWN
        assert map_gitlab_pipeline_status(None) is PipelineStatus.UNKNOWN
        assert map_gitlab_pipeline_status("") is PipelineStatus.UNKNOWN

    def test_github_status_and_conclusion(self):
        assert map_github_pipeline_status("queued") is PipelineStatus.PENDING
        assert map_github_pipeline_status("in_progress") is PipelineStatus.RUNNING
        assert map_github_pipeline_status("success") is PipelineStatus.SUCCESS
        assert map_github_pipeline_status("failure") is PipelineStatus.FAILED
        assert map_github_pipeline_status("cancelled") is PipelineStatus.CANCELED
        assert map_github_pipeline_status("skipped") is PipelineStatus.SKIPPED

    def test_github_job_status(self):
        assert map_github_job_status("in_progress") is PipelineStatus.RUNNING
        assert map_github_job_status("queued") is PipelineStatus.PENDING
        assert map_github_job_status("success") is PipelineStatus.SUCCESS
        assert map_github_job_status("cancelled") is PipelineStatus.CANCELED
        assert map_github_job_status("weird") is PipelineStatus.UNKNOWN

    def test_gitea_status_state(self):
        assert map_gitea_status_state("success") is PipelineStatus.SUCCESS
        assert map_gitea_status_state("failure") is PipelineStatus.FAILED
        assert map_gitea_status_state("error") is PipelineStatus.FAILED
        assert map_gitea_status_state("pending") is PipelineStatus.PENDING
        assert map_gitea_status_state("nope") is PipelineStatus.UNKNOWN


# ---- 极端输入防御 ----

class TestMappingBoundaries:
    def test_from_github_non_dict_payload_guarded(self):
        # 映射函数只接受 dict，非 dict 属于调用方错误；这里验证健壮字段兜底
        issue = Issue.from_github({"number": None, "state": None, "labels": 123})
        assert issue.iid == 0
        assert issue.labels == []

    def test_gitlab_labels_commas_with_spaces(self):
        issue = Issue.from_gitlab({"labels": "feature, bug , docs"})
        assert issue.labels == ["feature", "bug", "docs"]

    def test_gitlab_mixed_labels_list(self):
        # 平台返回非标准结构时容忍并跳过
        issue = Issue.from_gitlab({"labels": ["a", {"name": "b"}, 3, None]})
        assert issue.labels == ["a", "b"]

    def test_iid_numeric_string(self):
        issue = Issue.from_gitlab({"iid": "42"})
        assert issue.iid == 42
        pr = PullRequest.from_gitlab({"iid": "99"})
        assert pr.number == 99
