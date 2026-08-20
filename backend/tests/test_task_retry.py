"""任务手动重试测试（issue #36）。

「任务页面增加一个按钮，可以让用户手动重试任务」的后端实现分三层：
- database.retry_task：终态任务（failed/interrupted）重置为 queued（清空
  失败相关字段、attempt_count 归零、triggered_by 标记 manual，保留
  claude_session_id 断点续跑与 log_path），同 issue 已有活跃任务时拒绝
  （部分唯一索引去重）；
- API POST /api/tasks/{task_id}/retry：404（不存在）/ 400（状态不可重试）/
  409（同 issue 已有活跃任务）/ 200 成功并交给调度器重新入队；
- 调度器：复用 enqueue 派发（重试后任务回到仓库 FIFO 队列正常执行）。
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
worker: {precheck_enabled: false}
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


def _mk_task(db, repo_id: int, issue_iid: int = 1, status: str = "failed",
             **fields) -> int:
    """创建任务并按需更新状态/字段，返回 task_id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, f"任务 {issue_iid}",
                             triggered_by="webhook")
    db.set_task_status(task_id, status, **fields)
    return task_id


# ---- database.retry_task ----

class TestRetryTaskDb:
    """db 层：终态任务重置为 queued，非法状态/活跃冲突拒绝。"""

    def test_failed_task_reset_to_queued(self, db):
        repo_id = _mk_repo(db)
        tid = _mk_task(
            db, repo_id, status="failed", attempt_count=3, exit_code=1,
            error_message="重试耗尽后仍失败",
            error_detail='{"summary": "失败", "attempts": []}',
            commit_sha="deadbeef00", claude_session_id="sid-1",
            log_path="/tmp/task.log",
            started_at="2026-08-13 10:00:00", finished_at="2026-08-13 10:30:00")

        result = db.retry_task(tid)

        assert result == "ok"
        row = db.get_task(tid)
        assert row["status"] == "queued"
        assert row["attempt_count"] == 0, "重试后尝试次数应归零"
        assert row["triggered_by"] == "manual", "应标记为手动重试"
        assert row["exit_code"] is None
        assert row["error_message"] is None, "失败原因应清空"
        assert row["error_detail"] is None, "失败详情应清空"
        assert row["commit_sha"] is None, "上次提交记录应清空"
        assert row["started_at"] is None
        assert row["finished_at"] is None
        # 断点续跑与会话日志保留（重试接续上次 claude 会话继续执行）
        assert row["claude_session_id"] == "sid-1"
        assert row["log_path"] == "/tmp/task.log"

    def test_interrupted_task_reset_to_queued(self, db):
        """一键停止/平台重启中断的任务同样可手动重试。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="interrupted",
                       error_message="用户手动停止（一键停止所有任务）")

        assert db.retry_task(tid) == "ok"
        row = db.get_task(tid)
        assert row["status"] == "queued"
        assert row["error_message"] is None

    def test_active_or_succeeded_rejected(self, db):
        """非终态失败任务（queued/running/retrying/succeeded）不可重试。"""
        repo_id = _mk_repo(db)
        for iid, status in [(1, "queued"), (2, "running"), (3, "retrying"),
                            (4, "succeeded")]:
            tid = _mk_task(db, repo_id, issue_iid=iid, status=status)
            assert db.retry_task(tid) == "bad_state", f"{status} 任务应拒绝重试"
            assert db.get_task(tid)["status"] == status, "拒绝时状态不应被改动"

    def test_conflict_when_issue_has_active_task(self, db):
        """同一 issue 已有活跃任务（如 webhook 新任务）→ 冲突拒绝，不撞唯一索引。"""
        repo_id = _mk_repo(db)
        failed_id = _mk_task(db, repo_id, issue_iid=1, status="failed")
        # 同 issue 已有一条活跃任务（对账新建）
        db.create_task(repo_id, 42, 1, "任务 1", triggered_by="reconcile")

        assert db.retry_task(failed_id) == "conflict"
        assert db.get_task(failed_id)["status"] == "failed", "冲突时原任务保持终态"

    def test_missing_task_not_found(self, db):
        assert db.retry_task(99999) == "not_found"

    def test_retry_writes_log(self, db):
        """重试应写 info 日志记录原状态（便于追溯）。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="failed", error_message="原因")
        db.retry_task(tid)
        logs = db.list_logs(tid)
        assert any(l["level"] == "info" and "重试" in l["message"] for l in logs), \
            "重试应写 info 日志"

    def test_double_retry_rejected(self, db):
        """重试一次后任务已 queued（活跃），再次重试应拒绝（bad_state）。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="failed")
        assert db.retry_task(tid) == "ok"
        assert db.retry_task(tid) == "bad_state"


# ---- database.find_latest_task（issue #117：概览页重试按钮定位任务）----

class TestFindLatestTask:
    """db 层：按 project_id+iid 取最近一条任务记录。"""

    def test_returns_latest_task(self, db):
        """同 issue 多条任务记录时返回 id 最大（最新创建）的一条。"""
        repo_id = _mk_repo(db)
        # create_task 对同 issue 活跃任务去重，首条先落终态再建第二条
        first = db.create_task(repo_id, 42, 1, "任务 1", triggered_by="webhook")
        db.set_task_status(first, "succeeded")
        second = db.create_task(repo_id, 42, 1, "任务 1", triggered_by="reconcile")
        assert second is not None, "前置条件：首条任务终态后允许新建"

        row = db.find_latest_task(42, 1)

        assert row is not None and row["id"] == second, "应返回最新创建的任务"
        assert row["triggered_by"] == "reconcile"
        assert first != second

    def test_no_task_returns_none(self, db):
        """该 issue 无任何任务记录 → None。"""
        repo_id = _mk_repo(db)
        db.create_task(repo_id, 42, 1, "任务 1")
        assert db.find_latest_task(42, 999) is None
        assert db.find_latest_task(999, 1) is None

    def test_returns_terminal_status_task(self, db):
        """终态任务（failed/succeeded）同样返回，供重试端点判定。"""
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, issue_iid=1, status="failed")
        assert db.find_latest_task(42, 1)["id"] == tid
        assert db.find_latest_task(42, 1)["status"] == "failed"


# ---- API POST /api/tasks/{task_id}/retry ----

@pytest.fixture
def api_app(tmp_path, config, db, executor):
    """最小测试 app：ctx 带真实 scheduler（重试后入队全链路）。"""
    scheduler = TaskScheduler(config, db, executor)
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, renderer=None,
                          executor=executor, scheduler=scheduler)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


class TestRetryApi:
    """POST /api/tasks/{id}/retry 数据契约。"""

    def test_retry_failed_task_requeues(self, api_app):
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="failed", error_message="原因")

        resp = TestClient(app).post(f"/api/tasks/{tid}/retry")

        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == tid
        assert body["status"] == "queued"
        row = db.get_task(tid)
        assert row["status"] == "queued"
        assert row["triggered_by"] == "manual"

    def test_retry_enqueues_to_scheduler(self, api_app):
        """重试成功后任务应回到调度队列（stats.queued 计数增加）。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="failed")

        resp = TestClient(app).post(f"/api/tasks/{tid}/retry")

        assert resp.status_code == 200
        scheduler = app.state.ctx.scheduler
        assert scheduler.stats()["queued"] == 1, "重试任务应重新入队"

    def test_retry_missing_404(self, api_app):
        app, _, _ = api_app
        resp = TestClient(app).post("/api/tasks/99999/retry")
        assert resp.status_code == 404

    def test_retry_bad_state_400(self, api_app):
        """succeeded / running / queued 等状态不可重试 → 400。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        for iid, status in [(1, "succeeded"), (2, "running"), (3, "queued")]:
            tid = _mk_task(db, repo_id, issue_iid=iid, status=status)
            resp = TestClient(app).post(f"/api/tasks/{tid}/retry")
            assert resp.status_code == 400, f"{status} 任务重试应 400"
            assert db.get_task(tid)["status"] == status

    def test_retry_conflict_409(self, api_app):
        """同 issue 已有活跃任务 → 409 冲突。"""
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        failed_id = _mk_task(db, repo_id, issue_iid=1, status="failed")
        db.create_task(repo_id, 42, 1, "任务 1", triggered_by="reconcile")

        resp = TestClient(app).post(f"/api/tasks/{failed_id}/retry")

        assert resp.status_code == 409
        assert db.get_task(failed_id)["status"] == "failed"


# ---- issue #69：停止请求残留导致重试任务被打回 interrupted ----

class TestRetryClearsStaleStopRequest:
    """一键停止登记的停止请求残留 → 重试任务被立即打回 interrupted。

    复现链路（生产日志 task_93/115 证实）：用户一键停止所有任务 →
    executor.request_stop 把 task_id 登记进 _stop_requests 内存集合
    （登记后从未清除）→ 任务落 interrupted → 用户手动重试 → 任务重置
    queued 入队 → worker 领取后 run_task 开头 _stop_requested 命中旧
    请求 → _finish_stopped 立即打回 interrupted。表现为「每次手动重试
    过几秒就变成中断状态」，只有平台重启（集合随内存清空）才能逃脱。
    """

    def test_retry_clears_stale_stop_request(self, api_app):
        """手动重试成功后，历史停止请求应被清除，worker 领取不再误命中。"""
        app, db, _ = api_app
        executor = app.state.ctx.executor
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="interrupted",
                       error_message="用户手动停止（一键停止所有任务）")
        # 模拟一键停止时 executor 登记过停止请求（此后集合中一直残留）
        executor.request_stop(tid)
        assert executor._stop_requested(tid), "前置条件：停止请求已登记"

        resp = TestClient(app).post(f"/api/tasks/{tid}/retry")

        assert resp.status_code == 200
        assert db.get_task(tid)["status"] == "queued"
        assert not executor._stop_requested(tid), \
            "重试后残留停止请求应被清除，否则 worker 领取时立即被打回 interrupted"

    def test_finish_stopped_consumes_request(self, api_app):
        """停止收尾消费请求：_finish_stopped 落终态后停止请求即清除。"""
        app, db, _ = api_app
        executor = app.state.ctx.executor
        repo_id = _mk_repo(db)
        # _finish_stopped 兜底的是「worker 已领取（claim → running）但尚未
        # 被 stop_active_tasks 覆盖」的任务，前置状态应为 running
        tid = _mk_task(db, repo_id, status="running")
        executor.request_stop(tid)

        executor._finish_stopped(tid)

        assert db.get_task(tid)["status"] == "interrupted"
        assert not executor._stop_requested(tid), "停止收尾后请求应被消费，防止集合无限膨胀"

    def test_stop_after_retry_still_works(self, api_app):
        """回归保障：重试后用户再次一键停止，停止机制仍正常生效。"""
        app, db, _ = api_app
        executor = app.state.ctx.executor
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, status="interrupted")
        executor.request_stop(tid)
        assert TestClient(app).post(f"/api/tasks/{tid}/retry").status_code == 200

        resp = TestClient(app).post("/api/tasks/stop-all")

        assert tid in resp.json()["stopped"], "重试后的 queued 任务应被再次停止"
        assert db.get_task(tid)["status"] == "interrupted"
