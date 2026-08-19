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


class TestMigrateIssueCreatedAt:
    """tasks 表 issue_created_at 列迁移（issue #234：同权重按 issue 创建时间排序派发）。"""

    def test_old_db_gets_issue_created_at_column(self, tmp_path):
        """旧库初始化后 tasks 表应补出 issue_created_at 列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "issue_created_at" in cols, "旧库应补出 issue_created_at 列"

    def test_new_db_has_issue_created_at_column(self, tmp_path):
        """新库建表语句应直接含 issue_created_at 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "issue_created_at" in cols

    def test_create_task_writes_issue_created_at(self, tmp_path):
        """create_task 写入 issue_created_at（入队时记录 issue 创建时间）。"""
        db = Database(str(tmp_path / "c.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(
            repo_id, 42, 1, "任务",
            issue_labels=["bug"],
            issue_created_at="2026-08-14 08:00:00",
            issue_updated_at="2026-08-14 09:00:00")
        task = db.get_task(task_id)
        assert task["issue_created_at"] == "2026-08-14 08:00:00"
        assert task["issue_updated_at"] == "2026-08-14 09:00:00"

    def test_create_task_defaults_issue_created_at_empty(self, tmp_path):
        """create_task 未传 issue_created_at 时默认为空串。"""
        db = Database(str(tmp_path / "d.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 1, "任务")
        assert db.get_task(task_id)["issue_created_at"] == ""

    def test_normalize_issue_created_at(self):
        """normalize_issue_created_at：ISO8601（含时区）→ UTC 无后缀串。"""
        from botler.database import normalize_issue_created_at
        assert normalize_issue_created_at("2026-08-14T08:00:00.000+08:00") == "2026-08-14 00:00:00"
        assert normalize_issue_created_at("2026-08-14T08:00:00Z") == "2026-08-14 08:00:00"
        assert normalize_issue_created_at("2026-08-14 08:00:00") == "2026-08-14 08:00:00"
        assert normalize_issue_created_at(None) == ""


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


# ---- issue #131：inspirations 灵感表（v8 迁移）----

V7_SCHEMA = """
CREATE TABLE repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gitlab_project_id INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  local_path TEXT,
  remote_name TEXT,
  prompt_template TEXT,
  enabled INTEGER DEFAULT 1,
  priority INTEGER DEFAULT 100,
  deleted_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
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
  engine TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _build_v7_db(path) -> None:
    """手工构造 v7 旧库（无 inspirations 表，模拟 issue #131 前的线上库）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(V7_SCHEMA)
    conn.execute(
        """INSERT INTO repos (gitlab_project_id, name, url)
           VALUES (42, 'botler', 'https://x/botler.git')""")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()


class TestMigrateInspirations:
    """v8 迁移（issue #131）：inspirations 灵感表。"""

    def test_old_db_gets_inspirations_table(self, tmp_path):
        """旧库初始化后应补出 inspirations 表并推进版本号。"""
        path = tmp_path / "old.db"
        _build_v7_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            # issue #153：v10 迁移补 remote_username 列（仓库用户）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "inspirations" in tables, "旧库应补出 inspirations 表"
        assert "inspiration_messages" in tables, "旧库应补出灵感 AI 对话消息表（issue #166）"
        assert ver == 21, f"user_version 应推进到 21（v16 task_usage + v17 base_sha + v18 failure_category + v19 issue_manual_orders + v20 tools/tool_meta + v21 manual_priority，issue #242），实际 {ver}"
        assert "remote_username" in cols, "旧库应补出 remote_username 列"

    def test_new_db_has_inspirations_table(self, tmp_path):
        """新库建表语句应直接含 inspirations 表（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "inspirations" in tables

    def test_inspiration_crud(self, tmp_path):
        """灵感 CRUD：创建 / 列表（带仓库名）/ 更新刷新 updated_at / 删除。"""
        db = Database(str(tmp_path / "c.db"))
        repo_id = db.upsert_repo(42, "botler", "https://x/botler.git")
        insp_id = db.create_inspiration(repo_id, "支持批量处理 issue")
        rows = db.list_inspirations()
        assert len(rows) == 1
        assert rows[0]["repo_name"] == "botler"
        assert rows[0]["content"] == "支持批量处理 issue"
        got = db.get_inspiration(insp_id)
        assert got["id"] == insp_id
        assert db.update_inspiration(insp_id, "更新后的内容") is True
        assert db.get_inspiration(insp_id)["content"] == "更新后的内容"
        assert db.list_inspirations()[0]["updated_at"] >= got["created_at"]
        # 过滤 repo_id
        assert len(db.list_inspirations(repo_id=repo_id)) == 1
        assert len(db.list_inspirations(repo_id=999)) == 0
        assert db.delete_inspiration(insp_id) is True
        assert db.get_inspiration(insp_id) is None
        assert db.delete_inspiration(insp_id) is False

    def test_inspiration_list_sorted_by_updated_at_desc(self, tmp_path):
        """list_inspirations 按 updated_at 降序（最新改动在前）。"""
        import time
        db = Database(str(tmp_path / "s.db"))
        repo_id = db.upsert_repo(42, "botler", "https://x/botler.git")
        id1 = db.create_inspiration(repo_id, "第一条")
        id2 = db.create_inspiration(repo_id, "第二条")
        time.sleep(1.1)
        db.update_inspiration(id1, "第一条（更新）")
        assert [r["id"] for r in db.list_inspirations()] == [id1, id2]


class TestMigrateDshTranscript:
    """issue #146：dsh 引擎提示词/聊天记录——tasks.dsh_transcript 列迁移（v9）。"""

    def test_old_db_gets_dsh_transcript_column(self, tmp_path):
        """最老旧库（无任何新增列）初始化后应依次迁移补出 dsh_transcript 列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "dsh_transcript" in cols

    def test_new_db_has_dsh_transcript_column(self, tmp_path):
        """新库建表语句应直接含 dsh_transcript 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "dsh_transcript" in cols

    def test_set_task_status_writes_dsh_transcript(self, tmp_path):
        """dsh_transcript 进入 _TASK_FIELDS 白名单（executor 落库可用）。"""
        db = Database(str(tmp_path / "w.db"))
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        task_id = db.create_task(repo_id, 42, 7, "标题")
        raw = '{"prompt": "提示词", "messages": [], "truncated": false}'
        db.set_task_status(task_id, None, dsh_transcript=raw)
        assert db.get_task(task_id)["dsh_transcript"] == raw


class TestMigrateInspirationMessages:
    """issue #166：灵感 AI 对话消息表迁移（v11）。"""

    def test_old_db_gets_inspiration_messages_table(self, tmp_path):
        """v10 旧库初始化后应补出 inspiration_messages 表与索引。"""
        path = tmp_path / "old.db"
        _build_v7_db(path)  # 复用最旧库：走完整迁移链到 v11
        db = Database(str(path))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert "inspiration_messages" in tables, "旧库应补出灵感对话消息表"
        assert "idx_inspiration_messages_insp" in indexes, "旧库应补出消息索引"
        assert ver == 21, f"user_version 应推进到 21（v16 task_usage + v17 base_sha + v18 failure_category + v19 issue_manual_orders + v20 tools/tool_meta + v21 manual_priority，issue #242），实际 {ver}"

    def test_new_db_has_inspiration_messages_table(self, tmp_path):
        """新库建表语句应直接含 inspiration_messages 表（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "inspiration_messages" in tables

    def test_message_crud_ordered(self, tmp_path):
        """对话消息 CRUD：写入 / 时间序读取 / limit 取最近 N 条 / 单条删除。"""
        db = Database(str(tmp_path / "m.db"))
        repo_id = db.upsert_repo(42, "botler", "https://x/botler.git")
        insp_id = db.create_inspiration(repo_id, "灵感")
        db.add_inspiration_message(insp_id, "user", "第一问")
        m2 = db.add_inspiration_message(insp_id, "assistant", "第一答")
        db.add_inspiration_message(insp_id, "user", "第二问")
        rows = db.list_inspiration_messages(insp_id)
        assert [r["content"] for r in rows] == ["第一问", "第一答", "第二问"]
        assert [r["role"] for r in rows] == ["user", "assistant", "user"]
        # limit：取最近 2 条并按时间升序返回
        tail = db.list_inspiration_messages(insp_id, limit=2)
        assert [r["content"] for r in tail] == ["第一答", "第二问"]
        # 单条查询与删除
        assert db.get_inspiration_message(m2)["content"] == "第一答"
        assert db.delete_inspiration_message(m2) is True
        assert db.get_inspiration_message(m2) is None
        assert [r["content"] for r in db.list_inspiration_messages(insp_id)] == [
            "第一问", "第二问"]

    def test_delete_inspiration_cascades_messages(self, tmp_path):
        """删除灵感时级联删除其全部对话消息。"""
        db = Database(str(tmp_path / "c.db"))
        repo_id = db.upsert_repo(42, "botler", "https://x/botler.git")
        insp_id = db.create_inspiration(repo_id, "灵感")
        db.add_inspiration_message(insp_id, "user", "提问")
        db.add_inspiration_message(insp_id, "assistant", "回复")
        assert db.delete_inspiration(insp_id) is True
        assert db.list_inspiration_messages(insp_id) == []

    def test_cascade_only_target_inspiration(self, tmp_path):
        """级联删除只清理目标灵感的消息，不影响其他灵感。"""
        db = Database(str(tmp_path / "c2.db"))
        repo_id = db.upsert_repo(42, "botler", "https://x/botler.git")
        insp_a = db.create_inspiration(repo_id, "灵感 A")
        insp_b = db.create_inspiration(repo_id, "灵感 B")
        db.add_inspiration_message(insp_a, "user", "A 提问")
        db.add_inspiration_message(insp_b, "user", "B 提问")
        db.delete_inspiration(insp_b)
        assert [r["content"] for r in db.list_inspiration_messages(insp_a)] == ["A 提问"]


class TestMigrateEnvironment:
    """v13 迁移（issue #276）：tasks.environment 任务执行环境快照列。"""

    def test_old_db_gets_environment_column(self, tmp_path):
        """旧库（无 environment 列）初始化后应补出该列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)  # 复用最早无 commit_sha 的旧库：走完整迁移链
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert "environment" in cols, "旧库应补出 tasks.environment 列"
        assert ver == 21, f"user_version 应推进到 21（v16 task_usage + v17 base_sha + v18 failure_category + v19 issue_manual_orders + v20 tools/tool_meta + v21 manual_priority，issue #242），实际 {ver}"

    def test_new_db_has_environment_column(self, tmp_path):
        """新库建表语句应直接含 environment 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert "environment" in cols
        assert ver == 21

    def test_set_task_status_accepts_environment(self, tmp_path):
        """set_task_status 应能写入 environment（_TASK_FIELDS 白名单）。"""
        db = Database(str(tmp_path / "new.db"))
        repo_id = db.upsert_repo(1, "demo", "https://x/demo.git")
        task_id = db.create_task(repo_id, 1, 5, "任务")
        assert task_id is not None
        db.set_task_status(task_id, None, environment='{"engine": {"name": "claude"}}')
        row = db.get_task(task_id)
        assert row["environment"] == '{"engine": {"name": "claude"}}'

    def test_finish_task_accepts_environment(self, tmp_path):
        """finish_task 应能写入 environment（终态收尾不丢快照）。"""
        db = Database(str(tmp_path / "new.db"))
        repo_id = db.upsert_repo(1, "demo", "https://x/demo.git")
        task_id = db.create_task(repo_id, 1, 6, "任务")
        assert task_id is not None
        db.set_task_status(task_id, None,
                           environment='{"git": {"branch": "main"}}')
        assert db.claim_task(task_id)  # finish_task 仅 running/retrying 状态生效
        ok = db.finish_task(task_id, "succeeded", exit_code=0)
        assert ok
        row = db.get_task(task_id)
        assert row["status"] == "succeeded"
        assert row["environment"] == '{"git": {"branch": "main"}}'


class TestTaskProgressLedger:
    """issue #281 §4.1：任务进度账本表 task_progress（表 + 迁移 + 快照语义）。"""

    @staticmethod
    def _task(db) -> int:
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        return db.create_task(repo_id, 42, 7, "标题")

    def test_new_db_has_task_progress_table(self, tmp_path):
        """新库建表语句应直接含 task_progress 表与索引（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "task_progress" in tables
        assert "idx_task_progress_task" in indexes

    def test_old_db_gets_task_progress_table(self, tmp_path):
        """旧库初始化后应补出 task_progress 表并推进版本号。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert "task_progress" in tables, "旧库应补出 task_progress 表"
        assert ver == 21

    def test_record_and_latest_per_step(self, tmp_path):
        """record/list/latest：只增不改快照式，latest 取每步最新状态。"""
        db = Database(str(tmp_path / "w.db"))
        task_id = self._task(db)
        db.record_task_progress(task_id, 1, "定位根因", "pending")
        db.record_task_progress(task_id, 1, "定位根因", "done", evidence="pytest 通过")
        db.record_task_progress(task_id, 2, "补充边界测试", "pending")
        rows = db.list_task_progress(task_id)
        assert len(rows) == 3  # 快照式：历史行保留
        latest = db.latest_task_progress(task_id)
        assert len(latest) == 2  # 每步最新一行
        assert latest[0]["step_no"] == 1
        assert latest[0]["status"] == "done"  # step1 最新状态覆盖旧快照
        assert latest[0]["evidence"] == "pytest 通过"
        assert latest[1]["step_no"] == 2
        assert latest[1]["status"] == "pending"

    def test_task_progress_scoped_per_task(self, tmp_path):
        """账本按任务隔离（不同 task 互不串扰）。"""
        db = Database(str(tmp_path / "w.db"))
        t1 = self._task(db)
        db.upsert_repo(43, "demo2", "https://gitlab.example.com/group/demo2.git")
        t2 = db.create_task(db.get_repo_by_project_id(43)["id"], 43, 8, "标题2")
        db.record_task_progress(t1, 1, "步骤A", "done")
        db.record_task_progress(t2, 1, "步骤B", "pending")
        assert len(db.latest_task_progress(t1)) == 1
        assert db.latest_task_progress(t1)[0]["step_desc"] == "步骤A"
        assert db.latest_task_progress(t2)[0]["step_desc"] == "步骤B"


class TestMigrateRepoLogo:
    """repos 表 logo 三列迁移（issue #188：「生成图标」生成的 logo 元信息）。"""

    def test_old_db_gets_logo_columns(self, tmp_path):
        """旧库初始化后 repos 表补出 logo_path / logo_updated_at / logo_mime。"""
        path = tmp_path / "old.db"
        _build_old_repos_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "logo_path" in cols, "旧库应补出 logo_path 列"
        assert "logo_updated_at" in cols, "旧库应补出 logo_updated_at 列"
        assert "logo_mime" in cols, "旧库应补出 logo_mime 列"
        # 存量行默认值为 NULL（未生成 logo）
        assert db.get_repo_by_project_id(42)["logo_path"] is None

    def test_new_db_has_logo_columns(self, tmp_path):
        """新库建表语句应直接含 logo 三列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "logo_path" in cols
        assert "logo_updated_at" in cols
        assert "logo_mime" in cols

    def test_update_repo_accepts_logo_fields(self, tmp_path):
        """update_repo 应支持写入 logo 元信息（生成成功后落库）。"""
        db = Database(str(tmp_path / "u.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        db.update_repo(repo_id, logo_path="42.png",
                       logo_updated_at="2026-08-18 10:00:00",
                       logo_mime="image/png")
        row = db.get_repo(repo_id)
        assert row["logo_path"] == "42.png"
        assert row["logo_updated_at"] == "2026-08-18 10:00:00"
        assert row["logo_mime"] == "image/png"


class TestMigrateTaskBaseSha:
    """tasks.base_sha 迁移（issue #252：任务改动基线提交，结构化报告 diff 采集）。"""

    def test_old_db_gets_base_sha_column(self, tmp_path):
        """旧库初始化后 tasks 表补出 base_sha 列。"""
        path = tmp_path / "old.db"
        _build_old_db(path)
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "base_sha" in cols, "旧库应补出 base_sha 列"

    def test_new_db_has_base_sha_column(self, tmp_path):
        """新库建表语句应直接含 base_sha 列（无需迁移）。"""
        db = Database(str(tmp_path / "new.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "base_sha" in cols

    def test_set_task_status_accepts_base_sha(self, tmp_path):
        """set_task_status 白名单应支持写入 base_sha。"""
        db = Database(str(tmp_path / "u.db"))
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 1, "标题")
        db.set_task_status(task_id, None, base_sha="a" * 40)
        assert db.get_task(task_id)["base_sha"] == "a" * 40


class TestMigrateFailureCategory:
    """issue #274：旧库迁移补 tasks.failure_category 列（失败原因分类落库）。"""

    def test_old_db_gets_failure_category_column(self, tmp_path):
        """旧库（user_version=17，无 failure_category 列）初始化后应补出该列。"""
        path = tmp_path / "old274.db"
        _build_old_db(path)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 17")
        conn.commit()
        conn.close()
        db = Database(str(path))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert "failure_category" in cols
        assert ver >= 19

    def test_new_db_has_failure_category_column(self, tmp_path):
        """新库建表语句应直接含 failure_category 列（无需迁移）。"""
        db = Database(str(tmp_path / "new274.db"))
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "failure_category" in cols

    def test_finish_task_writes_failure_category(self, tmp_path):
        """finish_task 应支持写入 failure_category（白名单内）。"""
        db = Database(str(tmp_path / "finish274.db"))
        db.upsert_repo(274, "demo274", "https://x/demo274.git")
        rid = db.get_repo_by_project_id(274)["id"]
        tid = db.create_task(rid, 274, 274, "任务")
        assert db.claim_task(tid)
        assert db.finish_task(tid, "failed",
                              error_message="任务超时", failure_category="env")
        assert db.get_task(tid)["failure_category"] == "env"
