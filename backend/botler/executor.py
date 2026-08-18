"""Claude Code 执行器。

流程（设计方案 §5.5）：
1. 准备干净工作区（fetch / 切回默认主分支 / reset --hard / clean -fd / git pull --rebase）
2. 渲染提示词（全局/仓库模版 + 变量）
3. 注入环境变量（GITLAB_TOKEN 等只走子进程 env，不进提示词 transcript）
4. subprocess 跑 `claude -p --output-format json`，带超时
5. 结果判定：exit 0 且 issue 已关闭 → 成功；否则重试（最多 max_retries）
6. 收尾：仍失败 → issue 留失败评论 + 打 bot-failed 标签

断点续跑（issue #8）：每次执行后把 claude 会话 id 落库；重试或平台
重启恢复（调度器 requeue_interrupted 重新入队）时用 `claude --resume`
接续上次会话，且工作区只 fetch 不清空（保留 Claude 已做的修改），
从上次中断处继续而非从头重跑。会话文件丢失时自动降级为全新会话。

git 凭据通过 GIT_ASKPASS 注入：askpass 脚本（0700）保留在工作区根目录，
每次 prepare 覆盖刷新（token 轮换自动生效）。保留不删除，避免并发/重试时
脚本缺失导致 fetch 回退 credential helper 旧凭据（issue #12）。
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import DEFAULT_RESUME_PROMPT, ConfigManager

from .database import (
    Database, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED,
    STATUS_INTERRUPTED,
)
from .dsh_runner import DshRunner, DshSdkNotInstalledError
from .dsh_sessions import effective_session_root, normalize_session_root_encoding
from .hermes_sdk_runner import HermesSdkRunner, HermesSdkNotInstalledError
from .events import EventBus, parse_claude_stream_line, parse_hermes_event_line
from .env_snapshot import (
    collect_env_snapshot,
    error_snapshot,
    serialize_snapshot,
)
from .gitlab_client import (
    PIPELINE_TERMINAL_STATES, GitLabClient, GitLabError, is_transient_error,
)
from .git_remote import (NoGitRemoteError, build_repo_client_with_username,
                         list_local_remotes, parse_remote_url)
from .templates import TemplateRenderer
from .failure_classify import (
    category_advice,
    category_label,
    classify_failure,
)
from .report import (
    DEFAULT_COMMENT_TEMPLATE,
    DEFAULT_FAILURE_COMMENT_TEMPLATE,
    EMPTY_DIFF,
    build_diff_table,
    collect_diff_data,
    format_duration,
    format_test_summary,
    parse_test_summary,
    render_comment,
)
from .usage import finalize_usage, parse_claude_result_usage
from .plugins import (
    PluginKind,
    get_plugin,
    has_plugin,
    list_plugins,
)

# issue #281 §4.1 进度上报约定：agent 在输出中以 [PROGRESS] 行上报里程碑，
# dsh executor 增量解析落库 task_progress 账本；中断恢复时据此渲染确定性
# 交接单（§4.4），替代「模型自查 git 反推」导致的反复检查/重复实现。
# 解析容忍大小写与字段顺序；desc/evidence 带引号（可含空格），解析失败
# 的行整体跳过（账本缺失不影响任务执行）。
PROGRESS_MARKER = "[PROGRESS]"
_PROGRESS_RE = re.compile(
    r"\[PROGRESS\]\s+step=(\d+)\s+status=(done|failed|pending|skipped)"
    r"(?:\s+desc=\"([^\"]*)\")?"
    r"(?:\s+evidence=\"([^\"]*)\")?",
    re.IGNORECASE)

# dsh 引擎提示词追加的「进度上报约定」节（Phase 1 仅 dsh 引擎解析落库，
# claude/hermes 不解析该标记，保持现状不受影响）。
PROGRESS_REPORT_INSTRUCTION = """
【进度上报约定】（中断恢复机制 issue #281）：每完成一个里程碑（定位根因 /
编写代码 / 运行测试 / 推送等），请单独输出一行固定格式进度，平台会记录并在
中断恢复时生成确定性交接单，避免你中断恢复后反复检查、重复实现：
[PROGRESS] step=<序号> status=<done|failed|pending> desc=\"<本步做了什么>\" evidence=\"<验证命令与结果摘要>\"
示例：[PROGRESS] step=2 status=done desc=\"编写修复代码\" evidence=\"pytest tests/test_x.py -q 通过\"
"""

logger = logging.getLogger(__name__)

# 明确「无法解决」的表述（模版要求 Claude 如实汇报），命中则不再重试
UNRESOLVABLE_PATTERNS = [
    r"无法解决", r"无法修复", r"无法完成", r"不能解决", r"不能修复", r"未能解决",
    r"无法复现", r"cannot (?:be )?(?:fix|solve|resolve)", r"can'?t (?:fix|solve|resolve)",
    r"not able to (?:fix|solve|resolve)", r"could not (?:fix|solve|resolve)",
    r"out of scope", r"unable to (?:fix|solve|resolve)",
]
_UNRESOLVABLE_RE = re.compile("|".join(UNRESOLVABLE_PATTERNS), re.IGNORECASE)

# 「等待用户决策」提问信号（issue #67）：无人值守执行中 Claude 停在
# 需要用户选择/回答的节点时，最终回复的结尾会出现选项型提问
# （「请选择 A 或 B」「请回复 1 或 2」「请问……？」等）。任务完成汇报的
# 礼貌收尾（「请确认后关闭本 issue」「如有问题请回复我」）不在此列。
# 命中的结尾再结合「无任务提交」双重确认才判定为等待用户决策。
DECISION_QUESTION_RE = re.compile(
    r"(请选择\s*[A-Za-zＡ-Ｚａ-ｚ][^。\n]{0,60}或|"
    r"请选择\s*[A-Za-zＡ-Ｚａ-ｚ]\s*[/、]\s*[A-Za-zＡ-Ｚａ-ｚ]|"
    r"请回复\s*[0-9１-９][^。\n]{0,60}(?:或|和)|"
    r"请回复\s*[0-9１-９]\s*[/、]\s*[0-9１-９]|"
    r"请决定[^。\n]{0,80}[?？]|"
    r"请确认(?:是否|要|需)[^。\n]{0,80}[?？]|"
    r"请问[^。\n]{0,120}[?？])"
)

# 日志保留行数（落盘 + 失败评论摘要）
LOG_TAIL_LINES = 400
COMMENT_TAIL_CHARS = 3000

# 手动停止约定退出码（issue #35）：读循环检测到停止标记时返回，
# 区别于 124（超时）与其他环境失败，run_task 据此走停止收尾
STOP_EXIT_CODE = 125

# issue #280：任务启动阶段拉取 issue 遇 GitLab 瞬时故障（网关 502/限流/
# 网络抖动）时不立即判失败，按指数退避重试——08-17 生产事故：GitLab 短暂
# 不可用返回 502，44 个排队任务 get_issue 一次 502 即全部打成 failed，且
# 失败评论同样发不出，issue 上「没有任何回复评论」。重试耗尽后才判失败。
ISSUE_FETCH_MAX_ATTEMPTS = 5
ISSUE_FETCH_BASE_DELAY = 5.0
ISSUE_FETCH_MAX_DELAY = 60.0
# 收尾评论/标签尽力重试（同 issue #280）：GitLab 恢复后仍要保证用户能
# 收到失败反馈，不能只试一次就放弃。
FINISH_RETRY_ATTEMPTS = 5
FINISH_RETRY_BASE_DELAY = 5.0
FINISH_RETRY_MAX_DELAY = 60.0

class ExecutorError(Exception):
    pass


def _strip_credential_sections(text: str) -> str:
    """从 gitconfig 文本中剥离 [credential] section（含子键，如 [credential "https://x"]）。

    返回去除 credential 配置后的文本；无 credential section 时原样返回。
    用于生成净化版全局 gitconfig（见 ClaudeExecutor._git_global_config）。
    """
    out: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.startswith("["):
            section = line.strip("[]").split()[0].split('"')[0].strip()
            skip = section == "credential"
        if not skip:
            out.append(line)
    return "\n".join(out)


def _row_get(row, key, default=None):
    """兼容 sqlite3.Row 与 dict 的字段读取。

    database 层返回的是 sqlite3.Row（无 .get() 方法，issue #11）；
    调用方传 dict（如测试）时同样可用。键不存在返回 default。
    """
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _on_rmtree_error(func, path, exc_info) -> None:
    """rmtree 删除条目失败时的恢复处理（issue #91）。

    先尝试恢复该条目及其父目录的权限后重试一次：残留属主为本进程
    用户时（如目录被 chmod 只读）可借此删除干净；属主是其他用户
    （root 残留）时 chmod 同样失败，放弃该条目由 rmtree 继续处理
    其余条目，最终残留项由 _force_remove 报告并降级警告。
    """
    for target in (path, str(Path(path).parent)):
        try:
            os.chmod(target, 0o700)
        except OSError:
            continue
    try:
        func(path)
    except OSError:
        pass


def _load_json_output(output: str) -> dict | None:
    """从 claude 输出中解析首个 JSON 对象，失败返回 None。

    容错两类污染：
    - 前缀：claude 无 stdin 时 stderr 先打印 "Warning: no stdin data
      received..."（executor 把 stderr 合并进 stdout），整串 json.loads
      必失败，导致 session_id 永不落库（断点续跑失效）、错误提取落空；
    - 尾随：同一次执行里 stderr 可能继续混入后续行。
    用 JSONDecoder.raw_decode 只取首个完整 JSON 对象，忽略其余内容。
    """
    if not output:
        return None
    start = output.find("{")
    if start == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(output[start:])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# 宽松转义解码（issue #16）：claude result 内嵌工具调用记录时，\n \" \'
# 等转义按字面量存放，直接展示可读性差。json.loads 对 \' 等 Python 风格
# 转义会抛 Invalid \escape，这里用正则宽松解码常见转义，其余 \X 保留原样。
_ESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
               "\\": "\\", "'": "'", '"': '"', "/": "/"}
_ESCAPE_RE = re.compile(r"\\([nrtbf\\'\"\/])")


def _format_struct(value, depth: int = 0) -> str:
    """把解码后的 JSON 结构递归展开为可读文本（字符串值不再二次转义）。"""
    pad = "  " * depth
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for k, v in value.items():
            lines.append(f"{pad}  {k}: {_format_struct(v, depth + 1)}")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for v in value:
            lines.append(f"{pad}  {_format_struct(v, depth + 1)}")
        lines.append(pad + "]")
        return "\n".join(lines)
    if isinstance(value, str):
        return _decode_escapes(value, depth + 1)
    return json.dumps(value, ensure_ascii=False)


def _decode_escapes(text: str, depth: int = 0) -> str:
    """递归解码 result 中嵌套序列化的转义文本（issue #16）。

    外层 json.loads 已解码一次 JSON 转义；result 内嵌的工具调用记录是
    再次序列化的 JSON 文本（\\n \\" 等按字面量存放）。这里逐层解码：
    先试严格 json.loads（标准 JSON → 结构展开），失败则宽松解码一层
    常见转义后继续递归。普通可读文本（无转义）原样返回。
    """
    if depth > 4 or not text:
        return text
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, str):
        return _decode_escapes(decoded, depth + 1)
    if isinstance(decoded, (dict, list)):
        return _format_struct(decoded, depth + 1)
    # 严格解码失败（含 \' 等非标准转义）→ 宽松解码一层后继续
    unescaped = _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(1)], text)
    if unescaped == text:
        return text
    return _decode_escapes(unescaped, depth + 1)


def format_display_line(line: str) -> str:
    """把 claude 输出行重排为可读文本（issue #16）。

    JSON 行：解码 result 字段的嵌套转义（\\n → 换行等），只保留对排查
    有用的核心字段，丢弃 ttft_ms / uuid 等机器噪音；非 JSON 行原样返回。
    """
    data = _load_json_output(line)
    if data is None or not isinstance(data.get("result"), str):
        return line
    parts = []
    for key in ("type", "subtype", "session_id", "exit_code", "error"):
        if key in data:
            parts.append(f"{key}: {json.dumps(data[key], ensure_ascii=False)}")
    parts.append("result:\n" + _decode_escapes(data["result"]))
    return "\n".join(parts)


# ---- 实时查看任务执行（issue #20）----

_TRANSCRIPT_MAX_MESSAGES = 500
_TRANSCRIPT_MAX_TEXT = 5000


def find_session_file(session_id: str, claude_home: Path | None = None) -> Path | None:
    """按 session_id 查找 claude 会话文件 <claude_home>/projects/*/<sid>.jsonl。

    实时查看聊天记录（issue #20）与断点续跑降级判定共用；找不到返回 None。
    claude_home 缺省为 ~/.claude（测试可注入）。
    """
    base = claude_home if claude_home is not None else Path.home() / ".claude"
    projects = base / "projects"
    if not projects.is_dir():
        return None
    try:
        for proj in projects.iterdir():
            f = proj / f"{session_id}.jsonl"
            if f.is_file():
                return f
    except OSError:
        return None
    return None


def _transcript_text(content, default: str = "") -> str:
    """从消息 content 提取纯文本（str 或 text 片段拼接），非文本返回 default。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        return "\n".join(parts)
    return default


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """截断长文本，返回 (text, truncated)。"""
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _first_user_line_index(lines: list[str]) -> int | None:
    """找会话文件首条 user 消息所在行下标，找不到返回 None（issue #90）。"""
    for i, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (isinstance(record, dict) and record.get("type") == "user"
                and isinstance(record.get("message"), dict)
                and record["message"].get("role") == "user"):
            return i
    return None


def read_session_prompt(session_file: Path) -> str | None:
    """读取会话文件首条 user 消息全文（渲染后的完整提示词，issue #90）。

    提示词不落库，仅存在于会话 jsonl 首条 user 消息（任务创建时由
    executor 构造后传给 claude）。供「查看提示词」按钮展示，与全局模版
    逐字节比对；文件缺失 / 无 user 消息 / 文本为空返回 None。
    """
    if session_file is None or not session_file.is_file():
        return None
    try:
        lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        msg = record.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _transcript_text(msg.get("content"))
        if text:
            return text
    return None


def parse_transcript(session_file: Path, max_messages: int = _TRANSCRIPT_MAX_MESSAGES,
                     max_text_chars: int = _TRANSCRIPT_MAX_TEXT) -> tuple[list[dict], bool]:
    """解析 claude 会话 jsonl 为结构化聊天消息（issue #20 实时查看）。

    只保留 user / assistant 两类行（跳过 system / result 与杂讯），
    拆分为四类消息：
      {"role": "user", "text", "ts", "truncated"}
      {"role": "assistant", "text", "ts", "truncated"}
      {"role": "tool", "tool", "input", "ts"}                （工具调用）
      {"role": "tool_result", "tool_use_id", "text", "tool_error", "ts", "truncated"}
    返回 (messages, truncated)：消息过多时保留首条 user 消息（提示词）与
    最后 max_messages-1 条并置 truncated=True；首条 user 消息（渲染后的
    完整提示词，issue #90）跳过 max_text_chars 文本截断；文件不存在 /
    无有效行返回空列表。
    """
    if session_file is None or not session_file.is_file():
        return [], False
    try:
        lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], False
    if len(lines) > max_messages:
        # issue #90：截断窗口置顶保留首条 user 消息（提示词），其余保留
        # 最后 max_messages-1 条——提示词不因消息数量截断从聊天记录中消失
        idx = _first_user_line_index(lines)
        keep = max_messages - 1
        tail = lines[-keep:] if keep > 0 else []
        if idx is not None and (not tail or idx < len(lines) - keep):
            lines = [lines[idx]] + tail
        else:
            lines = tail
        truncated = True
    else:
        truncated = False

    messages: list[dict] = []
    first_user_done = False  # issue #90：首条可见 user 消息（提示词）完整保留不截断
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
            continue
        msg = record.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        ts = msg.get("timestamp") or record.get("timestamp")
        if role == "assistant" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text, cut = _truncate_text(part.get("text", ""), max_text_chars)
                    if text:
                        messages.append({"role": "assistant", "text": text,
                                         "ts": ts, "truncated": cut})
                elif part.get("type") == "tool_use":
                    messages.append({"role": "tool", "tool": part.get("name", "?"),
                                     "input": part.get("input"), "ts": ts})
        elif role == "user" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    if first_user_done:
                        text, cut = _truncate_text(part.get("text", ""), max_text_chars)
                    else:
                        text, cut = part.get("text", ""), False
                    if text:
                        first_user_done = True
                        messages.append({"role": "user", "text": text,
                                         "ts": ts, "truncated": cut})
                elif part.get("type") == "tool_result":
                    result_text, cut = _truncate_text(
                        _transcript_text(part.get("content")), max_text_chars)
                    messages.append({
                        "role": "tool_result",
                        "tool_use_id": part.get("tool_use_id"),
                        "text": result_text, "tool_error": bool(part.get("is_error")),
                        "ts": ts, "truncated": cut,
                    })
        elif role == "user" and isinstance(content, str):
            if first_user_done:
                text, cut = _truncate_text(content, max_text_chars)
            else:
                text, cut = content, False
            if text:
                first_user_done = True
                messages.append({"role": "user", "text": text, "ts": ts, "truncated": cut})
    return messages, truncated


def read_log_delta(path: Path, after_byte: int = 0,
                   max_lines: int = 500) -> tuple[list[str], int]:
    """从日志文件 after_byte 字节处读取增量行，返回 (lines, new_offset)。

    日志文件被 executor 逐行实时追加（append-only）。offset 落在行中间时
    自动对齐到最近的行首（保证只返回完整行）；尾部若为写入中的半行
    （无换行结尾）则回退到该行开头（offset 回退），等下一轮补全，
    避免撕裂行；文件不存在 / offset 超界返回空增量。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], after_byte
    if after_byte >= size:
        return [], size
    start = after_byte
    if start > 0:
        # 对齐行首：回读一段窗口找最近的换行符
        lookback = min(start, 4096)
        with open(path, "rb") as f:
            f.seek(start - lookback)
            window = f.read(lookback)
        idx = window.rfind(b"\n")
        start = start - lookback + idx + 1
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    new_offset = start + len(raw)
    if raw and not raw.endswith(b"\n") and not raw.endswith(b"\r"):
        line_start = raw.rfind(b"\n") + 1
        new_offset -= len(raw[line_start:])
        raw = raw[:line_start]
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines, new_offset


class ClaudeExecutor:
    def __init__(self, config: ConfigManager, db: Database,
                 gitlab: GitLabClient, renderer: TemplateRenderer,
                 workspace_root: str | None = None,
                 event_bus: EventBus | None = None):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        self.renderer = renderer
        # 实时事件总线（SSE 推送）：executor 读流时逐事件发布；API 层订阅。
        # seq 计数按任务递增且跨重试轮次持久——断线重连后 API 回放日志
        # （从 1 重算）与实时事件 seq 衔接，前端按 seq 去重不丢事件
        self.event_bus = event_bus if event_bus is not None else EventBus()
        self._seq: dict[int, int] = {}
        base = Path(workspace_root) if workspace_root else Path(__file__).resolve().parents[1] / "workspace"
        self.workspace_root = base.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        # 网页通知事件（issue #21）：任务收尾时记录，前端轮询弹系统通知
        from .notifier import Notifier
        self.notifier = Notifier(db)
        # Webhook 消息推送（issue #136）：任务成功收尾时按设置页配置推送
        from .webhook_push import WebhookPusher
        self.webhook_pusher = WebhookPusher(config)
        # 一键停止（issue #35）：运行中进程注册表 + 停止请求集合。
        # task_id 自增唯一，集合只增不减（进程退出注销的是注册表不是集合）。
        self._procs: dict[int, subprocess.Popen] = {}
        self._stop_requests: set[int] = set()
        self._proc_lock = threading.Lock()
        # 拉取冲突交接（issue #147 补充）：prepare_workspace 的
        # git pull --rebase 遇到合并冲突时保留冲突现场并登记工作区，
        # _build_prompt / _resume_prompt 据此追加「先手工解决冲突」指引，
        # 由 agent 完成合并，而不是让任务在准备阶段直接失败
        self._pull_conflict_workdirs: set[Path] = set()
    # ---- GitLab 调用兜底 ----

    def _call_with_fallback(self, repo, call):
        """用全局 client 执行 call(client)；遇 401/403（全局 token 失效）
        时用仓库 remote url 内嵌 token 构建 per-repo client 重试一次。

        issue #130 + #132：任务侧（生命周期评论、打标签等）绝不使用
        owner token——owner token 只允许在概览页 issue 编辑操作时由平台
        使用（见 api/issues.py），agent 无论如何都不能使用 owner token。
        因此这里固定走「全局 → remote」链路（issue #87 的 prefer_owner
        机制已按 #130 移除）。非编辑调用（流水线等待、查询提交等）同样
        只走此链路，绝不使用 owner token（严禁用于推送代码与处理流水线）。

        issue #65 补充：对账/webhook 已有此兜底，executor 的 issue 查询、
        评论、打标签仍只走全局 client——全局 token 被撤销后任务领取即
        401 失败、issue 上收不到任何评论（生产任务 #88/#89）。repo 为
        None（无仓库上下文的测试等）时仅用全局 client（行为同旧）。
        """
        if repo is None:
            return call(self.gitlab), self.gitlab
        try:
            return call(self.gitlab), self.gitlab
        except GitLabError as e:
            if e.status_code not in (401, 403):
                raise
            fallback, _ = build_repo_client_with_username(
                repo, self.config.get().verify_ssl)
            if fallback is None:
                raise
            logger.info("任务仓库 %s：全局 token 失效（%s），"
                        "改用 remote url 内嵌 token 重试", repo["name"], e)
            return call(fallback), fallback

    def _transient_retry(self, what: str, call, *,
                         attempts: int = FINISH_RETRY_ATTEMPTS,
                         base_delay: float = FINISH_RETRY_BASE_DELAY,
                         max_delay: float = FINISH_RETRY_MAX_DELAY):
        """对 call() 执行瞬时故障重试（指数退避）；非瞬时错误立即抛出。

        issue #280：GitLab 短暂不可用（502/503/限流/网络抖动）时，收尾
        评论/标签不能只试一次就放弃——一次 502 会让 issue 上「没有任何
        回复评论」，用户无法感知任务失败/处理中。重试耗尽后抛最后一个
        GitLabError，由调用方记日志降级。
        """
        last: GitLabError | None = None
        for attempt in range(attempts):
            try:
                return call()
            except GitLabError as e:
                if not is_transient_error(e):
                    raise
                last = e
                if attempt >= attempts - 1:
                    break
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning("%s瞬时故障（%s），%.0fs 后重试（第 %d/%d 次）",
                               what, e, delay, attempt + 1, attempts)
                time.sleep(delay)
        assert last is not None
        raise last

    # ---- 工作区管理 ----

    def _repo_workdir(self, repo: dict) -> Path:
        """仓库工作区：有 local_path（本地文件夹方式添加）时直接用该文件夹。"""
        if _row_get(repo, "local_path"):
            return Path(repo["local_path"])
        return self.workspace_root / repo["name"]

    def _git(self, workdir: Path, *args: str, env: dict | None = None,
             timeout: int = 300) -> None:
        """执行 git 命令，失败抛 ExecutorError。"""
        cmd = ["git", "-c", "http.sslVerify=false"] + list(args)
        try:
            result = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExecutorError(f"git 命令超时: {args[0]} {args[1] if len(args) > 1 else ''}")
        if result.returncode != 0:
            raise ExecutorError(
                f"git {args[0]} 失败 (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[-500:]}")

    def _clean_process_env(self) -> dict:
        """剔除 gitlab-runner CI 环境变量，避免污染 git/claude 子进程。

        CI 部署在构建目录里 pm2 start，作业环境（CI_JOB_TOKEN、GITLAB_CI、
        GIT_CONFIG_* 等）被 pm2 进程继承；git 子进程凭据流程可能经 runner
        注入的 GIT_CONFIG_* 或 credential store 误用 CI_JOB_TOKEN → GitLab
        403（"Authentication by CI/CD job token not allowed..."）→ 403 不
        触发凭据重试 → fetch/push 必失败（issue #18 部署后任务频繁失败根因）。
        """
        return {k: v for k, v in os.environ.items()
                if not (k.startswith("CI_") or k == "GITLAB_CI"
                        or k.startswith("GIT_CONFIG_"))}

    def _git_global_config(self) -> Path:
        """净化版全局 gitconfig 路径：剥离 [credential] section，其余原样保留。

        直接 GIT_CONFIG_GLOBAL=/dev/null 会连带丢失 user.name/email
        （claude 子进程 commit 报错）、http.sslVerify（自签名 GitLab 握手
        失败）等全局设置；这里复制 ~/.gitconfig 并仅剥离 [credential]——
        其中失效的 gitlab-ci-token store 条目会被 git 优先于 GIT_ASKPASS
        选用（store helper 先于 askpass，且 403 不重试），是任务失败的
        直接来源。原文件无 credential 配置时直接复用原路径；无全局配置
        时返回 /dev/null（等价于无全局配置）。
        """
        src = Path.home() / ".gitconfig"
        if not src.is_file():
            return Path(os.devnull)
        text = src.read_text(encoding="utf-8", errors="replace")
        cleaned = _strip_credential_sections(text)
        if cleaned == text:
            return src
        out = self.workspace_root / ".gitconfig-sanitized"
        out.write_text(cleaned, encoding="utf-8")
        return out

    def _askpass_script(self, repo_name: str) -> Path:
        """生成 GIT_ASKPASS 脚本（用户名 oauth2，密码 = bot token）。

        放在工作区父目录（不能在 clone 目标目录里，否则 git clone 拒绝非空目录）。
        """
        script = self.workspace_root / f".botler-askpass-{repo_name}.sh"
        token = self.config.get().gitlab_token
        # token 里可能含引号，用单引号包裹并转义
        esc = token.replace("'", "'\\''")
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "oauth2" ;;\n'
            '  *Password*) echo \'%s\' ;;\n'
            '  *) echo \'%s\' ;;\n'
            "esac\n" % (esc, esc),
            encoding="utf-8",
        )
        script.chmod(0o700)
        return script

    def prepare_workspace(self, repo: dict, resume: bool = False) -> tuple[Path, dict]:
        """确保工作区存在且干净，返回 (workdir, git_env)。

        local_path 仓库直接用该文件夹（不 clone）；普通仓库首次执行时 clone。
        resume=True（会话断点续跑）时只 fetch 更新远端引用，跳过
        checkout / reset --hard / clean -fd——保留 Claude 上次的未提交改动
        与本地提交，供恢复会话接续使用。
        """
        cfg = self.config.get()
        workdir = self._repo_workdir(repo)
        askpass = self._askpass_script(repo["name"])
        # 先剔除 CI 环境变量再设置关键项：gitlab-runner 的 CI_JOB_TOKEN 等
        # 会被 git 凭据流程误用（经 store 优先于 GIT_ASKPASS → 403 不重试），
        # 且外部 GIT_ASKPASS 可能指向别处覆盖凭据注入
        git_env = self._clean_process_env()
        git_env["GIT_ASKPASS"] = str(askpass)
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        git_env["HOME"] = str(Path.home())
        # 禁用全局 credential store（失效 job token 条目优先于 askpass 被选用）
        git_env["GIT_CONFIG_GLOBAL"] = str(self._git_global_config())
        git_env["GIT_CONFIG_SYSTEM"] = os.devnull

        if not (workdir / ".git").exists():
            if _row_get(repo, "local_path"):
                raise ExecutorError(
                    f"本地文件夹不是 git 仓库: {workdir}（local_path 方式要求存在 .git 目录）")
            logger.info("首次克隆仓库 %s", repo["name"])
            # 不要预先创建 workdir：git clone 要求目标目录不存在（或为空）
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "-c", "http.sslVerify=false", "clone", repo["url"], str(workdir)]
            try:
                result = subprocess.run(cmd, env=git_env, capture_output=True,
                                        text=True, timeout=600)
            except subprocess.TimeoutExpired:
                raise ExecutorError(f"克隆仓库 {repo['name']} 超时")
            if result.returncode != 0:
                raise ExecutorError(
                    f"克隆仓库 {repo['name']} 失败: {(result.stderr or result.stdout).strip()[-500:]}")

        # 每次执行前重置到远端默认主分支，从根上消除脏状态。
        # issue #147：模版库前两点「任务开始先校验当前分支切回默认主分支 +
        # 每次开发前 git pull 同步」下沉为平台代码自动完成，agent 无需再
        # 自行执行（节省 token）。
        # remote_name 记录本地方式添加时用户选中的 remote（老数据缺省为 origin）
        remote = _row_get(repo, "remote_name") or "origin"
        self._git(workdir, "fetch", remote, "--prune", env=git_env)
        if not resume:
            # 1) 解析远端默认主分支名：优先 ls-remote --symref 的服务端权威
            #    HEAD 符号引用，不依赖本地 {remote}/HEAD——手工加 remote 的
            #    仓库可能缺失该引用（issue #12）
            branch = self._resolve_default_branch(workdir, remote, git_env)
            # 2) 校验当前分支，非默认主分支 → checkout 切回主分支
            self._checkout_default_branch(workdir, remote, branch, git_env)
            self._git(workdir, "reset", "--hard", f"{remote}/{branch}", env=git_env)
            self._clean_untracked(workdir, git_env)
            # 3) git pull --rebase 显式同步远端默认主分支最新提交（兜底
            #    fetch 之后、本次执行前远端新推送的提交）。若拉取遇到合并
            #    冲突（本地提交与远端分叉、untracked 残留被远端新提交占用
            #    等），不直接失败：保留冲突现场交由 agent 手工合并
            #    （issue #147 补充需求「如果拉取代码的时候出现了冲突，
            #    让 agent 来进行合并」）。
            try:
                self._git(workdir, "pull", "--rebase", remote, branch, env=git_env)
            except ExecutorError as exc:
                if not self._is_pull_conflict(workdir, git_env, exc):
                    raise
                self._pull_conflict_workdirs.add(workdir)
                logger.warning(
                    "%s: git pull --rebase 出现合并冲突，保留冲突现场交由 "
                    "agent 手工合并: %s", repo["name"], str(exc)[:200])
            else:
                self._pull_conflict_workdirs.discard(workdir)
                logger.info("%s: 工作区已切到默认主分支 %s 并 git pull 同步最新",
                            repo["name"], branch)
        # askpass 脚本保留不删除（issue #12）：并发任务/重试时序下脚本被删 →
        # fetch 回退 credential helper 旧凭据 → HTTP Basic: Access denied。
        # 脚本内容每次 prepare 覆盖刷新（token 轮换自动生效），权限 0700，
        # 且在工作区父目录，不受 clean -fd 波及。
        return workdir, git_env

    def _resolve_default_branch(self, workdir: Path, remote: str,
                                git_env: dict) -> str:
        """解析远端默认主分支名（issue #147 / #148 强化，不再硬编码 main）。

        优先读取服务端权威信息：``git ls-remote --symref`` 返回的 HEAD
        符号引用（并校验该分支真实存在于远端 refs，``git init --bare`` 的
        裸仓库 HEAD 可能指向不存在的 master，只有 main 被推送）。ls-remote
        探测失败（网络/认证抖动、超时、git 异常等）时不直接回退硬编码
        main，而是逐级降级：

          1. ``git ls-remote --symref <remote>``（服务端权威）；
          2. ``git remote show <remote>`` 解析 "HEAD branch:" 行；
          3. 本地跟踪引用兜底：优先 ``refs/remotes/<remote>/HEAD`` 符号
             引用，再按 main → master → 字典序 在本地已存在的跟踪分支中
             探测（远端彻底不可达时也能拿到实际存在的分支）。

        任一环节拿到的分支名都必须与「远端/本地实际存在的分支集合」核对，
        避免解析出不存在的分支导致 checkout / pull 失败（任务 #249 根因：
        远端只有 master 时解析出 main → fetch/checkout main 必然失败）。
        全链路均失败（远端不可达且本地无任何跟踪引用）才最终回退 "main"。
        """
        branch = self._remote_default_branch_via_lsremote(
            workdir, remote, git_env)
        if branch:
            return branch
        branch = self._remote_default_branch_via_show(workdir, remote, git_env)
        if branch:
            return branch
        branch = self._local_default_branch(workdir, remote)
        if branch:
            return branch
        return "main"

    def _remote_default_branch_via_lsremote(self, workdir: Path, remote: str,
                                            git_env: dict) -> str | None:
        """git ls-remote --symref 解析服务端权威默认主分支，失败返回 None。

        HEAD 符号引用指向的分支不存在（如新裸仓库）时按 main → master →
        字典序 在远端真实存在的分支中回退；远端可达但没有任何分支返回
        None（空仓库交由下一级兜底）。
        """
        cmd = ["git", "-c", "http.sslVerify=false", "ls-remote", "--symref", remote]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=120)
        except Exception:  # git 缺失/超时/异常等一律走下一级降级，探测不阻塞任务
            logger.debug("ls-remote --symref 解析默认主分支失败，走 git remote show 降级",
                         exc_info=True)
            return None
        if result.returncode != 0:
            logger.debug("ls-remote --symref 返回非零（%s），走 git remote show 降级: %s",
                         result.returncode, (result.stderr or result.stdout).strip()[-200:])
            return None
        candidate: str | None = None
        heads: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == "ref:" and len(parts) == 3 and parts[2] == "HEAD":
                if parts[1].startswith("refs/heads/"):
                    candidate = parts[1].rsplit("/", 1)[-1]
            elif parts[0].startswith("refs/heads/"):
                heads.add(parts[0].rsplit("/", 1)[-1])
        if candidate and candidate in heads:
            return candidate
        # HEAD 符号引用指向的分支不存在（如新裸仓库）→ 按常见命名回退
        for name in ("main", "master"):
            if name in heads:
                return name
        if heads:
            return sorted(heads)[0]
        return None

    def _remote_default_branch_via_show(self, workdir: Path, remote: str,
                                        git_env: dict) -> str | None:
        """git remote show <remote> 解析 "HEAD branch:" 行，失败返回 None。

        ls-remote --symref 不可用（服务器不支持 / 探测异常）时的二次服务端
        探测。``git remote show`` 是 git 查询远端默认分支的标准命令，输出
        ``HEAD branch: <名>``；HEAD 悬空时输出 ``(unknown)``。
        """
        cmd = ["git", "remote", "show", remote]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=120)
        except Exception:  # 同上：任何失败走本地跟踪引用兜底
            logger.debug("git remote show 解析默认主分支失败，走本地跟踪引用兜底",
                         exc_info=True)
            return None
        if result.returncode != 0:
            logger.debug("git remote show 返回非零（%s），走本地跟踪引用兜底",
                         result.returncode)
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("HEAD branch:"):
                name = stripped.split(":", 1)[1].strip()
                if name and name != "(unknown)":
                    return name
        return None

    def _local_default_branch(self, workdir: Path, remote: str) -> str | None:
        """远端不可达时用本地跟踪引用兜底解析默认主分支，没有则返回 None。

        先收集本地已拉取的 ``refs/remotes/<remote>/*`` 跟踪分支集合，再
        按优先级探测：① ``refs/remotes/<remote>/HEAD`` 符号引用（clone /
        git remote set-head 生成，目标分支必须真实存在于本地，避免陈旧
        HEAD 指向已删除分支）；② main → master → 字典序 取实际存在的分支。
        注意：单分支克隆的 origin/HEAD 指向克隆分支而非远端默认分支——
        远端不可达时无法确认真实默认分支，取本地已有分支已是最优近似
        （远端恢复后 ls-remote 会纠正）。
        """
        try:
            result = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:short)",
                 f"refs/remotes/{remote}/"],
                cwd=workdir, capture_output=True, text=True, timeout=30)
        except Exception:  # git 缺失/超时等一律视为无本地引用
            return None
        if result.returncode != 0:
            return None
        names = {line.strip().rsplit("/", 1)[-1]
                 for line in result.stdout.splitlines() if line.strip()}
        names.discard("HEAD")  # 排除符号引用本身（refs/remotes/<remote>/HEAD）
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
                cwd=workdir, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                ref = result.stdout.strip()
                prefix = f"refs/remotes/{remote}/"
                if ref.startswith(prefix):
                    name = ref[len(prefix):]
                    if name in names:  # 陈旧 HEAD 指向已删除分支时忽略
                        return name
        except Exception:  # 同上：任何失败按本地跟踪分支探测
            logger.debug("本地 remote HEAD 符号引用解析失败，按本地跟踪分支探测",
                         exc_info=True)
        for name in ("main", "master"):
            if name in names:
                return name
        if names:
            return sorted(names)[0]
        return None

    def _checkout_default_branch(self, workdir: Path, remote: str,
                                 branch: str, git_env: dict) -> None:
        """校验当前分支：非默认主分支则 checkout 切回主分支（issue #147）。

        已处于默认主分支时直接返回（不重复切换）；detached HEAD（rev-parse
        输出 HEAD）同样视为非默认分支重新检出。``-B`` 保证本地分支不存在时
        基于远端分支创建、已存在时重置到远端提交，随后显式写
        branch.<name>.remote / branch.<name>.merge 建立上游跟踪
        （受限 fetch refspec 下 ``--track`` 无法建立跟踪，见下）。

        issue #148：执行前先补齐远端默认主分支的本地跟踪引用
        （refs/remotes/<remote>/<branch>）。工作区仓库可能是单分支克隆
        （--single-branch）或手工配置了受限 fetch refspec，fetch 只拉取了
        部分分支——此时即便远端确实存在默认主分支，本地也查不到对应跟踪
        引用：checkout -B <branch> --track <remote>/<branch> 会报
        "'origin/main' is not a commit"（任务 #249 失败根因），后续
        reset --hard <remote>/<branch> 同样报 'ambiguous argument'。
        缺失时用显式 refspec 拉取该分支补齐（命令行 refspec 不受受限配置
        影响），再走切回/重置流程。
        """
        if not self._remote_tracking_ref_exists(workdir, remote, branch, git_env):
            logger.warning(
                "%s: 远端默认主分支 %s 的本地跟踪引用 refs/remotes/%s/%s "
                "缺失（单分支克隆或受限 fetch refspec），显式拉取补齐",
                workdir, branch, remote, branch)
            self._git(workdir, "fetch", remote,
                      f"{branch}:refs/remotes/{remote}/{branch}", env=git_env)
        current = ""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0:
                current = result.stdout.strip()
        except Exception:  # 探测失败视为未知分支，走 checkout 切回
            logger.debug("读取当前分支失败，按非默认分支处理", exc_info=True)
        if current == branch:
            return
        logger.info("工作区当前分支 %s ≠ 默认主分支 %s，切回主分支",
                    current or "（detached HEAD）", branch)
        # 不用 --track 建跟踪：受限 fetch refspec（单分支克隆等）下 git 无法
        # 把 refs/remotes/<remote>/<branch> 映射回远端分支名，--track 会报
        # "cannot set up tracking information; starting point ... is not a
        # branch"（与引用是否已补齐无关）。改为 checkout 后直接写
        # branch.<name>.remote / branch.<name>.merge，标准仓库结果等价。
        self._git(workdir, "checkout", "-B", branch,
                  f"{remote}/{branch}", env=git_env)
        self._git(workdir, "config", f"branch.{branch}.remote", remote,
                  env=git_env)
        self._git(workdir, "config", f"branch.{branch}.merge",
                  f"refs/heads/{branch}", env=git_env)

    def _remote_tracking_ref_exists(self, workdir: Path, remote: str,
                                    branch: str, git_env: dict) -> bool:
        """判断本地远端跟踪引用 refs/remotes/<remote>/<branch> 是否存在。

        返回 False 的情形：引用从未拉取过（单分支克隆/受限 refspec）、
        被 --prune 清除、git 异常等。探测失败一律按缺失处理，由调用方
        显式拉取补齐。
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"refs/remotes/{remote}/{branch}"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
        except Exception:  # git 缺失/超时等一律视为引用不存在
            return False
        return result.returncode == 0

    def _is_pull_conflict(self, workdir: Path, git_env: dict,
                          exc: ExecutorError) -> bool:
        """判断 git pull 失败是否为可交由 agent 手工解决的合并冲突。

        issue #147 补充：拉取冲突不应让任务在准备阶段直接失败，而应保留
        冲突现场交由 agent 合并。判断依据按权威性排序：
        1) 工作区实际处于冲突状态——rebase/merge 进行中（.git/rebase-merge、
           .git/rebase-apply、.git/MERGE_HEAD）或存在未合并路径
           （git ls-files -u 非空）；
        2) git 输出包含明确的冲突标志（CONFLICT / could not apply /
           untracked 文件被远端新提交覆盖等）。
        凭据/网络等非冲突失败不在此列，照常抛错。
        """
        git_dir = workdir / ".git"
        # worktree 等场景 .git 可能是文件：先解析实际 git 目录再探测
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "--git-dir"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0:
                git_dir = Path(result.stdout.strip())
                if not git_dir.is_absolute():
                    git_dir = (workdir / git_dir).resolve()
        except Exception:  # 探测失败时退回默认 .git 目录
            logger.debug("解析 git 目录失败，按默认 .git 处理", exc_info=True)
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
            if (git_dir / marker).exists():
                return True
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "ls-files", "-u"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return True
        except Exception:  # git 缺失/异常时仅靠错误文本兜底
            logger.debug("git ls-files -u 探测未合并路径失败", exc_info=True)
        text = str(exc).lower()
        for marker in ("conflict", "automatic merge failed", "fix conflicts",
                       "could not apply", "untracked working tree files "
                       "would be overwritten", "divergent branches",
                       "have diverged", "unmerged files"):
            if marker in text:
                return True
        return False

    @staticmethod
    def _conflict_handoff_instructions() -> str:
        """拉取冲突交接指引（issue #147 补充）：prepare 的 git pull 遇到
        合并冲突时追加到任务提示词末尾，引导 agent 先手工解决冲突再继续。"""
        return (
            "\n\n【重要：工作区存在拉取冲突，请先手工解决再开始任务】\n"
            "平台在任务开始前的 git pull --rebase 同步最新代码时遇到合并冲突，\n"
            "冲突现场已原样保留（未回退、未丢弃任何内容）。请先完成合并：\n"
            "1. 运行 git status 查看冲突文件与当前 rebase/merge 状态；\n"
            "2. 用 git diff 或编辑器逐个解决冲突文件，保留两侧合理内容；\n"
            "3. 解决后 git add <冲突文件>，rebase 冲突执行 git rebase --continue，\n"
            "   merge 冲突执行 git commit 完成合并；\n"
            "4. 严禁 git push --force / --force-with-lease 强制覆盖远端；\n"
            "5. 若冲突确实无法解决，如实汇报失败原因与冲突文件清单，不要强行提交。"
        )

    def _clean_untracked(self, workdir: Path, git_env: dict) -> None:
        """清理未跟踪文件，容忍无权限删除的外部残留（issue #91）。

        用户以 root 等身份在 local_path 工作区跑过构建（如 flutter build
        生成的 .plugin_symlinks）会留下属主非本进程用户的 untracked 目录，
        git clean -fd 删除其中条目时 Permission denied 而整体失败（issue #91
        诊断的任务 #136 场景：daymark 仓库重试 3 次全败）。
        此类残留不影响 fetch / checkout / reset（只涉及 tracked 文件），
        不应拖垮整个任务：先尝试 Python 层尽力删除，仍删不掉的降级为
        警告继续执行，由用户手动清理。
        """
        try:
            self._git(workdir, "clean", "-fd", env=git_env)
            return
        except ExecutorError as exc:
            if "Permission denied" not in str(exc):
                raise
        logger.warning("%s: git clean 权限受限，尝试 Python 层清理残留", workdir)
        for rel in self._untracked_paths(workdir, git_env):
            path = workdir / rel
            if not self._force_remove(path):
                logger.warning("无法删除残留项（可能需要 root 权限手动清理）: %s", path)
        # 复检：残留清干净则无感；仍权限失败则警告放行（不阻塞任务）
        try:
            self._git(workdir, "clean", "-fd", env=git_env)
        except ExecutorError as exc:
            if "Permission denied" in str(exc):
                logger.warning("git clean 仍有权限受限残留，跳过继续执行: %s",
                               str(exc)[:200])
            else:
                raise

    @staticmethod
    def _untracked_paths(workdir: Path, git_env: dict) -> list[str]:
        """列出当前 untracked 条目（相对路径），供失败后的尽力清理使用。"""
        cmd = ["git", "-c", "http.sslVerify=false",
               "ls-files", "--others", "--exclude-standard"]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return []
        if result.returncode != 0:
            return []
        paths = []
        for line in result.stdout.splitlines():
            rel = line.rstrip("/")
            if rel and not rel.startswith("..") and not os.path.isabs(rel):
                paths.append(rel)
        return paths

    @staticmethod
    def _force_remove(path: Path) -> bool:
        """尽力删除 untracked 残留（文件/符号链接/目录），返回是否删除成功。

        issue #91：残留目录无写权限（chmod 只读 / 属主非本进程用户）时，
        删除内部条目受父目录写权限约束而 EACCES。先常规删除，失败后尝试
        恢复条目及父目录权限再重试一次；chmod 也失败（root 属主）则放弃。
        """
        for attempt in (False, True):
            try:
                if path.is_symlink() or not path.is_dir():
                    path.unlink(missing_ok=True)
                else:
                    shutil.rmtree(path, onerror=_on_rmtree_error)
                if not path.exists():
                    return True
            except OSError:
                pass
            if attempt:
                return False
            # 恢复权限后重试：条目 chmod 失败 = 非本进程用户属主，不再折腾
            try:
                os.chmod(path, 0o700)
            except OSError:
                return False
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        return False

    # ---- 提示词与环境 ----

    def _build_prompt(self, repo: dict, issue: dict) -> str:
        template = self.renderer.resolve_template(repo)
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        prompt = self.renderer.render(template, variables)
        if self._repo_workdir(repo) in self._pull_conflict_workdirs:
            prompt += self._conflict_handoff_instructions()
        return prompt

    def _task_gitlab_token(self, repo: dict) -> str | None:
        """任务会话 GITLAB_TOKEN 注入源：仓库 remote url 内嵌 token（issue #79）。

        与平台 _call_with_fallback 的 per-repo 兜底（issue #65）对齐：
        全局 bot token 失效后 Claude 会话内的 API（读 issue/写结果评论）
        401 失败，改用 remote 内嵌 token（与仓库绑定的凭据通常更新鲜）。
        解析失败 / 无 token 时返回 None（调用方回退全局 token）。
        """
        try:
            remotes = list_local_remotes(str(self._repo_workdir(repo)))
        except NoGitRemoteError:
            return None
        remote_name = _row_get(repo, "remote_name") or "origin"
        match = next((r for r in remotes if r["name"] == remote_name), None)
        if match is None:
            return None
        return parse_remote_url(match["url"])["token"]

    def _build_env(self, repo: dict, issue: dict) -> dict:
        cfg = self.config.get()
        env = self._clean_process_env()
        # 会话 GITLAB_TOKEN 注入（issue #130 调整）：agent 会话绝不注入
        # owner token（owner token 只允许在概览页 issue 编辑操作时由平台
        # 使用，见 api/issues.py；agent 无论如何都不能使用 owner token）。
        # 优先级：remote url 内嵌 token（仓库自己的认证 token）> 全局
        # bot token。issue #79：全局 bot token 失效后 Claude 侧 API
        # （写结果评论等）401 失败，remote 内嵌 token 与平台侧 per-repo
        # 兜底对齐。git 推送凭据不走 GITLAB_TOKEN（走 GIT_ASKPASS 的
        # bot token）。
        env["GITLAB_TOKEN"] = (self._task_gitlab_token(repo)
                               or cfg.gitlab_token)
        env["GITLAB_URL"] = cfg.gitlab_url
        env["PROJECT_ID"] = str(issue["project_id"])
        env["ISSUE_IID"] = str(issue["iid"])
        # git 凭据统一走 GIT_ASKPASS（bot token）：claude 内部 git push/fetch
        # 同样受全局 credential store 中失效 job token 污染（issue #16 推送时
        # 已遇 403），此处一并净化，保证 push 凭据与 API 一致
        askpass = self._askpass_script(repo["name"])
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_GLOBAL"] = str(self._git_global_config())
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        return env

    # ---- 会话断点续跑（issue #8）----

    def _extract_session_id(self, output: str) -> str | None:
        """从 claude JSON 输出解析 session_id（无 / 非法 JSON 返回 None）。

        stderr 警告混入 stdout 时同样可解析（见 _load_json_output）。
        """
        if not output:
            return None
        data = _load_json_output(output)
        sid = data.get("session_id") if data else None
        return sid or None

    def _claude_home(self) -> Path:
        """claude 会话文件根目录（~/.claude，session 的 .jsonl 落盘处）。"""
        return Path.home() / ".claude"

    def _session_file(self, session_id: str) -> Path | None:
        """查找 session 文件 ~/.claude/projects/*/<sid>.jsonl；不存在返回 None。"""
        return find_session_file(session_id, self._claude_home())

    def _resume_prompt(self, repo: dict, issue: dict,
                      task_id: int | None = None) -> str:
        """恢复执行引导语：确定性交接单渲染（issue #281 §4.4）。

        模版优先取 config 的 templates.resume（issue #116 起用户可编辑，
        与全局默认模版同机制）；未配置/清空时回退内置默认。占位符与
        全局模版共用 build_variables（claude/hermes/dsh 三引擎统一入口），
        另注入 {progress_summary}：有 task_id 且账本非空时渲染「已完成
        步骤 + 证据 / 下一步」确定性交接单；账本为空（确属首次/状态
        丢失）如实说明「无进度记录」，不再声称「对话与改动已保留」。
        """
        template = self.config.get().resume_template or DEFAULT_RESUME_PROMPT
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        variables["progress_summary"] = self._render_progress_handoff(task_id)
        prompt = self.renderer.render(template, variables)
        if self._repo_workdir(repo) in self._pull_conflict_workdirs:
            prompt += self._conflict_handoff_instructions()
        return prompt

    def _render_progress_handoff(self, task_id: int | None) -> str:
        """从 task_progress 账本渲染确定性进度交接单（§4.4 数据源）。

        无 task_id 或账本为空 → 如实降级文案「无进度记录」；有记录 →
        渲染每步最新状态 + 证据 + 下一步，供 agent 直接接续，禁止重做
        已标记 done 的步骤（替代「模型自查 git 反推」）。
        """
        if task_id is None:
            return ("（平台暂无任务进度账本记录：无已完成步骤可交接，"
                    "请先检查工作区状态后从断点继续，勿重复已完成工作）")
        steps = self.db.latest_task_progress(task_id)
        if not steps:
            return ("（平台暂无任务进度账本记录：无已完成步骤可交接，"
                    "请先检查工作区状态后从断点继续，勿重复已完成工作）")
        lines = ["平台已记录以下确定性进度（非模型自查结果）："]
        for row in steps:
            desc = row["step_desc"] or ""
            evidence = row["evidence"] or ""
            lines.append(f"- 步骤 {row['step_no']}「{desc}」→ {row['status']}"
                         + (f"，证据：{evidence}" if evidence else ""))
        pending = [r for r in steps if r["status"] != "done"]
        if pending:
            nxt = pending[0]
            lines.append(f"下一步：步骤 {nxt['step_no']}「{nxt['step_desc']}」")
        else:
            lines.append("已完成全部记录步骤，请继续完成剩余收尾工作。")
        lines.append("要求：按上述进度直接接续，禁止重新检查/重做已标记 done 的步骤。")
        return "\n".join(lines)

    def _persist_progress_markers(self, task_id: int, text: str) -> None:
        """从文本扫描 [PROGRESS] 里程碑并落库 task_progress（§4.1）。

        解析失败/落库失败不阻塞执行（账本尽力而为，恢复时缺失则如实降级）。
        """
        if not text or PROGRESS_MARKER not in text:
            return
        for m in _PROGRESS_RE.finditer(text):
            step_no = int(m.group(1))
            status = m.group(2).lower()
            desc = m.group(3) or ""
            evidence = m.group(4) or ""
            try:
                self.db.record_task_progress(
                    task_id, step_no, desc, status, evidence=evidence)
            except Exception as e:  # noqa: BLE001 账本写入失败不影响任务执行
                self.db.add_log(task_id, "warn",
                                f"[PROGRESS] 账本落库失败: {e}")

    # ---- 单次执行 ----

    def _kill_process_group(self, proc) -> None:
        """向进程组发 SIGKILL（超时与手动停止共用，issue #35 抽取）。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def request_stop(self, task_id: int) -> None:
        """登记停止请求并终止任务进程组（issue #35 一键停止所有任务）。

        登记先行：进程尚未创建（worker 还在准备阶段）时，_run_once 在
        Popen 后立即检查登记表自行终止；进程已存在则直接 SIGKILL 进程组
        （readline 读到 EOF 后 _run_once 自然退出）。
        """
        with self._proc_lock:
            self._stop_requests.add(task_id)
            proc = self._procs.get(task_id)
        if proc is not None and proc.poll() is None:
            self._kill_process_group(proc)

    def _stop_requested(self, task_id: int) -> bool:
        with self._proc_lock:
            return task_id in self._stop_requests

    def clear_stop_request(self, task_id: int) -> None:
        """清除停止请求登记（issue #69 手动重试时调用，幂等）。

        一键停止登记的停止请求若不清除会永久残留：任务被停止后用户手动
        重试，worker 领取任务时 run_task 开头的 _stop_requested 检查命中
        旧请求，任务被 _finish_stopped 立即打回 interrupted（表现为「每次
        手动重试过几秒就变成中断状态」，只有平台重启内存集合清空才能
        逃脱）。手动重试即用户明确恢复执行，历史停止请求必须清除。
        """
        with self._proc_lock:
            self._stop_requests.discard(task_id)

    def _engine(self, cfg) -> str:
        """任务执行引擎（issue #47/#84，插件化 issue #140）：claude（默认）/
        hermes / dsh；未注册的引擎名回退 claude。引擎插件见 botler.plugins.executors。"""
        engine = str(getattr(cfg, "engine", "") or "claude").strip().lower()
        return engine if has_plugin(PluginKind.EXECUTOR, engine) else "claude"

    def _drain_process_output(self, proc, task_id: int, log_path: Path,
                              deadline: float, on_chunk=None) -> tuple[bool, bool, list[str]]:
        """边读子进程 stdout 边写日志文件，返回 (timed_out, stopped, chunks)。

        issue #47 从 _run_once 抽取，claude 与 hermes 两引擎共用：
        - 每轮检查停止请求（readline 阻塞时外部 request_stop 已 SIGKILL
          进程组 → readline 返回 EOF 自然退出，此处兜底长期无输出时感知）
        - 超时由调用方 kill 进程组后收尾（返回 timed_out=True）
        - on_chunk：每读到一个 chunk 回调（claude 引擎用于运行中实时落
          session_id，issue #20；hermes 引擎无此需求传 None）
        """
        chunks: list[str] = []
        timed_out = False
        stopped = False
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            while not stopped:
                if self._stop_requested(task_id) and proc.poll() is None:
                    stopped = True
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    chunk = proc.stdout.readline() if proc.stdout else b""
                except Exception:
                    chunk = ""
                if chunk == "" and proc.poll() is not None:
                    break
                if chunk:
                    f.write(chunk)
                    chunks.append(chunk)
                    if len(chunks) > 20000:  # 约 20MB 上限
                        chunks.pop(0)
                    if on_chunk is not None:
                        on_chunk(chunk)
                if time.time() >= deadline and proc.poll() is None:
                    timed_out = True
                    break
                time.sleep(0.05)
        return timed_out, stopped, chunks

    def _capture_env_snapshot(self, task_id: int, workdir: Path) -> None:
        """任务首次执行开始时采集环境快照（issue #276）落库 tasks.environment。

        只采一次：重试/断点续跑不覆盖首次快照（起始 commit 基线以首次
        执行的工作区 HEAD 为准）。采集全程尽力而为——任何失败不影响任务
        执行：整体异常时落库 {"error": "环境快照获取失败"} 标记，前端
        「元信息」区据此显示「环境快照获取失败」。
        """
        task = self.db.get_task(task_id)
        if task is not None and _row_get(task, "environment"):
            return  # 已采集过（重试/续跑），保持首次快照
        try:
            snapshot = collect_env_snapshot(
                engine=self._engine(self.config.get()),
                workdir=workdir,
                cfg=self.config.get(),
            )
        except Exception as e:  # noqa: BLE001 采集失败不阻塞任务执行
            logger.warning("任务 %s 环境快照采集失败: %s", task_id, e)
            snapshot = error_snapshot()
        try:
            self.db.set_task_status(task_id, None,
                                    environment=serialize_snapshot(snapshot))
            self.db.add_log(task_id, "info",
                            "已采集任务执行环境快照（引擎/模型/起始提交/平台版本/配置哈希）")
        except Exception as e:  # noqa: BLE001 落库失败也不阻塞任务执行
            logger.warning("任务 %s 环境快照落库失败: %s", task_id, e)

    def _capture_base_sha(self, task_id: int, workdir: Path,
                         git_env: dict | None = None) -> None:
        """任务首次执行开始时记录工作区基线提交（issue #252）。

        prepare_workspace 已把工作区重置到远端默认主分支最新提交，此时
        HEAD 即「任务开始前 main 基线」；收尾时用 git diff base_sha..HEAD
        采集任务改动（相对 main 的改动文件与行数）。只采一次：重试/断点
        续跑不覆盖首次基线（同 issue #276 环境快照的首次语义），保证
        diff 边界稳定。采集失败不阻塞任务执行——无基线时评论隐藏改动
        段落（report.collect_diff_data 返回空，验收标准 3 不报错）。
        """
        task = self.db.get_task(task_id)
        if task is not None and _row_get(task, "base_sha"):
            return
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "HEAD"],
                env=git_env, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                self.db.set_task_status(task_id, None,
                                        base_sha=result.stdout.strip())
                self.db.add_log(task_id, "info",
                                "已记录任务改动基线提交（结构化报告 diff 采集用）")
        except Exception as e:  # noqa: BLE001 采集失败不阻塞任务执行
            logger.warning("任务 %s 基线提交采集失败: %s", task_id, e)

    def _run_once(self, task_id: int, repo: dict, issue: dict,
                  resume_session: str | None = None,
                  resume_history: list | None = None) -> tuple[int, str]:
        """执行一次任务引擎（插件体系分发，issue #140）。返回 (exit_code, output)。

        按 ``worker.engine`` 配置的引擎名查执行引擎插件（内置 claude /
        hermes / dsh 见 botler.plugins.executors）并委托执行；未知引擎回退
        claude。断点续跑语义由各引擎插件承担：
        - claude（issue #8）：resume_session 非空时 --resume 接续上次会话；
        - hermes（issue #47）：resume_history 为历史消息（显式传入优先，
          未传入时从任务落库 hermes_history 解析）；
        - dsh（issue #84）：resume_session 为上次会话 id（SDK 持久化会话）。
        """
        cfg = self.config.get()
        plugin = get_plugin(PluginKind.EXECUTOR, self._engine(cfg))
        return plugin.run(self, task_id, repo, issue,
                          resume_session, resume_history)

    def _persist_engine_usage(self, task_id: int, engine: str,
                             usage: dict | None,
                             model: str | None = None) -> None:
        """把引擎采集的 token 用量落库 task_usage（issue #235）。

        usage 为 None（引擎无用量数据）→ 不写库（任务详情显示「无数据」）；
        费用估算：优先引擎自带费用（claude total_cost_usd / hermes
        session_estimated_cost_usd），否则按 config usage.pricing 单价
        估算；无单价 → estimated_cost 为 None（前端只展示 token 数）。
        落库失败仅记日志，绝不阻塞任务收尾。
        """
        try:
            record = finalize_usage(
                engine, usage, model=model,
                pricing=self.config.get().usage_pricing,
                currency=self.config.get().usage_currency)
        except Exception as e:  # noqa: BLE001 估算失败按无用量处理
            logger.warning("任务 %s 用量归一化失败: %s", task_id, e)
            record = None
        if record is None:
            return
        try:
            self.db.save_task_usage(
                task_id, engine=engine,
                model=record["model"],
                prompt_tokens=record["prompt_tokens"],
                completion_tokens=record["completion_tokens"],
                total_tokens=record["total_tokens"],
                estimated_cost=record["estimated_cost"],
                currency=record["currency"],
                raw_usage=json.dumps(record["raw_usage"], ensure_ascii=False)
                if record["raw_usage"] is not None else None)
            cost_text = (f"，估算费用 {record['estimated_cost']} "
                         f"{record['currency']}") if record["estimated_cost"] is not None else ""
            self.db.add_log(
                task_id, "info",
                f"token 用量已记录：{record['prompt_tokens']} 输入 / "
                f"{record['completion_tokens']} 输出"
                f"（模型 {record['model'] or engine}）{cost_text}")
        except Exception as e:  # noqa: BLE001 落库失败不影响任务收尾
            self.db.add_log(task_id, "warn", f"token 用量落库失败: {e}")

    def _persist_claude_usage(self, task_id: int, output: str) -> None:
        """从 claude 输出解析用量并落库（执行结束/停止/超时路径共用）。

        结果行（type=result）含 usage 字段（stream-json 与单行
        --output-format json 同构），modelUsage 提供模型名；解析失败
        （异常中断无结果行）不落库，任务详情显示「无数据」。
        """
        data = self._last_json_object(output)
        usage = parse_claude_result_usage(data)
        if usage is None:
            return
        self._persist_engine_usage(task_id, "claude", usage)

    def _run_claude_once(self, task_id: int, repo: dict, issue: dict,
                         resume_session: str | None = None) -> tuple[int, str]:
        """执行一次 claude 引擎（Claude Code CLI 无头模式）。

        resume_session 非空时为断点续跑（claude --resume 接续上次会话，
        工作区保留）；执行后解析 JSON 输出中的 session_id 落库。本方法由
        ClaudeEnginePlugin（botler.plugins.executors）委托调用。
        """
        cfg = self.config.get()
        workdir, git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, git_env)
        if resume_session:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次会话 {resume_session[:8]}… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 claude -p（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        env = self._build_env(repo, issue)

        log_path = self._log_file(task_id)

        cmd = [cfg.claude_command, *cfg.claude_args]
        # stream-json 输出在 claude 2.1.x 强制要求 --verbose，缺失直接报错；
        # 用户配置可能只写 --output-format stream-json，这里自动补齐
        if ("--output-format" in cmd
                and cmd[cmd.index("--output-format") + 1] == "stream-json"
                and "--verbose" not in cmd):
            cmd.append("--verbose")
        # 无人值守（-p）下跳过权限确认：GIT_ASKPASS/GITLAB_TOKEN 只解决
        # 凭据，Bash/curl/Read/MCP 等操作仍会被权限系统拦截（task_7/8/9
        # 的 permission_denials），且无人值守无法交互授权，任务必然失败。
        cmd.append("--dangerously-skip-permissions")
        if resume_session:
            cmd.extend(["--resume", resume_session])
        cmd.append(prompt)
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
        except FileNotFoundError:
            raise ExecutorError(f"找不到 claude 命令: {cfg.claude_command}（请先 npm install -g @anthropic-ai/claude-code）")

        # 注册运行中进程（issue #35：一键停止可定位并终止 claude 进程组）
        with self._proc_lock:
            self._procs[task_id] = proc
        try:
            deadline = time.time() + cfg.task_timeout_seconds
            session_known = False

            # 停止请求先于进程创建到达（worker 准备阶段收到 stop）→ 立即终止
            stopped = self._stop_requested(task_id)

            def _on_chunk(chunk: str) -> None:
                nonlocal session_known
                # 运行中即把 session_id 落库（issue #20 实时查看），
                # 只落首次：进程未结束时 API 就能定位当前会话文件。
                # stream-json 下 init 行首条即带 session_id，比 result 更早
                if not session_known and self._persist_session_from_chunk(task_id, chunk):
                    session_known = True
                # 实时事件流（SSE）：逐行解析 stream-json 输出发布到总线
                self._publish_stream_line(task_id, chunk, parse_claude_stream_line)

            if not stopped:
                timed_out, stopped, chunks = self._drain_process_output(
                    proc, task_id, log_path, deadline, _on_chunk)
            else:
                timed_out, chunks = False, []

            if stopped:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已强制终止 claude 进程组")
                self._persist_session_id(task_id, output)
                self._persist_claude_usage(task_id, output)  # issue #235
                return STOP_EXIT_CODE, output

            if timed_out:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止进程组")
                self._persist_session_id(task_id, output)
                self._persist_claude_usage(task_id, output)  # issue #235
                return 124, output  # 124 = timeout 约定退出码

            exit_code = proc.wait(timeout=30)
            output = "".join(chunks)
            self.db.add_log(task_id, "info", f"claude 退出码: {exit_code}")
            self._persist_session_id(task_id, output)
            self._persist_claude_usage(task_id, output)  # issue #235
            return exit_code, output
        finally:
            with self._proc_lock:
                self._procs.pop(task_id, None)

    # ---- hermes 引擎（issue #47）----

    def _last_json_object(self, output: str) -> dict | None:
        """取输出中最后一个完整 JSON 对象（流式协议的结果行在最后）。

        claude stream-json 多行输出：最后一行是 result 事件；hermes runner
        流式输出：事件行在前，结果 JSON 收尾。旧单行协议（唯一行）同样适用。
        逐行从尾部扫描，容忍行间/行内噪音（每行内 raw_decode 取首个对象）。
        """
        if not output:
            return None
        for line in reversed(output.splitlines()):
            data = _load_json_output(line)
            if data is not None:
                return data
        return None

    def _result_line(self, output: str) -> dict | None:
        """claude 结果行或 None（异常中断时无结果行）。

        run_task 成功判定依据：stream-json 多行输出下 _load_json_output
        取首个 JSON 对象（init 行）会误判成功，必须找最后的结果行。
        判定宽松兼容两种格式：旧 --output-format json 单行结果（可能有
        type=result）与 stream-json 尾部 result 事件行——两者都带字符串
        result 字段；init/assistant/user 事件行均无该字段，不会误判。
        """
        data = self._last_json_object(output)
        if data is not None and isinstance(data.get("result"), str):
            return data
        return None

    def _hermes_history_from_output(self, output: str) -> list:
        """从 hermes runner 输出解析会话消息历史（messages 缺失/非列表 → 空列表）。"""
        data = self._last_json_object(output)
        messages = data.get("messages") if data else None
        return messages if isinstance(messages, list) else []

    def _hermes_result(self, output: str) -> str:
        """判定 hermes runner 输出：success / unresolvable / failed。

        - success：JSON 合法、error 为空、final_response 非空、未自认无法解决
        - unresolvable：final_response 命中「无法解决」表述（不重试）
        - failed：非 JSON / error 非空 / 缺 final_response（按失败重试）
        """
        data = self._last_json_object(output)
        if data is None or data.get("error"):
            return "failed"
        final_response = data.get("final_response")
        if not isinstance(final_response, str) or not final_response.strip():
            return "failed"
        if self._is_unresolvable(final_response):
            return "unresolvable"
        return "success"

    def _hermes_resume_data(self, raw: str | None) -> tuple[list | None, str | None]:
        """解析任务落库的 hermes_history（{"session_id", "messages"} JSON）。

        解析失败 / 为空 / messages 非列表 → (None, None)（降级全新会话）。
        """
        if not raw or not raw.strip():
            return None, None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        messages = data.get("messages")
        session_id = data.get("session_id")
        return (messages if isinstance(messages, list) and messages else None,
                session_id if isinstance(session_id, str) and session_id else None)

    def _persist_hermes_history(self, task_id: int, output: str) -> None:
        """执行结束后把 hermes 会话数据（session_id + messages）落库（Q3-B）。

        messages 为空时不落库（无从恢复，保持上次值）；落库失败不影响任务收尾。
        """
        data = self._last_json_object(output)
        messages = self._hermes_history_from_output(output)
        if data is None or not messages:
            return
        session_id = data.get("session_id")
        raw = json.dumps(
            {"session_id": session_id if isinstance(session_id, str) else "",
             "messages": messages},
            ensure_ascii=False)
        try:
            self.db.set_task_status(task_id, None, hermes_history=raw)
        except Exception as e:  # noqa: BLE001 历史落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"hermes 会话历史落库失败: {e}")

    def _run_hermes_once(self, task_id: int, repo: dict, issue: dict,
                         resume_history: list | None,
                         resume_session_id: str | None = None) -> tuple[int, str]:
        """执行一次 hermes 引擎（hermes agent SDK 进程内调用）。返回 (exit_code, output)。

        issue #171：集成方式从「子进程 + 部署机独立 hermes venv
        （hermes.command/hermes.args）」改为「hermes agent SDK 进程内
        集成」（对齐 dsh 引擎的 SDK 方式，issue #84）：worker 线程跑
        HermesSdkRunner（run_agent.AIAgent），主循环轮询停止请求与超时；
        停止/超时通过 runner.stop()（AIAgent.interrupt()，跨线程安全）
        请求中断并通知进行中的工具提前终止，语义等价旧模式的 SIGKILL
        进程组。输出协议不变（事件行 + 结果行），SSE 解析
        （parse_hermes_event_line）与结果判定（_hermes_result）、会话
        落库（hermes_history）直接复用。
        """
        cfg = self.config.get()
        workdir, _git_env = self.prepare_workspace(repo, resume=bool(resume_history))
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, _git_env)
        if resume_history:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次 hermes 会话（{len(resume_history)} 条历史）… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 hermes 引擎（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        env = self._build_env(repo, issue)

        log_path = self._log_file(task_id)
        lines: list[str] = []
        log_f = open(log_path, "w", encoding="utf-8", errors="replace")

        def _on_line(line: str) -> None:
            """worker 线程回调：写日志 + 收行 + 发布 SSE（单线程顺序调用）。"""
            log_f.write(line + "\n")
            log_f.flush()
            lines.append(line)
            self._publish_stream_line(task_id, line, parse_hermes_event_line)

        try:
            try:
                runner = HermesSdkRunner(
                    prompt=prompt,
                    session_id=resume_session_id,
                    history=resume_history,
                    task_id=str(task_id),
                    cwd=str(workdir),
                    env=env,
                    on_line=_on_line,
                )
                runner.start()
            except HermesSdkNotInstalledError as e:
                raise ExecutorError(str(e))

            deadline = time.time() + cfg.task_timeout_seconds
            timed_out = False
            stopped = False
            while not runner.done():
                if self._stop_requested(task_id):
                    stopped = True
                    runner.stop()
                    break
                if time.time() >= deadline:
                    timed_out = True
                    runner.stop()
                    break
                time.sleep(0.05)
            exit_code = runner.finish()
            # 事件行拼接必须保留换行分隔（与日志落盘 line + "\n" 一致）：
            # _last_json_object 按行扫描解析结果行，缺换行会误判 failed
            # （issue #119 dsh 同类问题）
            output = "\n".join(lines)

            if stopped:
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已请求 hermes 中断")
                # getattr 防御：旧 runner / 测试假 runner 可能无 usage/model
                self._persist_engine_usage(  # issue #235
                    task_id, "hermes", getattr(runner, "usage", None),
                    model=getattr(runner, "model", ""))
                return STOP_EXIT_CODE, output

            if timed_out:
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已请求 hermes 中断")
                self._persist_engine_usage(  # issue #235
                    task_id, "hermes", getattr(runner, "usage", None),
                    model=getattr(runner, "model", ""))
                return 124, output  # 124 = timeout 约定退出码

            self.db.add_log(task_id, "info", f"hermes 引擎退出码: {exit_code}")
            self._persist_engine_usage(  # issue #235
                task_id, "hermes", getattr(runner, "usage", None),
                model=getattr(runner, "model", ""))
            return exit_code, output
        finally:
            log_f.close()

    # ---- dsh 引擎（issue #84）----

    def _dsh_credentials(self, cfg) -> tuple[str | None, str | None]:
        """dsh 引擎 API Key / Base URL 解析链，返回 (api_key, base_url)。

        优先级：dsh 段显式配置 > 设置页「AI 供应商」中 provider=deepseek
        且 enabled 的项（issue #115）> 环境变量 DEEPSEEK_API_KEY /
        DEEPSEEK_BASE_URL（SDK 默认读取，botler 不覆盖，返回 None 即
        由 SDK 兜底）。

        issue #115 根因：任务 #194 #195 切 dsh 引擎后全部失败——用户
        只在设置页「AI 供应商」配过 DeepSeek key，dsh 段未配、部署机
        环境无 DEEPSEEK_API_KEY，SDK 无 key 可用 → DeepSeek API 401
        AUTH → finish_reason=error → 判失败。key 已在平台上配过却不
        消费，属于配置链路断裂，在此补齐回退。
        """
        api_key = cfg.dsh_api_key or None
        base_url = cfg.dsh_base_url or None
        if api_key is None:
            provider = next(
                (p for p in (getattr(cfg, "ai_providers", None) or [])
                 if isinstance(p, dict)
                 and str(p.get("provider", "")).strip() == "deepseek"
                 and bool(p.get("enabled", True))
                 and str(p.get("api_key", "") or "").strip()),
                None)
            if provider is not None:
                api_key = str(provider.get("api_key", "")).strip() or None
                base_url = base_url or (
                    str(provider.get("base_url", "") or "").strip() or None)
        return api_key, base_url

    def _run_dsh_once(self, task_id: int, repo: dict, issue: dict,
                      resume_session: str | None = None) -> tuple[int, str]:
        """执行一次 dsh 引擎（deepseek-harness SDK 进程内调用）。返回 (exit_code, output)。

        与 claude/hermes 不同，SDK 在 botler 进程内运行：worker 线程跑
        harness.run()（DshRunner），本循环轮询停止请求与超时；停止/超时
        通过 runner.stop() 关闭运行时强制终止（语义等价 SIGKILL 进程组）。
        输出协议与 hermes 对齐（事件行 + 结果行），SSE 解析
        （parse_hermes_event_line）与结果判定（_dsh_result）复用。
        会话 id 执行后落库 dsh_session_id（断点续跑，含停止/超时路径）。
        """
        cfg = self.config.get()
        workdir, _git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        self._capture_env_snapshot(task_id, workdir)
        self._capture_base_sha(task_id, workdir, _git_env)
        # issue #281 §4.7：resume 前校验会话可恢复性——session_root 目录
        # 已配置但不存在 = 会话必然丢失，如实降级为全新会话（不假装「对话
        # 已保留」）；未配置 session_root 时无法校验，按可恢复处理（现状）。
        if resume_session and not self._dsh_session_available(cfg, resume_session):
            self.db.set_task_status(task_id, None, dsh_session_id=None)
            self.db.add_log(
                task_id, "warn",
                f"上次 dsh 会话 {resume_session[:8]}… 的会话目录已不存在，"
                f"降级为全新会话（issue #281 诚实降级）")
            resume_session = None
        # issue #281 §4.7：会话 id 任务开始即落库（先落 id 再开跑）——
        # 新建任务预生成 id 并原子写库，任何时刻被强杀/重启 id 都已落库
        # 可恢复；恢复场景直接复用已落库 id。写入失败 = 任务失败（不静默
        # 降级，避免「以为能恢复、实际不能」）。
        if resume_session:
            dsh_sid = resume_session
        else:
            dsh_sid = self._new_dsh_session_id(task_id)
            try:
                self.db.set_task_status(task_id, None, dsh_session_id=dsh_sid)
            except Exception as e:  # noqa: BLE001 前置落库失败 = 任务失败
                raise ExecutorError(f"dsh 会话 id 前置落库失败: {e}") from e
            self.db.add_log(task_id, "info",
                            f"已预生成 dsh 会话 id {dsh_sid[:8]}… 并落库"
                            f"（任务开始即落库，issue #281）")
        if resume_session:
            prompt = self._resume_prompt(repo, issue, task_id)
            self.db.add_log(
                task_id, "info",
                f"恢复上次 dsh 会话 {resume_session[:8]}… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 dsh 引擎（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        # issue #281 §4.1：dsh 提示词追加「进度上报约定」节（Phase 1 仅
        # dsh 引擎解析落库 [PROGRESS] 里程碑，claude/hermes 不受影响）。
        prompt += PROGRESS_REPORT_INSTRUCTION
        env = self._build_env(repo, issue)

        # issue #146：dsh 引擎提示词持久化 + 聊天记录落库（dsh_transcript）。
        # claude 引擎的提示词/聊天记录来自会话 jsonl（首条 user 消息 +
        # user/assistant/tool 行）；dsh SDK 会话文件是 runtime 内部格式，
        # 无法像 jsonl 那样解析。这里在 executor 侧把 prompt 与事件行累积
        # 出的消息落库，execution 接口读取返回——dsh 任务「查看提示词」
        # 与聊天记录不再显示「提示词未持久化 / 暂无聊天记录」。
        def _dsh_utc_ts() -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # 断点续跑：resume 时保留上次会话历史，追加本次恢复引导语为新
        # user 消息（与 SDK 会话内的真实输入一致）；fresh 从提示词开始。
        dsh_messages: list[dict] = (
            self._dsh_resume_messages(task_id) if resume_session else [])
        dsh_messages.append({"role": "user", "text": prompt,
                             "ts": _dsh_utc_ts(), "truncated": False})
        dsh_pending: list[str] = []  # 当前 assistant 回复的流式文本片段

        def _dsh_flush_pending() -> None:
            """把累积的 assistant 流式文本收口为一条聊天消息（空则跳过）。"""
            if not dsh_pending:
                return
            text = "".join(dsh_pending)
            dsh_pending.clear()
            if not text:
                return
            cut_text, cut = _truncate_text(text, _TRANSCRIPT_MAX_TEXT)
            dsh_messages.append({"role": "assistant", "text": cut_text,
                                 "ts": _dsh_utc_ts(), "truncated": cut})
            # issue #281 §4.1：assistant 文本收口时扫描 [PROGRESS] 里程碑
            # 落库 task_progress（增量落库，中断/强杀时已收口部分不丢）
            self._persist_progress_markers(task_id, text)

        def _dsh_accumulate(line: str) -> None:
            """从事件行累积聊天消息（issue #146）。

            stream_delta → assistant 文本片段；tool_start → 工具调用；
            thinking/status/raw 与结果行不展示（思考/状态/簿记已在事件流
            SSE 实时呈现，与 claude transcript 只保留 text 对齐），但会
            先收口当前回复片段保序。
            """
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                return
            if not isinstance(data, dict):
                return
            event = data.get("event")
            if event == "stream_delta":
                text = data.get("text")
                if isinstance(text, str) and text:
                    dsh_pending.append(text)
            elif event == "tool_start":
                _dsh_flush_pending()
                dsh_messages.append({
                    "role": "tool",
                    "tool": data.get("tool", "?"),
                    "input": data.get("input"),
                    "ts": _dsh_utc_ts()})
            elif event in ("status", "raw") or "final_response" in data:
                _dsh_flush_pending()  # 回合收口：先收口当前回复片段

        def _dsh_persist() -> None:
            """落库当前消息列表（每次事件行后调用，运行中实时可见）。

            进行中的流式回复片段（dsh_pending）不强制收口——半截文本不进
            聊天记录（与 claude 会话 jsonl 写完完整行才落盘一致），实时
            增量由事件流 SSE 呈现；收口只发生在 tool_start / status /
            raw / 结果行与执行结束时。
            """
            self._persist_dsh_transcript(task_id, prompt, dsh_messages)

        # 提示词先落库：runner 启动前（执行中）「查看提示词」/聊天记录
        # 首条 user 消息即可用，不等执行结束
        self._persist_dsh_transcript(task_id, prompt, dsh_messages)

        log_path = self._log_file(task_id)
        lines: list[str] = []
        log_f = open(log_path, "w", encoding="utf-8", errors="replace")

        def _on_line(line: str) -> None:
            """worker 线程回调：写日志 + 收行 + 发布 SSE + 累积聊天记录（单线程顺序调用）。"""
            log_f.write(line + "\n")
            log_f.flush()
            lines.append(line)
            events = parse_hermes_event_line(line)
            if events:
                for event in events:
                    self._publish_event(task_id, event)
            _dsh_accumulate(line)
            _dsh_persist()

        # issue #235：捕获本轮 runner 供执行结束后读 token 用量（runner
        # 内部在 worker 完成后聚合 usage；碰撞重跑后指向最新一轮 runner）
        last_runner: DshRunner | None = None

        def _run_round(session_id: str, round_prompt: str) -> tuple[int, bool, bool]:
            """跑一轮 dsh：构造 runner、启动、等待完成（停止/超时强制终止）。

            返回 (exit_code, stopped, timed_out)；SDK 未安装抛
            ExecutorError（run_task 捕获重试）。
            """
            nonlocal last_runner
            try:
                # issue #115：dsh 段未配 key 时回退设置页「AI 供应商」的
                # deepseek 项（用户已在该处配过 key，此前未被消费）
                dsh_api_key, dsh_base_url = self._dsh_credentials(cfg)
                runner = DshRunner(
                    prompt=round_prompt, session_id=session_id,
                    provider=cfg.dsh_provider, model=cfg.dsh_model,
                    max_tokens=cfg.dsh_max_tokens,
                    # 推理等级（issue #123）：dsh.reasoning_effort 经
                    # DshRunner 派生 Cordis 注入 SDK，空串 = 不设置
                    reasoning_effort=cfg.dsh_reasoning_effort,
                    cwd=str(workdir),
                    session_root=cfg.dsh_session_root or None,
                    cordis=cfg.dsh_cordis or None,
                    runtime_bin=cfg.dsh_runtime_bin or None,
                    base_url=dsh_base_url,
                    api_key=dsh_api_key,
                    env=env, on_line=_on_line)
                runner.start()
                last_runner = runner
            except DshSdkNotInstalledError as e:
                raise ExecutorError(str(e))

            deadline = time.time() + cfg.task_timeout_seconds
            round_timed_out = False
            round_stopped = False
            while not runner.done():
                if self._stop_requested(task_id):
                    round_stopped = True
                    runner.stop()
                    break
                if time.time() >= deadline:
                    round_timed_out = True
                    runner.stop()
                    break
                time.sleep(0.05)
            return runner.finish(), round_stopped, round_timed_out

        # issue #302：dsh runtime 会话持久化默认 zstd 压缩，且启动时会做
        # 根级编码检查——会话根目录残留旧版部署遗留的明文 session.jsonl
        # 会让整个 runtime 拒绝启动（encodingMismatch，任务 #415 反复失败
        # 的根因）。每次执行前把会话根目录归一化到 zstd（转换并删除明文
        # 遗留文件），保证新会话与断点续跑都能正常启动。
        try:
            session_root = effective_session_root(cfg.dsh_session_root, workdir)
            fixed = normalize_session_root_encoding(session_root)
            if fixed:
                self.db.add_log(
                    task_id, "info",
                    f"dsh 会话根目录 {session_root} 归一化到 zstd 压缩："
                    f"转换/清理 {fixed} 个遗留明文 session.jsonl（issue #302）")
        except Exception as e:  # noqa: BLE001 归一化失败不阻塞执行（runtime 会自报错）
            self.db.add_log(
                task_id, "warn",
                f"dsh 会话根目录编码归一化失败（继续执行）: {e}")

        try:
            exit_code, stopped, timed_out = _run_round(dsh_sid, prompt)
            # issue #291：SDK 会话 id collision（磁盘残留与 live 会话不
            # 匹配）→ 会话实际不可恢复，如实降级为全新会话重跑一次。
            # 背景：dsh SDK 0.1.0rc6 的 runtime 要求跨进程 resume 的输入
            # 与磁盘已持久化事件前缀逐事件一致（seq-aligned 重放），
            # botler 恢复引导语必然不匹配 → 每次 resume 必 collision，
            # 旧逻辑交给重试循环后仍复用同一落库 id 反复撞，重试耗尽
            # 任务失败（任务 #388/#390/#391）。降级不无限递归：新 id 无
            # 磁盘残留，重跑再撞则如实失败（防死循环）。
            output = "\n".join(lines)
            if (not stopped and not timed_out
                    and self._dsh_collision(output)):
                old_sid = dsh_sid
                dsh_sid = self._new_dsh_session_id(task_id)
                self.db.set_task_status(
                    task_id, None, dsh_session_id=dsh_sid)
                self.db.add_log(
                    task_id, "warn",
                    f"SDK 报告会话 {old_sid[:8]}… 无法恢复（id collision，"
                    f"磁盘残留与 live 会话不匹配），降级为全新会话 "
                    f"{dsh_sid[:8]}… 重跑（issue #291 诚实降级）")
                prompt = (self._dsh_downgrade_prompt(repo, issue, task_id)
                          + PROGRESS_REPORT_INSTRUCTION)
                # 聊天记录重置为全新会话视角（首条 user 消息 = 新提示词）；
                # 流式回复缓冲一并清空（碰撞轮无文本，防御性收口）
                _dsh_flush_pending()
                dsh_messages = [{
                    "role": "user", "text": prompt,
                    "ts": _dsh_utc_ts(), "truncated": False}]
                self._persist_dsh_transcript(task_id, prompt, dsh_messages)
                exit_code, stopped, timed_out = _run_round(dsh_sid, prompt)

            # issue #119：事件行拼接必须保留换行分隔（与日志落盘 line + "\n"
            # 一致）。DshRunner 的 on_line 回调行尾无换行，若用 ''.join 拼接，
            # output 整串无换行 → _last_json_object 按行扫描只解析到首个事件
            # 对象（finish_reason 缺失）→ _dsh_result 误判 failed → 触发重试；
            # _persist_dsh_session_id 同样解析不到 session_id → 断点续跑失效
            # → 每次重试都是全新会话（重复开发任务），重试耗尽后任务显示失败
            # （任务 #198 #199 日志：引擎 exit 0、结果行 completed 仍失败）。
            output = "\n".join(lines)
            _dsh_flush_pending()  # 收口最后一段回复
            _dsh_persist()  # 最终落库（停止/超时/正常共用）

            if stopped:
                self.db.add_log(task_id, "warn",
                                "任务被用户停止，已强制终止 dsh 运行时")
                self._persist_dsh_session_id(task_id, output)
                self._persist_engine_usage(  # issue #235
                    task_id, "dsh",
                    getattr(last_runner, "usage", None) if last_runner else None,
                    model=cfg.dsh_model)
                return STOP_EXIT_CODE, output

            if timed_out:
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止 dsh 运行时")
                self._persist_dsh_session_id(task_id, output)
                self._persist_engine_usage(  # issue #235
                    task_id, "dsh",
                    getattr(last_runner, "usage", None) if last_runner else None,
                    model=cfg.dsh_model)
                return 124, output  # 124 = timeout 约定退出码

            self.db.add_log(task_id, "info", f"dsh 引擎退出码: {exit_code}")
            self._persist_dsh_session_id(task_id, output)
            self._persist_engine_usage(  # issue #235
                task_id, "dsh",
                getattr(last_runner, "usage", None) if last_runner else None,
                model=cfg.dsh_model)
            return exit_code, output
        finally:
            log_f.close()

    def _dsh_result(self, output: str) -> str:
        """判定 dsh runner 输出：success / unresolvable / failed。

        - success：结果行合法、无 error、finish_reason=completed、
          final_response 非空、未自认无法解决
        - unresolvable：final_response 命中「无法解决」表述（不重试）
        - failed：error 非空 / finish_reason 非 completed（max-tokens 截断、
          error、无回合结束、未知 reason 一律不静默成功）/ 非 JSON /
          final_response 空 → 按失败重试
        """
        data = self._last_json_object(output)
        if data is None or data.get("error"):
            return "failed"
        if data.get("finish_reason") != "completed":
            return "failed"
        final_response = data.get("final_response")
        if not isinstance(final_response, str) or not final_response.strip():
            return "failed"
        if self._is_unresolvable(final_response):
            return "unresolvable"
        return "success"

    def _dsh_collision(self, output: str) -> bool:
        """识别 SDK 会话 id collision（issue #291）：结果行 error 且输出含
        「id collision」特征（runtime 报「already has a persisted log on
        disk that does not match this live session (id collision)」等变体）。

        跨进程 resume 在 dsh SDK 0.1.0rc6 下必撞该错误（seed 必须与磁盘
        已持久化事件前缀逐事件一致），命中即会话不可恢复，应降级全新会话，
        不应交给重试循环反复撞同一 id。
        """
        if "id collision" not in output:
            return False
        data = self._last_json_object(output)
        return bool(data and not data.get("error")
                    and data.get("finish_reason") == "error")

    def _dsh_downgrade_prompt(self, repo: dict, issue: dict,
                              task_id: int) -> str:
        """collision 降级后的全新会话提示词（issue #291 补充）：基础任务
        提示词 + 进度账本交接单。

        降级丢的只是对话历史（SDK id collision 无法恢复），task_progress
        账本（运行中增量落库，跨会话持久化）与保留的工作区是可靠的——
        如实说明后引导新会话按账本接续，禁止重做已标记 done 的步骤，
        避免全新对话从头重复实现（issue #281 用户抱怨的原始痛点）。
        """
        handoff = self._render_progress_handoff(task_id)
        return (self._build_prompt(repo, issue)
                + "\n\n【会话恢复失败，全新会话接续】上次 dsh 会话因 SDK "
                "限制无法恢复（id collision），对话历史已丢失；但平台进度"
                "账本与保留的工作区是可靠的，按以下记录直接接续：\n"
                + handoff)

    def _new_dsh_session_id(self, task_id: int) -> str:
        """预生成 dsh 会话 id（issue #281 §4.7）：botler-<task_id>-<ts>-<rand>。

        SDK `run(session_id=<id>)` 支持以指定 id 创建全新会话（用户确认），
        任务开始前即生成并落库，强杀/重启后凭已落库 id 经 SDK resume 续跑。
        """
        return (f"botler-{task_id}-"
                f"{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-"
                f"{secrets.token_hex(4)}")

    def _dsh_session_available(self, cfg, session_id: str) -> bool:
        """dsh 会话可恢复性校验（§4.7）：session_root 目录存在才可恢复。

        SDK 以 DSH_SESSION_ROOT 为会话持久化根目录；已配置但目录不存在 =
        会话必然已丢失（重部署重建目录/未挂载），如实降级为全新会话；
        未配置 session_root（无法校验，按可恢复处理，保持现状）或目录存在
        （信任 SDK 按 id 定位会话）时返回 True。
        """
        root = (getattr(cfg, "dsh_session_root", "") or "").strip()
        if not root:
            return True
        return Path(root).is_dir()

    def _persist_dsh_session_id(self, task_id: int, output: str) -> None:
        """执行结束后把 dsh 会话 id 落库（停止/超时/失败均落，供断点续跑）。

        结果行缺 session_id 或输出非法时保持旧值；落库失败不影响任务收尾。
        """
        data = self._last_json_object(output)
        sid = data.get("session_id") if data else None
        if not isinstance(sid, str) or not sid:
            return
        try:
            self.db.set_task_status(task_id, None, dsh_session_id=sid)
        except Exception as e:  # noqa: BLE001 会话 id 落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"dsh 会话 id 落库失败: {e}")

    def _dsh_resume_messages(self, task_id: int) -> list[dict]:
        """断点续跑：读取上次落库的 dsh 聊天记录消息列表（issue #146）。

        解析失败 / 无记录返回空列表（降级全新会话，与 _hermes_resume_data
        的容错语义一致）。
        """
        task = self.db.get_task(task_id)
        raw = _row_get(task, "dsh_transcript") if task is not None else None
        if not raw or not str(raw).strip():
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def _persist_dsh_transcript(self, task_id: int, prompt: str,
                                messages: list[dict]) -> None:
        """把 dsh 聊天记录（prompt + messages）落库（issue #146）。

        messages 超上限时截断：保留首条（提示词 user 消息）与最后
        _TRANSCRIPT_MAX_MESSAGES-1 条并置 truncated，与 claude
        parse_transcript 截断语义一致；落库失败不影响任务收尾。
        """
        truncated = False
        if len(messages) > _TRANSCRIPT_MAX_MESSAGES:
            head = messages[:1]
            keep = _TRANSCRIPT_MAX_MESSAGES - 1
            messages = head + (messages[-keep:] if keep > 0 else [])
            truncated = True
        raw = json.dumps(
            {"prompt": prompt or "", "messages": messages,
             "truncated": bool(truncated)},
            ensure_ascii=False)
        try:
            self.db.set_task_status(task_id, None, dsh_transcript=raw)
        except Exception as e:  # noqa: BLE001 聊天记录落库失败不阻塞任务收尾
            self.db.add_log(task_id, "warn", f"dsh 聊天记录落库失败: {e}")

    def _publish_event(self, task_id: int, event: dict) -> None:
        """归一化事件补 seq/ts 后发布到总线（SSE 实时推送）。"""
        seq = self._seq.get(task_id, 0) + 1
        self._seq[task_id] = seq
        event["seq"] = seq
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.event_bus.publish(task_id, event)

    def _publish_stream_line(self, task_id: int, chunk: str, parser) -> None:
        """把一行引擎输出解析为归一化事件并发布（解析失败/无事件静默跳过）。"""
        events = parser(chunk.strip())
        if not events:
            return
        for event in events:
            self._publish_event(task_id, event)

    def _persist_session_id(self, task_id: int, output: str) -> None:
        """执行结束后把 claude 会话 id 落库（供下次重试 / 平台重启断点续跑）。"""
        session_id = self._extract_session_id(output)
        if session_id:
            self.db.set_task_status(task_id, None, claude_session_id=session_id)

    def _persist_session_from_chunk(self, task_id: int, chunk: str) -> bool:
        """运行中首次发现 session_id 即落库（issue #20 实时查看聊天记录）。

        此前 session_id 只在执行完全结束后才落库，任务 running 期间 API
        拿不到当前会话，无法实时读聊天记录；这里在读循环里每行检测，
        首次解析到即落库（幂等，与结束后落库同一值）。
        """
        session_id = self._extract_session_id(chunk)
        if session_id:
            self.db.set_task_status(task_id, None, claude_session_id=session_id)
            return True
        return False

    def _log_file(self, task_id: int) -> Path:
        base = Path(__file__).resolve().parents[1] / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"task_{task_id}.log"

    # ---- 重试与结果判定 ----

    def _is_unresolvable(self, output: str) -> bool:
        return bool(_UNRESOLVABLE_RE.search(output))

    def _result_text(self, output: str) -> str:
        """提取引擎最终回复文本（claude result 行 / hermes final_response）。

        输出非 JSON 时原样返回（与 _extract_error 的容错一致）。
        """
        data = self._last_json_object(output)
        if data is None:
            return output
        if isinstance(data.get("result"), str):
            return _decode_escapes(data["result"])
        if isinstance(data.get("final_response"), str):
            return data["final_response"]
        return output

    def _output_ends_with_question(self, output: str, window: int = 400) -> bool:
        """最终回复结尾是否停在「等待用户决策」的提问上（issue #67）。

        只看结尾 window 字符：提问信号出现在回复末尾才代表 Claude 停在
        提问节点；中间提到过「请选择」但结尾是完成汇报的不算。
        """
        text = self._result_text(output)
        if not text:
            return False
        return bool(DECISION_QUESTION_RE.search(text[-window:]))

    def _extract_question(self, output: str, max_chars: int = 1000) -> str:
        """提取最终回复中的提问段落（issue #67 反馈到 issue 评论用）。

        从最后一个提问信号的所在行开始截到结尾（提问连同其上下文一起
        反馈，用户才能理解要决策什么）；无提问信号时取回复尾部。
        """
        text = self._result_text(output)
        if not text:
            return ""
        match = None
        for match in DECISION_QUESTION_RE.finditer(text):
            pass
        start = 0
        if match is not None:
            start = text.rfind("\n", 0, match.start()) + 1
        return text[start:][:max_chars]

    def _extract_error(self, output: str, max_chars: int = 3000) -> str:
        """从一次执行的输出中提取错误信息（trace 优先，否则取尾部）。

        claude -p --output-format json 时输出为 JSON，核心内容在 result 字段；
        result 内若含 Python Traceback 则从其起始处截取（异常堆栈对调试最有价值）。
        result 里嵌套序列化的转义（\\n 等字面量）先解码，保证展示可读（issue #16）。
        """
        if not output:
            return ""
        text = output
        data = self._last_json_object(output)
        if data is not None and isinstance(data.get("result"), str):
            text = _decode_escapes(data["result"])
        idx = text.rfind("Traceback (most recent call last)")
        if idx != -1:
            text = text[idx:]
        return text[-max_chars:]

    def _issue_state(self, project_id: int, iid: int,
                     repo: dict | None = None) -> str:
        try:
            issue, _ = self._call_with_fallback(
                repo, lambda c: c.get_issue(project_id, iid))
            return issue.get("state", "unknown")
        except GitLabError as e:
            return f"error: {e}"

    def _wait_pipeline_for_commit(self, task_id: int, project_id: int,
                                  commit_sha: str,
                                  repo: dict | None = None,
                                  detect_timeout: float | None = None,
                                  wait_timeout: float | None = None) -> str:
        """等待 commit_sha 触发的 CI 流水线到终态（issue #40）。

        任务 #63 缺陷：claude push 代码后退出，平台立即把任务判 succeeded，
        此时流水线还在运行（#63 于 13:31:45 收尾，流水线到 13:48:34 才结束）。
        现在成功收尾前先等流水线终态：

        - 探测窗口（默认 ci_wait_detect_seconds）：GitLab 收到 push 即创建
          流水线记录，窗口内找到 sha 匹配的最新流水线就进入终态等待；
          窗口内始终无匹配 → "no_pipeline"（仓库无 CI），调用方不等待；
        - 等待阶段（默认 ci_wait_timeout_seconds 总上限）：轮询到
          success/failed/canceled/skipped 任一终态即返回该状态；
          超上限仍非终态 → "timeout"；
        - 任一阶段收到用户停止请求 → "stopped"。

        detect_timeout / wait_timeout 仅测试注入用（None 时用配置默认值）。
        """
        cfg = self.config.get()
        detect = detect_timeout if detect_timeout is not None else cfg.ci_wait_detect_seconds
        total = wait_timeout if wait_timeout is not None else cfg.ci_wait_timeout_seconds
        deadline = time.time() + total
        detect_deadline = time.time() + min(detect, total)

        pipeline: dict | None = None
        while time.time() < detect_deadline:
            if self._stop_requested(task_id):
                return "stopped"
            try:
                latest, _ = self._call_with_fallback(
                    repo, lambda c: c.get_latest_pipeline(project_id))
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"查询最新流水线失败: {e}")
                return "no_pipeline"
            if latest is not None and latest.get("sha") == commit_sha:
                pipeline = latest
                break
            time.sleep(cfg.ci_wait_interval_seconds)
        if pipeline is None:
            return "no_pipeline"

        self.db.add_log(task_id, "info",
                        f"发现任务提交触发的流水线 #{pipeline['id']}，等待其到达终态…")
        while time.time() < deadline:
            if self._stop_requested(task_id):
                return "stopped"
            status = pipeline.get("status")
            if status in PIPELINE_TERMINAL_STATES:
                self.db.add_log(task_id, "info", f"CI 流水线 #{pipeline['id']} 终态: {status}")
                return status
            time.sleep(cfg.ci_wait_interval_seconds)
            try:
                pipeline, _ = self._call_with_fallback(
                    repo, lambda c: c.get_pipeline(project_id, pipeline["id"]))
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"查询流水线 #{pipeline['id']} 失败: {e}")
        return "timeout"

    def _await_task_pipeline(self, task_id: int, project_id: int,
                             issue_iid: int, output: str = "",
                             repo: dict | None = None) -> str:
        """成功收尾前的流水线等待入口（issue #40）：拿任务提交 sha 并等待终态。

        查不到提交（Claude 未推送代码，仅评论/分析）→ "no_pipeline" 不等待；
        查询提交失败（GitLab 报错）→ 同样降级 "no_pipeline"（不阻塞成功收尾）。
        查不到提交且最终回复以「等待用户决策」提问结尾（issue #67）→
        "awaiting_decision"：无人值守下 Claude 停在提问节点后自行退出，
        并无任何交付，任务不能判成功，提问应反馈到 issue 等待用户回复。
        """
        try:
            sha, _ = self._call_with_fallback(
                repo, lambda c: c.find_commit_for_issue(project_id, issue_iid))
        except GitLabError as e:
            self.db.add_log(task_id, "warn", f"查询任务提交失败，跳过流水线等待: {e}")
            return "no_pipeline"
        if not sha:
            if self._output_ends_with_question(output):
                self.db.add_log(
                    task_id, "info",
                    "未找到任务提交，且 Claude 最终回复以提问结尾，"
                    "判定为等待用户决策（问题反馈到 issue）")
                return "awaiting_decision"
            self.db.add_log(task_id, "info", "未找到任务提交，无流水线可等，直接成功收尾")
            return "no_pipeline"
        self.db.add_log(task_id, "info", f"等待任务提交 {sha[:8]} 触发的 CI 流水线到达终态…")
        return self._wait_pipeline_for_commit(task_id, project_id, sha, repo)

    def _await_pipeline_and_finish_succeeded(self, task_id: int, project_id: int,
                                             issue_iid: int, output: str,
                                             repo: dict | None = None) -> None:
        """成功收尾前的流水线等待与成功收尾（issue #40 + #47 抽取）。

        claude 与 hermes 两引擎共用：等待任务提交触发的 CI 流水线终态，
        failed/canceled/timeout → 失败收尾；success/skipped/no_pipeline →
        成功收尾（打 bot-done、记录 commit、发通知）；awaiting_decision →
        提问反馈收尾（issue #67，任务未完成，等用户回复）。
        """
        # issue #40：成功收尾前等待任务提交触发的 CI 流水线终态。
        # 此前 claude exit 0 即判成功，流水线还在运行任务就显示
        # 已完成（任务 #63 于 13:31:45 收尾，流水线到 13:48:34 才结束）。
        pipeline_state = self._await_task_pipeline(task_id, project_id,
                                                   issue_iid, output, repo)
        if pipeline_state == "awaiting_decision":
            self._finish_asked(task_id, output, repo=repo)
            return
        if pipeline_state == "stopped":
            self._finish_stopped(task_id)
            return
        if pipeline_state in ("failed", "canceled"):
            self._finish_failed(
                task_id,
                f"CI 流水线状态为 {pipeline_state}，任务视为失败",
                output, repo=repo)
            return
        if pipeline_state == "timeout":
            self._finish_failed(
                task_id,
                "CI 流水线超时未完成，任务视为失败",
                output, repo=repo)
            return
        # success / skipped / no_pipeline → 成功收尾
        self._finish_succeeded(task_id, output, repo=repo)

    def run_task(self, task_id: int) -> None:
        """任务主流程：单次或重试执行，写状态机与收尾评论。"""
        cfg = self.config.get()
        engine = self._engine(cfg)  # issue #47：claude / hermes 引擎分派
        task = self.db.get_task(task_id)
        if task is None:
            logger.warning("任务 %s 不存在，跳过", task_id)
            return
        repo = self.db.get_repo(task["repo_id"])
        if repo is None:
            self.db.set_task_status(task_id, STATUS_FAILED,
                                    error_message="仓库记录不存在")
            return

        # 原子抢占（issue #24）：多实例并存时同一任务可能被多次领取，
        # 只有状态为 queued/retrying 的任务能抢到 running；抢不到说明
        # 其他实例已领取或任务已结束，直接跳过避免重复执行/状态错乱。
        if not self.db.claim_task(task_id):
            logger.info("任务 %s 已被其他实例领取或已结束（状态非 queued/retrying），跳过", task_id)
            return

        # 用户一键停止（issue #35）：停止请求可能先于 worker 领取到达
        # （scheduler.stop_all 先落库再登记请求），领取后立即检查，
        # 避免已经停止的任务再发起执行
        if self._stop_requested(task_id):
            self._finish_stopped(task_id)
            return

        # issue #120：执行引擎按任务落库——记录本次实际执行的引擎
        # （claude / hermes / dsh），概览页 issue 右边栏按任务展示历史
        # 引擎，全局 worker.engine 切换后旧 issue 不再误显新引擎
        self.db.set_task_status(task_id, None, engine=engine)

        project_id, issue_iid = task["project_id"], task["issue_iid"]
        self.db.set_task_status(task_id, None, log_path=str(self._log_file(task_id)))
        # issue #280：拉取 issue 遇 GitLab 瞬时故障（502/503/限流/网络抖动）
        # 时按指数退避重试，不立即判失败——08-17 生产 GitLab 短暂不可用，
        # 44 个排队任务启动阶段 get_issue 一次 502 即全部打成 failed，且失败
        # 评论同样发不出，issue 上「没有任何回复评论」。重试耗尽才判失败。
        for attempt in range(ISSUE_FETCH_MAX_ATTEMPTS):
            try:
                issue, _ = self._call_with_fallback(
                    repo, lambda c: c.get_issue(project_id, issue_iid))
                break
            except GitLabError as e:
                if attempt >= ISSUE_FETCH_MAX_ATTEMPTS - 1 or not is_transient_error(e):
                    self._finish_failed(task_id,
                                        f"获取 issue {project_id}#{issue_iid} 失败: {e}",
                                        repo=repo)
                    return
                delay = min(ISSUE_FETCH_BASE_DELAY * (2 ** attempt), ISSUE_FETCH_MAX_DELAY)
                self.db.add_log(task_id, "warn",
                                f"获取 issue 瞬时故障（{e}），{delay:.0f}s 后重试"
                                f"（第 {attempt + 1}/{ISSUE_FETCH_MAX_ATTEMPTS} 次）")
                time.sleep(delay)

        max_retries = cfg.max_retries
        attempt = 0
        last_output = ""
        last_exit = -1
        attempt_details: list[dict] = []  # 每次失败的详情（退出码 + 提取的 trace/错误），供 error_detail 落库

        while True:
            # 用户一键停止（issue #35）：重试循环每轮检查停止请求
            # （请求可能在第 N 次失败后、重试间隙到达），命中即终止
            if self._stop_requested(task_id):
                self._finish_stopped(task_id)
                return
            attempt += 1
            # issue #8 断点续跑：上次执行留过 claude 会话 → 接续（resume）；
            # 会话文件丢失（如 ~/.claude 未持久化）→ 清除后降级全新会话。
            # hermes 引擎（issue #47）的断点续跑数据在 tasks.hermes_history，
            # 由 _run_once 内部读取（session 文件机制仅 claude 有）；
            # dsh 引擎（issue #84）的断点续跑数据在 tasks.dsh_session_id
            # （SDK 在 session_root 持久化会话，无需本地会话文件校验）。
            task = self.db.get_task(task_id)
            resume_session = None
            if engine == "hermes":
                pass
            elif engine == "dsh":
                resume_session = _row_get(task, "dsh_session_id") if task else None
            else:
                resume_session = task["claude_session_id"] if task else None
                if resume_session and not self._session_file(resume_session):
                    self.db.set_task_status(task_id, None, claude_session_id=None)
                    self.db.add_log(
                        task_id, "warn",
                        f"上次会话 {resume_session[:8]}… 的会话文件已不存在，降级为全新会话")
                    resume_session = None
            self.db.set_task_status(
                task_id, STATUS_RUNNING,
                attempt_count=attempt,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                finished_at=None, error_message=None)
            self.db.add_log(task_id, "info", f"第 {attempt} 次尝试开始")
            logger.info("任务 %s（%s#%s）第 %s 次执行", task_id, project_id, issue_iid, attempt)

            # 首次尝试时在 issue 上回复「处理中」，提升体验（不刷屏，重试不再重复）。
            # issue #280：瞬时故障退避重试，避免 GitLab 短暂不可用时用户收不到任何回复
            if attempt == 1:
                try:
                    self._transient_retry(
                        "发送处理中评论",
                        lambda: self._call_with_fallback(
                            repo, lambda c: c.add_comment(
                                project_id, issue_iid,
                                "🤖 Botler 已收到该 issue，开始处理中…")))
                except GitLabError as e:
                    self.db.add_log(task_id, "warn", f"发送处理中评论失败: {e}")

            try:
                exit_code, output = self._run_once(task_id, repo, issue, resume_session)
            except ExecutorError as e:
                exit_code, output = -1, f"[executor] {e}"
                self.db.add_log(task_id, "error", output)
            except Exception as e:  # 兜底异常
                exit_code, output = -1, f"[executor] 未预期异常: {e}"
                self.db.add_log(task_id, "error", output)

            last_output, last_exit = output, exit_code

            # 被停止（issue #35）：进程组被杀（STOP_EXIT_CODE）且已登记
            # 停止请求 → 直接收尾，不进入重试分支（避免状态闪现 retrying）
            if exit_code == STOP_EXIT_CODE and self._stop_requested(task_id):
                self._finish_stopped(task_id)
                return

            if exit_code == 0:
                state = self._issue_state(project_id, issue_iid, repo)
                self.db.add_log(task_id, "info", f"执行结束，issue 当前状态: {state}")
                if engine in ("hermes", "dsh"):
                    # hermes（issue #47）/ dsh（issue #84）引擎：成功判定看
                    # runner 输出 JSON，非 JSON / error 非空 / dsh 回合未正常
                    # 完成落入下方重试分支（与 claude exit 0 无 JSON 一致）
                    result = (self._hermes_result(output) if engine == "hermes"
                              else self._dsh_result(output))
                    if result == "unresolvable":
                        detail = {"attempt": attempt, "exit_code": exit_code,
                                  "error": self._extract_error(output)}
                        self._finish_failed(task_id, f"{engine} 报告无法解决该 issue", output,
                                            error_detail=self._dump_error_detail(
                                                [*attempt_details, detail], last_exit),
                                            repo=repo)
                        return
                    if result == "success":
                        # Q3-B：会话数据落库（断点续跑），收尾流程与 claude 引擎一致
                        # （dsh 的会话 id 已在 _run_dsh_once 内部落库）
                        if engine == "hermes":
                            self._persist_hermes_history(task_id, output)
                        self._await_pipeline_and_finish_succeeded(
                            task_id, project_id, issue_iid, output, repo)
                        return
                else:
                    # exit 0 但 Claude 自认无法解决 → 失败终态（不重试）
                    if self._is_unresolvable(output):
                        detail = {"attempt": attempt, "exit_code": exit_code,
                                  "error": self._extract_error(output)}
                        self._finish_failed(task_id, "Claude Code 报告无法解决该 issue", output,
                                            error_detail=self._dump_error_detail(
                                                [*attempt_details, detail], last_exit),
                                            repo=repo)
                        return
                    # 成功判定（issue #25 第二轮）：完成任务即成功，不再要求关闭 issue。
                    # 模版库规范（docs/labels.md）：任务完成后不关闭 issue——留结果评论、
                    # 打 bot-done，等用户确认后手动关闭。旧逻辑以 issue closed 为成功
                    # 标志，exit 0 但 issue 仍 open 时判失败并重试，迫使 Claude 在完成
                    # 开发后违规关闭 issue（生产日志 task_30/31：issue #28 完成即被关）。
                    # 新判定：正常完成输出（JSON result，非「无法解决」）即成功，
                    # 无论 issue 是否仍 open。
                    # stream-json 多行输出下必须定位 type=result 行
                    # （_result_line 从尾部扫描），首个 JSON 对象是 init 行，
                    # 不能作为成功依据（异常中断的输出同样含 init 行）
                    if self._result_line(output) is not None:
                        self._await_pipeline_and_finish_succeeded(
                            task_id, project_id, issue_iid, output, repo)
                        return

            # 记录本次失败详情（含 trace 提取），供界面「查看详细原因」按钮展示
            attempt_details.append({
                "attempt": attempt,
                "exit_code": exit_code,
                "error": self._extract_error(output),
            })

            # 环境性失败 → 按策略重试
            if attempt > max_retries:
                break
            self.db.set_task_status(task_id, STATUS_RETRYING)
            self.db.add_log(task_id, "warn", f"第 {attempt} 次失败（exit {exit_code}），准备重试（剩余 {max_retries - attempt} 次）")
            time.sleep(5)

        self._finish_failed(
            task_id, f"重试耗尽（{max_retries} 次）后仍失败，最后退出码 {last_exit}",
            last_output,
            error_detail=self._dump_error_detail(attempt_details, last_exit),
            repo=repo)

    # ---- 收尾 ----

    def _finish_stopped(self, task_id: int) -> None:
        """用户一键停止收尾（issue #35）：条件落 interrupted 终态（幂等）。

        常规路径状态已由 scheduler.stop_all → db.stop_active_tasks 统一
        落库，此处条件更新兜底「刚被领取尚未被停止流程覆盖」的任务；
        状态已终态时跳过覆盖（多实例场景由先完成者生效）。
        """
        if not self.db.finish_task(
                task_id, STATUS_INTERRUPTED,
                exit_code=None,
                error_message="用户手动停止（一键停止所有任务）",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            return
        self.db.add_log(task_id, "warn", "任务已停止：用户一键停止所有任务")
        # issue #69：停止请求已消费，收尾即清除登记——请求残留会导致任务
        # 手动重试后 worker 领取时再次命中，被立即打回 interrupted
        self.clear_stop_request(task_id)

    def _emit_task_event(self, task_id: int, event: str, reason: str = "") -> None:
        """任务收尾向全部已注册的 notifier 插件分发事件（issue #21/#136，
        插件化 issue #140）。查库失败不阻塞收尾。

        统一分发网页通知（in_app）与外部 webhook 推送（webhook）两类
        通道：webhook 插件需要 issue 完整信息（正文/链接供模板占位符
        渲染），分发前统一拉取一次，失败降级用任务记录数据；各通道自行
        检查启用条件（webhook.enabled / 地址配置，未启用返回 None 跳过），
        任一通道失败仅记日志，绝不阻塞任务收尾。
        """
        from .webhook_push import WebhookPushError
        try:
            task = self.db.get_task(task_id)
            if task is None:
                return
            repo = self.db.get_repo(task["repo_id"])
            repo_name = repo["name"] if repo else ""
            repo_url = repo["url"] if repo else ""
            issue = None
            if repo is not None and event == "task_succeeded":
                try:
                    issue, _ = self._call_with_fallback(
                        repo, lambda c: c.get_issue(
                            task["project_id"], task["issue_iid"]))
                except Exception as e:  # noqa: BLE001 拉取失败降级用任务记录数据
                    logger.warning("通知分发查询 issue %s#%s 失败: %s",
                                   task["project_id"], task["issue_iid"], e)
            for plugin in list_plugins(PluginKind.NOTIFIER):
                try:
                    if event == "task_succeeded":
                        result = plugin.send_task_succeeded(
                            self, dict(task), repo_name=repo_name,
                            repo_url=repo_url, issue=issue)
                        if plugin.name == "webhook" and result is not None:
                            self.db.add_log(
                                task_id, "info",
                                f"webhook 推送成功（HTTP {result['status_code']}）")
                    elif event == "task_failed":
                        plugin.send_task_failed(
                            self, dict(task), reason, repo_name=repo_name)
                except WebhookPushError as e:
                    try:
                        self.db.add_log(task_id, "warn", f"webhook 推送失败: {e}")
                    except Exception:  # noqa: BLE001 日志落库失败忽略
                        pass
                    logger.warning("任务 %s webhook 推送失败: %s", task_id, e)
                except Exception:  # noqa: BLE001 任一通道失败不阻塞任务收尾
                    logger.exception("任务 %s 通知插件 %s 分发失败",
                                     task_id, plugin.name)
        except Exception:  # noqa: BLE001 通知失败不影响任务收尾
            logger.exception("任务 %s 通知事件记录失败", task_id)

    def _dump_error_detail(self, attempts: list[dict], last_exit: int) -> str:
        """把每次尝试的失败详情序列化为 error_detail（JSON 字符串，界面「详情」按钮展示）。"""
        return json.dumps(
            {"summary": f"重试耗尽后仍失败，最后退出码 {last_exit}", "attempts": attempts},
            ensure_ascii=False)

    def _tail_output(self, output: str) -> str:
        # 逐行重排 claude JSON 输出（result 嵌套转义解码，issue #16）
        lines = [format_display_line(l) for l in output.strip().splitlines()]
        if len(lines) > LOG_TAIL_LINES:
            lines = lines[-LOG_TAIL_LINES:]
        return "\n".join(lines)

    def _finish_succeeded(self, task_id: int, output: str,
                          repo: dict | None = None) -> None:
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_SUCCEEDED,
                exit_code=0,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 成功收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "info", "任务成功：Claude Code 已完成处理（issue 保持打开，等用户确认后手动关闭）")
        self._write_log_tail(task_id, output)
        self._record_commit(task_id, repo)
        # issue #109：检测并恢复被 GitLab autoclose 自动关闭的 issue
        # （提交信息命中默认关闭模式时 GitLab 系统自动关闭，用户侧表现为
        # 「agent 自己 close issue」）；人工关闭不干预，检测失败不阻塞。
        task = self.db.get_task(task_id)
        if task is not None:
            self._restore_autoclosed_issue(task, repo)
        # issue #34：成功时由平台代码直接打 bot-done 标签（幂等），不再依赖
        # Claude 按模板打——Claude 忘打会导致 issue 无终态标签被重复领取。
        # issue #67：同步移除 in-progress（Claude 领取时打的处理中标签），
        # 避免收尾后与终态标签并存。
        # 打标签失败不阻塞任务成功（仅记 warn，用户可手动补标签）。
        if task is not None:
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_labels(
                        task["project_id"], task["issue_iid"], ["bot-done"],
                        remove=["in-progress"]))
                # issue #49：finished_at 语义 = 系统给 issue 打上 bot-done
                # 标记的时间。打标签成功后把 finished_at 更新为打标时刻，
                # 任务页「用时」以它与 created_at（系统接收时间）动态计算
                # 完整处理周期；打标失败保留收尾时刻（下方 warn 兜底）。
                self.db.set_task_status(
                    task_id, None,
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
                self.db.add_log(task_id, "info", "已在 issue 上打 bot-done 标签，等待用户确认后手动关闭")
            except GitLabError as e:
                self.db.add_log(task_id, "warn", f"打 bot-done 标签失败: {e}")
        # issue #79：结果评论不再依赖 Claude 按模板自行留言，平台兜底写
        # 完成报告（防重：最后一条评论是 bot 本人则跳过）；写评论失败
        # 不阻塞任务成功（与打标签一致的容错策略）。
        if task is not None:
            self._leave_success_comment(task, output, repo)
        # 任务消息分发（issue #21/#136，插件化 issue #140）：网页通知 +
        # webhook 推送统一由 notifier 插件分发（各通道自检启用条件，
        # 失败仅记日志不阻塞任务成功收尾）
        self._emit_task_event(task_id, "task_succeeded")
        logger.info("任务 %s 成功", task_id)

    def _task_duration_text(self, task: dict) -> str:
        """任务用时文案（created_at → finished_at，UTC 串，issue #252）。

        与前端 fmtDuration 同语义：系统接收到 issue → 收尾打 bot-done
        标记；finished_at 缺失（异常收尾/打标失败兜底）时返回空串
        （渲染层隐藏用时行）。
        """
        created = _row_get(task, "created_at") or ""
        finished = _row_get(task, "finished_at") or ""
        if not created or not finished:
            return ""
        try:
            start = calendar.timegm(time.strptime(created, "%Y-%m-%d %H:%M:%S"))
            end = calendar.timegm(time.strptime(finished, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            return ""
        return format_duration(max(0, end - start))

    def _build_report_comment(self, task: dict, output: str, *,
                              repo: dict | None = None,
                              failed: bool = False,
                              reason: str = "",
                              log_tail: str = "") -> str:
        """按可配置评论模版渲染结构化执行报告（issue #252）。

        采集：任务改动（相对任务开始前 main 基线 base_sha 的 git diff，
        改动文件表格 + 新增/删除列表）、测试摘要（从执行输出日志提取
        pass/fail 计数）、提交链接、用时。无基线/无数据 → 对应占位符
        为空，渲染后段落自动隐藏（验收标准 3），全程不抛错阻塞收尾
        （采集/渲染失败记 warn 后回退内置模版）。
        """
        base_sha = _row_get(task, "base_sha") or ""
        diff = EMPTY_DIFF
        workdir = None
        if repo is not None:
            try:
                workdir = self._repo_workdir(repo)
            except Exception:  # noqa: BLE001 工作区不可用 → 隐藏改动段落
                workdir = None
        if workdir is not None and base_sha:
            try:
                diff = collect_diff_data(workdir, base_sha)
            except Exception as e:  # noqa: BLE001 采集失败不阻塞收尾
                self.db.add_log(task["id"], "warn", f"采集任务改动失败: {e}")
        cfg = self.config.get()
        template = (cfg.comment_template or
                    (DEFAULT_FAILURE_COMMENT_TEMPLATE if failed
                     else DEFAULT_COMMENT_TEMPLATE))
        sha = _row_get(task, "commit_sha") or ""
        commit_link = ""
        if sha:
            short = sha[:8]
            url = f"{cfg.gitlab_url.rstrip('/')}/-/commit/{sha}"
            commit_link = f"[{short}]({url})"
        # issue #274：失败分类徽章与处理建议占位符（供自定义评论模版使用；
        # 未配置新占位符的模版保持原样，分类信息由 _finish_failed 前缀行补齐）
        category = _row_get(task, "failure_category") or ""
        variables = {
            "result_summary": "" if failed else self._success_summary(output),
            "diff_stat": build_diff_table(diff),
            "test_summary": format_test_summary(parse_test_summary(output)),
            "commit_link": commit_link,
            "commit_sha": sha[:8] if sha else "",
            "duration": self._task_duration_text(task),
            "error_message": reason,
            "log_tail": log_tail,
            "failure_category": category,
            "failure_category_badge": (
                f"{category_label(category)}（{category}）" if category else ""),
            "failure_advice": category_advice(category) if category else "",
        }
        try:
            return render_comment(template, variables)
        except Exception as e:  # noqa: BLE001 用户模版写坏 → 回退内置
            self.db.add_log(task["id"], "warn",
                            f"渲染结果评论失败（回退内置模版）: {e}")
            return render_comment(
                DEFAULT_FAILURE_COMMENT_TEMPLATE if failed
                else DEFAULT_COMMENT_TEMPLATE, variables)

    def _leave_success_comment(self, task: dict, output: str,
                               repo: dict | None = None) -> None:
        """任务成功时平台兜底写完成评论（issue #79）。

        此前结果评论依赖 Claude 按模板自行留言，全局 bot token 失效后
        Claude 侧 API 401 失败，任务成功（bot-done 已打）但 issue 上没
        有任何报告评论。防重：最后一条非系统评论是 bot 本人（Claude 已
        留）→ 跳过；检查/写评论失败均不阻塞任务成功（仅记 warn 日志）。
        """
        project_id, issue_iid = task["project_id"], task["issue_iid"]
        try:
            last_author, client = self._call_with_fallback(
                repo, lambda c: c.last_note_author_id(project_id, issue_iid))
        except GitLabError as e:
            self.db.add_log(task["id"], "warn", f"检查最后评论作者失败: {e}")
            return
        cfg = self.config.get()
        bot_ids = {cfg.bot_id} if getattr(cfg, "bot_id", None) else set()
        try:
            # remote 兜底客户端（如有）的账号同样视为 bot 本人——
            # 会话内 Claude 可能用 remote token 写评论（issue #79 修复后）
            bot_ids.add(client.get_bot_id())
        except Exception:  # noqa: BLE001 查询失败/无该方法不阻塞防重
            pass
        if last_author in bot_ids:
            self.db.add_log(task["id"], "info", "Claude 已留结果评论，平台不重复写")
            return
        body = self._build_report_comment(task, output, repo=repo)
        if not body.strip():
            # 兜底：渲染为空（极端场景）时保留最小可读文案，不写空评论
            body = ("🤖 Botler 自动回复：任务已完成。\n\n"
                    "开发已完成，请确认后手动关闭本 issue"
                    "（平台已打 bot-done 标签）。")
        try:
            self._call_with_fallback(
                repo, lambda c: c.add_comment(project_id, issue_iid, body))
            self.db.add_log(task["id"], "info", "已在 issue 上留任务完成评论")
        except GitLabError as e:
            self.db.add_log(task["id"], "warn", f"留任务完成评论失败: {e}")

    def _restore_autoclosed_issue(self, task: dict,
                                  repo: dict | None = None) -> None:
        """检测并恢复被 GitLab autoclose 自动关闭的 issue（issue #109）。

        背景：GitLab 实例开启了 autoclose_referenced_issues——提交信息
        命中默认关闭模式（fix: #NN / fixes #NN / closes #NN 等）且推送
        到默认主分支时，issue 被 GitLab 系统自动关闭（closed_by 为该
        项目的 project bot，非任何真人用户）。graph2plan 任务的提交
        信息「fix: #24 …」曾反复触发，用户侧表现为「agent 自己 close
        issue」（实际 agent 从未调用关闭 API）。

        恢复规则：
        - closed 且 closed_by 是本项目的 project bot（autoclose 特征）
          → reopen + 补说明评论 + warn 日志；
        - closed 但 closed_by 是真实用户（人工关闭）→ 不干预；
        - 任意步骤失败 → 仅记 warn，不阻塞任务成功收尾（本方法为
          尽力而为护栏，任何异常都必须被吞掉）。
        """
        project_id, issue_iid = task["project_id"], task["issue_iid"]
        try:
            issue = self._call_with_fallback(
                repo, lambda c: c.get_issue(project_id, issue_iid))[0]
        except Exception as e:  # noqa: BLE001 护栏方法：任何查询异常都不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"autoclose 检测失败（查询 issue 状态出错）: {e}")
            return
        issue = issue or {}
        if issue.get("state") != "closed":
            return
        closed_by = issue.get("closed_by") or {}
        username = closed_by.get("username") or ""
        # autoclose 由该项目的 project bot 执行（username 形如
        # project_<id>_bot_<hash>），其余关闭者视为人工操作
        if not username.startswith(f"project_{project_id}_bot"):
            self.db.add_log(task["id"], "info",
                            "issue 为人工关闭（closed_by 非 project bot），平台不干预")
            return
        try:
            self._call_with_fallback(
                repo, lambda c: c.reopen_issue(project_id, issue_iid))
        except Exception as e:  # noqa: BLE001 同上：reopen 失败不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"重新打开被 autoclose 误关的 issue 失败: {e}")
            return
        body = ("补充说明：本 Issue 曾被 GitLab 的 autoclose 机制自动关闭"
                "（提交信息中的 `fix: #N` / `closes #N` 等 issue 引用命中"
                "实例默认关闭模式，随代码推送自动触发，非人工/Agent 主动"
                "关闭操作）。平台已重新打开本 Issue，开发结果请人工验证后"
                "手动关闭。")
        try:
            self._call_with_fallback(
                repo, lambda c: c.add_comment(project_id, issue_iid, body))
        except Exception as e:  # noqa: BLE001 同上：补评论失败不阻塞收尾
            self.db.add_log(task["id"], "warn",
                            f"autoclose 补充说明评论失败: {e}")
        self.db.add_log(task["id"], "warn",
                        "检测到 issue 被 GitLab autoclose 自动关闭，"
                        "已重新打开并补说明评论")

    def _success_summary(self, output: str) -> str:
        """从执行输出提取结果摘要（claude 的 result / hermes 的 final_response）。

        供成功收尾评论使用（issue #79）：两引擎字段都没有 / 为空时返回
        空串（评论省略摘要段）；超长按 COMMENT_TAIL_CHARS 截断。
        """
        data = self._last_json_object(output)
        if not isinstance(data, dict):
            return ""
        summary = data.get("result")
        if not isinstance(summary, str):
            summary = data.get("final_response")
        if not isinstance(summary, str):
            return ""
        summary = summary.strip()
        if not summary:
            return ""
        if len(summary) > COMMENT_TAIL_CHARS:
            summary = summary[:COMMENT_TAIL_CHARS] + "…"
        return summary

    def _record_commit(self, task_id: int,
                       repo: dict | None = None) -> None:
        """任务成功时查询对应提交并落库（issue #19：任务页面 commit 链接）。

        Claude 按模板提交（message 含 "issue #N"）并关闭 issue 后，用
        GitLab commits API 匹配该提交，完整 sha 落库供前端拼链接。
        查询失败/找不到不阻塞任务成功（页面不显示链接即可）。
        """
        task = self.db.get_task(task_id)
        if task is None:
            return
        try:
            sha, _ = self._call_with_fallback(
                repo, lambda c: c.find_commit_for_issue(
                    task["project_id"], task["issue_iid"]))
        except GitLabError as e:
            self.db.add_log(task_id, "warn", f"查询任务提交失败: {e}")
            return
        if sha:
            self.db.set_task_status(task_id, None, commit_sha=sha)
            self.db.add_log(task_id, "info", f"已记录任务提交 {sha[:8]}")

    def _finish_failed(self, task_id: int, reason: str, output: str = "",
                       error_detail: str | None = None,
                       repo: dict | None = None) -> None:
        task = self.db.get_task(task_id)
        # issue #274：任务收尾时对失败原因做规则分类（env/engine/unsolvable/
        # unknown），结果落库 tasks.failure_category——详情页展示分类徽章+建议、
        # 失败评论带分类前缀、统计看板按分类聚合。综合失败原因、错误详情与
        # 执行输出三路文本匹配，未命中兜底 unknown（不抛错）。
        category = classify_failure(
            reason, error_detail or "", output,
            rules=self.config.get().failure_classify_rules)
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message=reason,
                error_detail=error_detail,
                failure_category=category,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 失败收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(
            task_id, "error",
            f"任务失败: {reason}（失败分类：{category_label(category)}）")
        self._write_log_tail(task_id, output)

        # 在 issue 上留失败评论 + 打标签
        if task:
            # issue #252：失败任务输出结构化失败报告（失败原因 + 相关文件 +
            # 测试摘要 + 日志尾部）；无改动/无测试数据时对应段落自动隐藏
            tail = self._tail_output(output)
            log_tail = ""
            if tail and tail != output.strip():
                log_tail = f"```\n{tail[-COMMENT_TAIL_CHARS:]}\n```"
            body = self._build_report_comment(
                task, output, repo=repo, failed=True,
                reason=reason, log_tail=log_tail)
            # issue #274：失败评论同步带分类前缀（分类徽章 + 处理建议），
            # 无论默认/自定义模版都保证用户第一眼看到失败分类；兜底
            # unknown 同样带「未知」徽章，不抛错
            prefix = (f"> **失败分类：{category_label(category)}（{category}）**"
                      f" — {category_advice(category)}\n\n")
            body = prefix + body
            if not body.strip():
                # 兜底：渲染为空（极端场景）时保留最小可读文案
                body = f"🤖 Botler 自动回复：无法完成此 issue。\n\n**原因**：{reason}"
            # issue #280：GitLab 短暂不可用时评论/标签只试一次会因 502 发不出
            # （08-17 事故：失败评论与 bot-failed 标签全部 502 失败，issue 上
            # 「没有任何回复评论」）。瞬时故障退避重试，恢复后仍能送达。
            try:
                self._transient_retry(
                    "留失败评论",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_comment(
                            task["project_id"], task["issue_iid"], body)))
                self.db.add_log(task_id, "info", "已在 issue 上留失败评论")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留失败评论失败: {e}")
            try:
                self._transient_retry(
                    "打 bot-failed 标签",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_labels(
                            task["project_id"], task["issue_iid"], ["bot-failed"],
                            remove=["in-progress"])))
                self.db.add_log(task_id, "info", "已打 bot-failed 标签")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"打 bot-failed 标签失败: {e}")
            # 网页通知：任务需要人工介入（issue #21）
            self._emit_task_event(task_id, "task_failed", reason)
        logger.warning("任务 %s 失败: %s", task_id, reason)

    def _finish_asked(self, task_id: int, output: str,
                      repo: dict | None = None) -> None:
        """「等待用户决策」收尾（issue #67）：Claude 的提问反馈到 issue，任务判 failed。

        无人值守下 Claude 停在需要用户决策的提问节点后自行退出，任务实际
        未完成（无提交、无 CI）——不能判 succeeded 打 bot-done，也不能按
        普通失败重试（重试仍会停在同一个问题）。把提问原文贴到 issue 评论，
        打 blocked 标签（不在领取过滤标签中）：用户回复后经重新指派或
        对账扫描再次入队，新任务可读到回复后继续处理。
        """
        task = self.db.get_task(task_id)
        # issue #274：等待用户决策按「无法解决类（unsolvable）」分类落库
        # （agent 停在提问节点、无法独立继续，处理建议引导用户回复/改描述）
        category = classify_failure(
            "Claude 在执行中遇到需要用户决策的问题，等待用户回复",
            rules=self.config.get().failure_classify_rules)
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message="Claude 在执行中遇到需要用户决策的问题，"
                              "提问已反馈至 issue，等待用户回复后重新处理",
                failure_category=category,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 提问收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "error",
                        "任务未完成：Claude 停在等待用户决策的提问节点，问题已反馈到 issue")
        self._write_log_tail(task_id, output)

        if task:
            question = self._extract_question(output)
            comment = (
                f"> **失败分类：{category_label(category)}（{category}）**"
                f" — {category_advice(category)}\n\n"
                "🤖 Botler 自动回复：Claude 在执行中遇到需要您决策的问题，"
                "暂时无法继续，请回复后重新处理。\n\n"
                "**Claude 的问题**：\n\n"
                f"{question}\n\n"
                "请在本 issue 直接回复您的选择，回复后 bot 会重新领取处理。")
            # issue #280：瞬时故障退避重试，保证用户能收到提问反馈
            try:
                self._transient_retry(
                    "留提问评论",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_comment(
                            task["project_id"], task["issue_iid"], comment)))
                self.db.add_log(task_id, "info", "已在 issue 上留提问评论，等待用户回复")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留提问评论失败: {e}")
            try:
                self._transient_retry(
                    "打 blocked 标签",
                    lambda: self._call_with_fallback(
                        repo, lambda c: c.add_labels(
                            task["project_id"], task["issue_iid"], ["blocked"],
                            remove=["in-progress"])))
                self.db.add_log(task_id, "info", "已打 blocked 标签，等待用户回复")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"打 blocked 标签失败: {e}")
            # 网页通知：任务需要用户决策（issue #21 渠道复用失败通知）
            self._emit_task_event(task_id, "task_failed",
                                  "Claude 等待用户决策，问题已反馈到 issue")
        logger.warning("任务 %s 等待用户决策，问题已反馈到 issue", task_id)

    def _write_log_tail(self, task_id: int, output: str) -> None:
        tail = self._tail_output(output)
        if not tail:
            return
        try:
            with open(self._log_file(task_id), "a", encoding="utf-8", errors="replace") as f:
                f.write("\n----- 执行结束（摘要）-----\n" + tail + "\n")
        except OSError:
            pass
