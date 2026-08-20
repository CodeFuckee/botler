"""排队任务人工优先级测试（issue #242）。

需求：任务表新增人工优先级字段 manual_priority（可选，null = 按系统规则）；
任务列表页排队中的任务提供「置顶/上移/下移/置底/移出队列」操作；概览页
issue 右边栏对 queued 任务提供「优先处理」按钮；系统派发时 manual_priority
优先于仓库/标签规则；操作记录到任务日志与审计。

覆盖：
- Database：set/clear manual_priority（含非排队状态拒绝）、reorder 置顶/
  上移/下移/置底（重排编号 0..n-1、未设置人工优先级的边界）、dequeue →
  canceled_by_user（终态、可重试、写日志）；
- TaskScheduler：人工优先级优先于标签权重、按值升序、未设置任务排后、
  与 issue #287 手动顺序的叠加次序；
- API：POST /tasks/{id}/priority（top/up/down/bottom/clear + 404/400）、
  POST /tasks/{id}/dequeue、GET /tasks 列表携带 manual_priority、
  POST /issues/{project_id}/{iid}/prioritize（优先处理）、
  GET /issues/{project_id}/{iid}/detail 携带 task_status。
"""

import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.scheduler import TaskScheduler

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


class FakeExecutor:
    """记录 run_task 调用的执行器桩（不真正执行任务）。"""

    def __init__(self):
        self.run_ids: list[int] = []
        self._done: list[threading.Event] = []

    def run_task(self, task_id: int):
        self.run_ids.append(task_id)
        self._done.pop(0).set()

    def request_stop(self, task_id: int):
        pass

    def expect(self) -> threading.Event:
        """登记下一次 run_task 的 done 事件。"""
        done = threading.Event()
        self._done.append(done)
        return done


@pytest.fixture
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def executor():
    return FakeExecutor()


def _mk_repo(db, project_id: int = 11) -> int:
    db.upsert_repo(project_id, f"repo-{project_id}",
                   f"https://gitlab.example.com/group/repo-{project_id}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, project_id: int = 11, issue_iid: int = 1,
             issue_labels: list[str] | None = None,
             issue_created_at: str | None = None) -> int:
    return db.create_task(
        repo_id, project_id, issue_iid, f"任务 {project_id}#{issue_iid}",
        triggered_by="webhook",
        issue_labels=issue_labels or [],
        issue_created_at=issue_created_at or "")


# ---- Database 层 ----

