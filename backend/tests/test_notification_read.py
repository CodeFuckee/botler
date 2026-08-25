"""通知已读/未读功能测试（issue #215）。

背景：notification_events 表 + GET /api/notifications/events（游标 after
增量拉取，issue #21）实现通知推送，但无已读/未读状态。

本文件覆盖：
- 迁移 v31：旧库（user_version=30）补 read_at 列平滑、数据不丢；新库直建；
- DB 方法：mark_notification_read / mark_all_notifications_read /
  count_unread_notifications（含不存在 id、幂等、空表边界）；
- API：GET /events 返回 read 字段与 unread_count、GET /api/notifications
  最新优先列表、POST /{id}/read（200/404/幂等）、POST /read-all（计数/空表）。
"""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.notifier import Notifier

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

# issue #21 时代的 notification_events 旧表结构（无 read_at 列）
OLD_NOTIFICATION_SCHEMA = """
CREATE TABLE notification_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  repo_name TEXT,
  task_id INTEGER,
  data TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _build_old_notification_db(path) -> None:
    """手工构造 user_version=30 的旧库（模拟线上部署中的历史库）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_NOTIFICATION_SCHEMA)
    conn.execute(
        "INSERT INTO notification_events (type, title, body, repo_name) "
        "VALUES ('task_failed', '历史通知', '旧库存量数据', 'demo')")
    conn.execute("PRAGMA user_version = 30")
    conn.commit()
    conn.close()


# ---------- 迁移 v31 ----------

