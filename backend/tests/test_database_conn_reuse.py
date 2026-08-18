"""SQLite 连接复用与并发写测试（issue #191）。

背景：`_conn()` 每次调用都 `sqlite3.connect` + `PRAGMA journal_mode=WAL`
+ row_factory 设置，高频路径（任务列表/日志写入/通知拉取）每秒都会
打开/关闭连接。

验收标准对应：
- 高频路径连接复用生效，无重复 PRAGMA（同线程多次读写只 connect 一次）；
- 并发写无 database is locked 报错（多线程压测模拟）；
- 全量测试通过。
"""

import sqlite3
import threading

import pytest

from botler.database import Database


def test_same_thread_reuses_single_connection(tmp_path, monkeypatch):
    """同一线程多次读写复用同一条连接，且只 connect 一次（无重复 PRAGMA）。"""
    connect_calls: list = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connect_calls.append(conn)
        return conn

    # 统计本测试期间 botler.database 触发的 sqlite3.connect 次数
    monkeypatch.setattr("botler.database.sqlite3.connect", counting_connect)

    db = Database(str(tmp_path / "reuse.db"))  # __init__ 建库触发第 1 次 connect
    assert len(connect_calls) == 1

    # 高频路径模拟：任务列表 / 日志写入 / 仓库读写交替执行
    repo_id = None
    for i in range(20):
        repo_id = db.upsert_repo(i, f"repo{i}", "https://example.com/r.git")
        db.list_repos()
        db.get_repo(repo_id)
        task_id = db.create_task(repo_id, i, i, f"任务 {i}")
        assert task_id is not None
        db.add_log(task_id, "info", f"log {i}")
        db.list_logs(task_id)

    assert len(connect_calls) == 1  # 复用连接，不再新建（无重复 PRAGMA）

    # 同一线程两次进入 _conn 拿到的是同一个连接对象
    with db._conn() as c1:
        pass
    with db._conn() as c2:
        assert c2 is c1
    db.close()


def test_threads_get_isolated_connections(tmp_path):
    """不同线程各持一条独立连接（threading.local 隔离，check_same_thread 语义正确）。"""
    db = Database(str(tmp_path / "multi.db"))
    worker_conns: list = []

    def worker():
        with db._conn() as c:
            worker_conns.append(c)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    with db._conn() as main_conn:
        assert main_conn is not worker_conns[0]  # 跨线程不共享连接

    # 各自线程内仍复用
    with db._conn() as main_conn2:
        assert main_conn2 is main_conn
    db.close()


def test_concurrent_writes_no_database_locked(tmp_path):
    """并发写压测：多线程同时写/读，不得出现 database is locked，且全部落库。"""
    db = Database(str(tmp_path / "stress.db"))
    n_threads = 8
    per_thread = 30
    errors: list = []
    locked_errors: list = []

    def worker(tid: int) -> None:
        try:
            for i in range(per_thread):
                project_id = tid * 1000 + i
                repo_id = db.upsert_repo(
                    project_id, f"repo-{tid}-{i}", "https://example.com/r.git")
                task_id = db.create_task(repo_id, project_id, i, f"任务 {tid}-{i}")
                assert task_id is not None
                db.add_log(task_id, "info", f"t{tid}-{i}")
                db.list_tasks()  # 并发读（WAL 下读不阻塞写）
                db.list_logs(task_id)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                locked_errors.append(exc)
            else:
                errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert locked_errors == []  # 并发写无 database is locked
    assert errors == []
    # 全部写入落库成功（无丢写/覆盖）
    assert db.count_tasks() == n_threads * per_thread
    db.close()


def test_write_transaction_rolls_back_and_connection_reusable(tmp_path):
    """写事务异常回滚；回滚后同一连接仍可继续复用。"""
    db = Database(str(tmp_path / "rollback.db"))
    with pytest.raises(RuntimeError):
        with db._conn(write=True) as conn:
            conn.execute(
                "INSERT INTO repos (gitlab_project_id, name, url) VALUES (?, ?, ?)",
                (9001, "should-not-exist", "https://example.com/x.git"))
            raise RuntimeError("boom")
    assert db.get_repo_by_project_id(9001) is None  # 已回滚

    # 回滚后连接复用正常
    repo_id = db.upsert_repo(9002, "ok", "https://example.com/y.git")
    assert db.get_repo_by_project_id(9002) is not None
    db.close()


def test_nested_write_inside_read_context(tmp_path):
    """兼容既有调用模式：外层 _conn() 内再调用写方法（复用同一连接不冲突）。"""
    db = Database(str(tmp_path / "nested.db"))
    with db._conn() as conn:
        repo_id = db.upsert_repo(1, "repo1", "https://example.com/r1.git")
        conn.execute("PRAGMA user_version = 1")
    assert db.get_repo_by_project_id(1) is not None
    db.close()


def test_reads_do_not_open_transaction(tmp_path):
    """纯读路径不开启事务（不占用写锁，WAL 下读写可并发）。"""
    db = Database(str(tmp_path / "ro.db"))
    db.upsert_repo(1, "r", "https://x/y.git")
    with db._conn() as conn:
        db.list_repos()
        assert not conn.in_transaction
    db.close()


def test_connection_settings_applied_once(tmp_path):
    """复用连接保留初始化一次的设置：row_factory 与 WAL 模式。"""
    db = Database(str(tmp_path / "settings.db"))
    with db._conn() as conn:
        # row_factory 生效（查询返回 Row）
        row = conn.execute("SELECT 1 AS v").fetchone()
        assert dict(row) == {"v": 1}
        # journal_mode 为 WAL（数据库级持久属性，无需重复设置）
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    db.close()