class TestDatabaseManualPriority:
    def test_set_clear_roundtrip(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        assert db.set_task_manual_priority(tid, 3) == "ok"
        assert db.get_task(tid)["manual_priority"] == 3
        assert db.set_task_manual_priority(tid, None) == "ok"
        assert db.get_task(tid)["manual_priority"] is None, "清除后应恢复系统规则"

    def test_set_priority_writes_log(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        db.set_task_manual_priority(tid, 1)
        logs = [l["message"] for l in db.list_logs(tid)]
        assert any("人工优先级" in m for m in logs), "操作应写入任务日志（审计）"

    def test_non_queued_task_rejected(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        db.set_task_status(tid, "running")
        assert db.set_task_manual_priority(tid, 1) == "bad_state", \
            "running 任务不可设置人工优先级"
        assert db.reorder_manual_priority(tid, "top")[0] == "bad_state", \
            "running 任务不可重排"
        assert db.dequeue_task(tid) == "bad_state", "running 任务不可移出队列"

    def test_missing_task(self, db):
        assert db.set_task_manual_priority(999, 1) == "not_found"
        assert db.reorder_manual_priority(999, "top")[0] == "not_found"
        assert db.dequeue_task(999) == "not_found"

    def test_bad_action(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        assert db.reorder_manual_priority(tid, "jump")[0] == "bad_action"

    def test_top_and_bottom_renumber(self, db):
        repo = _mk_repo(db)
        t1 = _mk_task(db, repo, issue_iid=1)
        t2 = _mk_task(db, repo, issue_iid=2)
        t3 = _mk_task(db, repo, issue_iid=3)
        # 置顶 t2 → 0
        res, pri = db.reorder_manual_priority(t2, "top")
        assert (res, pri) == ("ok", 0)
        assert db.get_task(t2)["manual_priority"] == 0
        # 置底 t1（此时 t1 无人工优先级 → 不动作）
        res, pri = db.reorder_manual_priority(t1, "bottom")
        assert (res, pri) == ("ok", None)
        assert db.get_task(t1)["manual_priority"] is None
        # t3 上移（无人工优先级 → 追加到手动序列尾，优先级 1）
        res, pri = db.reorder_manual_priority(t3, "up")
        assert (res, pri) == ("ok", 1)
        assert db.get_task(t2)["manual_priority"] == 0
        assert db.get_task(t3)["manual_priority"] == 1
        # t2 下移 → 与 t3 交换
        res, pri = db.reorder_manual_priority(t2, "down")
        assert (res, pri) == ("ok", 1)
        assert db.get_task(t2)["manual_priority"] == 1
        assert db.get_task(t3)["manual_priority"] == 0

    def test_renumber_is_compact_after_removal(self, db):
        """移出队列后剩余任务保持紧凑编号且顺序不变。"""
        repo = _mk_repo(db)
        t1 = _mk_task(db, repo, issue_iid=1)
        t2 = _mk_task(db, repo, issue_iid=2)
        t3 = _mk_task(db, repo, issue_iid=3)
        db.reorder_manual_priority(t1, "top")
        db.reorder_manual_priority(t2, "top")   # t2→0, t1→1
        db.reorder_manual_priority(t3, "top")   # t3→0, t2→1, t1→2
        assert [db.get_task(t)["manual_priority"] for t in (t3, t2, t1)] == [0, 1, 2]
        db.dequeue_task(t3)
        # t2 上移后重排仍连续（t2→0, t1→1）
        db.reorder_manual_priority(t2, "top")
        assert [db.get_task(t)["manual_priority"] for t in (t2, t1)] == [0, 1]

    def test_dequeue_marks_canceled_by_user(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        assert db.dequeue_task(tid) == "ok"
        row = db.get_task(tid)
        assert row["status"] == "canceled_by_user", "移出队列应标记 canceled_by_user"
        assert row["finished_at"], "终态应记录结束时间"
        logs = [l["message"] for l in db.list_logs(tid)]
        assert any("移出队列" in m for m in logs), "移出队列应写日志（可追溯）"

    def test_canceled_task_can_retry(self, db):
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        db.dequeue_task(tid)
        assert db.retry_task(tid) == "ok", "canceled_by_user 任务应可手动重试重新入队"
        assert db.get_task(tid)["status"] == "queued"

    def test_canceled_task_not_requeued_on_restart(self, db):
        """重启恢复只捞 running/retrying：canceled_by_user 保持终态。"""
        repo = _mk_repo(db)
        tid = _mk_task(db, repo)
        db.dequeue_task(tid)
        assert db.requeue_interrupted() == []
        assert db.get_task(tid)["status"] == "canceled_by_user"

    def test_priorities_isolated_per_repo(self, db):
        r1 = _mk_repo(db, 11)
        r2 = _mk_repo(db, 22)
        a = _mk_task(db, r1, project_id=11, issue_iid=1)
        b = _mk_task(db, r2, project_id=22, issue_iid=1)
        db.reorder_manual_priority(a, "top")
        db.reorder_manual_priority(b, "top")
        assert db.get_task(a)["manual_priority"] == 0
        assert db.get_task(b)["manual_priority"] == 0, "不同仓库各自重排"


# ---- 调度器层 ----

def _scheduler(config, db, executor) -> TaskScheduler:
    return TaskScheduler(config, db, executor)


class TestSchedulerManualPriority:
    def test_manual_priority_beats_label_priority(self, config, db, executor):
        """人工置顶的 feature 应先于自动排序的 bug 派发。"""
        repo = _mk_repo(db)
        t_bug = _mk_task(db, repo, issue_iid=1, issue_labels=["bug"],
                         issue_created_at="2026-08-01")
        t_feat = _mk_task(db, repo, issue_iid=2, issue_labels=["feature"],
                          issue_created_at="2026-08-02")
        db.reorder_manual_priority(t_feat, "top")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bug)
        sched.enqueue(t_feat)
        done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_feat], "人工置顶任务应先派发"

    def test_relative_order_by_priority_value(self, config, db, executor):
        """人工优先级按值升序派发（0 最前）。"""
        repo = _mk_repo(db)
        t1 = _mk_task(db, repo, issue_iid=1, issue_labels=["feature"])
        t2 = _mk_task(db, repo, issue_iid=2, issue_labels=["bug"])
        t3 = _mk_task(db, repo, issue_iid=3, issue_labels=["feature"])
        db.reorder_manual_priority(t3, "top")    # t3 → 0
        db.reorder_manual_priority(t1, "top")    # t1 → 0, t3 → 1

        sched = _scheduler(config, db, executor)
        for tid in (t1, t2, t3):
            sched.enqueue(tid)
        for expected in (t1, t3, t2):
            done = executor.expect()
            sched._dispatch()
            assert done.wait(timeout=2), f"任务 {expected} 应被派发"
        assert executor.run_ids == [t1, t3, t2], "按人工优先级 0,1 再默认"

    def test_unlisted_follow_after_manual(self, config, db, executor):
        """未设置人工优先级的任务按默认排序排在人工优先级之后。"""
        repo = _mk_repo(db)
        t_bug_old = _mk_task(db, repo, issue_iid=1, issue_labels=["bug"],
                             issue_created_at="2026-08-01")
        t_bug_new = _mk_task(db, repo, issue_iid=2, issue_labels=["bug"],
                             issue_created_at="2026-08-02")
        t_feat = _mk_task(db, repo, issue_iid=3, issue_labels=["feature"],
                          issue_created_at="2026-08-03")
        db.reorder_manual_priority(t_feat, "top")

        sched = _scheduler(config, db, executor)
        for tid in (t_bug_old, t_bug_new, t_feat):
            sched.enqueue(tid)
        for expected in (t_feat, t_bug_old, t_bug_new):
            done = executor.expect()
            sched._dispatch()
            assert done.wait(timeout=2)
        assert executor.run_ids == [t_feat, t_bug_old, t_bug_new], \
            "人工 feature 先派发，其余按默认（bug 权重 → 创建时间升序）"

    def test_manual_priority_beats_issue287_manual_order(self, config, db, executor):
        """任务级人工优先级（#242）应先于 issue 级手动顺序（#287）。"""
        repo = _mk_repo(db)
        t_287 = _mk_task(db, repo, issue_iid=10, issue_labels=["feature"])
        t_242 = _mk_task(db, repo, issue_iid=20, issue_labels=["feature"])
        # #287：手动把 10 置顶
        db.replace_manual_orders(repo, [10])
        # #242：手动把 20 置顶（任务级）
        db.reorder_manual_priority(t_242, "top")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_287)
        sched.enqueue(t_242)
        done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_242], "任务级人工优先级应先于 #287 手动顺序"

    def test_empty_manual_priority_keeps_default(self, config, db, executor):
        repo = _mk_repo(db)
        t_feat = _mk_task(db, repo, issue_iid=1, issue_labels=["feature"])
        t_bug = _mk_task(db, repo, issue_iid=2, issue_labels=["bug"])
        sched = _scheduler(config, db, executor)
        sched.enqueue(t_feat)
        sched.enqueue(t_bug)
        done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_bug], "无人工优先级时应按默认排序派发"


