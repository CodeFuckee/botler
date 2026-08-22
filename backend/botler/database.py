"""SQLite 持久化层。

表结构按设计方案 §6：
- repos：仓库注册信息（config.yaml 里的仓库同步进来）
- tasks：任务状态机 queued → running → retrying → succeeded / failed / interrupted / canceled_by_user
- task_logs：任务日志

去重靠部分唯一索引：同一 (project_id, issue_iid) 只允许一条活跃记录。
"""

from __future__ import annotations

import json

from botler.failure_classify import category_label, classify_failure
from botler.log_redact import redact
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TypedDict, cast

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("BOTLER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "botler.db"))

# 任务状态机
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRYING = "retrying"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
# 用户手动移出队列（issue #242）：排队任务被「移出队列」操作终止的终态
STATUS_CANCELED = "canceled_by_user"

# 活跃状态（去重索引覆盖范围）
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING)

# 仓库默认优先级（issue #51）：整数 1~999，数字越小越优先
DEFAULT_PRIORITY = 100

# set_task_status / finish_task 可写的附加字段白名单
_TASK_FIELDS = {"attempt_count", "exit_code", "error_message", "error_detail",
                "log_path", "started_at", "finished_at", "claude_session_id",
                "hermes_history", "commit_sha", "dsh_session_id", "dsh_transcript",
                "engine", "environment", "base_sha",
                "failure_category", "engine_fallback", "precheck_result"}

# ---- 关键行类型化（issue #213）----
# tasks / repos 表行类型：mypy 下 dict key 拼错（如 row["commit_shaa"]）与
# None 未判在静态检查时即报错，不再等到运行时才暴露。返回对象运行时仍是
# sqlite3.Row（支持按列名取值），仅静态类型提升为 TypedDict，零运行时开销。


class TaskRow(TypedDict):
    """tasks 表行（issue #213）：任务状态机数据行类型。"""

    id: int
    repo_id: int
    project_id: int
    issue_iid: int
    issue_title: str | None
    status: str
    attempt_count: int | None
    triggered_by: str | None
    exit_code: int | None
    error_message: str | None
    error_detail: str | None
    log_path: str | None
    started_at: str | None
    finished_at: str | None
    commit_sha: str | None
    hermes_history: str | None
    issue_labels: str | None
    issue_updated_at: str | None
    issue_created_at: str | None
    engine: str | None
    dsh_transcript: str | None
    environment: str | None
    base_sha: str | None
    failure_category: str | None
    # issue #236：引擎降级原因文案（如「引擎 claude 不可用（...），已降级
    # dsh 执行」）；未发生降级为空串
    engine_fallback: str | None
    # issue #238：任务执行前预检结果 JSON（检查项 ✓/✗ 明细）；未启用预检
    # 或旧任务为 NULL
    precheck_result: str | None
    manual_priority: int | None
    created_at: str | None


class RepoRow(TypedDict):
    """repos 表行（issue #213）：仓库注册信息行类型。"""

    id: int
    gitlab_project_id: int
    name: str
    url: str
    local_path: str | None
    remote_name: str | None
    remote_username: str | None
    prompt_template: str | None
    enabled: int | None
    priority: int | None
    timeout_seconds: int | None
    max_retries: int | None
    engine: str | None
    deleted_at: str | None
    logo_path: str | None
    logo_updated_at: str | None
    logo_mime: str | None
    token_expires_at: str | None
    created_at: str | None


def _as_task(row: sqlite3.Row | None) -> TaskRow | None:
    """sqlite3.Row → TaskRow 静态类型收窄（issue #213）。

    运行时对象保持不变（sqlite3.Row 支持按列名取值，行为与 TypedDict
    下标访问一致），仅提升静态类型，让 mypy 能校验 key 拼写与 None 处理。
    """
    return cast(TaskRow, row) if row is not None else None


def _as_task_list(rows: list[sqlite3.Row]) -> list[TaskRow]:
    return [cast(TaskRow, r) for r in rows]


def _as_repo(row: sqlite3.Row | None) -> RepoRow | None:
    """sqlite3.Row → RepoRow 静态类型收窄（issue #213），见 _as_task。"""
    return cast(RepoRow, row) if row is not None else None


def _as_repo_list(rows: list[sqlite3.Row]) -> list[RepoRow]:
    return [cast(RepoRow, r) for r in rows]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gitlab_project_id INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  local_path TEXT,
  remote_name TEXT,
  remote_username TEXT,
  prompt_template TEXT,
  enabled INTEGER DEFAULT 1,
  priority INTEGER DEFAULT 100,
  timeout_seconds INTEGER,
  max_retries INTEGER,
  engine TEXT,
  deleted_at TEXT,
  logo_path TEXT,
  logo_updated_at TEXT,
  logo_mime TEXT,
  token_expires_at TEXT,
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
  issue_created_at TEXT DEFAULT '',
  engine TEXT DEFAULT '',
  dsh_transcript TEXT,
  environment TEXT,
  base_sha TEXT,
  failure_category TEXT DEFAULT '',
  engine_fallback TEXT DEFAULT '',
  precheck_result TEXT,
  manual_priority INTEGER,
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

