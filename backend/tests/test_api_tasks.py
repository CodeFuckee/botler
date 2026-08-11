"""任务 API 测试：列表（过滤/搜索/分页）、详情、统计与失败原因数据契约。

任务列表「失败原因显示」功能依赖 GET /api/tasks 返回的 error_message / error_detail
字段（error_detail 为每次尝试失败详情的结构化对象，供「查看详细原因」按钮使用），
本文件验证该数据契约及其余列表行为。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database

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


@pytest.fixture
def api_app(tmp_path):
    """最小测试 app：只挂 api 路由，ctx 用临时 config + db（无 gitlab 依赖）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


@pytest.fixture
def client(api_app):
    app, db, tmp_path = api_app
    return TestClient(app), db


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    """插入一条仓库记录，返回 repo_id。"""
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1, title: str = "修复登录问题",
             status: str = "succeeded", error_message: str | None = None,
             error_detail: str | None = None) -> int:
    """创建任务并按需更新状态，返回 task_id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, title, triggered_by="webhook")
    db.set_task_status(task_id, status, error_message=error_message,
                       error_detail=error_detail)
    return task_id


class TestListTasks:
    """GET /api/tasks 列表：字段契约、过滤、搜索、分页。"""

    def test_empty_list(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tasks"] == []
        assert body["total"] == 0
        assert body["stats"] == {}

    def test_list_returns_error_message_field(self, client):
        """列表项必须携带 error_message 字段（失败原因显示的数据契约）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="会失败的任务",
                 status="failed", error_message="重试耗尽（2 次）后仍失败，最后退出码 -1")
        _mk_task(db, repo_id, issue_iid=2, title="成功的任务", status="succeeded")

        body = app_client.get("/api/tasks").json()
        assert body["total"] == 2
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["error_message"] == "重试耗尽（2 次）后仍失败，最后退出码 -1"
        assert by_iid[1]["status"] == "failed"
        # 成功任务无失败原因
        assert by_iid[2]["error_message"] is None
        # 列表项应包含前端所需的全部字段
        for key in ("id", "repo_id", "repo_name", "issue_iid", "issue_title",
                    "status", "attempt_count", "triggered_by", "error_message"):
            assert key in by_iid[1], f"列表项缺少字段 {key}"

    def test_list_returns_resumed_flag(self, client):
        """列表项 resumed 字段：有 claude_session_id（会话恢复过）为 true，否则 false。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="恢复过的任务")
        db.set_task_status(_mk_task(db, repo_id, issue_iid=2, title="全新任务"),
                           "running", claude_session_id="sid-abc")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["resumed"] is False
        assert by_iid[2]["resumed"] is True

    def test_list_returns_error_detail_object(self, client):
        """列表项 error_detail 应解析为结构化对象（「查看详细原因」按钮的数据契约）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        detail_json = json.dumps(
            {"summary": "重试耗尽后仍失败，最后退出码 1",
             "attempts": [{"attempt": 1, "exit_code": 1, "error": "构建超时"},
                          {"attempt": 2, "exit_code": 1, "error": "Traceback: boom"}]})
        _mk_task(db, repo_id, issue_iid=1, title="失败任务",
                 status="failed", error_message="重试耗尽（2 次）后仍失败，最后退出码 1",
                 error_detail=detail_json)
        _mk_task(db, repo_id, issue_iid=2, title="无详情的失败",
                 status="failed", error_message="平台重启导致中断")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["error_detail"]["summary"] == "重试耗尽后仍失败，最后退出码 1"
        assert by_iid[1]["error_detail"]["attempts"][1]["error"] == "Traceback: boom"
        assert by_iid[2]["error_detail"] is None

    def test_invalid_error_detail_returns_none(self, client):
        """error_detail 存了非法 JSON 时 API 返回 None（不 500）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="脏数据任务",
                 status="failed", error_message="原因", error_detail="not-json{{")
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["error_detail"] is None

    def test_status_filter(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, status="failed", error_message="原因 A")
        _mk_task(db, repo_id, issue_iid=2, status="interrupted", error_message="平台重启导致中断")
        _mk_task(db, repo_id, issue_iid=3, status="succeeded")

        failed = app_client.get("/api/tasks", params={"status": "failed"}).json()
        assert [t["issue_iid"] for t in failed["tasks"]] == [1]
        assert failed["total"] == 1

        interrupted = app_client.get("/api/tasks", params={"status": "interrupted"}).json()
        assert [t["issue_iid"] for t in interrupted["tasks"]] == [2]
        assert interrupted["tasks"][0]["error_message"] == "平台重启导致中断"

    def test_invalid_status_returns_400(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks", params={"status": "bogus"})
        assert resp.status_code == 400

    def test_search_by_title_and_iid(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=7, title="数据库连接失败排查")
        _mk_task(db, repo_id, issue_iid=8, title="优化构建速度")

        by_title = app_client.get("/api/tasks", params={"search": "数据库"}).json()
        assert [t["issue_iid"] for t in by_title["tasks"]] == [7]

        by_iid = app_client.get("/api/tasks", params={"search": "8"}).json()
        assert [t["issue_iid"] for t in by_iid["tasks"]] == [8]

    def test_pagination_and_limit_bounds(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        for i in range(5):
            _mk_task(db, repo_id, issue_iid=100 + i, title=f"任务 {i}")

        page = app_client.get("/api/tasks", params={"limit": 2, "offset": 1}).json()
        # ORDER BY id DESC
        assert [t["issue_iid"] for t in page["tasks"]] == [103, 102]

        # limit 上限 200、下限 1（FastAPI Query 约束 → 422）
        assert app_client.get("/api/tasks", params={"limit": 0}).status_code == 422
        assert app_client.get("/api/tasks", params={"limit": 201}).status_code == 422

    def test_repo_name_resolved(self, client):
        app_client, db = client
        repo_id = _mk_repo(db, project_id=42, name="my-awesome-repo")
        _mk_task(db, repo_id)
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["repo_name"] == "my-awesome-repo"

    def test_repo_missing_shows_null_name(self, client):
        """仓库记录被删后，列表 repo_name 应为 None（前端显示 '—' 不报错）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.delete_repo(repo_id)
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["repo_name"] is None
        assert body["tasks"][0]["id"] == task_id


