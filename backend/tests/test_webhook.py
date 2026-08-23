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
import botler.webhook as webhook_mod

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
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "bot-done"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot-done" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_rejects_bot_failed_issue(self, ctx):
        """已打 bot-failed：webhook 事件拒绝入队（失败待人工介入，避免无限重试）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "bot-failed"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot-failed" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_accepts_clean_issue(self, ctx):
        """回归：不带终态标签的 issue 照常入队（原有行为不变）。"""
        _add_repo(ctx.db)
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
        _add_repo(ctx.db)
        # 快照里是干净标签，API 最新状态已打 bot-done
        ctx.gitlab.current_issue = make_api_issue(labels=["bot-done"])

        result = ctx.handler.handle(make_event(labels=["bug"]), "test-secret")

        assert result["accepted"] is False
        assert ctx.db.count_tasks() == 0


class TestWebhookSkipsNeedVerifyIssues:
    """issue #41：带 need-verify 标签（用户标记需人工验证）的 issue 不领取。

    与终态标签（bot-done/bot-failed）同理，webhook 收到 assignee / open
    事件时若 issue 已打 need-verify，不得创建任务——用户已明确该 issue
    需要人工验证，bot 不应领取处理。
    """

    def test_rejects_need_verify_issue(self, ctx):
        """已打 need-verify：webhook 事件拒绝入队（需人工验证，bot 不领取）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "need-verify"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "need-verify" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_snapshot_clean_but_api_need_verify(self, ctx):
        """事件快照无 need-verify 但 API 已带：以 API 为准拒绝（快照不可靠）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["need-verify"])

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
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = BOT_ID

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "发言人" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_accepts_when_user_last_spoke(self, ctx):
        """最后一条非系统评论是用户（有新指示）：照常入队。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = 1  # 用户 id，非 bot

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.db.find_active_task(PROJECT_ID, IID) is not None

    def test_accepts_when_no_notes(self, ctx):
        """无任何非系统评论（新任务，仅系统事件）：照常入队。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.gitlab.last_note_author = None

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.db.find_active_task(PROJECT_ID, IID) is not None


class TestWebhookBotIdentityFallback:
    """issue #65：全局 token 失效时 webhook 的 bot 身份判定降级。

    全局 token 失效（get_bot_id 抛 401）时 webhook 不应直接 500，而是
    改用仓库 remote 身份（remote token 账号 + remote URL 用户名对应账号）
    判定 assignee——分配给 @agent 的 issue 照常入队，与对账修复对齐。
    """

    @staticmethod
    def _add_local_repo(db, tmp_path, project_id=PROJECT_ID, name="demo") -> int:
        import subprocess
        repo_dir = tmp_path / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)],
                       check=True)
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin",
                        "https://agent:glpat-repo@gitlab.example.com/group/repo.git"],
                       check=True)
        return db.upsert_repo(
            project_id=project_id, name=name,
            url=f"https://gitlab.example.com/{name}.git",
            local_path=str(repo_dir), remote_name="origin")

    @staticmethod
    def _fail_global_bot_id(ctx) -> None:
        def fail_bot_id(force=False):
            raise GitLabError("token 无效或已过期（401）", 401)
        ctx.gitlab.get_bot_id = fail_bot_id

    class _FallbackStub:
        """remote token 兜底客户端桩。"""

        def __init__(self, bot_id: int = 11,
                     username_ids: dict[str, int] | None = None):
            self.bot_id = bot_id
            self.username_ids = username_ids or {}

        def get_bot_id(self):
            return self.bot_id

        def get_user_id_by_username(self, username):
            return self.username_ids.get(username)

    def test_accepts_with_remote_identity_when_global_fails(
            self, ctx, tmp_path, monkeypatch):
        """全局 token 失效、issue 分配给 @agent（remote URL 用户名对应
        id=3）：webhook 应接受入队。

        修复前：get_bot_id 抛 401 未捕获，webhook 直接 500，
        issue 只能等对账兜底（且对账身份漂移扫不到 → 完全漏任务）。
        """
        self._add_local_repo(ctx.db, tmp_path)
        self._fail_global_bot_id(ctx)
        ctx.gitlab.current_issue = make_api_issue(
            labels=["bug"], assignees=[{"id": 3, "username": "agent"}])
        fallback = self._FallbackStub(bot_id=11, username_ids={"agent": 3})
        monkeypatch.setattr(webhook_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.db.find_active_task(PROJECT_ID, IID) is not None

    def test_rejects_when_identity_unavailable(self, ctx, monkeypatch):
        """全局身份失败且仓库 remote 无可用身份：拒绝入队而非崩溃。"""
        _add_repo(ctx.db)  # 无 local_path，remote 身份不可解析
        self._fail_global_bot_id(ctx)
        ctx.gitlab.current_issue = make_api_issue(
            labels=["bug"], assignees=[{"id": 3, "username": "agent"}])
        monkeypatch.setattr(webhook_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (None, None), raising=False)

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot" in result["reason"]
        assert ctx.db.count_tasks() == 0

    def test_rejects_when_issue_not_assigned_to_bot(self, ctx, monkeypatch):
        """身份集合与 issue assignee 不匹配：拒绝入队（不误领取）。"""
        _add_repo(ctx.db)
        self._fail_global_bot_id(ctx)
        ctx.gitlab.current_issue = make_api_issue(
            labels=["bug"], assignees=[{"id": 3, "username": "agent"}])
        # remote 身份是 id=11，issue 分配给 id=3 且 remote username 查无此人
        fallback = self._FallbackStub(bot_id=11, username_ids={})
        monkeypatch.setattr(webhook_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "bot" in result["reason"]
        assert ctx.db.count_tasks() == 0


class TestWebhookGlobalTokenFallback:
    """issue #65 补充：全局 bot token 失效（401/403）时，webhook 的
    issue 查询与最后发言人查询也应用仓库 remote 内嵌 token 兜底。

    此前 401 直接「查询 issue 失败，拒绝入队」——全局 token 被撤销期间
    webhook 事件全部丢弃（对账兜底 5 分钟一轮才补入队，实时性受损）。
    """

    class BoomGitLab:
        """全局 client 桩：所有 API 调用一律 401。"""

        def __init__(self):
            self.get_bot_id_calls = 0

        def get_bot_id(self):
            self.get_bot_id_calls += 1
            raise GitLabError("token 无效或已过期（401）", 401)

        def get_issue(self, project_id, iid):
            raise GitLabError("token 无效或已过期（401）", 401)

        def last_note_author_id(self, project_id, iid):
            raise GitLabError("token 无效或已过期（401）", 401)

    class RemoteStub:
        """remote token 客户端桩：正常返回，并记录调用。"""

        def __init__(self, issue):
            self.issue = issue
            self.issue_calls = 0
            self.last_note_author = None

        def get_bot_id(self):
            return BOT_ID

        def get_user_id_by_username(self, username):
            return BOT_ID if username == "agent" else None

        def get_issue(self, project_id, iid):
            self.issue_calls += 1
            return self.issue

        def last_note_author_id(self, project_id, iid):
            return self.last_note_author

    def test_global_401_falls_back_and_enqueues(self, ctx, monkeypatch):
        """全局 401：issue 查询与发言人查询经 remote token 兜底，正常入队。"""
        _add_repo(ctx.db)
        fallback = self.RemoteStub(make_api_issue(labels=["bug"]))
        monkeypatch.setattr(webhook_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"),
                            raising=False)
        ctx.gitlab = self.BoomGitLab()
        ctx.handler.gitlab = ctx.gitlab

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        assert ctx.gitlab.get_bot_id_calls == 1
        assert fallback.issue_calls == 1
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task is not None and task["status"] == "queued"

    def test_global_401_without_remote_token_rejects(self, ctx, monkeypatch):
        """全局 401 且 remote 无 token：维持「查询 issue 失败，拒绝入队」。"""
        _add_repo(ctx.db)
        monkeypatch.setattr(webhook_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (None, None),
                            raising=False)
        ctx.gitlab = self.BoomGitLab()
        ctx.handler.gitlab = ctx.gitlab

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "查询 issue 失败" in result["reason"]
        assert ctx.db.count_tasks() == 0


class TestWebhookStoresIssueLabelsForPriority:
    """issue #76：webhook 入队时把 issue 标签与更新时间落库，供调度器排序。"""

    def test_task_stores_api_labels(self, ctx):
        """任务创建时记录 API 最新标签（供调度器标签权重排序）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug", "ui"])
        ctx.gitlab.current_issue["updated_at"] = "2026-08-14T08:00:00.000+08:00"

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task["issue_labels"] == '["bug", "ui"]'
        assert task["issue_updated_at"] == "2026-08-14 00:00:00"  # 归一化 UTC

    def test_task_stores_empty_labels_when_none(self, ctx):
        """issue 无标签：写空数组（调度器把无标签任务排最后）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=[])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task["issue_labels"] == "[]"
        assert task["issue_updated_at"] == ""  # 无 updated_at 时存空串


