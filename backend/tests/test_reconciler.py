"""Reconciler 对账兜底扫描单元测试（issue #30）。

agent 只处理「没有 bot-done / bot-failed 标签且未关闭」的 issue。
对账补入队时必须过滤已打 bot-done（完成待用户确认）/ bot-failed（失败待
人工介入）的 issue——否则平台重启、任务表清理或手动「对账」后，会把已完成的
issue 重复入队，失败 issue 甚至无限重试循环。
"""

from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError
from botler.reconciler import Reconciler

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


class StubGitLab:
    """对账用的 GitLab 桩：bot 身份固定，open issues 列表可配置、可故障注入。"""

    def __init__(self, issues_by_project: dict[int, list[dict]] | None = None):
        self.issues_by_project = issues_by_project or {}
        self.fail_projects: set[int] = set()

    def get_bot_id(self):
        return BOT_ID

    def list_open_issues(self, project_id, assignee_id=None):
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab API 故障")
        return self.issues_by_project.get(project_id, [])


def make_issue(iid: int, title: str = "测试 issue", labels: list[str] | None = None) -> dict:
    return {"iid": iid, "title": title, "labels": labels or []}


@pytest.fixture
def ctx(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    # scheduler 只用到 enqueue，用桩即可（不启动调度线程）
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    reconciler = Reconciler(config, db, stub, scheduler)
    return SimpleNamespace(config=config, db=db, gitlab=stub, reconciler=reconciler)


def _add_repo(db, project_id=42, name="demo", enabled=True) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled)


class TestReconcileSkipsTerminalLabeledIssues:
    def test_skips_bot_done_issue(self, ctx):
        """已打 bot-done 的 issue 不再补入队（完成待用户确认，勿重复处理）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(7, labels=["bug", "bot-done"])]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 0}
        assert ctx.db.count_tasks() == 0

    def test_skips_bot_failed_issue(self, ctx):
        """已打 bot-failed 的 issue 不再补入队（失败待人工介入，避免无限重试）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(8, labels=["bot-failed"])]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 0}
        assert ctx.db.count_tasks() == 0

    def test_only_clean_issues_enqueued(self, ctx):
        """混合队列：只有不带终态标签的 issue 被入队，其余全部跳过。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {
            42: [
                make_issue(1, labels=["bug"]),
                make_issue(2, labels=["bot-done"]),
                make_issue(3, labels=["feature", "bot-failed"]),
                make_issue(4),
            ]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 4, "enqueued": 2}
        assert ctx.db.find_active_task(42, 1) is not None
        assert ctx.db.find_active_task(42, 4) is not None
        assert ctx.db.find_active_task(42, 2) is None
        assert ctx.db.find_active_task(42, 3) is None

    def test_clean_issue_still_enqueued(self, ctx):
        """回归：不带终态标签的普通 issue 照常入队（原有行为不变）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(1, labels=["bug"])]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        task = ctx.db.find_active_task(42, 1)
        assert task is not None
        assert task["status"] == "queued"
        assert task["triggered_by"] == "reconcile"
