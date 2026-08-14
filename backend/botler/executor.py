"""Claude Code 执行器。

流程（设计方案 §5.5）：
1. 准备干净工作区（fetch / checkout main / reset --hard / clean -fd）
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

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ConfigManager
from .database import (
    Database, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED,
    STATUS_INTERRUPTED,
)
from .events import EventBus, parse_claude_stream_line, parse_hermes_event_line
from .gitlab_client import PIPELINE_TERMINAL_STATES, GitLabClient, GitLabError
from .git_remote import build_repo_client_with_username
from .templates import TemplateRenderer

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

# 恢复执行引导语（issue #8）：中断恢复时不用完整模版重发 issue 描述，
# 而是让 Claude 检查工作区现状后从断点继续（避免重复分析与重复评论）
RESUME_PROMPT = """【继续处理（中断恢复）】你正在处理 {repo_name} 仓库的 issue #{issue_iid}「{issue_title}」：{issue_url}

上次处理因平台重新部署而中断，你的对话与工作区改动已保留。请先检查当前状态
（git status / git log / 未提交改动），弄清上次做到哪一步，然后从断点继续：
完成剩余的修复/实现 → 自测 → 推送 → 用 GitLab API 关闭 issue。
不要从零重新分析 issue（除非确认上次未开始实质工作），不要重复已经完成的工作。"""


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


def parse_transcript(session_file: Path, max_messages: int = _TRANSCRIPT_MAX_MESSAGES,
                     max_text_chars: int = _TRANSCRIPT_MAX_TEXT) -> tuple[list[dict], bool]:
    """解析 claude 会话 jsonl 为结构化聊天消息（issue #20 实时查看）。

    只保留 user / assistant 两类行（跳过 system / result 与杂讯），
    拆分为四类消息：
      {"role": "user", "text", "ts", "truncated"}
      {"role": "assistant", "text", "ts", "truncated"}
      {"role": "tool", "tool", "input", "ts"}                （工具调用）
      {"role": "tool_result", "tool_use_id", "text", "tool_error", "ts", "truncated"}
    返回 (messages, truncated)：消息过多时保留最后 max_messages 条并置
    truncated=True；文件不存在 / 无有效行返回空列表。
    """
    if session_file is None or not session_file.is_file():
        return [], False
    try:
        lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], False
    if len(lines) > max_messages:
        lines = lines[-max_messages:]
        truncated = True
    else:
        truncated = False

    messages: list[dict] = []
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
                    text, cut = _truncate_text(part.get("text", ""), max_text_chars)
                    if text:
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
            text, cut = _truncate_text(content, max_text_chars)
            if text:
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
        # 一键停止（issue #35）：运行中进程注册表 + 停止请求集合。
        # task_id 自增唯一，集合只增不减（进程退出注销的是注册表不是集合）。
        self._procs: dict[int, subprocess.Popen] = {}
        self._stop_requests: set[int] = set()
        self._proc_lock = threading.Lock()

    # ---- GitLab 调用兜底 ----

    def _call_with_fallback(self, repo, call):
        """用全局 client 执行 call(client)；遇 401/403（全局 token 失效）
        时用仓库 remote url 内嵌 token 构建 per-repo client 重试一次。

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

        # 每次执行前重置到远端 main，从根上消除脏状态。
        # remote_name 记录本地方式添加时用户选中的 remote（老数据缺省为 origin）
        remote = _row_get(repo, "remote_name") or "origin"
        self._git(workdir, "fetch", remote, "--prune", env=git_env)
        if not resume:
            # checkout 实际所在的分支（main → master），reset 跟随同一分支。
            # 不依赖 {remote}/HEAD 符号引用：手工加 remote 的仓库无 origin/HEAD，
            # reset --hard origin/HEAD 必现 ambiguous argument（issue #12）
            branch = "main"
            try:
                self._git(workdir, "checkout", "main", env=git_env)
            except ExecutorError:
                logger.warning("%s: 无 main 分支，尝试 checkout master", repo["name"])
                branch = "master"
                self._git(workdir, "checkout", "master", env=git_env)
            self._git(workdir, "reset", "--hard", f"{remote}/{branch}", env=git_env)
            self._git(workdir, "clean", "-fd", env=git_env)
        # askpass 脚本保留不删除（issue #12）：并发任务/重试时序下脚本被删 →
        # fetch 回退 credential helper 旧凭据 → HTTP Basic: Access denied。
        # 脚本内容每次 prepare 覆盖刷新（token 轮换自动生效），权限 0700，
        # 且在工作区父目录，不受 clean -fd 波及。
        return workdir, git_env

    # ---- 提示词与环境 ----

    def _build_prompt(self, repo: dict, issue: dict) -> str:
        template = self.renderer.resolve_template(repo)
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        return self.renderer.render(template, variables)

    def _build_env(self, repo: dict, issue: dict) -> dict:
        cfg = self.config.get()
        env = self._clean_process_env()
        env["GITLAB_TOKEN"] = cfg.gitlab_token
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

    def _resume_prompt(self, repo: dict, issue: dict) -> str:
        """恢复执行引导语：基于上次会话继续，不重复已完成的工作。"""
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        return self.renderer.render(RESUME_PROMPT, variables)

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

    def _engine(self, cfg) -> str:
        """任务执行引擎（issue #47）：claude（默认）/ hermes；非法值回退 claude。"""
        engine = str(getattr(cfg, "engine", "") or "claude").strip().lower()
        return engine if engine in ("claude", "hermes") else "claude"

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

    def _run_once(self, task_id: int, repo: dict, issue: dict,
                  resume_session: str | None = None,
                  resume_history: list | None = None) -> tuple[int, str]:
        """执行一次任务引擎（claude -p 或 hermes runner）。返回 (exit_code, output)。

        claude 引擎：resume_session 非空时为断点续跑（claude --resume 接续
        上次会话，工作区保留）；执行后解析 JSON 输出中的 session_id 落库。
        hermes 引擎（issue #47）：resume_history 为断点续跑的历史消息
        （工作区保留），显式传入优先；未传入时从任务落库的 hermes_history
        解析（含会话 id），等价 Q3-B conversation_history 落库断点续跑。
        """
        cfg = self.config.get()
        if self._engine(cfg) == "hermes":
            task_row = self.db.get_task(task_id)
            messages, sid = self._hermes_resume_data(
                _row_get(task_row, "hermes_history") if task_row is not None else None)
            if resume_history is not None:
                messages = resume_history
            return self._run_hermes_once(task_id, repo, issue, messages, sid)

        workdir, git_env = self.prepare_workspace(repo, resume=bool(resume_session))
        if resume_session:
            prompt = self._resume_prompt(repo, issue)
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
                return STOP_EXIT_CODE, output

            if timed_out:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止进程组")
                self._persist_session_id(task_id, output)
                return 124, output  # 124 = timeout 约定退出码

            exit_code = proc.wait(timeout=30)
            output = "".join(chunks)
            self.db.add_log(task_id, "info", f"claude 退出码: {exit_code}")
            self._persist_session_id(task_id, output)
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
        """执行一次 hermes runner。返回 (exit_code, output)。

        runner 协议：stdin 写 JSON 请求（prompt/history/session_id），
        stdout 输出单行 JSON 结果（final_response/messages/session_id/error）。
        terminal 工具经 TERMINAL_CWD 在 botler 仓库工作区执行，git 凭据
        继承 _build_env 的 GIT_ASKPASS 注入（与 claude 引擎一致）。
        """
        cfg = self.config.get()
        if not cfg.hermes_command:
            raise ExecutorError(
                "hermes 引擎未配置 hermes.command（部署机 hermes venv 的 python 路径）")
        workdir, _git_env = self.prepare_workspace(repo, resume=bool(resume_history))
        if resume_history:
            prompt = self._resume_prompt(repo, issue)
            self.db.add_log(
                task_id, "info",
                f"恢复上次 hermes 会话（{len(resume_history)} 条历史）… 继续执行"
                f"（工作区保留，超时 {cfg.task_timeout_seconds}s）")
        else:
            prompt = self._build_prompt(repo, issue)
            self.db.add_log(task_id, "info",
                            f"执行 hermes runner（工作区 {workdir}，超时 {cfg.task_timeout_seconds}s）")
        env = self._build_env(repo, issue)
        # hermes 的 terminal 工具按 TERMINAL_CWD 在 botler 仓库工作区执行命令
        env["TERMINAL_CWD"] = str(workdir)

        log_path = self._log_file(task_id)
        cmd = [cfg.hermes_command, *cfg.hermes_args]
        request: dict = {"prompt": prompt}
        if resume_history:
            request["history"] = resume_history
            if resume_session_id:
                request["session_id"] = resume_session_id
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
        except FileNotFoundError:
            raise ExecutorError(
                f"找不到 hermes 命令: {cfg.hermes_command}"
                f"（请确认部署机 hermes venv 的 python 路径）")

        with self._proc_lock:
            self._procs[task_id] = proc
        try:
            try:
                proc.stdin.write(json.dumps(request, ensure_ascii=False))
                proc.stdin.close()
            except (BrokenPipeError, OSError) as e:
                self.db.add_log(task_id, "warn", f"hermes runner stdin 写入失败: {e}")

            deadline = time.time() + cfg.task_timeout_seconds
            stopped = self._stop_requested(task_id)
            # 实时事件流（SSE）：runner 流式协议的事件行发布到总线；
            # 结果行（parse 返回 None）不发布，由收尾判定
            def _on_chunk(chunk: str) -> None:
                self._publish_stream_line(task_id, chunk, parse_hermes_event_line)

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
                                "任务被用户停止，已强制终止 hermes 进程组")
                return STOP_EXIT_CODE, output

            if timed_out:
                self._kill_process_group(proc)
                proc.wait(timeout=10)
                output = "".join(chunks)
                self.db.add_log(task_id, "error",
                                f"任务超时（>{cfg.task_timeout_seconds}s），已强制终止进程组")
                return 124, output  # 124 = timeout 约定退出码

            exit_code = proc.wait(timeout=30)
            output = "".join(chunks)
            self.db.add_log(task_id, "info", f"hermes runner 退出码: {exit_code}")
            return exit_code, output
        finally:
            with self._proc_lock:
                self._procs.pop(task_id, None)

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

        project_id, issue_iid = task["project_id"], task["issue_iid"]
        self.db.set_task_status(task_id, None, log_path=str(self._log_file(task_id)))
        try:
            issue, _ = self._call_with_fallback(
                repo, lambda c: c.get_issue(project_id, issue_iid))
        except GitLabError as e:
            self._finish_failed(task_id,
                                f"获取 issue {project_id}#{issue_iid} 失败: {e}",
                                repo=repo)
            return

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
            # 由 _run_once 内部读取（session 文件机制仅 claude 有）。
            task = self.db.get_task(task_id)
            resume_session = None
            if engine != "hermes":
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

            # 首次尝试时在 issue 上回复「处理中」，提升体验（不刷屏，重试不再重复）
            if attempt == 1:
                try:
                    self._call_with_fallback(
                        repo, lambda c: c.add_comment(
                            project_id, issue_iid,
                            "🤖 Botler 已收到该 issue，开始处理中…"))
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
                if engine == "hermes":
                    # issue #47 hermes 引擎：成功判定看 runner 输出 JSON
                    # （final_response + error 为空），非 JSON / error 非空
                    # 落入下方重试分支（与 claude exit 0 无 JSON 一致）
                    result = self._hermes_result(output)
                    if result == "unresolvable":
                        detail = {"attempt": attempt, "exit_code": exit_code,
                                  "error": self._extract_error(output)}
                        self._finish_failed(task_id, "hermes 报告无法解决该 issue", output,
                                            error_detail=self._dump_error_detail(
                                                [*attempt_details, detail], last_exit),
                                            repo=repo)
                        return
                    if result == "success":
                        # Q3-B：会话数据落库（断点续跑），收尾流程与 claude 引擎一致
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

    def _emit_task_event(self, task_id: int, event: str, reason: str = "") -> None:
        """任务收尾产生网页通知事件（issue #21）。查库失败不阻塞收尾。"""
        try:
            task = self.db.get_task(task_id)
            if task is None:
                return
            repo = self.db.get_repo(task["repo_id"])
            repo_name = repo["name"] if repo else None
            if event == "task_succeeded":
                self.notifier.task_succeeded(dict(task), repo_name)
            elif event == "task_failed":
                self.notifier.task_failed(dict(task), reason, repo_name)
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
        # issue #34：成功时由平台代码直接打 bot-done 标签（幂等），不再依赖
        # Claude 按模板打——Claude 忘打会导致 issue 无终态标签被重复领取。
        # issue #67：同步移除 in-progress（Claude 领取时打的处理中标签），
        # 避免收尾后与终态标签并存。
        # 打标签失败不阻塞任务成功（仅记 warn，用户可手动补标签）。
        task = self.db.get_task(task_id)
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
        # 网页通知：issue 完成（issue #21）
        self._emit_task_event(task_id, "task_succeeded")
        logger.info("任务 %s 成功", task_id)

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
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message=reason,
                error_detail=error_detail,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 失败收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "error", f"任务失败: {reason}")
        self._write_log_tail(task_id, output)

        # 在 issue 上留失败评论 + 打标签
        if task:
            summary = reason
            tail = self._tail_output(output)
            if tail and tail != output.strip():
                summary += f"\n\n日志尾部：\n```\n{tail[-COMMENT_TAIL_CHARS:]}\n```"
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_comment(
                        task["project_id"], task["issue_iid"],
                        f"🤖 Botler 自动回复：无法完成此 issue。\n\n**原因**：{summary}"))
                self.db.add_log(task_id, "info", "已在 issue 上留失败评论")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留失败评论失败: {e}")
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_labels(
                        task["project_id"], task["issue_iid"], ["bot-failed"],
                        remove=["in-progress"]))
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
        # 条件终态（issue #24）：任务已被其他实例先收尾时不再覆盖状态、
        # 不重复评论/通知
        if not self.db.finish_task(
                task_id, STATUS_FAILED,
                exit_code=None,
                error_message="Claude 在执行中遇到需要用户决策的问题，"
                              "提问已反馈至 issue，等待用户回复后重新处理",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
            logger.info("任务 %s 提问收尾被跳过（状态已非运行中，可能已被其他实例收尾）", task_id)
            return
        self.db.add_log(task_id, "error",
                        "任务未完成：Claude 停在等待用户决策的提问节点，问题已反馈到 issue")
        self._write_log_tail(task_id, output)

        if task:
            question = self._extract_question(output)
            comment = (
                "🤖 Botler 自动回复：Claude 在执行中遇到需要您决策的问题，"
                "暂时无法继续，请回复后重新处理。\n\n"
                "**Claude 的问题**：\n\n"
                f"{question}\n\n"
                "请在本 issue 直接回复您的选择，回复后 bot 会重新领取处理。")
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_comment(
                        task["project_id"], task["issue_iid"], comment))
                self.db.add_log(task_id, "info", "已在 issue 上留提问评论，等待用户回复")
            except GitLabError as e:
                self.db.add_log(task_id, "error", f"留提问评论失败: {e}")
            try:
                self._call_with_fallback(
                    repo, lambda c: c.add_labels(
                        task["project_id"], task["issue_iid"], ["blocked"],
                        remove=["in-progress"]))
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