class TestTaskDetail:
    """GET /api/tasks/{id} 详情。"""

    def test_detail_includes_error_message_and_logs(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="失败任务",
                           status="failed", error_message="Claude Code 报告无法解决该 issue")
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task = resp.json()
        assert task["error_message"] == "Claude Code 报告无法解决该 issue"
        assert task["logs"] == []
        assert task["status"] == "failed"

    def test_get_missing_task_404(self, client):
        app_client, db = client
        assert app_client.get("/api/tasks/99999").status_code == 404

    def test_logs_endpoint_404_for_missing(self, client):
        app_client, db = client
        assert app_client.get("/api/tasks/99999/logs").status_code == 404


class TestStatsAndDedup:
    """task_stats 统计与活跃任务去重。"""

    def test_task_stats_counts_by_status(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, status="succeeded")
        _mk_task(db, repo_id, issue_iid=2, status="failed", error_message="原因")
        _mk_task(db, repo_id, issue_iid=3, status="queued")
        stats = app_client.get("/api/tasks").json()["stats"]
        assert stats == {"succeeded": 1, "failed": 1, "queued": 1}

    def test_dup_active_task_rejected(self, client):
        """同一 (project_id, issue_iid) 已有活跃任务时 create_task 返回 None（去重）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        first = db.create_task(repo_id, 42, 1, "重复任务")
        assert first is not None
        assert db.create_task(repo_id, 42, 1, "重复任务") is None
        # 失败（终态）后允许重新创建
        db.set_task_status(first, "failed", error_message="原因")
        assert db.create_task(repo_id, 42, 1, "重复任务") is not None