-- 任务进度账本（issue #281 §4.1）：记录任务级「步骤 + 验证证据」，与
-- 对话转录解耦。只增不改（快照式）：同一 step_no 可追加多行，恢复时取
-- 每步最新状态行，保留「上次怎么做的」可追溯。中断恢复时据此渲染
-- 确定性交接单（§4.4），替代「模型自查 git 反推进度」导致的反复检查。
CREATE TABLE IF NOT EXISTS task_progress (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  step_no INTEGER NOT NULL,
  step_desc TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  evidence TEXT,
  files TEXT,
  verified_at TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_progress_task
  ON task_progress(task_id, step_no);

-- 任务 token 用量与费用（issue #235）：每次任务执行采集引擎的模型调用
-- token 用量（claude result 行 usage / dsh SDK usage chunk / hermes 会话
-- 计数器），执行结束后落库一行（重试覆盖上一次）；estimated_cost 为估算
-- 费用（引擎自带费用或按 config 单价估算，无单价为 NULL=只展示 token 数）。
CREATE TABLE IF NOT EXISTS task_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  engine TEXT NOT NULL DEFAULT '',
  model TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost REAL,
  currency TEXT NOT NULL DEFAULT 'USD',
  raw_usage TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_usage_task
  ON task_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_task_usage_engine
  ON task_usage(engine);

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

-- 数据保留清理按创建时间筛选，索引避免全表扫描（issue #204）
CREATE INDEX IF NOT EXISTS idx_notification_events_created_at
  ON notification_events(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_finished_status
  ON tasks(finished_at, status);

-- 灵感（issue #131）：概览页「灵感」板块——按仓库随手记录新功能灵感，
-- 仅保存在 Botler 本地数据库，不提交到 GitLab issue。repo_id 引用
-- repos 表（软删除仍保留行，灵感记录不随仓库删除而丢失）。
CREATE TABLE IF NOT EXISTS inspirations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

-- 灵感 AI 对话消息（issue #166）：概览页灵感板块「与 AI 对话」——用户
-- 围绕某条灵感与 AI agent 探讨，消息成对保存（user 提问 + assistant
-- 回复），仅存本地数据库；删除灵感时级联清理（delete_inspiration）。
CREATE TABLE IF NOT EXISTS inspiration_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inspiration_id INTEGER NOT NULL REFERENCES inspirations(id),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inspiration_messages_insp
  ON inspiration_messages(inspiration_id, id);

-- issue #287：概览页「其他」分组手动调度顺序——用户在「调度器执行顺序」
-- 排序下拖动 issue 上下移动，把调整后的顺序落库（仅存 Botler 本地
-- 数据库，不改 GitLab 侧任何字段）。调度器派发时按 position 优先于
-- 标签权重/创建时间排序，实现「手动改变调度顺序」。整组顺序全量替换
-- （拖动一次即重排整组），position 从 0 连续编号。
CREATE TABLE IF NOT EXISTS issue_manual_orders (
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  issue_iid INTEGER NOT NULL,
  position INTEGER NOT NULL,
  updated_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (repo_id, issue_iid),
  UNIQUE (repo_id, position)
);

-- MCP 工具管理（issue #172）：工具页面的数据表。工具 = MCP server，
-- 通过 mcpServers 配置暴露给 agent 调用。来源：builtin（内置市场）/
-- url（URL 导入）/ market（远端市场索引）/ custom（自定义编写）。
CREATE TABLE IF NOT EXISTS tools (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'stdio',
  command TEXT DEFAULT '',
  args TEXT DEFAULT '[]',
  env TEXT DEFAULT '{}',
  url TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'custom',
  source_url TEXT DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

-- 工具页面元信息（键值）：远端市场索引 URL 等
CREATE TABLE IF NOT EXISTS tool_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""


def _like_escape(term: str) -> str:
    """转义 LIKE 通配符（% _ \）为字面匹配，配合 ESCAPE '\\' 使用。

    全局搜索（issue #216）按字面子串匹配：用户输入 % / _ 不应作为
    通配符（搜索「100%」应命中字面含 100% 的标题而非任意前缀）。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_issue_time(value: str | None) -> str:
    """归一化 GitLab issue 时间字段（ISO8601 含时区 → UTC 无后缀串）。

    与 tasks.created_at（SQLite datetime('now') UTC）同格式，字符串可直接
    比较。解析失败/空值返回空串。
    """
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 无时区按 UTC 语义（与 created_at 一致）
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_issue_updated_at(value: str | None) -> str:
    """归一化 GitLab issue 更新时间（issue #76）：ISO8601（含时区）→
    UTC 'YYYY-MM-DD HH:MM:SS'（与 tasks.created_at 同格式，字符串可直接
    比较）。解析失败/空值返回空串（调度器用创建时间/任务提交时间兜底）。"""
    return _normalize_issue_time(value)


def normalize_issue_created_at(value: str | None) -> str:
    """归一化 GitLab issue 创建时间（issue #234）：ISO8601（含时区）→
    UTC 'YYYY-MM-DD HH:MM:SS'（与 tasks.created_at 同格式，字符串可直接
    比较）。解析失败/空值返回空串（调度器用 issue 更新时间/任务提交时间兜底）。"""
    return _normalize_issue_time(value)


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
        # issue #191：连接复用——每线程各持一条长连接（threading.local），
        # 连接只初始化一次（WAL/busy_timeout/row_factory），后续复用不再
        # 重复 PRAGMA；写事务通过 _write_lock 跨线程串行化（WAL 下读可
        # 并发、写串行），从根本上消除 database is locked。
        self._local = threading.local()
        self._write_lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn(write=True) as conn:
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
        if ver < 7:
            # issue #120：执行引擎按任务落库——概览页 issue 右边栏「执行
            # 引擎」行按该 issue 最近任务实际使用的引擎展示，而非全局
            # worker.engine（全局引擎切换后历史 issue 不再误显新引擎）。
            # 存量任务回填：按断点续跑会话字段推断历史引擎（同一任务
            # 只可能跑过一种引擎，dsh > hermes > claude 优先级兜底）。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "engine" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN engine TEXT DEFAULT ''")
            conn.execute(
                """UPDATE tasks SET engine = CASE
                     WHEN dsh_session_id IS NOT NULL AND dsh_session_id != '' THEN 'dsh'
                     WHEN hermes_history IS NOT NULL AND hermes_history != '' THEN 'hermes'
                     WHEN claude_session_id IS NOT NULL AND claude_session_id != '' THEN 'claude'
                     ELSE engine END
                   WHERE engine IS NULL OR engine = ''""")
            conn.execute("PRAGMA user_version = 7")
        if ver < 8:
            # issue #131：新增灵感表（概览页「灵感」板块）。CREATE TABLE
            # IF NOT EXISTS 已在 _SCHEMA 覆盖新库；旧库（PRAGMA user_version
            # 为 7）首次启动时同样建表，并显式推进版本号保持迁移链完整。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS inspirations (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     repo_id INTEGER NOT NULL REFERENCES repos(id),
                     content TEXT NOT NULL,
                     created_at TEXT DEFAULT (datetime('now')),
                     updated_at TEXT DEFAULT (datetime('now'))
                   )""")
            conn.execute("PRAGMA user_version = 8")
        if ver < 9:
            # issue #146：dsh 引擎提示词持久化与聊天记录——claude 引擎的
            # 提示词/聊天记录来自会话文件（claude_session_id 定位 jsonl），
            # dsh SDK 会话文件是 runtime 内部格式无法解析，执行侧把
            # prompt + messages 落库本列，execution 接口读取返回
            # （「查看提示词」按钮与聊天记录数据源）。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "dsh_transcript" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN dsh_transcript TEXT")
            conn.execute("PRAGMA user_version = 9")
        if ver < 10:
            # issue #153：仓库用户（remote url userinfo 用户名）——仓库设置
            # 页读取 remote url 获取，灵感一键提交 issue 时作为默认分配人。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "remote_username" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN remote_username TEXT")
            conn.execute("PRAGMA user_version = 10")
        if ver < 11:
            # issue #166：灵感 AI 对话消息表——概览页灵感板块「与 AI 对话」：
            # 用户围绕灵感与 AI agent 探讨，消息成对落库（user + assistant）。
            # CREATE TABLE IF NOT EXISTS 已覆盖新库；旧库（user_version=10）
            # 首次启动时同样建表，并显式推进版本号保持迁移链完整（与
            # issue #131 灵感表 v8 迁移同模式）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS inspiration_messages (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     inspiration_id INTEGER NOT NULL REFERENCES inspirations(id),
                     role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                     content TEXT NOT NULL,
                     created_at TEXT DEFAULT (datetime('now'))
                   )""")
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_inspiration_messages_insp
                   ON inspiration_messages(inspiration_id, id)""")
            conn.execute("PRAGMA user_version = 11")
        if ver < 12:
            # issue #234：同权重按 issue 创建时间排序派发——入队时记录
            # issue 创建时间（UTC 串），调度器同权重时按创建时间升序选
            # 任务（创建早的 issue 先处理）；存量行缺失时按 issue 更新
            # 时间、再按任务提交时间兜底。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "issue_created_at" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN issue_created_at TEXT DEFAULT ''")
            conn.execute("PRAGMA user_version = 12")

        if ver < 13:
            # issue #276：任务执行环境快照——任务开始时采集执行环境
            # （引擎版本/模型/起始 commit 与分支/平台版本/config 关键项
            # hash）序列化为 JSON 落库本列，任务详情页「元信息」区折叠
            # 面板展示。存量任务无快照（NULL），页面显示「暂无环境快照」。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "environment" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN environment TEXT")
            conn.execute("PRAGMA user_version = 13")

        if ver < 14:
            # issue #281 §4.1：任务进度账本 task_progress 表。CREATE TABLE
            # IF NOT EXISTS 已在 _SCHEMA 覆盖新库；旧库（user_version=13）
            # 首次启动时同样建表，并显式推进版本号保持迁移链完整（与
            # issue #131 灵感表 v8 迁移同模式）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS task_progress (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     task_id INTEGER NOT NULL REFERENCES tasks(id),
                     step_no INTEGER NOT NULL,
                     step_desc TEXT NOT NULL DEFAULT '',
                     status TEXT NOT NULL,
                     evidence TEXT,
                     files TEXT,
                     verified_at TEXT,
                     created_at TEXT DEFAULT (datetime('now')),
                     updated_at TEXT DEFAULT (datetime('now'))
                   )""")
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_task_progress_task
                   ON task_progress(task_id, step_no)""")
            conn.execute("PRAGMA user_version = 14")

        if ver < 15:
            # issue #188：仓库 logo（生成图标）——「生成图标」按钮调用 AI 生成
            # 的 logo 图片落库信息：logo_path（LOGO 目录内相对文件名）、
            # logo_updated_at（生成时间，前端用作 img src 缓存击穿参数）、
            # logo_mime（图片 MIME 类型，读取接口回传 Content-Type）。
            # 旧库（user_version=14）补列；新库 _SCHEMA 已含三列。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "logo_path" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN logo_path TEXT")
            if "logo_updated_at" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN logo_updated_at TEXT")
            if "logo_mime" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN logo_mime TEXT")
            conn.execute("PRAGMA user_version = 15")

        if ver < 16:
            # issue #235：任务 token 用量表 task_usage。CREATE TABLE IF NOT EXISTS
            # 已在 _SCHEMA 覆盖新库；旧库（user_version=15）首次启动时同样建表，
            # 并显式推进版本号保持迁移链完整（与 issue #131 灵感表 v8 同模式）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS task_usage (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     task_id INTEGER NOT NULL REFERENCES tasks(id),
                     engine TEXT NOT NULL DEFAULT '',
                     model TEXT,
                     prompt_tokens INTEGER NOT NULL DEFAULT 0,
                     completion_tokens INTEGER NOT NULL DEFAULT 0,
                     total_tokens INTEGER NOT NULL DEFAULT 0,
                     estimated_cost REAL,
                     currency TEXT NOT NULL DEFAULT 'USD',
                     raw_usage TEXT,
                     created_at TEXT DEFAULT (datetime('now'))
                   )""")
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_task_usage_task
                   ON task_usage(task_id)""")
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_task_usage_engine
                   ON task_usage(engine)""")
            conn.execute("PRAGMA user_version = 16")

        if ver < 17:
            # issue #252：任务改动基线提交——任务首次执行开始时工作区 HEAD
            # （prepare_workspace 已重置到远端默认主分支最新提交），收尾时
            # 用 git diff base_sha..HEAD 采集「相对 main 的改动文件与行数」
            # 渲染结构化执行报告评论。旧库（user_version=16）补列；新库
            # _SCHEMA 已含。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "base_sha" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN base_sha TEXT")
            conn.execute("PRAGMA user_version = 17")

        if ver < 18:
            # issue #274：任务失败原因自动分类——任务收尾时对失败原因做规则
            # 分类（env/engine/unsolvable/unknown），结果落库本列，任务详情
            # 页展示分类徽章 + 处理建议，失败评论带分类前缀，统计看板按分类
            # 聚合失败原因 Top 分布。旧库（user_version=17）补列；新库
            # _SCHEMA 已含（默认空串 = 未分类，统计时实时分类兜底）。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "failure_category" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN failure_category TEXT DEFAULT ''")
            conn.execute("PRAGMA user_version = 18")

        if ver < 19:
            # issue #287：概览页「其他」分组手动调度顺序表。CREATE TABLE
            # IF NOT EXISTS 已覆盖新库；旧库（user_version=18）首次启动时
            # 同样建表，并显式推进版本号保持迁移链完整（与 issue #131
            # 灵感表 v8 迁移同模式）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS issue_manual_orders (
                     repo_id INTEGER NOT NULL REFERENCES repos(id),
                     issue_iid INTEGER NOT NULL,
                     position INTEGER NOT NULL,
                     updated_at TEXT DEFAULT (datetime('now')),
                     PRIMARY KEY (repo_id, issue_iid),
                     UNIQUE (repo_id, position)
                   )""")
            conn.execute("PRAGMA user_version = 19")

        if ver < 20:
            # issue #172：MCP 工具管理表（工具页面：内置市场 / URL 导入 /
            # 远端市场索引 / 自定义工具）。CREATE TABLE IF NOT EXISTS 已覆盖
            # 新库；旧库（user_version=19）首次启动时同样建表，并显式推进
            # 版本号保持迁移链完整（与 issue #287 的 v19 迁移同模式）。
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tools (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     name TEXT NOT NULL UNIQUE,
                     description TEXT DEFAULT '',
                     kind TEXT NOT NULL DEFAULT 'stdio',
                     command TEXT DEFAULT '',
                     args TEXT DEFAULT '[]',
                     env TEXT DEFAULT '{}',
                     url TEXT DEFAULT '',
                     source TEXT NOT NULL DEFAULT 'custom',
                     source_url TEXT DEFAULT '',
                     enabled INTEGER NOT NULL DEFAULT 1,
                     created_at TEXT DEFAULT (datetime('now')),
                     updated_at TEXT DEFAULT (datetime('now'))
                   )""")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tool_meta (
                     key TEXT PRIMARY KEY,
                     value TEXT NOT NULL DEFAULT ''
                   )""")
            conn.execute("PRAGMA user_version = 20")

        if ver < 21:
            # issue #242：任务人工优先级——任务表新增 manual_priority 列
            # （可选，NULL = 按系统规则排序）。任务列表页/概览页对排队任务
            # 提供「置顶/上移/下移/置底/移出队列」操作：前四项调整
            # manual_priority（同仓库排队任务内重排编号 0..n-1），调度器
            # 派发时 manual_priority 优先于仓库/标签规则；移出队列把任务
            # 置为终态 canceled_by_user（可手动重试重新入队）。旧库
            # （user_version=20）补列；新库 _SCHEMA 已含。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "manual_priority" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN manual_priority INTEGER")
            conn.execute("PRAGMA user_version = 21")
        if ver < 22:
            # issue #236：引擎降级原因落库——任务因主引擎健康探测不可用或
            # 连续引擎类失败自动降级到备用引擎时，记录原因文案（如「引擎
            # claude 不可用（...），已降级 dsh 执行」），任务详情页展示。
            # 新库 _SCHEMA 已含；旧库（user_version=21）补列。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "engine_fallback" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN engine_fallback TEXT DEFAULT ''")
            conn.execute("PRAGMA user_version = 22")
            ver = 22
        if ver < 23:
            # issue #237：仓库级任务参数覆盖——repos 表新增可选字段
            # timeout_seconds / max_retries / engine（NULL = 继承全局）。
            # 新库 _SCHEMA 已含；旧库（user_version=22）补列。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "timeout_seconds" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN timeout_seconds INTEGER")
            if "max_retries" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN max_retries INTEGER")
            if "engine" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN engine TEXT")
            conn.execute("PRAGMA user_version = 23")
            ver = 23
        if ver < 24:
            # issue #238：任务执行前预检结果——执行器领取任务后、消耗模型
            # 调用前对环境做快速检查（git 凭据/token、local_path、磁盘空间、
            # 工作区），结果 JSON 落库本列，任务详情页「元信息」区展示
            # （✓/✗）。新库 _SCHEMA 已含；旧库（user_version=23）补列。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "precheck_result" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN precheck_result TEXT")
            conn.execute("PRAGMA user_version = 24")
            ver = 24
        if ver < 25:
            # issue #279：仓库 token 到期日独立存储，避免 API URL 脱敏后丢失。
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
            if "token_expires_at" not in cols:
                conn.execute("ALTER TABLE repos ADD COLUMN token_expires_at TEXT")
            conn.execute("PRAGMA user_version = 25")
            ver = 25

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

    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的复用连接；首次调用时创建并一次性初始化。

        issue #191：连接按线程隔离（threading.local），每线程只创建一次，
        WAL + busy_timeout + row_factory 只在创建时设置一次，后续复用不再
        重复 PRAGMA（journal_mode 为数据库级持久属性，设置一次即长期生效）；
        check_same_thread 保持默认 True——每个连接只被其所属线程使用，
        跨线程安全语义由「读并发 + 写串行化」保证。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """关闭当前线程持有的复用连接（仅影响调用线程）。

        issue #191：连接随线程存续；进程退出/线程退出时由 GC 兜底释放，
        本方法供测试清理与优雅停机显式关闭。其他线程的连接在该线程
        退出时随 threading.local 自动释放。
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _conn(self, write: bool = False):
        """获取当前线程的复用连接（issue #191）。

        write=False（默认）：读/一般路径——直接复用连接，退出时提交
        （纯读无事务时 commit 为空操作），异常回滚；
        write=True：写事务路径——先取进程级写锁（_write_lock）把写事务
        跨线程串行化，再显式 BEGIN IMMEDIATE 抢占写锁（避免 DEFERRED
        事务在升级写锁时与并发写冲突产生 database is locked），退出时
        提交/回滚；WAL 模式下读事务可与写事务并发，不受写锁影响。

        嵌套兼容：同一线程外层已进入 _conn 时（如测试中 `with db._conn()`
        内再调用写方法），内层不再重复 BEGIN/COMMIT，直接透传同一连接，
        事务由最外层统一收口——与既有调用模式行为一致。
        """
        conn = self._get_connection()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            # 嵌套复用：内层不管理事务，避免打断外层事务
            try:
                yield conn
            except Exception:
                raise
            return
        self._local.depth = 1
        try:
            if not write:
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                with self._write_lock:
                    # 复用连接必须处于无事务状态才能显式 BEGIN；防御性清理
                    if conn.in_transaction:
                        conn.rollback()
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
        finally:
            self._local.depth = 0

    # ---- inspirations（issue #131）----
    # 概览页「灵感」板块数据：按仓库随手记录新功能灵感，仅存本地数据库。

    def list_inspirations(self, repo_id: int | None = None) -> list[sqlite3.Row]:
        """列出灵感；可选按 repo_id 过滤。

        JOIN repos 带出仓库名（软删除仓库行仍保留，名称可正常展示）；
        按 updated_at 降序（最新改动在前），同时间按 id 降序保证稳定。
        """
        with self._conn() as conn:
            if repo_id is not None:
                return conn.execute(
                    """SELECT i.*, r.name AS repo_name FROM inspirations i
                       JOIN repos r ON r.id = i.repo_id
                       WHERE i.repo_id = ?
                       ORDER BY i.updated_at DESC, i.id DESC""", (repo_id,)).fetchall()
            return conn.execute(
                """SELECT i.*, r.name AS repo_name FROM inspirations i
                   JOIN repos r ON r.id = i.repo_id
                   ORDER BY i.updated_at DESC, i.id DESC""").fetchall()

    def count_inspirations_by_repo(self) -> dict[int, int]:
        """返回每个仓库的灵感数量，供概览轻量轮询使用（issue #219）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT repo_id, COUNT(*) AS total FROM inspirations GROUP BY repo_id"
            ).fetchall()
        return {row["repo_id"]: row["total"] for row in rows}

    def list_inspirations_page(self, repo_id: int, offset: int, limit: int) -> list[sqlite3.Row]:
        """按稳定的更新时间倒序读取一个仓库的灵感分页（issue #219）。"""
        with self._conn() as conn:
            return conn.execute(
                """SELECT i.*, r.name AS repo_name FROM inspirations i
                   JOIN repos r ON r.id = i.repo_id
                   WHERE i.repo_id = ?
                   ORDER BY i.updated_at DESC, i.id DESC
                   LIMIT ? OFFSET ?""",
                (repo_id, limit, offset),
            ).fetchall()

    def count_inspirations(self, repo_id: int) -> int:
        """返回指定仓库的灵感总数（issue #219）。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM inspirations WHERE repo_id = ?", (repo_id,)
            ).fetchone()[0]

    def search_tasks(self, term: str, limit: int = 10) -> list[TaskRow]:
        """按 issue 标题/编号模糊匹配任务（issue #216 全局搜索）。

        与 list_tasks 的 search 过滤同字段（issue_title / issue_iid），
        但额外转义 LIKE 通配符——搜索词里的 % _ 按字面匹配；按任务 id
        倒序（最新任务在前），LIMIT 截断。
        """
        like = f"%{_like_escape(term)}%"
        with self._conn() as conn:
            return _as_task_list(conn.execute(
                """SELECT * FROM tasks
                   WHERE issue_title LIKE ? ESCAPE '\\'
                      OR CAST(issue_iid AS TEXT) LIKE ? ESCAPE '\\'
                   ORDER BY id DESC LIMIT ?""", (like, like, limit)).fetchall())

    def search_inspirations(self, term: str, limit: int = 10) -> list[sqlite3.Row]:
        """按内容模糊匹配灵感（issue #216 全局搜索），JOIN repos 带仓库名。

        与 list_inspirations 同排序（updated_at 降序、同时间按 id 降序）。
        """
        like = f"%{_like_escape(term)}%"
        with self._conn() as conn:
            return conn.execute(
                """SELECT i.*, r.name AS repo_name FROM inspirations i
                   JOIN repos r ON r.id = i.repo_id
                   WHERE i.content LIKE ? ESCAPE '\\'
                   ORDER BY i.updated_at DESC, i.id DESC LIMIT ?""",
                (like, limit)).fetchall()

    def search_repos(self, term: str, limit: int = 10) -> list[RepoRow]:
        """按名称模糊匹配仓库（issue #216 全局搜索），排除软删除。

        与 list_repos 同排序（priority 升序、同优先级按 id）。
        """
        like = f"%{_like_escape(term)}%"
        with self._conn() as conn:
            return _as_repo_list(conn.execute(
                """SELECT * FROM repos
                   WHERE deleted_at IS NULL AND name LIKE ? ESCAPE '\\'
                   ORDER BY priority, id LIMIT ?""", (like, limit)).fetchall())

    def get_inspiration(self, inspiration_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                """SELECT i.*, r.name AS repo_name FROM inspirations i
                   JOIN repos r ON r.id = i.repo_id
                   WHERE i.id = ?""", (inspiration_id,)).fetchone()

    def create_inspiration(self, repo_id: int, content: str) -> int:
        """创建灵感，返回新记录 id。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO inspirations (repo_id, content)
                   VALUES (?, ?)""", (repo_id, content))
            return cur.lastrowid

    def update_inspiration(self, inspiration_id: int, content: str) -> bool:
        """更新灵感内容并刷新 updated_at；记录不存在返回 False。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """UPDATE inspirations SET content=?, updated_at=datetime('now')
                   WHERE id=?""", (content, inspiration_id))
            return cur.rowcount > 0

    def delete_inspiration(self, inspiration_id: int) -> bool:
        """删除灵感及其全部 AI 对话消息（issue #166 级联清理）。

        灵感删除后其 AI 对话（inspiration_messages）不再有展示入口，
        一并删除避免孤儿数据；同一连接同一事务内先删灵感再删消息
        （灵感不存在时直接返回 False，不动消息表）。"""
        with self._conn(write=True) as conn:
            cur = conn.execute("DELETE FROM inspirations WHERE id=?", (inspiration_id,))
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM inspiration_messages WHERE inspiration_id=?",
                (inspiration_id,))
            return True

    # ---- inspiration_messages（issue #166）----
    # 灵感 AI 对话消息：用户围绕灵感与 AI agent 探讨，消息成对保存
    # （user 提问 + assistant 回复），按 id 升序 = 时间序。

    def list_inspiration_messages(
        self, inspiration_id: int, limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """列出灵感对话消息（时间序）；limit 指定时返回最近 limit 条。

        取最近 N 条是传给 AI 的上下文截断策略（避免历史无限膨胀撑爆
        上下文窗口）：``ORDER BY id DESC LIMIT ?`` 取尾部再反转回时间序。
        """
        with self._conn() as conn:
            if limit is not None:
                rows = conn.execute(
                    """SELECT * FROM inspiration_messages
                       WHERE inspiration_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (inspiration_id, limit)).fetchall()
                return rows[::-1]
            return conn.execute(
                """SELECT * FROM inspiration_messages
                   WHERE inspiration_id = ?
                   ORDER BY id ASC""", (inspiration_id,)).fetchall()

    def get_inspiration_message(self, message_id: int) -> sqlite3.Row | None:
        """按 id 查单条灵感对话消息（发送失败回滚删除用）。"""
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM inspiration_messages WHERE id = ?""",
                (message_id,)).fetchone()

    def add_inspiration_message(self, inspiration_id: int, role: str,
                                content: str) -> int:
        """保存一条灵感对话消息，返回新记录 id。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO inspiration_messages (inspiration_id, role, content)
                   VALUES (?, ?, ?)""", (inspiration_id, role, content))
            return cur.lastrowid

    def delete_inspiration_message(self, message_id: int) -> bool:
        """删除单条灵感对话消息（AI 调用失败时回滚用户消息）；不存在返回 False。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "DELETE FROM inspiration_messages WHERE id=?", (message_id,))
            return cur.rowcount > 0

    # ---- repos ----

    def upsert_repo(self, project_id: int, name: str, url: str,
                    prompt_template: str | None = None,
                    enabled: bool = True, local_path: str | None = None,
                    remote_name: str | None = None,
                    remote_username: str | None = None,
                    priority: int = DEFAULT_PRIORITY,
                    timeout_seconds: int | None = None,
                    max_retries: int | None = None,
                    engine: str | None = None,
                    token_expires_at: str | None = None) -> int:
        with self._conn(write=True) as conn:
            conn.execute(
                """INSERT INTO repos (gitlab_project_id, name, url, prompt_template, enabled, local_path, remote_name, remote_username, priority, timeout_seconds, max_retries, engine, token_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(gitlab_project_id) DO UPDATE SET
                     name=excluded.name, url=excluded.url,
                     prompt_template=excluded.prompt_template,
                     enabled=excluded.enabled, local_path=excluded.local_path,
                     remote_name=excluded.remote_name,
                     remote_username=excluded.remote_username,
                     priority=excluded.priority,
                     timeout_seconds=excluded.timeout_seconds,
                     max_retries=excluded.max_retries,
                     engine=excluded.engine,
                     token_expires_at=COALESCE(excluded.token_expires_at, repos.token_expires_at),
                     deleted_at=NULL""",
                (project_id, name, url, prompt_template, 1 if enabled else 0,
                 local_path, remote_name, remote_username, priority,
                 timeout_seconds, max_retries, engine, token_expires_at),
            )
            # 冲突更新路径 lastrowid 不可靠（issue #62：重新添加已删除仓库），
            # 按唯一键 project_id 反查
            row = conn.execute(
                "SELECT id FROM repos WHERE gitlab_project_id=?", (project_id,)).fetchone()
            return row["id"]

    def list_repos(self, include_deleted: bool = False) -> list[RepoRow]:
        """列出仓库；默认过滤已软删除（deleted_at 非空）的行（issue #62）。

        任务历史的仓库名解析等场景需要包含已删除仓库时传 include_deleted=True。
        两种过滤条件写成完整 SQL 常量（不拼接），避免 bandit B608 告警。
        """
        with self._conn() as conn:
            # 按优先级升序（数字小在前），同优先级按 id（issue #51）
            sql = ("SELECT * FROM repos ORDER BY priority, id"
                   if include_deleted else
                   "SELECT * FROM repos WHERE deleted_at IS NULL ORDER BY priority, id")
            return _as_repo_list(conn.execute(sql).fetchall())

    def get_repo(self, repo_id: int) -> RepoRow | None:
        with self._conn() as conn:
            return _as_repo(conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone())

    def get_repo_by_project_id(self, project_id: int,
                               include_deleted: bool = False) -> RepoRow | None:
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
        with self._conn(write=True) as conn:
            conn.execute(
                """UPDATE repos SET deleted_at=datetime('now'), enabled=0
                   WHERE id=?""", (repo_id,))

    def update_repo(self, repo_id: int, **fields) -> None:
        allowed = {"name", "url", "prompt_template", "enabled", "local_path", "remote_name", "remote_username", "priority", "timeout_seconds", "max_retries", "engine", "logo_path", "logo_updated_at", "logo_mime", "token_expires_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._conn(write=True) as conn:
            conn.execute(f"UPDATE repos SET {cols} WHERE id=?",  # nosec B608
                         (*sets.values(), repo_id))

    def set_repo_token_expiry(self, repo_id: int, expires_at: str | None) -> None:
        """保存仓库 token 的 ISO 到期日；None 表示尚未获知。"""
        self.update_repo(repo_id, token_expires_at=expires_at)

    def delete_repo(self, repo_id: int) -> None:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))

    # ---- issue_manual_orders（issue #287）----
    # 概览页「其他」分组手动调度顺序：用户在「调度器执行顺序」排序下拖动
    # issue 上下移动，整组顺序全量替换落库（position 从 0 连续编号），
    # 调度器派发时优先按 position 排序。仅存 Botler 本地数据库。

    def list_manual_orders(self, repo_id: int) -> list[int]:
        """返回指定仓库的 issue_iid 列表（按 position 升序 = 用户调整后的顺序）。

        无手动顺序（从未拖动）时返回空列表。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT issue_iid FROM issue_manual_orders
                   WHERE repo_id=? ORDER BY position ASC""",
                (repo_id,)).fetchall()
            return [r["issue_iid"] for r in rows]

    def get_manual_order_position(self, repo_id: int, issue_iid: int) -> int | None:
        """查询单个 issue 的手动调度位置；未设置返回 None（调度器按默认排序）。"""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT position FROM issue_manual_orders
                   WHERE repo_id=? AND issue_iid=?""",
                (repo_id, issue_iid)).fetchone()
            return row["position"] if row is not None else None

    def replace_manual_orders(self, repo_id: int, iids: list[int]) -> list[int]:
        """全量替换仓库的手动调度顺序（拖动后整组重排即调用此方法）。

        同一事务内先删旧行再逐条插入，position 按入参顺序从 0 连续编号；
        传入空列表清空手动顺序（恢复调度器默认排序）。返回按 position
        排序后的 iid 列表（即入参原序）。"""
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM issue_manual_orders WHERE repo_id=?", (repo_id,))
            for pos, iid in enumerate(iids):
                conn.execute(
                    """INSERT INTO issue_manual_orders
                       (repo_id, issue_iid, position, updated_at)
                       VALUES (?, ?, ?, datetime('now'))""",
                    (repo_id, iid, pos))
        return list(iids)

    # ---- tasks ----

    def create_task(self, repo_id: int, project_id: int, issue_iid: int,
                    issue_title: str, triggered_by: str = "webhook",
                    issue_labels: list[str] | None = None,
                    issue_updated_at: str | None = None,
                    issue_created_at: str | None = None) -> int | None:
        """创建任务。若已有活跃任务则返回 None（去重）。

        issue_labels / issue_updated_at（issue #76）：入队时记录 issue 标签
        与更新时间，调度器按配置的标签优先级选任务派发。
        issue_created_at（issue #234）：入队时记录 issue 创建时间，同标签
        权重时调度器按创建时间升序选任务（创建早的 issue 先处理）。
        """
        labels_json = json.dumps(issue_labels or [], ensure_ascii=False)
        with self._conn(write=True) as conn:
            dup = conn.execute(
                """SELECT id FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone()
            if dup:
                return None
            cur = conn.execute(
                """INSERT INTO tasks (repo_id, project_id, issue_iid, issue_title, status, triggered_by,
                                      issue_labels, issue_updated_at, issue_created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (repo_id, project_id, issue_iid, issue_title, triggered_by,
                 labels_json, issue_updated_at or "", issue_created_at or ""))
            return cur.lastrowid

    def get_task(self, task_id: int) -> TaskRow | None:
        with self._conn() as conn:
            return _as_task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(self, status: str | list[str] | None = None, repo_id: int | None = None,
                   search: str | None = None, limit: int = 50, offset: int = 0) -> list[TaskRow]:
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
            return _as_task_list(conn.execute(sql, params).fetchall())

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

    def list_tasks_export(self, status: str | list[str] | None = None,
                          repo_id: int | None = None, search: str | None = None,
                          date_from: str | None = None,
                          date_to: str | None = None) -> list[TaskRow]:
        """导出任务数据（issue #228）：与 list_tasks 同套过滤条件，不分页全量返回。

        供 GET /api/tasks/export 使用：status（含逗号分隔多值）/ repo_id /
        search（issue 标题或编号模糊匹配）过滤与任务列表完全一致，另支持
        创建时间范围 date_from / date_to（API 层已归一化为
        'YYYY-MM-DD HH:MM:SS' 无时区串，与 created_at 同格式可直接字符串
        比较）。按 id 倒序（最新在前），与任务列表排序一致。
        """
        sql = "SELECT * FROM tasks WHERE 1=1"
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
        if date_from:
            sql += " AND created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND created_at <= ?"
            params.append(date_to)
        sql += " ORDER BY id DESC"
        with self._conn() as conn:
            return _as_task_list(conn.execute(sql, params).fetchall())

    def find_active_task(self, project_id: int, issue_iid: int) -> TaskRow | None:
        with self._conn() as conn:
            return _as_task(conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   AND status IN ('queued','running','retrying')""",
                (project_id, issue_iid)).fetchone())

    def find_latest_task(self, project_id: int, issue_iid: int) -> TaskRow | None:
        """该 issue 最近一次的任务记录（issue #117：概览页重试按钮用）。

        按任务 id 倒序取最新一条（id 递增即创建先后，同 issue 可能因
        重新指派/对账补入队存在多条任务记录）；无记录返回 None。
        """
        with self._conn() as conn:
            return _as_task(conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   ORDER BY id DESC LIMIT 1""",
                (project_id, issue_iid)).fetchone())

    def list_tasks_by_issue(self, project_id: int, issue_iid: int,
                            limit: int = 50) -> list[TaskRow]:
        """该 issue 的全部任务记录（issue #167：概览页右边栏
        「查看执行的详情」数据源）。

        按任务 id 倒序（最新在前），与 find_latest_task 的排序约定一致；
        同 issue 可能因重新指派/对账补入队存在多条任务记录，全部返回
        供前端任务列表切换查看。
        """
        with self._conn() as conn:
            return _as_task_list(conn.execute(
                """SELECT * FROM tasks WHERE project_id=? AND issue_iid=?
                   ORDER BY id DESC LIMIT ?""",
                (project_id, issue_iid, limit)).fetchall())

    def succeeded_durations(self) -> list[tuple[int, str, str, float]]:
        """已完成（succeeded）任务的 (repo_id, repo_name, 完成日, 用时秒数)
        列表（issue #180 + #288）。

        概览页「Issue 完成耗时」统计的数据源：只取成功终态（succeeded）
        任务——成功时系统会给 issue 打 bot-done 标签（executor issue #49），
        用时 = finished_at - created_at（系统接收时间 → bot-done 打标时间，
        与任务详情/任务列表「处理用时」的语义一致，两字段均为 UTC 无后缀串）。
        完成日取 finished_at 的 UTC 日期（'YYYY-MM-DD'），供前端按日分组
        绘制走势图。缺时间字段、解析失败（_parse_db_ts 返回 None）或用时
        为负（时钟异常）的行跳过，不影响整体统计。

        issue #288：附带 repo_id 与 repo_name（LEFT JOIN repos，仓库已软
        删除/名称缺失时回退「未知仓库」），供概览页按「每个开启仓库」拆分
        平均耗时与走势；调用方按 list_repos（仅未删除）筛选展示仓库。
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT t.created_at, t.finished_at, t.repo_id,
                          COALESCE(r.name, '未知仓库') AS repo_name
                   FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id
                   WHERE t.status=? AND t.created_at <> '' AND t.finished_at <> ''
                   ORDER BY t.id""",
                (STATUS_SUCCEEDED,)).fetchall()
        out: list[tuple[int, str, str, float]] = []
        for row in rows:
            c = _parse_db_ts(row["created_at"])
            f = _parse_db_ts(row["finished_at"])
            if c is None or f is None:
                continue
            sec = (f - c).total_seconds()
            if sec < 0:
                continue
            out.append((row["repo_id"], row["repo_name"],
                        f.strftime("%Y-%m-%d"), round(sec, 3)))
        return out

    def set_task_status(self, task_id: int, status: str | None, **fields) -> None:
        """更新任务状态及附加字段（attempt_count / exit_code / error_message /
        error_detail / log_path / started_at / finished_at / claude_session_id /
        hermes_history / commit_sha / dsh_session_id / dsh_transcript）。

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
        with self._conn(write=True) as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id=?", vals)  # nosec B608

    def claim_task(self, task_id: int) -> bool:
        """原子抢占任务（防多实例并发执行同一任务，issue #24）。

        仅当任务处于 queued/retrying 时置为 running（条件 UPDATE），返回
        是否抢到；已是 running（其他实例已领取）或已终态时抢不到。
        跨实例安全：SQLite 写事务串行化保证条件判断与更新原子。
        """
        with self._conn(write=True) as conn:
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
        with self._conn(write=True) as conn:
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
        with self._conn(write=True) as conn:
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
                    [(tid, redact("任务已停止：用户一键停止所有任务")) for tid in stopped])
        return stopped

    def stop_task(self, task_id: int) -> str:
        """单任务手动停止（issue #214）：queued/running/retrying → interrupted。

        返回结果码：
        - "not_found"：任务不存在
        - "bad_state"：状态非活跃（终态任务不可停止）
        - "ok"：停止成功
        与 stop_active_tasks 语义一致：interrupted 为终态（requeue_interrupted
        只捞 running/retrying），用户手动停止的任务不会在平台重启后被重新
        入队执行；同时写入 warn 日志供任务详情页追溯。条件 UPDATE 兜底并发：
        多请求同时停止同一任务时先到者生效。
        """
        with self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] not in (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING):
                return "bad_state"
            conn.execute(
                """UPDATE tasks SET status='interrupted',
                   error_message='用户手动停止（单任务停止）',
                   finished_at=datetime('now')
                   WHERE id=? AND status IN ('queued','running','retrying')""",
                (task_id,))
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'warn', ?)",
                (task_id, "任务已停止：用户手动停止当前任务"))
        return "ok"

    # ---- 人工优先级（issue #242）----

    def set_task_manual_priority(self, task_id: int, priority: int | None) -> str:
        """设置/清除任务人工优先级（issue #242）。

        priority 为整数或 None（清除 = 恢复按系统规则排序）。仅排队中
        （queued）任务可设置——执行中（running）任务不受人工干预影响
        （验收标准：已 running 任务不受影响）。终态任务不可设置。
        返回结果码：ok / not_found / bad_state。
        """
        with self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] != STATUS_QUEUED:
                return "bad_state"
            if priority is None:
                conn.execute(
                    "UPDATE tasks SET manual_priority=NULL WHERE id=?", (task_id,))
                conn.execute(
                    "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'info', ?)",
                    (task_id, "人工优先级已清除：任务恢复按系统规则排序"))
            else:
                conn.execute(
                    "UPDATE tasks SET manual_priority=? WHERE id=?",
                    (int(priority), task_id))
                conn.execute(
                    "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'info', ?)",
                    (task_id, f"人工优先级已设置为 {int(priority)}"))
        return "ok"

    def reorder_manual_priority(self, task_id: int, action: str) -> tuple[str, int | None]:
        """对排队任务执行人工优先级重排动作（issue #242）。

        action 取值：top（置顶）/ up（上移）/ down（下移）/ bottom（置底）。
        仅排队中（queued）任务可重排。同仓库排队任务中已设置人工优先级
        的任务构成「手动序列表」，按 (manual_priority, id) 升序：
        - top：目标移到手动序列表最前（未在表内则插入最前）；
        - up：目标在表内则与前一任务交换；不在表内（按系统规则排序中）
          则追加到表尾（上移到所有手动任务之后、系统任务之前）；
        - down：目标在表内且非表尾则与后一任务交换；不在表内或已是表尾
          不动作；
        - bottom：目标在表内则移到表尾；不在表内不动作。
        动作完成后手动序列表重排编号 0..n-1 落库（manual_priority 紧凑
        连续），并写任务日志供审计追溯。返回 (结果码, 目标新优先级)：
        - ("ok", new_priority)：动作成功（未移动时 new_priority 为当前值）
        - ("not_found", None)：任务不存在
        - ("bad_state", None)：任务非排队状态
        - ("bad_action", None)：非法动作
        """
        if action not in ("top", "up", "down", "bottom"):
            return ("bad_action", None)
        with self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return ("not_found", None)
            if row["status"] != STATUS_QUEUED:
                return ("bad_state", None)
            manual = [r["id"] for r in conn.execute(
                """SELECT id FROM tasks WHERE repo_id=? AND status='queued'
                   AND manual_priority IS NOT NULL
                   ORDER BY manual_priority ASC, id ASC""",
                (row["repo_id"],))]
            idx = manual.index(task_id) if task_id in manual else None
            if action == "top":
                if idx is not None:
                    manual.pop(idx)
                manual.insert(0, task_id)
            elif action == "up":
                if idx is None:
                    # 按系统规则排序的任务上移一位 → 追加到手动序列表尾
                    # （排到所有手动任务之后、系统任务之前）
                    manual.append(task_id)
                elif idx > 0:
                    manual[idx], manual[idx - 1] = manual[idx - 1], manual[idx]
            elif action == "down":
                if idx is not None and idx < len(manual) - 1:
                    manual[idx], manual[idx + 1] = manual[idx + 1], manual[idx]
            elif action == "bottom":
                if idx is not None:
                    manual.pop(idx)
                    manual.append(task_id)
            new_priority = manual.index(task_id) if task_id in manual else None
            if new_priority is not None:
                conn.executemany(
                    "UPDATE tasks SET manual_priority=? WHERE id=?",
                    [(i, tid) for i, tid in enumerate(manual)])
                conn.execute(
                    "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'info', ?)",
                    (task_id, f"人工优先级操作：{action}，新优先级 {new_priority}"))
            else:
                conn.execute(
                    "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'info', ?)",
                    (task_id, f"人工优先级操作：{action}（队列位置未变化）"))
        return ("ok", new_priority)

    def dequeue_task(self, task_id: int) -> str:
        """手动移出队列（issue #242）：排队中任务 → canceled_by_user（终态）。

        与手动停止（issue #214）语义区分：移出队列是「取消排队」，状态
        标记 canceled_by_user 可追溯；任务不会在平台重启后自动恢复（终态，
        requeue_interrupted 只捞 running/retrying），用户可手动重试重新
        入队（retry_task 支持 canceled_by_user）。running/retrying 任务
        不可移出（需先停止）。返回结果码：ok / not_found / bad_state。
        """
        with self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] != STATUS_QUEUED:
                return "bad_state"
            conn.execute(
                """UPDATE tasks SET status='canceled_by_user',
                   error_message='用户手动移出队列（取消排队）',
                   finished_at=datetime('now')
                   WHERE id=? AND status='queued'""",
                (task_id,))
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, 'warn', ?)",
                (task_id, "任务已移出队列：用户手动取消排队（canceled_by_user）"))
        return "ok"

    def retry_task(self, task_id: int) -> str:
        """手动重试（issue #36）：终态失败任务重置为 queued，返回结果码。

        - "ok"：重置成功（failed/interrupted/canceled_by_user → queued）
        - "not_found"：任务不存在
        - "bad_state"：状态非 failed/interrupted/canceled_by_user
          （含已被重试过的情况）
        - "conflict"：同一 issue 已有活跃任务（部分唯一索引去重）

        重置失败相关字段（attempt_count 归零、清空 exit_code/error_message/
        error_detail/commit_sha/started_at/finished_at），triggered_by 标记
        manual 供前端「来源」列展示；保留 claude_session_id（断点续跑接续
        上次会话）与 log_path（日志文件重试时覆盖重写）。
        条件 UPDATE 兜底并发：多请求同时重试时先到者生效。
        """
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] not in (STATUS_FAILED, STATUS_INTERRUPTED,
                                    STATUS_CANCELED):
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
                   WHERE id=? AND status IN ('failed','interrupted','canceled_by_user')""",
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
        with self._conn(write=True) as conn:
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

    # ---- 数据保留清理（issue #204） ----

    def expired_task_log_paths(self, cutoff: str) -> list[str]:
        """返回过期终态任务的日志路径；任务摘要行不会删除。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT log_path FROM tasks
                   WHERE finished_at IS NOT NULL AND finished_at < ?
                     AND status IN (?, ?, ?, ?) AND log_path IS NOT NULL AND log_path != ''""",
                (cutoff, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_CANCELED),
            ).fetchall()
        return [str(row["log_path"]) for row in rows]

    def prune_task_logs(self, cutoff: str) -> int:
        """删除过期终态任务的明细日志，保留 tasks 中的执行摘要。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """DELETE FROM task_logs WHERE task_id IN (
                     SELECT id FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?
                       AND status IN (?, ?, ?, ?)
                   )""",
                (cutoff, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_CANCELED),
            )
            return cur.rowcount

    def prune_notification_events(self, cutoff: str) -> int:
        """删除指定时间之前的通知事件。"""
        with self._conn(write=True) as conn:
            return conn.execute(
                "DELETE FROM notification_events WHERE created_at < ?", (cutoff,)).rowcount

    # ---- task_logs ----

    def add_log(self, task_id: int, level: str, message: str) -> None:
        # 统一日志脱敏（issue #259）：任务日志可被导出/审计查看，写入前打码，
        # token/密钥（git remote userinfo、Authorization 头、PAT 等）不落库
        with self._conn(write=True) as conn:
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
                (task_id, level, redact(message)))

    def list_logs(self, task_id: int, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM task_logs WHERE task_id=?
                   ORDER BY id ASC LIMIT ?""", (task_id, limit)).fetchall()

    def add_logs(self, task_id: int, entries: list[tuple[str, str]]) -> None:
        """批量写日志：entries 为 [(level, message), ...]"""
        with self._conn(write=True) as conn:
            conn.executemany(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
                [(task_id, lv, redact(msg)) for lv, msg in entries])

    # ---- task_progress（issue #281 §4.1：结构化任务进度账本）----

    def record_task_progress(self, task_id: int, step_no: int, step_desc: str,
                             status: str, evidence: str | None = None,
                             files: str | None = None,
                             verified_at: str | None = None) -> int:
        """追加一条进度账本记录（只增不改快照式，恢复时取每步最新状态）。

        中断恢复时 executor 按每步最新状态行渲染确定性交接单（§4.4），
        避免 agent 反复检查实现/重复实现。返回新行 id。
        """
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO task_progress
                     (task_id, step_no, step_desc, status, evidence, files, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, step_no, step_desc, status, evidence, files, verified_at))
            return cur.lastrowid

    def list_task_progress(self, task_id: int) -> list[sqlite3.Row]:
        """按步骤序返回该任务全部账本行（含历史快照，可追溯）。"""
        with self._conn() as conn:
            return conn.execute(
                """SELECT * FROM task_progress WHERE task_id=?
                   ORDER BY step_no ASC, id ASC""", (task_id,)).fetchall()

    def latest_task_progress(self, task_id: int) -> list[sqlite3.Row]:
        """每步最新状态行（快照式账本取每步最新，恢复交接单数据源）。"""
        with self._conn() as conn:
            return conn.execute(
                """SELECT tp.* FROM task_progress tp
                   JOIN (SELECT step_no, MAX(id) AS max_id FROM task_progress
                         WHERE task_id=? GROUP BY step_no) m
                     ON tp.id = m.max_id
                   ORDER BY tp.step_no ASC""", (task_id,)).fetchall()


    # ---- task_usage（issue #235：任务 token 用量与费用统计）----

    def save_task_usage(self, task_id: int, *, engine: str,
                        model: str | None = None,
                        prompt_tokens: int = 0,
                        completion_tokens: int = 0,
                        total_tokens: int = 0,
                        estimated_cost: float | None = None,
                        currency: str = "USD",
                        raw_usage: str | None = None) -> None:
        """保存一次任务执行的 token 用量（同任务覆盖上一次执行）。

        一个任务只跑一种引擎（issue #120），重试/续跑以最后一次执行
        为准（与 tasks.engine 覆盖语义一致）；统计页按 task_usage 行聚合。
        """
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM task_usage WHERE task_id=?", (task_id,))
            conn.execute(
                """INSERT INTO task_usage
                     (task_id, engine, model, prompt_tokens, completion_tokens,
                      total_tokens, estimated_cost, currency, raw_usage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, engine, model,
                 int(prompt_tokens or 0), int(completion_tokens or 0),
                 int(total_tokens or 0),
                 estimated_cost, currency, raw_usage))

    def get_task_usage(self, task_id: int) -> sqlite3.Row | None:
        """取任务最近一次执行的 token 用量（无记录返回 None）。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM task_usage WHERE task_id=?",
                (task_id,)).fetchone()

    def get_task_usage_map(self, task_ids: list[int]) -> dict[int, sqlite3.Row]:
        """批量取多个任务的用量（任务列表可选展示，避免 N+1 查询）。"""
        if not task_ids:
            return {}
        q = ", ".join("?" * len(task_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM task_usage WHERE task_id IN ({q})",  # nosec B608
                task_ids).fetchall()
        return {r["task_id"]: r for r in rows}

    def usage_stats(self, repo_id: int | None = None,
                    engine: str | None = None,
                    since: str | None = None,
                    until: str | None = None) -> dict:
        """按仓库/引擎/时间段聚合 token 用量统计（issue #235 验收标准 3）。

        过滤条件：repo_id（task_usage JOIN tasks 按仓库）、engine（引擎名）、
        since/until（task_usage.created_at 的 UTC 日期串 'YYYY-MM-DD'，
        含端点）。返回：
        - summary：全局合计（记录数 / prompt / completion / total / 费用）
        - by_repo：按仓库分组 [{repo_id, repo_name, task_count, 各 token 合计, cost}]
        - by_engine：按引擎分组 [{engine, task_count, 各 token 合计, cost}]
        - by_date：按记录日期分组 [{date, task_count, 各 token 合计, cost}]
        费用合计只累加 estimated_cost 非空行；空结果返回空列表（前端空态）。
        """
        where = ["u.id IS NOT NULL"]
        params: list = []
        if repo_id is not None:
            where.append("t.repo_id=?")
            params.append(repo_id)
        if engine:
            where.append("u.engine=?")
            params.append(engine)
        if since:
            where.append("date(u.created_at) >= ?")
            params.append(since)
        if until:
            where.append("date(u.created_at) <= ?")
            params.append(until)
        cond = " AND ".join(where)
        base = f"""FROM task_usage u JOIN tasks t ON t.id = u.task_id WHERE {cond}"""

        def _agg(sql: str, extra_params: list) -> list[sqlite3.Row]:
            with self._conn() as conn:
                return conn.execute(sql, [*params, *extra_params]).fetchall()

        with self._conn() as conn:
            summary = conn.execute(
                f"""SELECT COUNT(*) AS task_count,
                           COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                           COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                           COALESCE(SUM(u.estimated_cost), 0) AS estimated_cost,
                           COALESCE(SUM(CASE WHEN u.estimated_cost IS NOT NULL
                                             THEN 1 ELSE 0 END), 0) AS costed_count
                    {base}""", params).fetchone()  # nosec B608


        by_repo = _agg(
            f"""SELECT t.repo_id AS repo_id, COALESCE(r.name, '') AS repo_name,
                       COUNT(*) AS task_count,
                       COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(u.estimated_cost), 0) AS estimated_cost
                FROM task_usage u JOIN tasks t ON t.id = u.task_id
                LEFT JOIN repos r ON r.id = t.repo_id
                WHERE {cond}
                GROUP BY t.repo_id ORDER BY total_tokens DESC""", [])  # nosec B608
        by_engine = _agg(
            f"""SELECT u.engine AS engine, COUNT(*) AS task_count,
                       COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(u.estimated_cost), 0) AS estimated_cost
                {base}
                GROUP BY u.engine ORDER BY total_tokens DESC""", [])  # nosec B608
        by_date = _agg(
            f"""SELECT date(u.created_at) AS date, COUNT(*) AS task_count,
                       COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(u.estimated_cost), 0) AS estimated_cost
                {base}
                GROUP BY date(u.created_at) ORDER BY date ASC""", [])  # nosec B608
        # 取首个非空 currency（同一任务库内货币一致，防御性兜底 USD）
        with self._conn() as conn:
            cur_row = conn.execute(
                f"""SELECT u.currency AS currency FROM task_usage u
                    JOIN tasks t ON t.id = u.task_id WHERE {cond}
                    ORDER BY u.id DESC LIMIT 1""", params).fetchone()  # nosec B608
        currency = cur_row["currency"] if cur_row else "USD"
        return {
            "summary": dict(summary),
            "currency": currency,
            "by_repo": [dict(r) for r in by_repo],
            "by_engine": [dict(r) for r in by_engine],
            "by_date": [dict(r) for r in by_date],
        }

    def dashboard_task_rows(self, days: int = 0) -> list[sqlite3.Row]:
        """拉取统计看板窗口内的任务行（issue #264）。

        days=0 表示全部时间段；days>0 按任务创建时间（UTC）最近 N 天过滤
        （与任务列表同表同口径，保证「统计页数字与任务列表一致」）。
        返回行含 id/repo_id/status/engine/triggered_by/created_at/
        finished_at/error_message 与仓库名（LEFT JOIN repos，软删仓库
        保留名称展示）。
        """
        sql = """SELECT t.id AS id, t.repo_id AS repo_id, t.status AS status,
                        t.engine AS engine, t.triggered_by AS triggered_by,
                        t.created_at AS created_at, t.finished_at AS finished_at,
                        t.error_message AS error_message,
                        t.failure_category AS failure_category,
                        COALESCE(r.name, '') AS repo_name
                 FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id WHERE 1=1"""
        params: list = []
        if days and days > 0:
            # 时间戳为 UTC 串（_parse_db_ts 语义），datetime('now') 亦为 UTC，
            # 字符串比较即时间比较
            sql += " AND t.created_at >= datetime('now', ?)"
            params.append(f"-{days} days")
        sql += " ORDER BY t.id ASC"
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()  # nosec B608

    def dashboard_stats(self, days: int = 0) -> dict:
        """统计看板聚合（issue #264）：本地任务表聚合，无 GitLab 依赖。

        数据源与任务列表同表（tasks），保证验收标准 1「统计页各维度数字
        与任务列表一致」；时间段按任务创建时间（UTC）过滤，days=0 为全部。
        days 同时传给 aggregate_dashboard 决定 by_source_daily 的窗口语义
        （days>0 最近 N 天零填充、days=0 仅返回有数据日期，issue #224）。
        聚合细节见模块级 aggregate_dashboard（纯函数，可单测）。
        """
        return aggregate_dashboard(self.dashboard_task_rows(days), days=days)
    def task_stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}


    def task_duration_histogram(self, buckets: list[float]) -> tuple[int, float, list[int]]:
        """任务执行时长直方图聚合（issue #208，/metrics 数据源）。

        时长 = finished_at - started_at（UTC，SQLite julianday 差值换算秒），
        仅统计两个时间戳都已写入的任务（queued/running 无 finished_at 自然
        排除）；时间串非法（julianday 解析失败 → NULL）与负值（时钟回拨）
        的行不参与统计。返回 (count, sum_seconds, bucket_counts)：
        bucket_counts[i] 为 duration < buckets[i] 的累计计数，+Inf 桶由
        调用方用 count 补齐（Prometheus 直方图语义）。
        """
        if not buckets:
            return (0, 0.0, [])
        # nosec B608：SQL 中仅拼接整数列别名 b{i}，全部取值走 ? 参数绑定，
        # 与同文件既有动态列名 SQL 同款注释豁免（bandit 无法识别参数化）
        marks = ", ".join(
            f"COALESCE(SUM(CASE WHEN dur < ? THEN 1 ELSE 0 END), 0) AS b{i}"  # nosec B608
            for i in range(len(buckets)))
        sql = (
            # nosec B608：marks 仅由整数索引拼成的列别名（b0..bn），无外部输入
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(dur), 0.0) AS total, " + marks + " "  # nosec B608
            "FROM ("
            "  SELECT (julianday(finished_at) - julianday(started_at)) * 86400.0 AS dur "
            "  FROM tasks "
            "  WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
            ") WHERE dur IS NOT NULL AND dur >= 0"
        )
        with self._conn() as conn:
            row = conn.execute(sql, [float(b) for b in buckets]).fetchone()
        return (
            int(row["cnt"]),
            float(row["total"]),
            [int(row[f"b{i}"]) for i in range(len(buckets))],
        )

    # ---- notification_events（issue #21）----

    def add_notification(self, type_: str, title: str, body: str = "",
                         repo_name: str | None = None, task_id: int | None = None,
                         data: str | None = None) -> int | None:
        """记录一条通知事件，返回 id；同一 task_id 重复记录返回 None（幂等）。"""
        with self._conn(write=True) as conn:
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


    # ---- 聚合告警数据源（issue #229）----

    def task_failure_stats(self, since_ts: str) -> dict:
        """近窗口内终态任务失败统计（失败率告警数据源）。

        统计 finished_at >= since_ts 的终态任务（succeeded + failed）：
        {"total": 总数, "failed": 失败数, "rate": 失败率（0.0~1.0，
        total=0 时 rate=0.0）}。失败率 = failed / (succeeded + failed)。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "WHERE finished_at IS NOT NULL AND finished_at >= ? "
                "AND status IN (?, ?) GROUP BY status",
                (since_ts, STATUS_SUCCEEDED, STATUS_FAILED)).fetchall()
        total = failed = 0
        for r in rows:
            n = int(r["n"])
            total += n
            if r["status"] == STATUS_FAILED:
                failed = n
        rate = failed / total if total else 0.0
        return {"total": total, "failed": failed, "rate": rate}

    def count_terminal_since(self, since_ts: str) -> int:
        """统计 since_ts 之后进入终态（succeeded/failed/interrupted/
        canceled_by_user）的任务数（队列「无进度」判定，issue #229）：

        窗口内有任务收尾 = 有进度，不触发队列堆积告警。interrupted/
        canceled 也计入进度（平台在推进，只是任务未成功）。
        """
        with self._conn() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE finished_at IS NOT NULL "
                "AND finished_at >= ? AND status IN (?, ?, ?, ?)",
                (since_ts, STATUS_SUCCEEDED, STATUS_FAILED,
                 STATUS_INTERRUPTED, STATUS_CANCELED)).fetchone()[0])

    def last_alert_notification(self, type_: str) -> sqlite3.Row | None:
        """最近一条指定类型的告警通知（全局节流判定，issue #229）。

        告警事件无 repo_name（平台级，跨仓库/跨配置），按 type 取最近
        一条；无则返回 None。
        """
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM notification_events WHERE type = ? "
                "ORDER BY id DESC LIMIT 1", (type_,)).fetchone()


    # ---- tools（issue #172：MCP 工具管理）----

    def list_tools(self) -> list[sqlite3.Row]:
        """列出全部工具（启用/停用都返回，前端按 enabled 展示开关）。

        按 id 升序（安装/创建顺序），与工具页列表稳定对应。
        """
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tools ORDER BY id ASC").fetchall()

    def get_tool(self, tool_id: int) -> sqlite3.Row | None:
        """按 id 查单个工具；不存在返回 None。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tools WHERE id=?", (tool_id,)).fetchone()

    def get_tool_by_name(self, name: str) -> sqlite3.Row | None:
        """按唯一名查工具（重名冲突检测用）。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tools WHERE name=?", (name,)).fetchone()

    def create_tool(self, name: str, description: str, kind: str,
                    command: str, args: str, env: str, url: str,
                    source: str, source_url: str) -> int:
        """创建工具，返回新记录 id（name 唯一约束冲突由调用方预检）。

        args / env 为 JSON 文本（tools 模块负责序列化与校验）。
        """
        with self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO tools
                   (name, description, kind, command, args, env, url,
                    source, source_url, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (name, description, kind, command, args, env, url,
                 source, source_url))
            return cur.lastrowid

    def update_tool(self, tool_id: int, **fields) -> bool:
        """按字段更新工具并刷新 updated_at；记录不存在返回 False。

        fields 白名单由调用方保证（api/tools.py 显式构造）。
        """
        if not fields:
            return self.get_tool(tool_id) is not None
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn(write=True) as conn:
            # 字段名白名单由调用方显式构造（api/tools.py 固定键），无注入风险
            cur = conn.execute(
                f"UPDATE tools SET {sets}, updated_at=datetime('now') "
                "WHERE id=?",  # nosec B608
                (*fields.values(), tool_id))
            return cur.rowcount > 0

    def delete_tool(self, tool_id: int) -> bool:
        """删除工具；不存在返回 False。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "DELETE FROM tools WHERE id=?", (tool_id,))
            return cur.rowcount > 0

    def set_tool_enabled(self, tool_id: int, enabled: bool) -> bool:
        """启用/停用工具并刷新 updated_at；不存在返回 False。"""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "UPDATE tools SET enabled=?, updated_at=datetime('now') "
                "WHERE id=?", (1 if enabled else 0, tool_id))
            return cur.rowcount > 0

    def get_tool_meta(self, key: str) -> str:
        """读工具页面元信息（如远端市场索引 URL）；无记录返回空串。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM tool_meta WHERE key=?",
                (key,)).fetchone()
            return row["value"] if row else ""

    def set_tool_meta(self, key: str, value: str) -> None:
        """写工具页面元信息（UPSERT）。"""
        with self._conn(write=True) as conn:
            conn.execute(
                """INSERT INTO tool_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value))

