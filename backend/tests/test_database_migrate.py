"""数据库迁移测试：旧库补新增列（issue #19 新增 tasks.commit_sha）。

CREATE TABLE IF NOT EXISTS 不会更新已存在的表，_migrate 必须给旧库
补上新增列，否则线上库（部署多时的 botler.db）任务成功落库 commit_sha
会报 no such column。
"""

import sqlite3

from botler.database import Database

OLD_SCHEMA = """
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL,
  project_id INTEGER NOT NULL,
  issue_iid INTEGER NOT NULL,
  issue_title TEXT,
  status TEXT NOT NULL,
  attempt_count INTEGER DEFAULT 0,
  triggered_by TEXT,
  exit_code INTEGER,
  error_message TEXT,
  error_detail TEXT,
  log_path TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _build_old_db(path) -> None:
    """手工构造无 commit_sha 列的旧库（模拟线上部署中的历史库）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()


class TestMigrateCommitSha:
    def test_old_db_gets_commit_sha_column(self, tmp_path):
        """旧库初始化后 tasks 表应补出 commit_sha 列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "commit_sha" in cols

    def test_new_db_has_commit_sha_column(self, tmp_path):
        """新库建表语句应直接含 commit_sha 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "commit_sha" in cols

    def test_set_task_status_accepts_commit_sha(self, tmp_path):
        """set_task_status 应支持写入 commit_sha（成功路径落库）。"""
        db = Database(str(tmp_path / "mig.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 1, "任务")
        db.set_task_status(task_id, "succeeded", commit_sha="deadbeef00")
        assert db.get_task(task_id)["commit_sha"] == "deadbeef00"
