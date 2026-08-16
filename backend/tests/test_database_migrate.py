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


class TestMigrateDshSessionId:
    """issue #84：dsh 引擎断点续跑——tasks.dsh_session_id 列迁移（v5）。"""

    def test_old_db_gets_dsh_session_id_column(self, tmp_path):
        """旧库初始化后 tasks 表应补出 dsh_session_id 列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "dsh_session_id" in cols

    def test_new_db_has_dsh_session_id_column(self, tmp_path):
        """新库建表语句应直接含 dsh_session_id 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "dsh_session_id" in cols

    def test_set_task_status_writes_dsh_session_id(self, tmp_path):
        """dsh_session_id 进入 _TASK_FIELDS 白名单（set_task_status 可写）。"""
        db = Database(str(tmp_path / "w.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        db.set_task_status(task_id, None, dsh_session_id="dsh-sess-1")
        assert db.get_task(task_id)["dsh_session_id"] == "dsh-sess-1"

    def test_finish_task_writes_dsh_session_id(self, tmp_path):
        """finish_task 附加字段白名单同样包含 dsh_session_id。"""
        db = Database(str(tmp_path / "f.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        # finish_task 为条件终态更新（仅 running/retrying 生效），先置 running
        db.set_task_status(task_id, "running")
        assert db.finish_task(task_id, "failed", dsh_session_id="dsh-sess-2")
        assert db.get_task(task_id)["dsh_session_id"] == "dsh-sess-2"


class TestMigrateIssueLabels:
    """v6 迁移（issue #76）：tasks 新增 issue_labels / issue_updated_at。"""

    def test_old_db_gets_issue_labels_columns(self, tmp_path):
        """旧库迁移补 issue_labels / issue_updated_at 列。"""
        _build_old_db(tmp_path / "old.db")
        db = Database(str(tmp_path / "old.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "issue_labels" in cols
        assert "issue_updated_at" in cols

    def test_new_db_has_issue_labels_columns(self, tmp_path):
        """新库建表语句应直接含 issue_labels / issue_updated_at 列。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "issue_labels" in cols
        assert "issue_updated_at" in cols

    def test_create_task_stores_issue_labels(self, tmp_path):
        """create_task 写入 issue_labels（JSON 数组）与 issue_updated_at。"""
        db = Database(str(tmp_path / "w.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(
            repo_id, 42, 7, "标题", issue_labels=["bug", "ui"],
            issue_updated_at="2026-08-14 08:00:00")
        task = db.get_task(task_id)
        assert task["issue_labels"] == '["bug", "ui"]'
        assert task["issue_updated_at"] == "2026-08-14 08:00:00"

    def test_create_task_defaults_labels_empty(self, tmp_path):
        """不传 issue_labels 时写空数组（不是 NULL），调度排序统一处理。"""
        db = Database(str(tmp_path / "d.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        task = db.get_task(task_id)
        assert task["issue_labels"] == "[]"
        assert task["issue_updated_at"] == ""


class TestNormalizeIssueUpdatedAt:
    """GitLab issue 更新时间归一化（issue #76）。"""

    def test_iso_with_timezone_normalized_to_utc(self):
        from botler.database import normalize_issue_updated_at
        # GitLab API 返回 +08:00 时间 → 转 UTC
        assert normalize_issue_updated_at("2026-08-14T08:00:00.000+08:00") \
            == "2026-08-14 00:00:00"
        assert normalize_issue_updated_at("2026-08-14T08:00:00+08:00") \
            == "2026-08-14 00:00:00"

    def test_utc_z_suffix(self):
        from botler.database import normalize_issue_updated_at
        assert normalize_issue_updated_at("2026-08-14T08:00:00Z") \
            == "2026-08-14 08:00:00"

    def test_naive_treated_as_utc(self):
        from botler.database import normalize_issue_updated_at
        assert normalize_issue_updated_at("2026-08-14 08:00:00") \
            == "2026-08-14 08:00:00"

    def test_empty_and_invalid_return_empty(self):
        from botler.database import normalize_issue_updated_at
        assert normalize_issue_updated_at(None) == ""
        assert normalize_issue_updated_at("") == ""
        assert normalize_issue_updated_at("not-a-time") == ""
        assert normalize_issue_updated_at("2026-13-45T99:99:99Z") == ""


# ---- issue #120：tasks.engine（执行引擎按任务落库）----

V6_SCHEMA = """
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
  claude_session_id TEXT,
  hermes_history TEXT,
  commit_sha TEXT,
  dsh_session_id TEXT,
  issue_labels TEXT DEFAULT '[]',
  issue_updated_at TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _build_v6_db(path) -> None:
    """手工构造 v6 旧库（含断点续跑会话字段、无 engine 列）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(V6_SCHEMA)
    conn.execute(
        """INSERT INTO tasks (repo_id, project_id, issue_iid, issue_title, status,
                              claude_session_id, hermes_history, dsh_session_id)
           VALUES (1, 42, 1, 'claude任务', 'succeeded', 'claude-sess-1', NULL, NULL),
                  (1, 42, 2, 'hermes任务', 'succeeded', NULL, '{"session_id":"h1"}', NULL),
                  (1, 42, 3, 'dsh任务', 'succeeded', NULL, NULL, 'dsh-sess-1'),
                  (1, 42, 4, '混合任务', 'succeeded', 'claude-old', NULL, 'dsh-sess-2'),
                  (1, 42, 5, '无会话任务', 'failed', NULL, NULL, NULL)""")
    conn.commit()
    conn.close()


class TestMigrateTaskEngine:
    """v7 迁移（issue #120）：tasks 新增 engine 列并回填历史执行引擎。

    概览页 issue 右边栏「执行引擎」行此前读取全局 worker.engine（issue
    #118），全局引擎切换后所有 issue 都显示新引擎；修复后按任务落库的
    engine 展示。本测试覆盖旧库补列、新库建列与存量回填。
    """

    def test_old_db_gets_engine_column(self, tmp_path):
        """旧库初始化后补出 engine 列。"""
        path = tmp_path / "old.db"
        _build_v6_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "engine" in cols, "旧库应补出 engine 列"

    def test_new_db_has_engine_column(self, tmp_path):
        """新库建表语句应直接含 engine 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "engine" in cols

    def test_legacy_rows_backfilled_by_session_fields(self, tmp_path):
        """迁移回填：按断点续跑会话字段推断历史执行引擎。"""
        path = tmp_path / "old.db"
        _build_v6_db(path)
        db = Database(str(path))
        by_iid = {r["issue_iid"]: r for r in db.list_tasks(limit=100)}
        assert by_iid[1]["engine"] == "claude", "claude_session_id → claude"
        assert by_iid[2]["engine"] == "hermes", "hermes_history → hermes"
        assert by_iid[3]["engine"] == "dsh", "dsh_session_id → dsh"
        # 多会话字段并存 → dsh 优先（同一任务实际只可能跑过一种引擎）
        assert by_iid[4]["engine"] == "dsh"
        # 无任何会话字段 → 保持空串（前端回退全局引擎展示）
        assert by_iid[5]["engine"] == ""

    def test_set_task_status_accepts_engine(self, tmp_path):
        """set_task_status 支持写入 engine（执行器按任务落库）。"""
        db = Database(str(tmp_path / "e.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        db.set_task_status(task_id, None, engine="dsh")
        assert db.get_task(task_id)["engine"] == "dsh"

    def test_finish_task_accepts_engine(self, tmp_path):
        """finish_task 条件更新也支持 engine 字段（白名单一致）。"""
        db = Database(str(tmp_path / "f.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        db.set_task_status(task_id, "running")
        assert db.finish_task(task_id, "succeeded", engine="hermes")
        assert db.get_task(task_id)["engine"] == "hermes"
