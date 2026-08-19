"""概览页「其他」分组手动调度顺序测试（issue #287）。

需求：概览页「其他」分组在「调度器执行顺序」排序下，用户可拖动 issue
上下移动来手动改变调度顺序。拖动后的整组顺序全量落库（issue_manual_orders
表，仅本地库），调度器派发时优先按手动顺序（scheduler._task_sort_key 的
手动标记/位置），后端提供 GET/PUT /api/issues/{project_id}/manual-orders
读写接口，overview 聚合结果携带 manual_order 字段。

覆盖：
- Database：replace_manual_orders 全量替换（增/删/重排/清空）、
  list_manual_orders 按 position 升序、get_manual_order_position 命中/未命中；
- TaskScheduler：手动顺序优先于标签权重（手动 feature 先于自动 bug）、
  手动顺序相对次序、未设置手动顺序的 issue 按默认排序排后、空手动顺序
  不影响默认派发；
- API：GET 空列表 / PUT 全量保存并读回 / 非法输入归一化（去重、非正
  数剔除、超长截断）/ 未知仓库 404 / overview 透传 manual_order / PUT
  清空 overview 缓存。
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


def _mk_repo(db, project_id: int) -> int:
    """插入一条仓库记录，返回 repo_id。"""
    db.upsert_repo(project_id, f"repo-{project_id}",
                   f"https://gitlab.example.com/group/repo-{project_id}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, project_id: int, issue_iid: int,
             issue_labels: list[str] | None = None,
             issue_created_at: str | None = None) -> int:
    """创建排队任务（带标签/issue 创建时间），返回 task_id。"""
    return db.create_task(
        repo_id, project_id, issue_iid, f"任务 {project_id}#{issue_iid}",
        triggered_by="webhook",
        issue_labels=issue_labels or [],
        issue_created_at=issue_created_at or "")


# ---- Database 层 ----

class TestDatabaseManualOrders:
    def test_replace_then_list_returns_position_order(self, db):
        repo = _mk_repo(db, 11)
        db.replace_manual_orders(repo, [5, 1, 3])
        assert db.list_manual_orders(repo) == [5, 1, 3], \
            "应按 position 升序返回（即入参原序）"

    def test_get_position_hit_and_miss(self, db):
        repo = _mk_repo(db, 11)
        assert db.get_manual_order_position(repo, 5) is None, "未设置应返回 None"
        db.replace_manual_orders(repo, [9, 5])
        assert db.get_manual_order_position(repo, 9) == 0
        assert db.get_manual_order_position(repo, 5) == 1
        assert db.get_manual_order_position(repo, 7) is None, "不在列表内应返回 None"

    def test_replace_updates_positions_and_removes_stale(self, db):
        repo = _mk_repo(db, 11)
        db.replace_manual_orders(repo, [1, 2, 3])
        db.replace_manual_orders(repo, [3, 1])  # 2 被移除，3/1 重排
        assert db.list_manual_orders(repo) == [3, 1]
        assert db.get_manual_order_position(repo, 2) is None, "旧条目应被清除"

    def test_empty_list_clears(self, db):
        repo = _mk_repo(db, 11)
        db.replace_manual_orders(repo, [1, 2])
        db.replace_manual_orders(repo, [])
        assert db.list_manual_orders(repo) == [], "空列表应清空手动顺序"

    def test_orders_isolated_per_repo(self, db):
        r1 = _mk_repo(db, 11)
        r2 = _mk_repo(db, 22)
        db.replace_manual_orders(r1, [1, 2])
        db.replace_manual_orders(r2, [7])
        assert db.list_manual_orders(r1) == [1, 2], "仓库间不应互相影响"
        assert db.list_manual_orders(r2) == [7]


# ---- 调度器层 ----

def _scheduler(config, db, executor) -> TaskScheduler:
    return TaskScheduler(config, db, executor)


class TestSchedulerManualOrder:
    def test_manual_order_beats_label_priority(self, config, db, executor):
        """手动置顶的 feature 应先于自动排序的 bug 派发（手动优先）。"""
        repo = _mk_repo(db, 11)
        t_bug = _mk_task(db, repo, 11, issue_iid=1, issue_labels=["bug"])
        t_feature = _mk_task(db, repo, 11, issue_iid=2,
                             issue_labels=["feature"])
        db.replace_manual_orders(repo, [2])  # 手动把 feature(2) 置顶

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bug)
        sched.enqueue(t_feature)

        done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "应派发一个任务"
        assert executor.run_ids == [t_feature], \
            "手动置顶的 feature 应先于 bug 派发"

    def test_manual_order_relative_sequence(self, config, db, executor):
        """同仓库内按手动顺序派发（用户拖动后的相对次序）。"""
        repo = _mk_repo(db, 11)
        t_a = _mk_task(db, repo, 11, issue_iid=10, issue_labels=["feature"])
        t_b = _mk_task(db, repo, 11, issue_iid=11, issue_labels=["bug"])
        t_c = _mk_task(db, repo, 11, issue_iid=12, issue_labels=["test"])
        # 手动顺序：c(12) → a(10) → b(11)，全部置顶且按此顺序
        db.replace_manual_orders(repo, [12, 10, 11])

        sched = _scheduler(config, db, executor)
        for tid in (t_a, t_b, t_c):
            sched.enqueue(tid)

        for expected in (t_c, t_a, t_b):
            done = executor.expect()
            sched._dispatch()
            assert done.wait(timeout=2), f"任务 {expected} 应被派发"
        assert executor.run_ids == [t_c, t_a, t_b], \
            "应按手动顺序 c → a → b 派发"

    def test_unlisted_issues_follow_after_manual(self, config, db, executor):
        """未设置手动顺序的 issue 按默认排序排在手动顺序之后。"""
        repo = _mk_repo(db, 11)
        t_bug = _mk_task(db, repo, 11, issue_iid=1, issue_labels=["bug"],
                         issue_created_at="2026-08-02")
        t_bug2 = _mk_task(db, repo, 11, issue_iid=2, issue_labels=["bug"],
                          issue_created_at="2026-08-01")
        t_feature = _mk_task(db, repo, 11, issue_iid=3,
                             issue_labels=["feature"],
                             issue_created_at="2026-08-03")
        db.replace_manual_orders(repo, [3])  # 手动置顶 feature

        sched = _scheduler(config, db, executor)
        for tid in (t_bug, t_bug2, t_feature):
            sched.enqueue(tid)

        for expected in (t_feature, t_bug2, t_bug):
            done = executor.expect()
            sched._dispatch()
            assert done.wait(timeout=2)
        assert executor.run_ids == [t_feature, t_bug2, t_bug], \
            "手动 feature 先派发，其余按默认（bug 权重 → 创建时间升序）"

    def test_empty_manual_order_keeps_default_dispatch(self, config, db, executor):
        """未设置任何手动顺序时派发顺序与默认一致（bug 先于 feature）。"""
        repo = _mk_repo(db, 11)
        t_feature = _mk_task(db, repo, 11, issue_iid=1,
                             issue_labels=["feature"])
        t_bug = _mk_task(db, repo, 11, issue_iid=2, issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_feature)
        sched.enqueue(t_bug)

        done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_bug], "无手动顺序时应按默认排序派发"


# ---- API 层 ----

class StubGitLab:
    """issue 查询桩：list_open_issues / list_project_labels 最小实现。"""

    def __init__(self):
        self.issues_by_project: dict[int, list[dict]] = {}

    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        return list(self.issues_by_project.get(project_id, []))

    def list_project_labels(self, project_id):
        return []


class StubScheduler:
    def enqueue(self, task_id: int) -> bool:
        return True


class StubExecutor:
    pass


@pytest.fixture
def api_client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          config_path=str(config_path),
                          scheduler=StubScheduler(), executor=StubExecutor())
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    from botler.api import issues as issues_mod
    issues_mod.clear_issue_cache()
    return TestClient(app), db


class TestManualOrdersAPI:
    def _mk_repo(self, db, project_id: int) -> int:
        return db.upsert_repo(project_id, f"repo-{project_id}",
                              f"https://gitlab.example.com/group/r{project_id}.git")

    def test_get_empty(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        r = tc.get("/api/issues/11/manual-orders")
        assert r.status_code == 200
        assert r.json() == {"project_id": 11, "iids": []}, "未设置时返回空列表"

    def test_put_then_get_roundtrip(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        r = tc.put("/api/issues/11/manual-orders", json={"iids": [3, 1, 2]})
        assert r.status_code == 200
        assert r.json() == {"project_id": 11, "iids": [3, 1, 2]}
        r = tc.get("/api/issues/11/manual-orders")
        assert r.json()["iids"] == [3, 1, 2], "保存后应原序读回"

    def test_put_normalizes_invalid_input(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        # 非正数剔除、重复去重（保序）
        r = tc.put("/api/issues/11/manual-orders",
                   json={"iids": [2, 0, -1, 2, 3, 3, 1]})
        assert r.status_code == 200
        assert r.json()["iids"] == [2, 3, 1], "应剔除非正整数并保序去重"

    def test_put_truncates_overlong(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        iids = list(range(1, 500))
        r = tc.put("/api/issues/11/manual-orders", json={"iids": iids})
        assert r.status_code == 200
        assert len(r.json()["iids"]) == 200, "超长列表应截断到上限"

    def test_put_empty_clears(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        tc.put("/api/issues/11/manual-orders", json={"iids": [1, 2]})
        r = tc.put("/api/issues/11/manual-orders", json={"iids": []})
        assert r.status_code == 200
        assert r.json()["iids"] == []
        assert tc.get("/api/issues/11/manual-orders").json()["iids"] == []

    def test_unknown_project_404(self, api_client):
        tc, db = api_client
        assert tc.get("/api/issues/999/manual-orders").status_code == 404
        assert tc.put("/api/issues/999/manual-orders",
                      json={"iids": [1]}).status_code == 404

    def test_disabled_repo_404(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        db.update_repo(db.get_repo_by_project_id(11)["id"], enabled=False)
        assert tc.get("/api/issues/11/manual-orders").status_code == 404

    def test_overview_carries_manual_order_and_put_clears_cache(self, api_client):
        tc, db = api_client
        self._mk_repo(db, 11)
        db.replace_manual_orders(db.get_repo_by_project_id(11)["id"], [7, 8])
        r = tc.get("/api/issues/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["repos"][0]["manual_order"] == [7, 8], \
            "overview 应透传 manual_order"
        assert data["repos"][0]["project_id"] == 11, "overview 应透传 project_id"
        # PUT 清空 overview 缓存：修改后下一次 overview 立即反映新顺序
        r = tc.put("/api/issues/11/manual-orders", json={"iids": [9]})
        assert r.status_code == 200
        r = tc.get("/api/issues/overview")
        assert r.json()["repos"][0]["manual_order"] == [9], \
            "PUT 后 overview 缓存应已清空并返回新顺序"