# ---- issue #264：统计看板聚合（模块级纯函数，可单测）----

# 失败原因 Top 分布条数上限
FAILURE_REASON_TOP_N = 10

# triggered_by 来源展示名（webhook/手动/对账，issue #224 来源维度）
_SOURCE_DISPLAY = {"webhook": "webhook", "manual": "手动", "reconcile": "对账"}


def _task_duration_seconds(created_at: str | None,

                           finished_at: str | None) -> float | None:
    """任务耗时秒数：finished_at - created_at。

    口径与任务列表「处理用时」（fmtDuration(created_at, finished_at)）及
    issue #180 一致；缺字段 / 格式非法（_parse_db_ts 返回 None）/ 结束早于
    开始（时钟异常）返回 None，由调用方剔除，不影响整体统计。
    """
    c = _parse_db_ts(created_at) if created_at else None
    f = _parse_db_ts(finished_at) if finished_at else None
    if c is None or f is None:
        return None
    sec = (f - c).total_seconds()
    return round(sec, 3) if sec >= 0 else None


def _normalize_failure_reason(msg: str) -> str:
    """失败原因归一化：空白折叠为单空格并截断到 100 字符。

    同一失败文案（换行/缩进差异）聚合到同一原因桶，Top 分布去噪；
    截断避免超长错误堆叠占满看板。
    """
    text = " ".join((msg or "").split())
    return text[:100]


