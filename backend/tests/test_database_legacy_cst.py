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
    """迁移完成后 user_version 应为 7（v2 CST 修正 + v3 仓库优先级列 + v4 deleted_at
    + v5 dsh_session_id + v6 issue_labels/issue_updated_at + v7 engine）。"""
    db = Database(str(tmp_path / "ver.db"))
    with db._conn() as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 7
