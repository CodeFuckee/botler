"""任务单任务手动停止测试（issue #214）。

「任务列表/详情页增加单任务停止按钮」的后端实现分三层：
- database.stop_task：活跃任务（queued/running/retrying）标记 interrupted
  （写 error_message 与 finished_at，落 warn 日志），终态任务拒绝；
- API POST /api/tasks/{task_id}/stop：404（不存在）/ 400（状态不可停止）/
  200 成功——先落库再登记停止请求（executor.request_stop）并移除调度器
  内存队列中的排队任务；
- 调度器：remove_queued 从内存队列移除排队任务（running 在 _running 中
  登记，由 executor 终止进程）。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.scheduler import TaskScheduler
from botler.templates import TemplateRenderer

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
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def executor(config, db, tmp_path):
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    """插入一条仓库记录，返回 repo_id。"""
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1, status: str = "running",
             **fields) -> int:
    """创建任务并按需更新状态/字段，返回 task_id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, f"任务 {issue_iid}",
                             triggered_by="webhook")
    db.set_task_status(task_id, status, **fields)
    return task_id


# ---- database.stop_task ----

class TestStopTaskDb:
    """db 层：活跃任务标记 interrupted，终态/不存在拒绝。"""

    @pytest.mark.parametrize("status", ["queued", "running", "retrying"])
    def test_active_status_marked_interrupted(self, db, status):
        """活跃任务（queued/running/retrying）应标记 interrupted 并写日志。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status=status)

        assert db.stop_task(tid) == "ok"

        row = db.get_task(tid)
        assert row["status"] == "interrupted", f"{status} 任务应停止为 interrupted"
        assert row["error_message"] == "用户手动停止（单任务停止）"
        assert row["finished_at"] is not None, "停止应落完成时间"
        logs = db.list_logs(tid)
        assert any(l["level"] == "warn" and "手动停止" in l["message"] for l in logs), \
            "停止应写 warn 日志"

    @pytest.mark.parametrize("status", ["succeeded", "failed", "interrupted"])
    def test_terminal_status_rejected(self, db, status):
        """终态任务（succeeded/failed/interrupted）不可停止。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status=status)
        assert db.stop_task(tid) == "bad_state"
        assert db.get_task(tid)["status"] == status, "终态任务状态不应被修改"

    def test_missing_task_not_found(self, db):
        """任务不存在返回 not_found。"""
        assert db.stop_task(99999) == "not_found"

    def test_double_stop_rejected(self, db):
        """停止一次后任务已 interrupted（终态），再次停止应拒绝。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="running")
        assert db.stop_task(tid) == "ok"
        assert db.stop_task(tid) == "bad_state"


# ---- API POST /api/tasks/{task_id}/stop ----

@pytest.fixture
def api_app(tmp_path, config, db, executor):
    """最小测试 app：ctx 带真实 scheduler（停止后移除排队任务全链路）。"""
    scheduler = TaskScheduler(config, db, executor)
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, renderer=None,
                          executor=executor, scheduler=scheduler)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


class TestStopApi:
    """POST /api/tasks/{id}/stop 数据契约。"""

    def test_stop_running_task_marks_interrupted(self, api_app):
        """running 任务停止后状态落库为 interrupted，返回任务 id。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="running")

        resp = TestClient(app).post(f"/api/tasks/{tid}/stop")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"task_id": tid, "status": "interrupted"}
        assert db.get_task(tid)["status"] == "interrupted"
        assert db.get_task(tid)["error_message"] == "用户手动停止（单任务停止）"

    def test_stop_queued_task_removed_from_scheduler_queue(self, api_app):
        """排队任务停止后应同时从调度器内存队列移除。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="queued")
        scheduler = app.state.ctx.scheduler
        assert scheduler.enqueue(tid), "前置条件：任务入队"
        assert scheduler.stats()["queued"] == 1

        resp = TestClient(app).post(f"/api/tasks/{tid}/stop")

        assert resp.status_code == 200
        assert db.get_task(tid)["status"] == "interrupted"
        assert scheduler.stats()["queued"] == 0, "停止后排队任务应从内存队列移除"

    def test_stop_terminal_task_400(self, api_app):
        """终态任务（succeeded/failed/interrupted）返回 400。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="succeeded")

        resp = TestClient(app).post(f"/api/tasks/{tid}/stop")

        assert resp.status_code == 400
        assert "仅排队中" in resp.json()["detail"]
        assert db.get_task(tid)["status"] == "succeeded", "终态任务状态不应被修改"

    def test_stop_missing_task_404(self, api_app):
        """任务不存在返回 404。"""
        app, db, _ = api_app
        resp = TestClient(app).post("/api/tasks/99999/stop")
        assert resp.status_code == 404

    def test_stop_registers_stop_request_on_executor(self, api_app, monkeypatch):
        """停止应调用 executor.request_stop（登记停止请求 + 终止进程）。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="running")
        executor = app.state.ctx.executor
        calls = []
        monkeypatch.setattr(executor, "request_stop",
                            lambda task_id: calls.append(task_id))

        resp = TestClient(app).post(f"/api/tasks/{tid}/stop")

        assert resp.status_code == 200
        assert calls == [tid], "应调用 executor.request_stop 登记停止请求"
        # 真实 request_stop 实现会登记 _stop_requests 并终止进程；
        # 重试接口随后 clear_stop_request 清除（issue #69）——此处验证调用链路

    def test_stop_idempotent_on_missing_process(self, api_app, monkeypatch):
        """进程不存在时 request_stop 幂等（仅登记），不抛错。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="queued")
        executor = app.state.ctx.executor
        calls = []
        monkeypatch.setattr(executor, "request_stop",
                            lambda task_id: calls.append(task_id))
        monkeypatch.setattr(app.state.ctx.scheduler, "remove_queued",
                            lambda task_id: True)

        resp = TestClient(app).post(f"/api/tasks/{tid}/stop")

        assert resp.status_code == 200
        assert calls == [tid]