class TestMigrateReadAt:
    """notification_events.read_at 列迁移（issue #215）。"""

    def test_old_db_gets_read_at_column(self, tmp_path):
        """旧库（user_version=30）初始化后应补出 read_at 列且数据保留。"""
        path = tmp_path / "old.db"
        _build_old_notification_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notification_events)")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            rows = conn.execute("SELECT * FROM notification_events").fetchall()
        assert "read_at" in cols, "旧库应补出 read_at 列"
        assert ver == 32, "迁移后 user_version 应为 32"
        assert len(rows) == 1 and rows[0]["title"] == "历史通知", "迁移不得丢数据"
        assert rows[0]["read_at"] is None, "存量数据默认未读"

    def test_new_db_has_read_at_column(self, tmp_path):
        """新库建表语句应直接含 read_at 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notification_events)")}
        assert "read_at" in cols

    def test_migrate_is_idempotent(self, tmp_path):
        """重复初始化不报错（迁移幂等，ALTER 前已判列存在）。"""
        path = tmp_path / "old.db"
        _build_old_notification_db(path)
        Database(str(path))
        Database(str(path))  # 第二次打开（已到 v31）不抛异常


# ---------- DB 方法 ----------

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def db_with_events(db):
    """预置 3 条未读通知：id 1/2/3。"""
    n = Notifier(db)
    for i in range(3):
        n.record("task_succeeded", f"t{i}", "b", task_id=i + 1)
    return db


class TestMarkNotificationRead:
    def test_mark_read_sets_read_at(self, db_with_events):
        db = db_with_events
        assert db.mark_notification_read(2) is True
        with db._conn() as conn:
            row = conn.execute(
                "SELECT read_at FROM notification_events WHERE id=2").fetchone()
        assert row["read_at"] is not None

    def test_mark_read_missing_returns_false(self, db_with_events):
        assert db_with_events.mark_notification_read(999) is False

    def test_mark_read_boundary_ids(self, db_with_events):
        """边界 id：0 / 负数 / 超大均不存在，返回 False 不抛错。"""
        db = db_with_events
        assert db.mark_notification_read(0) is False
        assert db.mark_notification_read(-5) is False
        assert db.mark_notification_read(2**31) is False

    def test_mark_read_idempotent(self, db_with_events):
        """重复标记已读不报错、结果一致。"""
        db = db_with_events
        assert db.mark_notification_read(1) is True
        assert db.mark_notification_read(1) is True  # 再标一次


class TestMarkAllNotificationsRead:
    def test_mark_all_returns_count(self, db_with_events):
        db = db_with_events
        assert db.mark_all_notifications_read() == 3

    def test_mark_all_idempotent(self, db_with_events):
        db = db_with_events
        db.mark_all_notifications_read()
        assert db.mark_all_notifications_read() == 0  # 已全部已读

    def test_mark_all_empty(self, db):
        assert db.mark_all_notifications_read() == 0

    def test_mark_all_partial(self, db_with_events):
        """部分已读时只更新未读行。"""
        db = db_with_events
        db.mark_notification_read(1)
        assert db.mark_all_notifications_read() == 2  # 只更新 id 2/3


class TestCountUnreadNotifications:
    def test_count_all_unread(self, db_with_events):
        assert db_with_events.count_unread_notifications() == 3

    def test_count_after_read(self, db_with_events):
        db = db_with_events
        db.mark_notification_read(1)
        db.mark_notification_read(2)
        assert db.count_unread_notifications() == 1

    def test_count_empty(self, db):
        assert db.count_unread_notifications() == 0


# ---------- notifications API ----------

@pytest.fixture
def api_client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db, gitlab=None,
                          reconciler=None, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), db


class TestEventsApiReadField:
    """GET /api/notifications/events：列表返回 read 字段 + unread_count。"""

    def test_events_have_read_field(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        data = tc.get("/api/notifications/events").json()
        assert data["events"][0]["read"] is False
        assert data["unread_count"] == 1

    def test_events_read_reflects_mark(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        db.mark_notification_read(1)
        data = tc.get("/api/notifications/events").json()
        assert data["events"][0]["read"] is True
        assert data["unread_count"] == 0

    def test_events_empty(self, api_client):
        tc, _ = api_client
        data = tc.get("/api/notifications/events").json()
        assert data == {"events": [], "latest_id": 0, "unread_count": 0}


class TestNotificationsListApi:
    """GET /api/notifications：通知中心全量列表（最新优先）。"""

    def test_empty(self, api_client):
        tc, _ = api_client
        resp = tc.get("/api/notifications")
        assert resp.status_code == 200
        assert resp.json() == {"notifications": [], "unread_count": 0}

    def test_newest_first(self, api_client):
        tc, db = api_client
        n = Notifier(db)
        for i in range(3):
            n.record("task_succeeded", f"t{i}", "b", task_id=i + 1)
        data = tc.get("/api/notifications").json()
        assert [e["id"] for e in data["notifications"]] == [3, 2, 1]
        assert data["unread_count"] == 3
        assert all(e["read"] is False for e in data["notifications"])

    def test_limit(self, api_client):
        tc, db = api_client
        n = Notifier(db)
        for i in range(5):
            n.record("task_succeeded", f"t{i}", "b", task_id=i + 1)
        data = tc.get("/api/notifications?limit=2").json()
        assert len(data["notifications"]) == 2
        assert [e["id"] for e in data["notifications"]] == [5, 4]

    def test_limit_rejects_zero_and_huge(self, api_client):
        tc, _ = api_client
        assert tc.get("/api/notifications?limit=0").status_code == 422
        assert tc.get("/api/notifications?limit=9999").status_code == 422


class TestMarkReadApi:
    """POST /api/notifications/{id}/read。"""

    def test_mark_one_read(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        resp = tc.post("/api/notifications/1/read")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 1 and body["read"] is True
        assert tc.get("/api/notifications").json()["unread_count"] == 0

    def test_mark_missing_404(self, api_client):
        tc, _ = api_client
        assert tc.post("/api/notifications/999/read").status_code == 404

    def test_mark_read_twice_ok(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        assert tc.post("/api/notifications/1/read").status_code == 200
        assert tc.post("/api/notifications/1/read").status_code == 200

    def test_mark_invalid_id_404(self, api_client):
        tc, _ = api_client
        assert tc.post("/api/notifications/0/read").status_code == 404
        assert tc.post("/api/notifications/-1/read").status_code == 404


class TestReadAllApi:
    """POST /api/notifications/read-all。"""

    def test_read_all(self, api_client):
        tc, db = api_client
        n = Notifier(db)
        for i in range(3):
            n.record("task_succeeded", f"t{i}", "b", task_id=i + 1)
        resp = tc.post("/api/notifications/read-all")
        assert resp.status_code == 200
        assert resp.json() == {"updated": 3}
        data = tc.get("/api/notifications").json()
        assert data["unread_count"] == 0
        assert all(e["read"] for e in data["notifications"])

    def test_read_all_empty(self, api_client):
        tc, _ = api_client
        assert tc.post("/api/notifications/read-all").json() == {"updated": 0}

    def test_read_all_idempotent(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        tc.post("/api/notifications/read-all")
        assert tc.post("/api/notifications/read-all").json() == {"updated": 0}
