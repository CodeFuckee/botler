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


OLD_REPOS_SCHEMA = """
CREATE TABLE repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gitlab_project_id INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  local_path TEXT,
  remote_name TEXT,
  prompt_template TEXT,
  enabled INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _build_old_repos_db(path) -> None:
    """手工构造无 priority 列的 repos 旧库（issue #51 迁移前）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_REPOS_SCHEMA)
    conn.execute(
        "INSERT INTO repos (gitlab_project_id, name, url) VALUES (42, '旧仓库', 'https://x/old.git')")
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


class TestMigrateRepoPriority:
    """repos 表 priority 列迁移（issue #51）。"""

    def test_old_db_gets_priority_column_default_100(self, tmp_path):
        """旧库初始化后 repos 表补出 priority 列，存量行默认 100。"""
        path = tmp_path / "old.db"
        _build_old_repos_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "priority" in cols, "旧库应补出 priority 列"
        row = db.get_repo_by_project_id(42)
        assert row["priority"] == 100, "存量仓库优先级应默认 100"

    def test_new_db_has_priority_column(self, tmp_path):
        """新库建表语句应直接含 priority 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "priority" in cols

    def test_upsert_repo_accepts_priority(self, tmp_path):
        """upsert_repo 应支持 priority 参数（添加仓库带优先级）。"""
        db = Database(str(tmp_path / "p.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git",
                                 priority=25)
        assert db.get_repo(repo_id)["priority"] == 25

    def test_update_repo_accepts_priority(self, tmp_path):
        """update_repo 应支持更新 priority（设置弹窗保存）。"""
        db = Database(str(tmp_path / "u.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.update_repo(repo_id, priority=77)
        assert db.get_repo(repo_id)["priority"] == 77


class TestMigrateRepoDeletedAt:
    """repos 表 deleted_at 列迁移与软删除行为（issue #62）。"""

    def test_old_db_gets_deleted_at_column(self, tmp_path):
        """旧库初始化后 repos 表补出 deleted_at 列（线上库迁移）。"""
        path = tmp_path / "old.db"
        _build_old_repos_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "deleted_at" in cols, "旧库应补出 deleted_at 列"

    def test_new_db_has_deleted_at_column(self, tmp_path):
        """新库建表语句应直接含 deleted_at 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "deleted_at" in cols

    def test_soft_delete_filters_list_by_default(self, tmp_path):
        """软删除后 list_repos 默认不返回；include_deleted=True 可见。"""
        db = Database(str(tmp_path / "d.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.soft_delete_repo(repo_id)
        assert db.list_repos() == [], "默认应过滤已软删除的仓库"
        rows = db.list_repos(include_deleted=True)
        assert [r["id"] for r in rows] == [repo_id]

    def test_soft_delete_marks_deleted_at_and_disables(self, tmp_path):
        """soft_delete_repo 写 deleted_at 并置 enabled=0。"""
        db = Database(str(tmp_path / "s.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.soft_delete_repo(repo_id)
        row = db.get_repo(repo_id)
        assert row["deleted_at"] is not None
        assert not row["enabled"]

    def test_get_by_project_id_hides_deleted_by_default(self, tmp_path):
        """get_repo_by_project_id 默认查不到已删除行（可重新添加，不误报 409）。"""
        db = Database(str(tmp_path / "g.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.soft_delete_repo(repo_id)
        assert db.get_repo_by_project_id(42) is None
        assert db.get_repo_by_project_id(42, include_deleted=True)["id"] == repo_id

    def test_readd_clears_deleted_mark(self, tmp_path):
        """重新 upsert 同 project_id 仓库清除删除标记。"""
        db = Database(str(tmp_path / "r.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.soft_delete_repo(repo_id)
        readded = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        assert readded == repo_id
        row = db.get_repo(repo_id)
        assert row["deleted_at"] is None
        assert row["enabled"]
