"""会话文件查找 / transcript 解析 / 日志增量读取（issue #192 拆分）。

从原 executor.py 拆出的会话相关职责：claude 会话 jsonl 查找与解析、
日志文件增量读取、[PROGRESS] 进度账本标记解析。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import _load_json_output

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


class SessionMixin:
    """会话断点续跑与进度账本（依赖 ClaudeExecutor 实例状态，见 executor/__init__.py）。"""

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