class TestWebhookMaintenanceMode:
    """维护模式（issue #241）：开启后新事件不派发新任务。

    - maintenance_hold_events=True（默认）：webhook 事件照常接收、照常建
      任务入队——「只入队不派发」由调度器维护模式检查保证（见
      test_scheduler_maintenance.py）；
    - maintenance_hold_events=False：直接不建任务（事件忽略，不消耗
      GitLab API 查询）。
    """

    def test_hold_events_true_creates_task(self, ctx):
        """维护模式开启 + hold_events=true（默认）：照常建任务入队。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.config.update_section("worker", {"maintenance_mode": True})

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task is not None and task["status"] == "queued", \
            "hold_events=true 时维护模式只入队不派发，任务应创建并排队"

    def test_hold_events_false_ignores_event(self, ctx):
        """维护模式开启 + hold_events=false：直接不建任务（事件忽略）。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])
        ctx.config.update_section(
            "worker", {"maintenance_mode": True, "maintenance_hold_events": False})

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert "维护模式" in result["reason"]
        assert ctx.db.count_tasks() == 0, "hold_events=false 时不得创建任务"

    def test_hold_events_false_skips_api_calls(self, ctx):
        """hold_events=false 时不消耗 GitLab API 查询（检查点在 assignee 判定前）。"""
        _add_repo(ctx.db)
        ctx.config.update_section(
            "worker", {"maintenance_mode": True, "maintenance_hold_events": False})

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is False
        assert ctx.gitlab.issue_calls == 0, "不建任务时不应发起 issue 查询"

    def test_maintenance_off_unchanged(self, ctx):
        """维护模式关闭：行为与现状一致，正常入队。"""
        _add_repo(ctx.db)
        ctx.gitlab.current_issue = make_api_issue(labels=["bug"])

        result = ctx.handler.handle(make_event(), "test-secret")

        assert result["accepted"] is True
        task = ctx.db.find_active_task(PROJECT_ID, IID)
        assert task is not None
