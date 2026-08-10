"""SQLite 持久化层。

表结构按设计方案 §6：
- repos：仓库注册信息（config.yaml 里的仓库同步进来）
- tasks：任务状态机 queued → running → retrying → succeeded / failed / interrupted
- task_logs：任务日志

去重靠部分唯一索引：同一 (project_id, issue_iid) 只允许一条活跃记录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("BOTLER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "botler.db"))

# 任务状态机
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRYING = "retrying"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

# 活跃状态（去重索引覆盖范围）
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gitlab_project_id INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  local_path TEXT,
  prompt_template TEXT,
  enabled INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  project_id INTEGER NOT NULL,
  issue_iid INTEGER NOT NULL,
  issue_title TEXT,
  status TEXT NOT NULL,
  attempt_count INTEGER DEFAULT 0,
  triggered_by TEXT,
  exit_code INTEGER,
  error_message TEXT,
  log_path TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

-- 部分唯一索引：同一 issue 只允许一条活跃记录（webhook/对账去重）
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active
  ON tasks(project_id, issue_iid)
  WHERE status IN ('queued', 'running', 'retrying');

CREATE TABLE IF NOT EXISTS task_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  ts TEXT DEFAULT (datetime('now')),
  level TEXT,
  message TEXT
);
"""


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        # 默认连接隔离：每个连接独立，写操作走各自的事务
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- repos ----

    def upsert_repo(self, project_id: int, name: str, url: str,
                    prompt_template: str | None = None,
                    enabled: bool = True, local_path: str | None = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO repos (gitlab_project_id, name, url, prompt_template, enabled, local_path)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(gitlab_project_id) DO UPDATE SET
                     name=excluded.name, url=excluded.url,
                     prompt_template=excluded.prompt_template,
                     enabled=excluded.enabled, local_path=excluded.local_path""",
                (project_id, name, url, prompt_template, 1 if enabled else 0, local_path),
            )
            return cur.lastrowid

    def list_repos(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM repos ORDER BY id").fetchall()

    def get_repo(self, repo_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()

    def get_repo_by_project_id(self, project_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM repos WHERE gitlab_project_id=?", (project_id,)).fetchone()

    def update_repo(self, repo_id: int, **fields) -> None:
        allowed = {"name", "url", "prompt_template", "enabled", "local_path"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._conn() as conn:
            conn.execute(f"UPDATE repos SET {cols} WHERE id=?",
                         (*sets.values(), repo_id))

    def delete_repo(self, repo_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))

    # ---- tasks ----

    def create_task(self, repo_id: int, project_id: int, issue_iid: int,
                    issue_title: str, triggered_by: str = "webhook") -> int | None:
        """创建任务。若已有活跃任务则返回 None（去重）。"""
        with self._conn() as conn:
            dup = conn.execute(
                """SELECT id FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone()
            if dup:
                return None
            cur = conn.execute(
                """INSERT INTO tasks (repo_id, project_id, issue_iid, issue_title, status, triggered_by)
                   VALUES (?, ?, ?, ?, 'queued', ?)""",
                (repo_id, project_id, issue_iid, issue_title, triggered_by))
            return cur.lastrowid

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, status: str | None = None, repo_id: int | None = None,
                   search: str | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if search:
            sql += " AND (issue_title LIKE ? OR CAST(issue_iid AS TEXT) LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    def count_tasks(self, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM tasks"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()["c"]

    def find_active_task(self, project_id: int, issue_iid: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone()

    def set_task_status(self, task_id: int, status: str | None, **fields) -> None:
        """更新任务状态及附加字段（attempt_count / exit_code / error_message / log_path / started_at / finished_at）。

        status 传 None 时只更新附加字段，不改状态。
        """
        allowed = {"attempt_count", "exit_code", "error_message", "log_path", "started_at", "finished_at"}
        cols: list[str] = []
        vals: list = []
        if status is not None:
            cols.append("status=?")
            vals.append(status)
        for k, v in fields.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return
        vals.append(task_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id=?", vals)

    def requeue_interrupted(self) -> list[int]:
        """重启恢复：queued 保持不变，running/retrying 标记 interrupted 后重新入队。"""
        restored: list[int] = []
        with self._conn() as conn:
            for row in conn.execute(
                    "SELECT id FROM tasks WHERE status IN ('running','retrying')"):
                restored.append(row["id"])
            conn.execute(
                """UPDATE tasks SET status='interrupted', error_message='平台重启导致中断'
                   WHERE status IN ('running','retrying')""")
            for task_id in restored:
                conn.execute(
                    """INSERT INTO task_logs (task_id, level, message)
                       VALUES (?, 'warn', '平台重启：running/retrying 任务标记 interrupted 并重新入队')""",
                    (task_id,))
            for task_id in restored:
                conn.execute(
                    """UPDATE tasks SET status='queued', started_at=NULL, finished_at=NULL
                       WHERE id=?""", (task_id,))
        return restored

    # ---- task_logs ----

    def add_log(self, task_id: int, level: str, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
                (task_id, level, message))

    def list_logs(self, task_id: int, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM task_logs WHERE task_id=?
                   ORDER BY id ASC LIMIT ?""", (task_id, limit)).fetchall()

    def add_logs(self, task_id: int, entries: list[tuple[str, str]]) -> None:
        """批量写日志：entries 为 [(level, message), ...]"""
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
                [(task_id, lv, msg) for lv, msg in entries])

    def task_stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