# ---- API 层 ----

class StubScheduler:
    def __init__(self):
        self.removed: list[int] = []

    def enqueue(self, task_id: int) -> bool:
        return True

    def remove_queued(self, task_id: int) -> bool:
        self.removed.append(task_id)
        return True


class StubGitLab:
    """issue 查询桩（detail 接口 list_issue_notes 最小实现）。"""

    def __init__(self):
        self.notes: list[dict] = []
        self.label_events: list[dict] = []

    def list_issue_notes(self, project_id, iid, limit=100):
        return list(self.notes)

    def list_issue_label_events(self, project_id, iid, limit=100):
        """标记活动事件桩（issue #349）：默认无事件。"""
        return list(self.label_events)


@pytest.fixture
def api_client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          config_path=str(config_path),
                          scheduler=StubScheduler(), executor=SimpleNamespace())
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    from botler.api import issues as issues_mod
    issues_mod.clear_issue_cache()
    return TestClient(app), db, ctx


class TestPriorityAPI:
    def _mk(self, db, status="queued"):
        repo_id = db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        tid = db.create_task(repo_id, 42, 1, "任务", triggered_by="webhook")
        if status != "queued":
            db.set_task_status(tid, status)
        return tid

    def test_priority_top_then_list(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db)
        r = tc.post(f"/api/tasks/{tid}/priority", params={"action": "top"})
        assert r.status_code == 200
        assert r.json()["manual_priority"] == 0
        # 列表携带 manual_priority
        r = tc.get("/api/tasks")
        assert r.status_code == 200
        assert r.json()["tasks"][0]["manual_priority"] == 0

    def test_priority_clear(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db)
        tc.post(f"/api/tasks/{tid}/priority", params={"action": "top"})
        r = tc.post(f"/api/tasks/{tid}/priority", params={"action": "clear"})
        assert r.status_code == 200
        assert r.json()["manual_priority"] is None

    def test_priority_bad_action(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db)
        r = tc.post(f"/api/tasks/{tid}/priority", params={"action": "jump"})
        assert r.status_code == 400

    def test_priority_bad_state_running(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db, status="running")
        r = tc.post(f"/api/tasks/{tid}/priority", params={"action": "top"})
        assert r.status_code == 400, "running 任务不可调整人工优先级"

    def test_priority_not_found(self, api_client):
        tc, db, ctx = api_client
        assert tc.post("/api/tasks/999/priority",
                       params={"action": "top"}).status_code == 404

    def test_dequeue(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db)
        r = tc.post(f"/api/tasks/{tid}/dequeue")
        assert r.status_code == 200
        assert r.json() == {"task_id": tid, "status": "canceled_by_user"}
        assert db.get_task(tid)["status"] == "canceled_by_user"
        assert tid in ctx.scheduler.removed, "应从调度器内存队列移除"

    def test_dequeue_bad_state(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db, status="running")
        r = tc.post(f"/api/tasks/{tid}/dequeue")
        assert r.status_code == 400

    def test_dequeue_not_found(self, api_client):
        tc, db, ctx = api_client
        assert tc.post("/api/tasks/999/dequeue").status_code == 404

    def test_status_filter_accepts_canceled(self, api_client):
        tc, db, ctx = api_client
        tid = self._mk(db)
        tc.post(f"/api/tasks/{tid}/dequeue")
        r = tc.get("/api/tasks", params={"status": "canceled_by_user"})
        assert r.status_code == 200
        assert [t["id"] for t in r.json()["tasks"]] == [tid]


