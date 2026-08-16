"""SQLite 持久化层。

表结构按设计方案 §6：
- repos：仓库注册信息（config.yaml 里的仓库同步进来）
- tasks：任务状态机 queued → running → retrying → succeeded / failed / interrupted
- task_logs：任务日志

去重靠部分唯一索引：同一 (project_id, issue_iid) 只允许一条活跃记录。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

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

# 仓库默认优先级（issue #51）：整数 1~999，数字越小越优先
DEFAULT_PRIORITY = 100

# set_task_status / finish_task 可写的附加字段白名单
_TASK_FIELDS = {"attempt_count", "exit_code", "error_message", "error_detail",
                "log_path", "started_at", "finished_at", "claude_session_id",
                "hermes_history", "commit_sha", "dsh_session_id"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
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
  error_detail TEXT,
  log_path TEXT,
  started_at TEXT,
  finished_at TEXT,
  commit_sha TEXT,
  hermes_history TEXT,
  issue_labels TEXT DEFAULT '[]',
  issue_updated_at TEXT DEFAULT '',
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

-- 网页通知事件（issue #21）：前端轮询增量拉取后弹系统通知。
-- 任务类事件以 task_id 唯一（同一任务收尾只记一次，幂等）；
-- 队列类事件（queue_empty/queue_no_work）task_id 为 NULL，靠 notifier 节流去重。
CREATE TABLE IF NOT EXISTS notification_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  repo_name TEXT,
  task_id INTEGER,
  data TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_task
  ON notification_events(task_id) WHERE task_id IS NOT NULL;
"""


