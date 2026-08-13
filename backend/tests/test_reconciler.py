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
        # issue iid → 最后一条非系统评论的作者 id（None = 无发言）
        self.last_author_by_issue: dict[int, int | None] = {}
        # 终态标签对账（issue #40）：(project_id, iid) → issue 详情桩
        self.issue_details: dict[tuple[int, int], dict] = {}
        self.labels_added: list[tuple[int, int, list[str]]] = []

    def get_bot_id(self):
        return BOT_ID

    def list_open_issues(self, project_id, assignee_id=None):
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab API 故障")
        return self.issues_by_project.get(project_id, [])

    def last_note_author_id(self, project_id, iid):
        return self.last_author_by_issue.get(iid)

    def get_issue(self, project_id, iid):
        return self.issue_details.get((project_id, iid), {"state": "opened", "labels": []})

    def add_labels(self, project_id, iid, labels):
        self.labels_added.append((project_id, iid, labels))
        return {"iid": iid}


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


class TestReconcileSkipsWhenBotLastSpoke:
    """issue #34：最后一个发言人（非系统评论）是 bot 时不对账补入队。

    bot 提问后用户未回复（最后发言人是 bot），平台重启/手动对账时不应把
    该 issue 再次补入队——否则 bot 会重复领取自己刚提问过的任务。
    """

    def test_skips_issue_with_bot_last_note(self, ctx):
        """最后发言人是 bot：对账跳过，不补入队。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(7, labels=["bug"])]}
        ctx.gitlab.last_author_by_issue = {7: BOT_ID}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 0}
        assert ctx.db.count_tasks() == 0

    def test_enqueues_issue_with_user_last_note(self, ctx):
        """最后发言人是用户（有新指示）：照常补入队。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(8, labels=["bug"])]}
        ctx.gitlab.last_author_by_issue = {8: 1}  # 用户 id，非 bot

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.find_active_task(42, 8) is not None

    def test_enqueues_issue_with_no_notes(self, ctx):
        """无任何非系统评论（新任务）：照常补入队。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(9, labels=["bug"])]}
        ctx.gitlab.last_author_by_issue = {9: None}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.find_active_task(42, 9) is not None


def _mk_terminal_task(db, repo_id: int, issue_iid: int, status: str) -> int:
    """创建终态任务（succeeded/failed）并返回任务 id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, f"终态任务 {issue_iid}")
    db.set_task_status(task_id, status)
    return task_id


class TestReconcileBackfillsTerminalLabels:
    """issue #40：终态任务对应的 issue 缺 bot-done/bot-failed 标签时对账补打。

    复现缺陷：任务收尾打标签时平台被部署重启打断（任务 #63 于 13:31:45
    收尾，PUT /issues/39 未发出进程即被 pm2 delete 杀死），issue 无终态
    标签会被 webhook/对账重复领取。对账兜底扫描终态任务补打标签。
    """

    def test_backfills_bot_done_for_succeeded_task(self, ctx):
        """succeeded 任务 + issue 仍 open 无终态标签 → 补打 bot-done。"""
        repo_id = _add_repo(ctx.db)
        _mk_terminal_task(ctx.db, repo_id, 1, "succeeded")

        ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert (42, 1, ["bot-done"]) in ctx.gitlab.labels_added

    def test_backfills_bot_failed_for_failed_task(self, ctx):
        """failed 任务 → 补打 bot-failed（失败 issue 不再被重复领取）。"""
        repo_id = _add_repo(ctx.db)
        _mk_terminal_task(ctx.db, repo_id, 2, "failed")

        ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert (42, 2, ["bot-failed"]) in ctx.gitlab.labels_added

    def test_skips_when_issue_has_terminal_label(self, ctx):
        """issue 已带 bot-done：不重复补打。"""
        repo_id = _add_repo(ctx.db)
        _mk_terminal_task(ctx.db, repo_id, 3, "succeeded")
        ctx.gitlab.issue_details[(42, 3)] = {"state": "opened", "labels": ["bot-done"]}

        ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert ctx.gitlab.labels_added == []

    def test_skips_when_issue_closed(self, ctx):
        """issue 已关闭（用户已确认）：不补打标签。"""
        repo_id = _add_repo(ctx.db)
        _mk_terminal_task(ctx.db, repo_id, 4, "succeeded")
        ctx.gitlab.issue_details[(42, 4)] = {"state": "closed", "labels": []}

        ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert ctx.gitlab.labels_added == []

    def test_backfill_error_does_not_block_enqueue(self, ctx):
        """补打标签失败（GitLab 报错）：不影响补入队主流程。"""
        repo_id = _add_repo(ctx.db)
        _mk_terminal_task(ctx.db, repo_id, 5, "succeeded")

        def flaky_add_labels(project_id, iid, labels):
            raise GitLabError("模拟 GitLab API 故障")

        ctx.gitlab.add_labels = flaky_add_labels
        ctx.gitlab.issues_by_project = {42: [make_issue(6, labels=["bug"])]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