def _source_daily_trend(rows, days: int = 0,
                              today: date | None = None) -> list[dict]:
    """按来源×日期聚合逐日趋势（issue #224）。

    rows 为 dashboard_task_rows 的查询结果（含 created_at/triggered_by/
    status/finished_at 等字段）。按任务创建日期（UTC，created_at 前 10 位
    YYYY-MM-DD）分组：
    - days>0：返回最近 N 天窗口（以 today 为参考日，默认当前 UTC 日期）
      内每个来源的逐日序列，窗口内出现过的来源在无任务日期零填充
      （task_count=0、success_rate/avg_duration 为 None），保证趋势图横轴
      连续；
    - days=0：仅返回有任务的日期（不零填充，日期升序）。
    每条记录：{date, source, name, task_count, succeeded_count,
    failed_count, interrupted_count, success_rate, avg_duration_seconds}，
    日期升序、同日按来源 key 升序；created_at 缺失或非 YYYY-MM-DD 格式的
    行跳过。来源展示名与 by_source 口径一致（webhook/手动/对账/其他）。
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    # 逐日×来源聚合桶
    buckets: dict[tuple[str, str], dict] = {}
    sources_in_window: set[str] = set()
    dates_with_data: set[str] = set()
    for row in rows:
        created = row["created_at"] or ""
        day = created[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue  # 缺失/非法日期：不参与趋势
        src_raw = row["triggered_by"] or ""
        status = row["status"] or ""
        dur = _task_duration_seconds(row["created_at"], row["finished_at"])
        key = (day, src_raw)
        b = buckets.setdefault(key, {
            "task_count": 0, "succeeded": 0, "failed": 0,
            "interrupted": 0, "durations": [],
        })
        b["task_count"] += 1
        if status == STATUS_SUCCEEDED:
            b["succeeded"] += 1
        elif status == STATUS_FAILED:
            b["failed"] += 1
        elif status == STATUS_INTERRUPTED:
            b["interrupted"] += 1
        if dur is not None:
            b["durations"].append(dur)
        sources_in_window.add(src_raw)
        dates_with_data.add(day)

    if days and days > 0:
        start = today - timedelta(days=days - 1)
        ordered_dates = [str(start + timedelta(days=i)) for i in range(days)]
    else:
        ordered_dates = sorted(dates_with_data)

    out: list[dict] = []
    for day in ordered_dates:
        # days>0：窗口内每个来源逐日零填充（趋势图横轴连续）；
        # days=0：仅输出当天有任务的来源（不零填充，输出紧凑）
        day_sources = (sorted(sources_in_window) if (days and days > 0)
                       else sorted(s for (d, s) in buckets if d == day))
        for src_raw in day_sources:
            gb = buckets.get((day, src_raw))
            n = gb["task_count"] if gb else 0
            out.append({
                "date": day,
                "source": src_raw,
                "name": _SOURCE_DISPLAY.get(src_raw, src_raw or "其他"),
                "task_count": n,
                "succeeded_count": gb["succeeded"] if gb else 0,
                "failed_count": gb["failed"] if gb else 0,
                "interrupted_count": gb["interrupted"] if gb else 0,
                "success_rate": (round(gb["succeeded"] / n, 4) if gb and n else None),
                "avg_duration_seconds": (
                    round(sum(gb["durations"]) / len(gb["durations"]), 3)
                    if gb and gb["durations"] else None),
            })
    return out


def aggregate_dashboard(rows, days: int = 0) -> dict:
    """由任务行聚合统计看板数据（issue #264）。

    rows 为 dashboard_task_rows 的查询结果（含 id/repo_id/status/engine/
    triggered_by/created_at/finished_at/error_message/repo_name）。返回：
    - overview: {task_count, succeeded_count, failed_count,
      interrupted_count, success_rate, avg_duration_seconds}
      success_rate = succeeded / task_count（保留 4 位小数，无任务为 None）；
      avg_duration_seconds 只统计有合法耗时的任务（无合法耗时任务为 None）；
    - by_engine / by_repo / by_source: 分组列表 [{key, name, task_count,
      succeeded_count, failed_count, interrupted_count, success_rate,
      avg_duration_seconds}]，按 task_count 降序、并列按名称升序；
      engine 为空显示「未指定」，triggered_by 未知显示原名（'' 显示「其他」）；
    - by_source_daily: 按来源×日期逐日趋势（issue #224）[{date, source,
      name, task_count, succeeded_count, failed_count, interrupted_count,
      success_rate, avg_duration_seconds}]——days>0 时最近 N 天窗口内每个
      来源逐日零填充（趋势图横轴连续），days=0 时仅返回有数据的日期；
      days 为 aggregate_dashboard 的窗口参数；
    - failure_reasons: 失败/中断任务 error_message 归一化后的 Top N
      [{reason, count}]，按 count 降序、count 相同按原因升序。
    成功率/耗时/失败原因口径与任务列表一致（失败原因展示口径：failed +
    interrupted 且有 error_message，见 Tasks.jsx）。
    """
    total = {"task_count": 0, "succeeded": 0, "failed": 0, "interrupted": 0}
    durations: list[float] = []
    by_engine: dict = {}
    by_repo: dict = {}
    by_source: dict = {}
    reasons: dict[str, int] = {}
    # issue #274：失败原因分类分布——failed/interrupted 任务按失败原因分类
    # 聚合（tasks.failure_category 优先，旧任务无分类时按 error_message 实时
    # 规则分类兜底），统计看板「失败原因 Top」与「分类分布」联动
    failure_categories: dict[str, int] = {}

    for row in rows:
        status = row["status"] or ""
        dur = _task_duration_seconds(row["created_at"], row["finished_at"])
        total["task_count"] += 1
        if status == STATUS_SUCCEEDED:
            total["succeeded"] += 1
        elif status == STATUS_FAILED:
            total["failed"] += 1
        elif status == STATUS_INTERRUPTED:
            total["interrupted"] += 1
        if dur is not None:
            durations.append(dur)

        engine = row["engine"] or "未指定"
        src_raw = row["triggered_by"] or ""
        src = src_raw or "其他"
        repo_id = row["repo_id"]
        for bucket, key, name in (
            (by_engine, engine, engine),
            (by_repo, repo_id, row["repo_name"] or f"仓库 {repo_id}"),
            (by_source, src, _SOURCE_DISPLAY.get(src_raw, src)),
        ):
            g = bucket.setdefault(key, {
                "key": key, "name": name, "task_count": 0,
                "succeeded": 0, "failed": 0, "interrupted": 0, "durations": [],
            })
            g["task_count"] += 1
            if status == STATUS_SUCCEEDED:
                g["succeeded"] += 1
            elif status == STATUS_FAILED:
                g["failed"] += 1
            elif status == STATUS_INTERRUPTED:
                g["interrupted"] += 1
            if dur is not None:
                g["durations"].append(dur)

        if status in (STATUS_FAILED, STATUS_INTERRUPTED) \
                and (row["error_message"] or "").strip():
            reason = _normalize_failure_reason(row["error_message"])
            reasons[reason] = reasons.get(reason, 0) + 1
            # issue #274：按分类聚合（落库值优先，缺失时实时分类兜底，
            # 保证存量任务与统计口径一致）
            category = (row["failure_category"] or "").strip() or \
                classify_failure(row["error_message"])
            failure_categories[category] = failure_categories.get(category, 0) + 1

    def _group(bucket: dict) -> list[dict]:
        out = []
        for g in bucket.values():
            n = g["task_count"]
            out.append({
                "key": g["key"],
                "name": g["name"],
                "task_count": n,
                "succeeded_count": g["succeeded"],
                "failed_count": g["failed"],
                "interrupted_count": g["interrupted"],
                "success_rate": round(g["succeeded"] / n, 4) if n else None,
                "avg_duration_seconds": (
                    round(sum(g["durations"]) / len(g["durations"]), 3)
                    if g["durations"] else None),
            })
        out.sort(key=lambda x: (-x["task_count"], str(x["name"])))
        return out

    n = total["task_count"]
    return {
        "overview": {
            "task_count": n,
            "succeeded_count": total["succeeded"],
            "failed_count": total["failed"],
            "interrupted_count": total["interrupted"],
            "success_rate": round(total["succeeded"] / n, 4) if n else None,
            "avg_duration_seconds": (
                round(sum(durations) / len(durations), 3) if durations else None),
        },
        "by_engine": _group(by_engine),
        "by_repo": _group(by_repo),
        "by_source": _group(by_source),
        "by_source_daily": _source_daily_trend(rows, days),
        "failure_reasons": [
            # issue #274：每条失败原因附分类（category + 展示名），统计看板
            # 失败原因 Top 列表展示分类徽章，与详情页/失败评论口径联动
            {"reason": r, "count": c, "category": classify_failure(r),
             "category_name": category_label(classify_failure(r))}
            for r, c in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
            [:FAILURE_REASON_TOP_N]
        ],
        # issue #274：失败原因分类分布（env/engine/unsolvable/unknown Top），
        # 与「失败原因 Top」同源（failed + interrupted 任务），排序按计数
        # 降序、同计数按分类展示名升序
        "failure_categories": [
            {"category": c, "name": category_label(c), "count": n}
            for c, n in sorted(failure_categories.items(),
                               key=lambda kv: (-kv[1], category_label(kv[0])))
        ],
    }
