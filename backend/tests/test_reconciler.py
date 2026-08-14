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
import botler.reconciler as reconciler_mod

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

    def get_user_id_by_username(self, username):
        """按用户名查用户 id（issue #65 身份提示用）；桩默认查不到。"""
        return None

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


class TestReconcileSkipsNeedVerifyIssues:
    """issue #41：带 need-verify 标签（用户标记需人工验证）的 issue 不对账补入队。

    与终态标签（bot-done/bot-failed）同理，对账扫描时若 issue 已打
    need-verify，跳过不补入队——用户已明确该 issue 需要人工验证，
    bot 不应领取处理。
    """

    def test_skips_need_verify_issue(self, ctx):
        """已打 need-verify 的 issue 不补入队（需人工验证，bot 不领取）。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {42: [make_issue(7, labels=["bug", "need-verify"])]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 0}
        assert ctx.db.count_tasks() == 0

    def test_need_verify_issue_skipped_among_clean_ones(self, ctx):
        """混合队列：只有不带 need-verify 的 issue 被入队，带标签的全部跳过。"""
        repo_id = _add_repo(ctx.db)
        ctx.gitlab.issues_by_project = {
            42: [
                make_issue(1, labels=["bug"]),
                make_issue(2, labels=["need-verify"]),
                make_issue(3, labels=["feature", "need-verify"]),
                make_issue(4),
            ]}

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 4, "enqueued": 2}
        assert ctx.db.find_active_task(42, 1) is not None
        assert ctx.db.find_active_task(42, 4) is not None
        assert ctx.db.find_active_task(42, 2) is None
        assert ctx.db.find_active_task(42, 3) is None


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


class TestReconcileFallsBackToRemoteToken:
    """issue #63：对账遇 token 失效（401/403）时，尝试用仓库 remote url
    内嵌的 token 构建 per-repo client 兜底重试。

    复现缺陷：全局 bot token 失效后对账整体失败（或 get_bot_id 失败直接
    放弃），各仓库 remote 里明明有可用 token 却不尝试。
    """

    @staticmethod
    def _make_git_repo(path, remote_url: str) -> None:
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add",
                        "origin", remote_url], check=True)

    @staticmethod
    def _add_local_repo(db, tmp_path, project_id=42, name="demo") -> int:
        repo_dir = tmp_path / name
        TestReconcileFallsBackToRemoteToken._make_git_repo(
            repo_dir, "https://agent:glpat-repo@gitlab.example.com/group/repo.git")
        return db.upsert_repo(
            project_id=project_id, name=name,
            url=f"https://gitlab.example.com/{name}.git",
            local_path=str(repo_dir), remote_name="origin")

    def test_global_401_falls_back_to_remote_token(self, ctx, tmp_path, monkeypatch):
        """全局 token 失效（401）：用 remote 内嵌 token 客户端重试，补入队成功。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)

        def fail_list(project_id, assignee_id=None):
            raise GitLabError("token 无效或已过期（401）", 401)

        ctx.gitlab.list_open_issues = fail_list
        fallback = StubGitLab()
        fallback.issues_by_project = {42: [make_issue(1, labels=["bug"])]}
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.find_active_task(42, 1) is not None

    def test_global_403_falls_back_to_remote_token(self, ctx, tmp_path, monkeypatch):
        """全局 token 权限不足（403）：同样尝试 remote token 兜底。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)

        def fail_list(project_id, assignee_id=None):
            raise GitLabError("权限不足（403）", 403)

        ctx.gitlab.list_open_issues = fail_list
        fallback = StubGitLab()
        fallback.issues_by_project = {42: [make_issue(2, labels=["bug"])]}
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.find_active_task(42, 2) is not None

    def test_401_without_remote_token_reports_error(self, ctx, monkeypatch):
        """全局 401 且仓库 remote 无可用 token：报错进 errors，不补入队。"""
        repo_id = _add_repo(ctx.db)  # 无 local_path，remote 不可解析

        def fail_list(project_id, assignee_id=None):
            raise GitLabError("token 无效或已过期（401）", 401)

        ctx.gitlab.list_open_issues = fail_list
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (None, None), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result["scanned"] == 0
        assert result["enqueued"] == 0
        assert any("仓库 demo" in e for e in result["errors"])

    def test_global_bot_id_unavailable_uses_remote_token_identity(
            self, ctx, tmp_path, monkeypatch):
        """全局 token 失效致 bot 身份不可用：以 remote token 账号身份对账。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)

        def fail_bot_id(force=False):
            raise GitLabError("token 无效或已过期（401）", 401)

        ctx.gitlab.get_bot_id = fail_bot_id
        fallback = StubGitLab()
        fallback.get_bot_id = lambda: 77  # remote token 账号的 user id
        fallback.issues_by_project = {42: [make_issue(3, labels=["bug"])]}
        seen_assignee: list = []
        fallback.list_open_issues = lambda project_id, assignee_id=None: (
            seen_assignee.append(assignee_id)
            or fallback.issues_by_project.get(project_id, []))
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert seen_assignee == [77]  # 用 remote token 账号 id 过滤 assignee

    def test_backfill_label_401_falls_back_to_remote_token(
            self, ctx, tmp_path, monkeypatch):
        """主查询正常、终态标签补打遇 401：改用 remote token 客户端补打。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)
        _mk_terminal_task(ctx.db, repo_id, 1, "succeeded")
        ctx.gitlab.issues_by_project = {42: []}  # 主查询走全局正常

        def fail_add_labels(project_id, iid, labels):
            raise GitLabError("token 无效或已过期（401）", 401)

        ctx.gitlab.add_labels = fail_add_labels
        fallback = StubGitLab()
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert (42, 1, ["bot-done"]) in fallback.labels_added

    def test_fallback_failure_reports_error(self, ctx, tmp_path, monkeypatch):
        """remote token 客户端也失败（401）：报错进 errors，不补入队。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)

        def fail_list(project_id, assignee_id=None):
            raise GitLabError("token 无效或已过期（401）", 401)

        ctx.gitlab.list_open_issues = fail_list
        fallback = StubGitLab()
        fallback.fail_projects = {42}
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result["scanned"] == 0
        assert result["enqueued"] == 0
        assert any("仓库 demo" in e for e in result["errors"])