class TestIssuePrioritizeAPI:
    def test_prioritize_queued_task(self, api_client):
        tc, db, ctx = api_client
        repo_id = db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        tid = db.create_task(repo_id, 42, 7, "任务", triggered_by="webhook")
        r = tc.post("/api/issues/42/7/prioritize")
        assert r.status_code == 200
        assert r.json()["task_id"] == tid
        assert r.json()["manual_priority"] == 0
        assert db.get_task(tid)["manual_priority"] == 0

    def test_prioritize_running_rejected(self, api_client):
        tc, db, ctx = api_client
        repo_id = db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        tid = db.create_task(repo_id, 42, 7, "任务", triggered_by="webhook")
        db.set_task_status(tid, "running")
        r = tc.post("/api/issues/42/7/prioritize")
        assert r.status_code == 400, "running 任务不可优先处理（验收标准）"

    def test_prioritize_no_task_rejected(self, api_client):
        tc, db, ctx = api_client
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        assert tc.post("/api/issues/42/9/prioritize").status_code == 400

    def test_prioritize_unknown_repo_404(self, api_client):
        tc, db, ctx = api_client
        assert tc.post("/api/issues/999/1/prioritize").status_code == 404

    def test_detail_carries_task_status(self, api_client):
        tc, db, ctx = api_client
        repo_id = db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        db.create_task(repo_id, 42, 7, "任务", triggered_by="webhook")
        r = tc.get("/api/issues/42/7/detail")
        assert r.status_code == 200
        assert r.json()["task_status"] == "queued", "detail 应携带最近任务状态"
