"""存量 CST 时间戳数据迁移测试（issue #49 第二轮）。

bug 现象：任务 #65 用时显示 8 小时（实际 9 分 29 秒）——created_at 由
SQLite datetime('now') 写 UTC 串（'2026-08-13 06:38:41'），而 finished_at
为 550e04f（issue #42）部署前旧版 executor 按容器本地 CST 写入的串
（'2026-08-13 14:48:10'，无时区后缀）；前端统一按 UTC 解析，终点偏移
+8 小时，时长虚增 8 小时。

修复约定：启动迁移以 task_logs（ts 恒为 datetime('now') UTC）为参照，
把存量 CST 串修正为 UTC 串：
- H_UTC 优先：串按 UTC 解析后与任一日志 ts 差 ≤ 10 分钟 → 已是 UTC，不动；
- 否则 H_CST：解析结果减 8 小时与任一日志 ts 差 ≤ 10 分钟 → CST 串，
  改写为减 8 小时后的 UTC 串；
- 均不命中（无日志等）→ 保守不动。
"""

import sqlite3

from botler.database import Database

# 任务 #65 真实数据：UTC 串与 CST 串并存
CREATED = "2026-08-13 06:38:41"       # UTC = 本地 14:38:41
STARTED_CST = "2026-08-13 14:46:53"   # CST 串 = UTC 06:46:53
FINISHED_CST = "2026-08-13 14:48:10"  # CST 串 = UTC 06:48:10（与打 bot-done 日志 ts 一致）
STARTED_UTC = "2026-08-13 06:46:53"
FINISHED_UTC = "2026-08-13 06:48:10"


def _build(path, task_factory) -> Database:
    """建库（跑完迁移 ver=2），插入待迁移数据与日志，回退 user_version 到 1。

    返回可重新实例化触发迁移的 Database 句柄（本身不用，仅为统一结构）。
    """
    db = Database(str(path))
    with db._conn() as conn:
        task_factory(db, conn)
        # 回退迁移版本：模拟「已建库/补列（v1）但未做 CST 修正」的历史库
        conn.execute("PRAGMA user_version = 1")
    return db


def _insert_task(db, conn, *, status="succeeded", started=None, finished=None,
                 log_ts=None, issue_iid=1):
    repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    task_id = db.create_task(repo_id, 42, issue_iid, "任务")
    db.set_task_status(task_id, status, started_at=started, finished_at=finished)
    for ts in log_ts or []:
        conn.execute(
            "INSERT INTO task_logs (task_id, ts, level, message) VALUES (?, ?, 'info', 'x')",
            (task_id, ts))
    return task_id


def test_cst_fields_fixed_to_utc(tmp_path):
    """任务 #65 场景：CST 存量串按日志参照修正为 UTC 串。"""
    def factory(db, conn):
        _insert_task(db, conn, started=STARTED_CST, finished=FINISHED_CST,
                     log_ts=["2026-08-13 06:38:42", "2026-08-13 06:46:53",
                             "2026-08-13 06:48:10"])
    db = _build(tmp_path / "cst.db", factory)
    db = Database(str(tmp_path / "cst.db"))  # 重新实例化触发迁移
    task = db.get_task(1)
    assert task["started_at"] == STARTED_UTC
    assert task["finished_at"] == FINISHED_UTC


def test_utc_fields_unchanged(tmp_path):
    """550e04f 后的 UTC 数据不受迁移影响。"""
    def factory(db, conn):
        _insert_task(db, conn, started=STARTED_UTC, finished=FINISHED_UTC,
                     log_ts=["2026-08-13 06:46:53", "2026-08-13 06:48:10"])
    db = _build(tmp_path / "utc.db", factory)
    db = Database(str(tmp_path / "utc.db"))
    task = db.get_task(1)
    assert task["started_at"] == STARTED_UTC
    assert task["finished_at"] == FINISHED_UTC


def test_stop_active_tasks_utc_unchanged(tmp_path):
    """一键停止（stop_active_tasks）用 datetime('now') 写 UTC finished_at，不应被修正。"""
    def factory(db, conn):
        _insert_task(db, conn, status="interrupted", started=None,
                     finished="2026-08-13 03:30:27",
                     log_ts=["2026-08-13 03:30:27"])
    db = _build(tmp_path / "stop.db", factory)
    db = Database(str(tmp_path / "stop.db"))
    task = db.get_task(1)
    assert task["finished_at"] == "2026-08-13 03:30:27"


