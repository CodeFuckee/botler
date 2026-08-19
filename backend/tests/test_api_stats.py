"""统计看板 API 与数据层聚合测试（issue #264）。

覆盖：
1. 数据层纯函数：_task_duration_seconds（合法/缺字段/负值/非法格式）、
   _normalize_failure_reason（空白折叠/截断）、aggregate_dashboard
   （总览/按引擎/按仓库/按来源/失败原因 Top，空输入返回合法结构，
   未指定引擎与未知来源兜底命名）；
2. Database.dashboard_stats / dashboard_task_rows：days 时间段过滤
   （0=全部、7=最近 7 天）；
3. API：GET /api/stats/dashboard 空库返回零值结构、有数据时各维度数字
   正确、days 非法参数返回 422。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import (
    FAILURE_REASON_TOP_N,
    Database,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    _normalize_failure_reason,
    _task_duration_seconds,
    aggregate_dashboard,
)

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
    ctx = SimpleNamespace(config=config, db=db, gitlab=None,
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db


@pytest.fixture
def client(api_app):
    app, _ = api_app
    return TestClient(app)


def _mk_repo(db, project_id: int, name: str) -> int:
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


_TASK_SEQ = 0


def _mk_task(db, repo_id: int, *, status: str = STATUS_QUEUED, engine: str = "",
             triggered_by: str = "webhook", created_at: str = "2026-08-18 01:00:00",
             finished_at: str = "2026-08-18 01:05:00",
             error_message: str | None = None) -> int:
    """插入一条可定制的任务记录并返回 id（issue_iid 全局递增防唯一索引碰撞）。"""
    global _TASK_SEQ
    _TASK_SEQ += 1
    task_id = db.create_task(repo_id, 1, 10000 + _TASK_SEQ,
                             "测试任务", triggered_by=triggered_by)
    # created_at 不在 set_task_status 白名单（_TASK_FIELDS），直接 SQL 更新
    # 整行字段，保证时间段过滤与耗时口径测试真实生效
    import sqlite3
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            """UPDATE tasks SET status=?, engine=?, created_at=?, finished_at=?,
               triggered_by=?, error_message=? WHERE id=?""",
            (status, engine, created_at, finished_at,
             triggered_by, error_message, task_id))
    return task_id


# ---- 纯函数：_task_duration_seconds ----

class TestTaskDurationSeconds:
    def test_valid(self):
        assert _task_duration_seconds("2026-08-18 01:00:00",
                                      "2026-08-18 01:05:30") == 330.0

    def test_missing_fields(self):
        assert _task_duration_seconds(None, "2026-08-18 01:05:00") is None
        assert _task_duration_seconds("2026-08-18 01:00:00", None) is None
        assert _task_duration_seconds("", "") is None

    def test_negative_duration_skipped(self):
        # 结束早于开始（时钟异常）→ 剔除
        assert _task_duration_seconds("2026-08-18 02:00:00",
                                      "2026-08-18 01:00:00") is None

    def test_bad_format(self):
        assert _task_duration_seconds("not-a-date", "2026-08-18 01:00:00") is None


# ---- 纯函数：_normalize_failure_reason ----

class TestNormalizeFailureReason:
    def test_collapse_whitespace(self):
        assert _normalize_failure_reason("a\n  b\t c") == "a b c"

    def test_empty(self):
        assert _normalize_failure_reason("") == ""
        assert _normalize_failure_reason(None) == ""

    def test_truncate(self):
        long_msg = "x" * 300
        assert len(_normalize_failure_reason(long_msg)) == 100


# ---- 纯函数：aggregate_dashboard ----

class TestAggregateDashboard:
    def _rows(self, tmp_path):
        db = Database(str(tmp_path / "agg.db"))
        repo_a = _mk_repo(db, 1, "repo-a")
        repo_b = _mk_repo(db, 2, "repo-b")
        _mk_task(db, repo_a, status=STATUS_SUCCEEDED, engine="claude",
                 triggered_by="webhook",
                 created_at="2026-08-18 01:00:00",
                 finished_at="2026-08-18 01:05:00")
        _mk_task(db, repo_a, status=STATUS_FAILED, engine="hermes",
                 triggered_by="manual",
                 created_at="2026-08-18 02:00:00",
                 finished_at="2026-08-18 02:02:00",
                 error_message="测试错误：网络超时\n重试多次仍失败")
        _mk_task(db, repo_b, status=STATUS_SUCCEEDED, engine="claude",
                 triggered_by="reconcile",
                 created_at="2026-08-18 03:00:00",
                 finished_at="2026-08-18 03:01:00")
        _mk_task(db, repo_b, status=STATUS_INTERRUPTED, engine="",
                 triggered_by="",
                 created_at="2026-08-18 04:00:00",
                 finished_at="2026-08-18 04:00:30",
                 error_message="用户手动停止")
        return db.dashboard_task_rows(days=0)

    def test_overview(self, tmp_path):
        res = aggregate_dashboard(self._rows(tmp_path))
        o = res["overview"]
        assert o["task_count"] == 4
        assert o["succeeded_count"] == 2
        assert o["failed_count"] == 1
        assert o["interrupted_count"] == 1
        assert o["success_rate"] == 0.5
        # 平均耗时 = (300 + 120 + 60 + 30) / 4 = 127.5
        assert o["avg_duration_seconds"] == 127.5

    def test_by_engine(self, tmp_path):
        res = aggregate_dashboard(self._rows(tmp_path))
        by_engine = {e["name"]: e for e in res["by_engine"]}
        assert by_engine["claude"]["task_count"] == 2
        assert by_engine["claude"]["success_rate"] == 1.0
        assert by_engine["hermes"]["task_count"] == 1
        assert by_engine["hermes"]["success_rate"] == 0.0
        # 空引擎显示「未指定」
        assert by_engine["未指定"]["task_count"] == 1

    def test_by_repo(self, tmp_path):
        res = aggregate_dashboard(self._rows(tmp_path))
        by_repo = {r["name"]: r for r in res["by_repo"]}
        assert by_repo["repo-a"]["task_count"] == 2
        assert by_repo["repo-a"]["failed_count"] == 1
        assert by_repo["repo-b"]["task_count"] == 2

    def test_by_source_display_names(self, tmp_path):
        res = aggregate_dashboard(self._rows(tmp_path))
        by_source = {s["name"]: s for s in res["by_source"]}
        assert set(by_source) == {"webhook", "手动", "对账", "其他"}
        assert by_source["手动"]["task_count"] == 1
        # 空 triggered_by 显示「其他」
        assert by_source["其他"]["task_count"] == 1

    def test_failure_reasons_grouped_and_top(self, tmp_path):
        db = Database(str(tmp_path / "agg2.db"))
        repo = _mk_repo(db, 1, "repo-a")
        for _ in range(3):
            _mk_task(db, repo, status=STATUS_FAILED, engine="dsh",
                     error_message=" 网络超时 \n重试 3 次仍失败 ")
        _mk_task(db, repo, status=STATUS_FAILED, engine="dsh",
                 error_message="权限不足")
        res = aggregate_dashboard(db.dashboard_task_rows(days=0))
        reasons = res["failure_reasons"]
        # 同一文案（空白差异）聚合为一个桶，count=3 排第一
        assert reasons[0]["count"] == 3
        assert reasons[0]["reason"] == "网络超时 重试 3 次仍失败"
        assert reasons[1]["count"] == 1

    def test_failure_reasons_truncated(self, tmp_path):
        db = Database(str(tmp_path / "agg3.db"))
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, status=STATUS_FAILED, engine="dsh",
                 error_message="e" * 500)
        res = aggregate_dashboard(db.dashboard_task_rows(days=0))
        assert len(res["failure_reasons"][0]["reason"]) == 100

    def test_failure_reasons_cap(self, tmp_path):
        db = Database(str(tmp_path / "agg4.db"))
        repo = _mk_repo(db, 1, "repo-a")
        for i in range(FAILURE_REASON_TOP_N + 3):
            _mk_task(db, repo, status=STATUS_FAILED, engine="dsh",
                     error_message=f"错误{i}")
        res = aggregate_dashboard(db.dashboard_task_rows(days=0))
        assert len(res["failure_reasons"]) == FAILURE_REASON_TOP_N

    def test_empty_input(self):
        res = aggregate_dashboard([])
        assert res["overview"]["task_count"] == 0
        assert res["overview"]["success_rate"] is None
        assert res["overview"]["avg_duration_seconds"] is None
        assert res["by_engine"] == []
        assert res["by_repo"] == []
        assert res["by_source"] == []
        assert res["failure_reasons"] == []

    def test_sort_by_task_count_desc(self, tmp_path):
        db = Database(str(tmp_path / "agg5.db"))
        repo_a = _mk_repo(db, 1, "repo-a")
        repo_b = _mk_repo(db, 2, "repo-b")
        _mk_task(db, repo_a, status=STATUS_SUCCEEDED, engine="claude")
        _mk_task(db, repo_a, status=STATUS_SUCCEEDED, engine="claude")
        _mk_task(db, repo_b, status=STATUS_SUCCEEDED, engine="hermes")
        res = aggregate_dashboard(db.dashboard_task_rows(days=0))
        assert [r["name"] for r in res["by_repo"]] == ["repo-a", "repo-b"]


# ---- 数据层：dashboard_task_rows / dashboard_stats ----

class TestDashboardTaskRows:
    def test_days_zero_returns_all(self, tmp_path):
        db = Database(str(tmp_path / "d.db"))
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, created_at="2026-01-01 00:00:00")
        _mk_task(db, repo, created_at="2026-08-18 00:00:00")
        assert len(db.dashboard_task_rows(days=0)) == 2

    def test_days_filter_keeps_recent(self, tmp_path):
        db = Database(str(tmp_path / "d2.db"))
        repo = _mk_repo(db, 1, "repo-a")
        old = _mk_task(db, repo, created_at="2026-01-01 00:00:00")
        recent = _mk_task(db, repo, created_at="2026-08-18 00:00:00")
        rows = db.dashboard_task_rows(days=7)
        ids = [r["id"] for r in rows]
        assert old not in ids
        assert recent in ids

    def test_repo_name_join(self, tmp_path):
        db = Database(str(tmp_path / "d3.db"))
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, status=STATUS_SUCCEEDED, engine="claude")
        row = db.dashboard_task_rows(days=0)[0]
        assert row["repo_name"] == "repo-a"

    def test_dashboard_stats_end_to_end(self, tmp_path):
        db = Database(str(tmp_path / "d4.db"))
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, status=STATUS_SUCCEEDED, engine="claude",
                 created_at="2026-08-18 01:00:00",
                 finished_at="2026-08-18 01:10:00")
        res = db.dashboard_stats(days=0)
        assert res["overview"]["task_count"] == 1
        assert res["overview"]["success_rate"] == 1.0
        assert res["overview"]["avg_duration_seconds"] == 600.0


# ---- API：GET /api/stats/dashboard ----

class TestDashboardApi:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """模块级 10s 缓存跨测试污染：每个用例前清空。"""
        from botler.api import stats as stats_api
        stats_api.clear_cache()
        yield

    def test_empty_db_zero_structure(self, client):
        r = client.get("/api/stats/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["overview"]["task_count"] == 0
        assert body["overview"]["success_rate"] is None
        assert body["by_engine"] == [] and body["failure_reasons"] == []

    def test_with_data(self, api_app):
        app, db = api_app
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, status=STATUS_SUCCEEDED, engine="claude",
                 triggered_by="webhook",
                 created_at="2026-08-18 01:00:00",
                 finished_at="2026-08-18 01:03:00")
        _mk_task(db, repo, status=STATUS_FAILED, engine="hermes",
                 triggered_by="manual",
                 created_at="2026-08-18 02:00:00",
                 finished_at="2026-08-18 02:01:00",
                 error_message="测试失败原因")
        client = TestClient(app)
        body = client.get("/api/stats/dashboard").json()
        assert body["overview"]["task_count"] == 2
        assert body["overview"]["succeeded_count"] == 1
        assert body["overview"]["failed_count"] == 1
        assert body["overview"]["success_rate"] == 0.5
        assert body["by_engine"][0]["name"] == "claude"
        assert body["failure_reasons"][0]["reason"] == "测试失败原因"

    def test_days_param(self, api_app):
        app, db = api_app
        repo = _mk_repo(db, 1, "repo-a")
        _mk_task(db, repo, status=STATUS_SUCCEEDED, engine="claude",
                 created_at="2026-01-01 00:00:00")
        client = TestClient(app)
        # 旧任务在 7 天窗口外 → task_count=0
        assert client.get("/api/stats/dashboard?days=7").json()["overview"]["task_count"] == 0
        assert client.get("/api/stats/dashboard?days=0").json()["overview"]["task_count"] == 1

    def test_invalid_days_422(self, client):
        assert client.get("/api/stats/dashboard?days=-1").status_code == 422
        assert client.get("/api/stats/dashboard?days=9999").status_code == 422

    def test_cache_returns_same_shape(self, client):
        r1 = client.get("/api/stats/dashboard?days=0").json()
        r2 = client.get("/api/stats/dashboard?days=0").json()
        assert r1 == r2
