"""Webhook 入队过滤单元测试（issue #30）。

agent 只处理「没有 bot-done / bot-failed 标签且未关闭」的 issue。
webhook 收到 assignee / open 事件时，若 issue 已打 bot-done（完成待用户确认）
或 bot-failed（失败待人工介入），不得创建任务——否则用户重新指派后会把已完成/
已失败的 issue 重复入队。事件快照的 assignee/labels 可能不可靠，一律以 API
最新状态为准。
"""

from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError
from botler.webhook import WebhookHandler

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""

BOT_ID = 99
PROJECT_ID = 42
IID = 7


class StubGitLab:
    """webhook 用的 GitLab 桩：get_issue 返回可配置的最新 issue 状态。"""

    def __init__(self):
        self.current_issue: dict | None = None
        self.issue_calls = 0
        self.last_note_author: int | None = None

    def get_bot_id(self):
        return BOT_ID

    def get_issue(self, project_id, iid):
        self.issue_calls += 1
        if self.current_issue is None:
            raise GitLabError("模拟 GitLab API 故障")
        return self.current_issue

    def last_note_author_id(self, project_id, iid):
        """最后一条非系统评论的作者 id；None 表示无发言。"""
        return self.last_note_author


def make_event(action: str = "open", labels: list[str] | None = None) -> dict:
    """构造 GitLab issue webhook 事件体（快照中 assignee 含 bot、标签任意）。"""
    return {
        "object_kind": "issue",
        "project": {"id": PROJECT_ID},
        "object_attributes": {"action": action, "iid": IID, "title": "测试 issue"},
        "issue": {
            "title": "测试 issue",
            "assignees": [{"id": BOT_ID, "username": "agent"}],
            "labels": labels or [],
        },
    }


def make_api_issue(labels: list[str] | None = None,
                   assignees: list[dict] | None = None) -> dict:
    return {
        "iid": IID,
        "title": "测试 issue",
        "state": "opened",
        "labels": labels or [],
        "assignees": assignees or [{"id": BOT_ID, "username": "agent"}],
    }


@pytest.fixture
def ctx(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    handler = WebhookHandler(config, db, stub, scheduler)
    return SimpleNamespace(config=config, db=db, gitlab=stub, handler=handler)


def _add_repo(db, project_id=PROJECT_ID, name="demo", enabled=True) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled)


class TestWebhookSkipsTerminalLabeledIssues:
    def test_rejects_bot_done_issue(self, ctx):
        """已打 bot-done：webhook 事件拒绝入队（完成待用户确认，勿重复处理）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "bot-done"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot-done" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_rejects_bot_failed_issue(self, ctx):
        """已打 bot-failed：webhook 事件拒绝入队（失败待人工介入，避免无限重试）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "bot-failed"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot-failed" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_accepts_clean_issue(self, ctx):
        """回归：不带终态标签的 issue 照常入队（原有行为不变）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert result["task_id"] is not None
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task is not None
        assert task["status"] == "queued"
        assert task["triggered_by"] == "webhook"

    def test_snapshot_labels_ignored_api_wins(self, ctx):
        """事件快照无标签但 API 带 bot-done：以 API 为准拒绝（快照不可靠）。"""
        repo_id = _add_repo(ctx.db)
        # 快照里是干净标签，API 最新状态已打 bot-done
        ctx.gitlab.current_issue = make_api_issue(labels=["bot-done"])

        result = ctx.handler.handle(make_event(labels=["bug"]), "test-secret")

        assert result["accepted"] is False
        assert ctx.db.count_tasks() == 0


class TestWebhookSkipsWhenBotLastSpoke:
    """issue #34：最后一个发言人（非系统评论）是 bot 时不重复领取。

    bot 提问/处理完留评论后，若用户仅重新指派（无新回复）触发 webhook，
    最后发言仍是 bot——此时不应入队，等用户回复后再领。
    """

    def test_rejects_when_bot_last_spoke(self, ctx):
        """最后一条非系统评论是 bot：拒绝入队（等用户回复）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = BOT_ID

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "发言人" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_accepts_when_user_last_spoke(self, ctx):
        """最后一条非系统评论是用户（有新指示）：照常入队。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = 1  # 用户 id，非 bot

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.db.find_active_task(PROJECT_ID, IID) is not None

    def test_accepts_when_no_notes(self, ctx):
        """无任何非系统评论（新任务，仅系统事件）：照常入队。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = None

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.db.find_active_task(PROJECT_ID, IID) is not None