def test_utc_after_long_queue_not_shifted(tmp_path):
    """排队 8 小时才执行的 UTC 数据不误判：H_UTC 优先于 H_CST。

    首条日志（入队时刻 08:00）恰在 t-8h 附近，若先判 H_CST 会把 UTC 串
    误减 8 小时；正确实现应先命中「尝试开始」日志（16:05 附近）判 UTC。
    """
    def factory(db, conn):
        _insert_task(db, conn, started="2026-08-13 16:05:00",
                     finished="2026-08-13 16:35:00",
                     log_ts=["2026-08-13 08:00:01", "2026-08-13 16:05:01",
                             "2026-08-13 16:35:01"])
    db = _build(tmp_path / "queue.db", factory)
    db = Database(str(tmp_path / "queue.db"))
    task = db.get_task(1)
    assert task["started_at"] == "2026-08-13 16:05:00"
    assert task["finished_at"] == "2026-08-13 16:35:00"


def test_no_logs_untouched(tmp_path):
    """无日志参照的 CST 串保守不动（不猜测）。"""
    def factory(db, conn):
        _insert_task(db, conn, started=STARTED_CST, finished=FINISHED_CST)
    db = _build(tmp_path / "nolog.db", factory)
    db = Database(str(tmp_path / "nolog.db"))
    task = db.get_task(1)
    assert task["started_at"] == STARTED_CST
    assert task["finished_at"] == FINISHED_CST


def test_migration_idempotent(tmp_path):
    """修正后重复执行迁移不再变化（幂等）。"""
    def factory(db, conn):
        _insert_task(db, conn, started=STARTED_CST, finished=FINISHED_CST,
                     log_ts=["2026-08-13 06:46:53", "2026-08-13 06:48:10"])
    db = _build(tmp_path / "idem.db", factory)
    db = Database(str(tmp_path / "idem.db"))
    db = Database(str(tmp_path / "idem.db"))
    task = db.get_task(1)
    assert task["started_at"] == STARTED_UTC
    assert task["finished_at"] == FINISHED_UTC


def test_user_version_marker(tmp_path):
    """迁移完成后 user_version 应为 16（v2 CST 修正 + v3 仓库优先级列 + v4 deleted_at
    + v5 dsh_session_id + v6 issue_labels/issue_updated_at + v7 engine
    + v8 inspirations 灵感表 issue #131 + v9 dsh_transcript issue #146
    + v10 repos.remote_username 仓库用户列 issue #153
    + v11 inspiration_messages 灵感 AI 对话消息表 issue #166
    + v12 tasks.issue_created_at 同权重按 issue 创建时间排序 issue #234
    + v13 tasks.environment 任务执行环境快照列 issue #276
    + v14 task_progress 任务进度账本表 issue #281
    + v15 repos.logo_* 仓库 logo 列 issue #188
    + v16 task_usage 任务 token 用量表 issue #235
    + v17 tasks.base_sha 任务改动基线提交 issue #252
    + v22 tasks.engine_fallback 引擎降级原因列 issue #236
    + v23 repos 仓库级任务参数覆盖列 issue #237
    + v24 tasks.precheck_result 任务执行前预检结果列 issue #238）。"""
    db = Database(str(tmp_path / "ver.db"))
    with db._conn() as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 24


def test_legacy_db_gets_remote_username_column(tmp_path):
    """旧库（v9 及更早）启动迁移后 repos 表补 remote_username 列（issue #153）。

    手工构造 v9 时代（repos 表无 remote_username 列）的历史库，设置
    user_version=9，重新实例化 Database 触发 v10 迁移补列；补列后新列
    可正常写入读取。
    """
    path = str(tmp_path / "legacy-remote-user.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE repos (
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
           PRAGMA user_version = 9;""")
    conn.close()

    db = Database(path)
    with db._conn() as conn2:
        cols = {r["name"] for r in conn2.execute("PRAGMA table_info(repos)")}
        assert "remote_username" in cols
        ver = conn2.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 24  # v9 旧库迁移应推进到最新版本（v24：tasks.precheck_result 预检结果列，issue #238）
    # 新列可正常写入读取
    repo_id = db.upsert_repo(
        42, "demo", "https://gitlab.example.com/group/demo.git",
        remote_username="agent")
    assert db.get_repo(repo_id)["remote_username"] == "agent"
