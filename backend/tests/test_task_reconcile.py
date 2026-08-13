"""任务页全局对账 API 测试：POST /api/tasks/reconcile-all（issue #38）。

一键对账所有启用仓库：同步执行全量对账扫描（复用 Reconciler.reconcile_once），
把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入队。
与仓库页单仓库对账（/repos/{id}/reconcile，issue #17）不同：多仓库场景下
单个仓库失败不中断整体（HTTP 200），失败明细放入 errors 列表返回。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
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

    def last_note_author_id(self, project_id, iid):
        return None  # 默认无发言


def make_issue(iid: int, title: str = "测试 issue", labels: list[str] | None = None) -> dict:
    issue = {"iid": iid, "title": title}
    if labels:
        issue["labels"] = labels
    return issue


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    # scheduler 只用到 enqueue，用桩即可（不启动调度线程）
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    reconciler = Reconciler(config, db, stub, scheduler)
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          reconciler=reconciler, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, stub, db, tmp_path


@pytest.fixture
def client(api_app):
    app, stub, db, tmp_path = api_app
    return TestClient(app), stub, db, tmp_path


def _add_repo(db, project_id=42, name="demo", enabled=True) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled)


class TestReconcileAll:
    def test_reconcile_all_enqueues_issues_from_all_repos(self, client):
        """正常路径：多个启用仓库的待处理 issue 全部补入队。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.issues_by_project = {42: [make_issue(1), make_issue(2)], 43: [make_issue(5)]}

        resp = tc.post("/api/tasks/reconcile-all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scanned"] == 3
        assert data["enqueued"] == 3
        assert data["errors"] == []
        for project_id, iid in ((42, 1), (42, 2), (43, 5)):
            task = db.find_active_task(project_id, iid)
            assert task is not None
            assert task["status"] == "queued"
            assert task["triggered_by"] == "reconcile"

    def test_reconcile_all_no_pending_returns_zero(self, client):
        """没有待处理 issue：scanned=0 enqueued=0 errors=[]，不创建任务。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: []}

        resp = tc.post("/api/tasks/reconcile-all")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "scanned": 0, "enqueued": 0, "errors": []}
        assert db.count_tasks() == 0

    def test_reconcile_all_idempotent_on_repeat(self, client):
        """重复调用：已有活跃任务的 issue 不重复入队（幂等）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(7)]}

        first = tc.post("/api/tasks/reconcile-all").json()
        second = tc.post("/api/tasks/reconcile-all").json()

        assert first == {"ok": True, "scanned": 1, "enqueued": 1, "errors": []}
        assert second == {"ok": True, "scanned": 1, "enqueued": 0, "errors": []}
        assert len(db.list_tasks(status="queued")) == 1

    def test_reconcile_all_skips_terminal_label_issue(self, client):
        """带终态标签（bot-done/bot-failed）的 issue 不补入队。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, labels=["bot-done"]),
                                       make_issue(2, labels=["bot-failed"]),
                                       make_issue(3)]}

        resp = tc.post("/api/tasks/reconcile-all")

        data = resp.json()
        assert data["scanned"] == 3
        assert data["enqueued"] == 1  # 仅无标签的 issue #3
        assert db.find_active_task(42, 3) is not None
        assert db.find_active_task(42, 1) is None
        assert db.find_active_task(42, 2) is None

    def test_reconcile_all_partial_repo_failure(self, client):
        """部分仓库 GitLab 故障：正常仓库继续入队，失败明细进 errors（HTTP 200）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.issues_by_project = {42: [make_issue(1)], 43: [make_issue(2)]}
        stub.fail_projects = {42}

        resp = tc.post("/api/tasks/reconcile-all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scanned"] == 1
        assert data["enqueued"] == 1
        assert len(data["errors"]) == 1
        assert "仓库 a" in data["errors"][0]
        # 正常仓库 b 的 issue 已入队
        assert db.find_active_task(43, 2) is not None

    def test_reconcile_all_all_repos_failed_still_200(self, client):
        """全部仓库失败：仍返回 200（不整体 502），errors 记录全部失败明细。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.fail_projects = {42, 43}

        resp = tc.post("/api/tasks/reconcile-all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scanned"] == 0
        assert data["enqueued"] == 0
        assert len(data["errors"]) == 2

    def test_reconcile_all_skips_disabled_repo(self, client):
        """停用仓库不扫描：结果只统计启用仓库。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="on")
        _add_repo(db, project_id=43, name="off", enabled=False)
        stub.issues_by_project = {42: [make_issue(1)], 43: [make_issue(2)]}

        resp = tc.post("/api/tasks/reconcile-all")

        data = resp.json()
        assert data == {"ok": True, "scanned": 1, "enqueued": 1, "errors": []}
        assert db.find_active_task(43, 2) is None

    def test_reconcile_all_without_any_repo(self, client):
        """边界：没有任何仓库时返回空结果（不 500）。"""
        tc, stub, db, tmp_path = client

        resp = tc.post("/api/tasks/reconcile-all")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "scanned": 0, "enqueued": 0, "errors": []}
