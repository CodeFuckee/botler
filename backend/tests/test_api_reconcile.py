"""仓库对账 API 测试：仓库页「对账」按钮 → POST /api/repos/{id}/reconcile（issue #17）。

对账 = 立即扫描该仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入队。
与设置页的全局对账（/settings/reconcile-now，异步）不同，这里按单仓库同步执行并直接返回结果。
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


def make_issue(iid: int, title: str = "测试 issue") -> dict:
    return {"iid": iid, "title": title}


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


class TestReconcileRepo:
    def test_reconcile_enqueues_pending_issue(self, client):
        """有待处理 issue：scanned=1 enqueued=1，任务表出现 queued 记录。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        stub.issues_by_project = {42: [make_issue(7)]}

        resp = tc.post(f"/api/repos/{repo_id}/reconcile")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["scanned"] == 1
        assert data["enqueued"] == 1
        task = db.find_active_task(42, 7)
        assert task is not None
        assert task["status"] == "queued"
        assert task["triggered_by"] == "reconcile"

    def test_reconcile_no_pending(self, client):
        """没有待处理 issue：scanned=0 enqueued=0，不创建任务。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        stub.issues_by_project = {42: []}

        resp = tc.post(f"/api/repos/{repo_id}/reconcile")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "scanned": 0, "enqueued": 0}
        assert db.count_tasks() == 0

    def test_reconcile_skips_issue_with_active_task(self, client):
        """已有活跃任务（如 webhook 已建）的 issue 不重复入队。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        stub.issues_by_project = {42: [make_issue(7)]}
        tc.post(f"/api/repos/{repo_id}/reconcile")  # 首次入队

        resp = tc.post(f"/api/repos/{repo_id}/reconcile")  # 再次对账

        assert resp.status_code == 200
        data = resp.json()
        assert data["scanned"] == 1
        assert data["enqueued"] == 0
        # 任务表仍只有一条活跃记录
        assert len(db.list_tasks(status="queued")) == 1

    def test_reconcile_repo_not_found(self, client):
        """仓库不存在 → 404。"""
        tc, stub, db, tmp_path = client
        resp = tc.post("/api/repos/999/reconcile")
        assert resp.status_code == 404

    def test_reconcile_disabled_repo(self, client):
        """停用仓库：不扫描 GitLab，返回提示。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, enabled=False)

        resp = tc.post(f"/api/repos/{repo_id}/reconcile")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scanned"] == 0
        assert data["enqueued"] == 0
        assert "停用" in data["note"]

    def test_reconcile_gitlab_error_returns_502(self, client):
        """GitLab API 故障 → 502，提示对账失败。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        stub.fail_projects = {42}

        resp = tc.post(f"/api/repos/{repo_id}/reconcile")

        assert resp.status_code == 502
        assert "对账失败" in resp.json()["detail"]


class TestReconcileOnceSingleRepo:
    def test_reconcile_once_only_scans_given_repo(self, api_app):
        """单仓库对账只扫指定仓库，不影响其他仓库。"""
        app, stub, db, tmp_path = api_app
        repo_a = _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.issues_by_project = {42: [make_issue(1)], 43: [make_issue(2), make_issue(3)]}

        result = app.state.ctx.reconciler.reconcile_once(repo_id=repo_a)

        assert result == {"scanned": 1, "enqueued": 1}
        # 仓库 b 的 issue 未被扫描入队
        assert db.find_active_task(43, 2) is None
        assert db.find_active_task(43, 3) is None

    def test_reconcile_once_unknown_repo_returns_empty(self, api_app):
        """指定的仓库 id 不存在时返回空结果（API 层已先 404）。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1)]}

        result = app.state.ctx.reconciler.reconcile_once(repo_id=999)

        assert result == {"scanned": 0, "enqueued": 0}

    def test_reconcile_once_full_scan_still_covers_all(self, api_app):
        """不带 repo_id 时仍为全量扫描（回归：原有行为不变）。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.issues_by_project = {42: [make_issue(1)], 43: [make_issue(2)]}

        result = app.state.ctx.reconciler.reconcile_once()

        assert result == {"scanned": 2, "enqueued": 2}