class TestReconcileRemoteTokenIdentityMismatch:
    """issue #65：全局 token 失效后对账 bot 身份漂移。

    全局 token 失效 → 对账降级用 remote 内嵌 token 的账号（如项目 bot，
    id=11）作为 bot 身份，而用户把新 issue 分配给 @agent（id=3，即 remote
    URL userinfo 里的用户名对应账号），assignee_id=11 过滤后扫描为 0——
    API 正常返回，无任何权限报错，新 issue 被静默漏扫。

    修复：remote URL 的用户名也作为 bot 身份候选（agent → id=3），对账
    以全部候选身份分别扫描后合并去重，扫到分配给 @agent 的 issue。
    """

    @staticmethod
    def _add_local_repo(db, tmp_path, project_id=42, name="demo") -> int:
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

    @staticmethod
    def _remote_stub(issues_by_assignee: dict[int, list[dict]],
                     bot_id: int = 11,
                     username_ids: dict[str, int] | None = None) -> StubGitLab:
        """remote token 兜底桩：按 assignee_id 返回不同 issue 列表。

        bot_id 为 remote token 账号 id；username_ids 模拟按用户名查用户 id
        （如 remote URL 用户名 agent → id=3）。"""
        stub = StubGitLab()
        stub.get_bot_id = lambda: bot_id
        stub.get_user_id_by_username = lambda u: (username_ids or {}).get(u)
        seen: list = []
        stub.list_open_issues = lambda project_id, assignee_id=None: (
            seen.append(assignee_id)
            or issues_by_assignee.get(assignee_id, []))
        stub.seen_assignees = seen
        return stub

    def test_scans_issue_assigned_to_remote_username(
            self, ctx, tmp_path, monkeypatch):
        """全局 token 失效、新 issue 分配给 @agent（remote URL 用户名对应
        id=3）：对账应扫到并补入队。

        修复前：只以 remote token 账号（id=11）扫描，assignee_id=11 为空，
        静默返回 scanned=0、enqueued=0，无任何错误。
        """
        repo_id = self._add_local_repo(ctx.db, tmp_path)
        self._fail_global_bot_id(ctx)
        fallback = self._remote_stub(
            {3: [make_issue(1, labels=["bug"])], 11: []},
            bot_id=11, username_ids={"agent": 3})
        # 修复前接口：让测试在旧实现下精确失败于 scanned=0（而非网络错误）
        monkeypatch.setattr(reconciler_mod, "build_repo_client",
                            lambda repo, verify_ssl: fallback, raising=False)
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.find_active_task(42, 1) is not None
        assert set(fallback.seen_assignees) == {3, 11}

    def test_unknown_remote_username_only_scans_token_account(
            self, ctx, tmp_path, monkeypatch):
        """remote URL 用户名不是真实用户（查无此人）：忽略该身份，
        只用 remote token 账号扫描，行为与修复前一致。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)
        self._fail_global_bot_id(ctx)
        fallback = self._remote_stub(
            {3: [make_issue(1, labels=["bug"])], 11: []},
            bot_id=11, username_ids={})  # agent 查无此人
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 0, "enqueued": 0}
        assert set(fallback.seen_assignees) == {11}

    def test_dedupes_issue_assigned_to_multiple_identities(
            self, ctx, tmp_path, monkeypatch):
        """同一 issue 同时分配给两个 bot 身份：两个身份各扫到一次，
        按 iid 去重后只补入队一次。"""
        repo_id = self._add_local_repo(ctx.db, tmp_path)
        self._fail_global_bot_id(ctx)
        shared = make_issue(1, labels=["bug"])
        fallback = self._remote_stub(
            {3: [shared], 11: [shared]},
            bot_id=11, username_ids={"agent": 3})
        monkeypatch.setattr(reconciler_mod, "build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"), raising=False)

        result = ctx.reconciler.reconcile_once(repo_id=repo_id)

        assert result == {"scanned": 1, "enqueued": 1}
        assert ctx.db.count_tasks() == 1