def normalize_issue_updated_at(value: str | None) -> str:
    """归一化 GitLab issue 更新时间（issue #76）：ISO8601（含时区）→
    UTC 'YYYY-MM-DD HH:MM:SS'（与 tasks.created_at 同格式，字符串可直接
    比较）。解析失败/空值返回空串（调度器用 created_at 兜底）。"""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 无时区按 UTC 语义（与 created_at 一致）
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_db_ts(s: str) -> datetime | None:
    """解析库内时间串（'YYYY-MM-DD HH:MM:SS' 无时区后缀）。失败返回 None。"""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _near_any(t: datetime, others: list[datetime], tol: timedelta) -> bool:
    """t 与列表任一时间差 ≤ tol（naive datetime 统一按 UTC 语义比较）。"""
    return any(abs(t - o) <= tol for o in others)


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        # 默认连接隔离：每个连接独立，写操作走各自的事务
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn) -> None:
        """轻量迁移（PRAGMA user_version 版本化）。

        v0 → v1：给旧库补新增列（CREATE TABLE IF NOT EXISTS 不更新已有表）；
        v1 → v2：修正旧版 executor 按本地 CST 写入的 started_at/finished_at
                 （issue #49 第二轮：550e04f 部署前的存量数据，前端按 UTC
                 解析会偏移 +8 小时）。
        """
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver < 1:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "remote_name" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN remote_name TEXT")
            task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "error_detail" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN error_detail TEXT")
            if "claude_session_id" not in task_cols:
                # issue #8：claude --resume 会话断点续跑——记录上次执行的 claude 会话，
                # 重启/重试后用于接续对话（从上次结束的地方继续）
                conn.execute("ALTER TABLE tasks ADD COLUMN claude_session_id TEXT")
            if "hermes_history" not in task_cols:
                # issue #47：hermes 引擎断点续跑——记录上次执行的会话消息历史
                # （runner 输出的 messages JSON），重试/重启后作为
                # conversation_history 传入接续对话（Q3-B 等价实现）
                conn.execute("ALTER TABLE tasks ADD COLUMN hermes_history TEXT")
            if "commit_sha" not in task_cols:
                # issue #19：任务成功时记录对应提交的完整 sha（任务页面 commit 链接）
                conn.execute("ALTER TABLE tasks ADD COLUMN commit_sha TEXT")
            conn.execute("PRAGMA user_version = 1")
            ver = 1
        if ver < 2:
            fixed = self._fix_legacy_cst_timestamps(conn)
            if fixed:
                logger.info("迁移：已修正 %s 个旧版 CST 时间戳字段为 UTC（issue #49）", fixed)
            conn.execute("PRAGMA user_version = 2")
            ver = 2
        if ver < 3:
            # issue #51：仓库优先级（1~999，默认 100，数字越小越优先）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "priority" not in cols:
                conn.execute(
                    "ALTER TABLE repos ADD COLUMN priority INTEGER DEFAULT 100")
            conn.execute("PRAGMA user_version = 3")
            ver = 3
        if ver < 4:
            # issue #62：仓库软删除标记（删除与停用区分，
            # list_repos 默认过滤 deleted_at 非空的行）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "deleted_at" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN deleted_at TEXT")
            conn.execute("PRAGMA user_version = 4")
            ver = 4
        if ver < 5:
            # issue #84：dsh 引擎断点续跑——记录上次执行的 dsh 会话 id
            # （SDK 在 session_root 持久化会话，重试/重启后同一 id 接续对话）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "dsh_session_id" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN dsh_session_id TEXT")
            conn.execute("PRAGMA user_version = 5")
        if ver < 6:
            # issue #76：issue 标签优先级排序——入队时记录 issue 标签
            # （JSON 数组）与 GitLab issue 更新时间（UTC 串），调度器按
            # 配置的标签顺序选任务派发
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "issue_labels" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN issue_labels TEXT DEFAULT '[]'")
            if "issue_updated_at" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN issue_updated_at TEXT DEFAULT ''")
            conn.execute("PRAGMA user_version = 6")

    def _fix_legacy_cst_timestamps(self, conn) -> int:
        """修正旧版 executor 按本地 CST 写入的 started_at/finished_at（issue #49 第二轮）。

        550e04f（issue #42）前旧版 executor 用 time.strftime()（无 gmtime）按
        容器本地时区（部署固定 Asia/Shanghai，UTC+8）写 started_at/finished_at
        无时区后缀串，与 created_at（SQLite datetime('now') UTC）及前端「按 UTC
        解析」契约不一致，任务页「用时」虚增 8 小时（任务 #65 显示 8 小时，
        实际 9 分钟）。

        以 task_logs 的 ts（恒为 datetime('now') UTC）为参照逐字段判定：
        - H_UTC 优先：串按 UTC 解析后与任一日志 ts 差 ≤ 10 分钟 → 已是 UTC，
          不动（排队 8 小时以上的任务首条日志恰在 t-8h 附近，先判 H_CST 会误减）；
        - 否则 H_CST：解析结果减 8 小时与任一日志 ts 差 ≤ 10 分钟 → CST 串，
          改写为减 8 小时后的 UTC 串；
        - 均不命中（无日志等）→ 保守不动。
        幂等：修正后串按 UTC 解析与日志直接吻合，重复执行不再命中 H_CST。
        返回修正的字段个数。
        """
        cst_offset = timedelta(hours=8)   # 旧数据写入时容器 TZ 固定 Asia/Shanghai
        tolerance = timedelta(minutes=10)
        fixed = 0
        rows = conn.execute(
            "SELECT id, started_at, finished_at FROM tasks").fetchall()
        for row in rows:
            logs = [r["ts"] for r in conn.execute(
                "SELECT ts FROM task_logs WHERE task_id=? ORDER BY id", (row["id"],))]
            log_times = [t for t in (_parse_db_ts(s) for s in logs) if t is not None]
            for col in ("started_at", "finished_at"):
                val = row[col]
                if not val or not log_times:
                    continue
                t = _parse_db_ts(val)
                if t is None:
                    continue
                if _near_any(t, log_times, tolerance):        # H_UTC：已是 UTC
                    continue
                if _near_any(t - cst_offset, log_times, tolerance):  # H_CST：存量本地串
                    # 列名来自上方固定元组 ("started_at", "finished_at")，无注入风险
                    conn.execute(
                        f"UPDATE tasks SET {col}=? WHERE id=?",  # nosec B608
                        ((t - cst_offset).strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
                    fixed += 1
        return fixed

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
                    enabled: bool = True, local_path: str | None = None,
                    remote_name: str | None = None,
                    priority: int = DEFAULT_PRIORITY) -> int:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO repos (gitlab_project_id, name, url, prompt_template, enabled, local_path, remote_name, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(gitlab_project_id) DO UPDATE SET
                     name=excluded.name, url=excluded.url,
                     prompt_template=excluded.prompt_template,
                     enabled=excluded.enabled, local_path=excluded.local_path,
                     remote_name=excluded.remote_name,
                     priority=excluded.priority,
                     deleted_at=NULL""",
                (project_id, name, url, prompt_template, 1 if enabled else 0,
                 local_path, remote_name, priority),
            )
            # 冲突更新路径 lastrowid 不可靠（issue #62：重新添加已删除仓库），
            # 按唯一键 project_id 反查
            row = conn.execute(
                "SELECT id FROM repos WHERE gitlab_project_id=?", (project_id,)).fetchone()
            return row["id"]

    def list_repos(self, include_deleted: bool = False) -> list[sqlite3.Row]:
        """列出仓库；默认过滤已软删除（deleted_at 非空）的行（issue #62）。

        任务历史的仓库名解析等场景需要包含已删除仓库时传 include_deleted=True。
        两种过滤条件写成完整 SQL 常量（不拼接），避免 bandit B608 告警。
        """
        with self._conn() as conn:
            # 按优先级升序（数字小在前），同优先级按 id（issue #51）
            sql = ("SELECT * FROM repos ORDER BY priority, id"
                   if include_deleted else
                   "SELECT * FROM repos WHERE deleted_at IS NULL ORDER BY priority, id")
            return conn.execute(sql).fetchall()

    def get_repo(self, repo_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()

    def get_repo_by_project_id(self, project_id: int,
                               include_deleted: bool = False) -> sqlite3.Row | None:
        """按 gitlab project_id 查询；默认不返回已软删除的行（issue #62）。"""
        with self._conn() as conn:
            sql = ("SELECT * FROM repos WHERE gitlab_project_id=?"
                   if include_deleted else
                   "SELECT * FROM repos WHERE gitlab_project_id=? AND deleted_at IS NULL")
            return conn.execute(sql, (project_id,)).fetchone()

    def soft_delete_repo(self, repo_id: int) -> None:
        """软删除仓库（issue #62）：写 deleted_at 标记 + enabled=0。

        行保留供任务历史解析仓库名（api/tasks 用 include_deleted=True）；
        list_repos 默认过滤，仓库列表/概览流水线/对账不再出现该仓库。
        重新添加同 project_id 的仓库时 upsert 会清除删除标记。
        """
        with self._conn() as conn:
            conn.execute(
                """UPDATE repos SET deleted_at=datetime('now'), enabled=0
                   WHERE id=?""", (repo_id,))

    def update_repo(self, repo_id: int, **fields) -> None:
        allowed = {"name", "url", "prompt_template", "enabled", "local_path", "remote_name", "priority"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._conn() as conn:
            conn.execute(f"UPDATE repos SET {cols} WHERE id=?",  # nosec B608
                         (*sets.values(), repo_id))

    def delete_repo(self, repo_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))

    # ---- tasks ----

    def create_task(self, repo_id: int, project_id: int, issue_iid: int,
                    issue_title: str, triggered_by: str = "webhook",
                    issue_labels: list[str] | None = None,
                    issue_updated_at: str | None = None) -> int | None:
        """创建任务。若已有活跃任务则返回 None（去重）。

        issue_labels / issue_updated_at（issue #76）：入队时记录 issue 标签
        与更新时间，调度器按配置的标签优先级选任务派发。
        """
        labels_json = json.dumps(issue_labels or [], ensure_ascii=False)
        with self._conn() as conn:
            dup = conn.execute(
                """SELECT id FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone()
            if dup:
                return None
            cur = conn.execute(
                """INSERT INTO tasks (repo_id, project_id, issue_iid, issue_title, status, triggered_by,
                                      issue_labels, issue_updated_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (repo_id, project_id, issue_iid, issue_title, triggered_by,
                 labels_json, issue_updated_at or ""))
            return cur.lastrowid

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, status: str | list[str] | None = None, repo_id: int | None = None,
                   search: str | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            if isinstance(status, str):
                sql += " AND status=?"
                params.append(status)
            else:
                # 多值过滤（issue #32 概览页：running,retrying 一次拉取）
                sql += f" AND status IN ({', '.join('?' * len(status))})"
                params.extend(status)
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

    def count_tasks(self, status: str | list[str] | None = None,
                    repo_id: int | None = None, search: str | None = None) -> int:
        # 过滤条件与 list_tasks 保持一致（issue #50：翻页组件按 total 计算
        # 总页数，total 必须跟随 repo_id/search 筛选，否则筛选后总页数偏大）
        sql = "SELECT COUNT(*) AS c FROM tasks WHERE 1=1"
        params: list = []
        if status:
            if isinstance(status, str):
                sql += " AND status=?"
                params.append(status)
            else:
                sql += f" AND status IN ({', '.join('?' * len(status))})"
                params.extend(status)
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if search:
            sql += " AND (issue_title LIKE ? OR CAST(issue_iid AS TEXT) LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()["c"]

    def find_active_task(self, project_id: int, issue_iid: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone()

    def find_latest_task(self, project_id: int, issue_iid: int) -> sqlite3.Row | None:
        """该 issue 最近一次的任务记录（issue #117：概览页重试按钮用）。

        按任务 id 倒序取最新一条（id 递增即创建先后，同 issue 可能因
        重新指派/对账补入队存在多条任务记录）；无记录返回 None。
        """
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   ORDER BY id DESC LIMIT 1""",
                (project_id, issue_iid)).fetchone()

    def set_task_status(self, task_id: int, status: str | None, **fields) -> None:
        """更新任务状态及附加字段（attempt_count / exit_code / error_message /
        error_detail / log_path / started_at / finished_at / claude_session_id /
        hermes_history / commit_sha / dsh_session_id）。

        status 传 None 时只更新附加字段，不改状态。
        """
        cols: list[str] = []
        vals: list = []
        if status is not None:
            cols.append("status=?")
            vals.append(status)
        for k, v in fields.items():
            if k in _TASK_FIELDS:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return
        vals.append(task_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id=?", vals)  # nosec B608

    def claim_task(self, task_id: int) -> bool:
        """原子抢占任务（防多实例并发执行同一任务，issue #24）。

        仅当任务处于 queued/retrying 时置为 running（条件 UPDATE），返回
        是否抢到；已是 running（其他实例已领取）或已终态时抢不到。
        跨实例安全：SQLite 写事务串行化保证条件判断与更新原子。
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status=? WHERE id=? AND status IN (?, ?)",
                (STATUS_RUNNING, task_id, STATUS_QUEUED, STATUS_RETRYING))
            return cur.rowcount > 0

    def finish_task(self, task_id: int, status: str, **fields) -> bool:
        """条件终态更新（issue #24）：仅当任务仍处于 running/retrying 时生效。

        多实例并发执行同一任务时先完成者生效，后完成者返回 False 且不改
        状态——避免慢实例把已成功/已失败的任务覆盖成相反结果。附加字段
        白名单与 set_task_status 一致。
        """
        cols: list[str] = ["status=?"]
        vals: list = [status]
        for k, v in fields.items():
            if k in _TASK_FIELDS:
                cols.append(f"{k}=?")
                vals.append(v)
        vals.extend([task_id, STATUS_RUNNING, STATUS_RETRYING])
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET {', '.join(cols)} WHERE id=? AND status IN (?, ?)",  # nosec B608
                vals)
            return cur.rowcount > 0

    def stop_active_tasks(self) -> list[int]:
        """一键停止所有活跃任务（issue #35）：queued/running/retrying → interrupted。

        返回被停止的任务 id 列表。interrupted 为终态：requeue_interrupted
        只捞 running/retrying，用户手动停止的任务不会在平台重启后被
        重新入队执行。
        """
        stopped: list[int] = []
        with self._conn() as conn:
            for row in conn.execute(
                    "SELECT id FROM tasks WHERE status IN ('queued','running','retrying')"):
                stopped.append(row["id"])
            if stopped:
                conn.execute(
                    """UPDATE tasks SET status='interrupted',
                       error_message='用户手动停止（一键停止所有任务）',
                       finished_at=datetime('now')
                       WHERE status IN ('queued','running','retrying')""")
                conn.executemany(
                    "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'warn', ?)",
                    [(tid, "任务已停止：用户一键停止所有任务") for tid in stopped])
        return stopped

    def retry_task(self, task_id: int) -> str:
        """手动重试（issue #36）：终态失败任务重置为 queued，返回结果码。

        - "ok"：重置成功（failed/interrupted → queued）
        - "not_found"：任务不存在
        - "bad_state"：状态非 failed/interrupted（含已被重试过的情况）
        - "conflict"：同一 issue 已有活跃任务（部分唯一索引去重）

        重置失败相关字段（attempt_count 归零、清空 exit_code/error_message/
        error_detail/commit_sha/started_at/finished_at），triggered_by 标记
        manual 供前端「来源」列展示；保留 claude_session_id（断点续跑接续
        上次会话）与 log_path（日志文件重试时覆盖重写）。
        条件 UPDATE 兜底并发：多请求同时重试时先到者生效。
        """
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] not in (STATUS_FAILED, STATUS_INTERRUPTED):
                return "bad_state"
            dup = conn.execute(
                """SELECT id FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (row["project_id"], row["issue_iid"])).fetchone()
            if dup is not None:
                return "conflict"
            cur = conn.execute(
                """UPDATE tasks SET status='queued', attempt_count=0,
                   triggered_by='manual', exit_code=NULL, error_message=NULL,
                   error_detail=NULL, commit_sha=NULL, started_at=NULL,
                   finished_at=NULL
                   WHERE id=? AND status IN ('failed','interrupted')""",
                (task_id,))
            if cur.rowcount == 0:
                return "bad_state"
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'info', ?)",
                (task_id,
                 f"手动重试：任务状态由 {row['status']} 重置为 queued，重新入队执行"))
        return "ok"

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

    # ---- notification_events（issue #21）----

    def add_notification(self, type_: str, title: str, body: str = "",
                         repo_name: str | None = None, task_id: int | None = None,
                         data: str | None = None) -> int | None:
        """记录一条通知事件，返回 id；同一 task_id 重复记录返回 None（幂等）。"""
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO notification_events (type, title, body, repo_name, task_id, data)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (type_, title, body, repo_name, task_id, data))
            except sqlite3.IntegrityError:
                return None
            return cur.lastrowid

    def list_notifications(self, after_id: int = 0, limit: int = 50) -> list[sqlite3.Row]:
        """增量拉取：返回 id > after_id 的事件（按 id 升序），最多 limit 条。"""
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM notification_events WHERE id > ?
                   ORDER BY id ASC LIMIT ?""", (after_id, limit)).fetchall()

    def last_notification(self, repo_name: str, type_: str) -> sqlite3.Row | None:
        """节流查询：同仓库同类型最近一条事件（无则 None）。"""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM notification_events
                   WHERE repo_name=? AND type=?
                   ORDER BY id DESC LIMIT 1""", (repo_name, type_)).fetchone()
            return row
