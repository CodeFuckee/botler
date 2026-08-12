"""实时查看任务执行测试（issue #20）：运行中落库 session_id + 聊天记录解析。

需求：任务页面「查看任务执行」按钮，实时查看 agent 的进度（claude 日志流）
与聊天记录（claude 会话 transcript）。
- 运行中：_run_once 读循环首次解析到 session_id 即落库（此前只有执行结束后
  才落库，运行中 API 拿不到当前会话）
- parse_transcript：把 claude session jsonl 解析为结构化聊天消息
- read_log_delta：按字节偏移读日志增量（半行回退，避免撕裂行）
"""

import json
import threading
import time
from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import (
    ClaudeExecutor, find_session_file, parse_transcript, read_log_delta,
)
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""


@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


# ---- 工具函数：session jsonl 行构造 ----

def _user_text(text: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": "2026-08-12T10:00:00Z"},
        "timestamp": "2026-08-12T10:00:00Z",
    }, ensure_ascii=False)


def _user_text_str_content(text: str) -> str:
    """部分 claude 版本的 user 消息 content 直接是字符串。"""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text,
                    "timestamp": "2026-08-12T10:00:01Z"},
        "timestamp": "2026-08-12T10:00:01Z",
    }, ensure_ascii=False)


def _assistant_text(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": "2026-08-12T10:00:02Z"},
        "timestamp": "2026-08-12T10:00:02Z",
    }, ensure_ascii=False)


def _assistant_tool(name: str, tool_input: dict, tool_id: str = "toolu_1") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": tool_id,
                                 "name": name, "input": tool_input}],
                    "timestamp": "2026-08-12T10:00:03Z"},
        "timestamp": "2026-08-12T10:00:03Z",
    }, ensure_ascii=False)


def _tool_result(tool_id: str, content, is_error: bool = False) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                 "content": content, "is_error": is_error}],
                    "timestamp": "2026-08-12T10:00:04Z"},
        "timestamp": "2026-08-12T10:00:04Z",
    }, ensure_ascii=False)


def _write_session(tmp_path: Path, lines: list[str]) -> Path:
    f = tmp_path / "sessions" / "sid-1.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


class TestParseTranscript:
    """parse_transcript：session jsonl → 结构化聊天消息。"""

    def test_parse_basic_messages(self, tmp_path):
        f = _write_session(tmp_path, [
            _user_text("请修复 bug"),
            _assistant_text("我来分析问题"),
            _user_text_str_content("好的"),
        ])
        msgs, truncated = parse_transcript(f)
        assert truncated is False
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert msgs[0]["text"] == "请修复 bug"
        assert msgs[1]["text"] == "我来分析问题"

    def test_parse_tool_use_and_result(self, tmp_path):
        f = _write_session(tmp_path, [
            _assistant_tool("Bash", {"command": "git status"}, "toolu_1"),
            _tool_result("toolu_1", "modified: a.py", is_error=False),
            _tool_result("toolu_2", "命令失败", is_error=True),
        ])
        msgs, truncated = parse_transcript(f)
        assert msgs[0]["role"] == "tool"
        assert msgs[0]["tool"] == "Bash"
        assert msgs[0]["input"] == {"command": "git status"}
        assert msgs[1]["role"] == "tool_result"
        assert msgs[1]["tool_use_id"] == "toolu_1"
        assert msgs[1]["text"] == "modified: a.py"
        assert msgs[1]["tool_error"] is False
        assert msgs[2]["tool_error"] is True

    def test_skip_system_result_and_garbage_lines(self, tmp_path):
        f = _write_session(tmp_path, [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "subtype": "success",
                        "session_id": "sid-1", "result": "done", "exit_code": 0}),
            "not-a-json-line",
            _user_text("hello"),
        ])
        msgs, _ = parse_transcript(f)
        assert len(msgs) == 1
        assert msgs[0]["text"] == "hello"

    def test_empty_and_missing_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        msgs, truncated = parse_transcript(f)
        assert msgs == [] and truncated is False
        msgs, truncated = parse_transcript(tmp_path / "nope.jsonl")
        assert msgs == [] and truncated is False

    def test_content_missing_or_not_list(self, tmp_path):
        f = _write_session(tmp_path, [
            json.dumps({"type": "user", "message": {"role": "user"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                         "content": []}}),
            json.dumps({"type": "user", "message": {"role": "user",
                                                    "content": 123}}),
        ])
        msgs, _ = parse_transcript(f)
        assert msgs == []

    def test_max_messages_truncation(self, tmp_path):
        lines = [_user_text(f"msg-{i}") for i in range(120)]
        f = _write_session(tmp_path, lines)
        msgs, truncated = parse_transcript(f, max_messages=50)
        assert truncated is True
        assert len(msgs) == 50
        assert msgs[0]["text"] == "msg-70"  # 保留最后 50 条
        assert msgs[-1]["text"] == "msg-119"

    def test_long_text_truncated(self, tmp_path):
        long_text = "x" * 10000
        f = _write_session(tmp_path, [_user_text(long_text)])
        msgs, _ = parse_transcript(f)
        assert len(msgs[0]["text"]) == 5000
        assert msgs[0]["truncated"] is True


class TestReadLogDelta:
    """read_log_delta：字节偏移增量读取，半行回退。"""

    def test_delta_after_offset(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        lines, offset = read_log_delta(f, 0)
        assert lines == ["line1", "line2", "line3"]
        assert offset == f.stat().st_size
        lines, offset2 = read_log_delta(f, offset)
        assert lines == [] and offset2 == offset

    def test_mid_line_offset(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("line1\nline2\n", encoding="utf-8")
        # 从 line2 中间开始（offset = len("line1\nli")）→ 自动对齐到行首
        mid = len("line1\nli".encode("utf-8"))
        lines, offset = read_log_delta(f, mid)
        assert lines == ["line2"]
        assert offset == f.stat().st_size

    def test_half_line_rewind(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("line1\nline2", encoding="utf-8")  # 无结尾换行：line2 是半行
        lines, offset = read_log_delta(f, 0)
        assert lines == ["line1"]  # 半行留给下一轮
        assert offset == len("line1\n".encode("utf-8"))
        # 补完半行后从回退处继续（追加，勿覆盖）；仍无换行结尾时继续回退
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("3")
        lines, offset = read_log_delta(f, offset)
        assert lines == [] and offset == len("line1\n".encode("utf-8"))
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\n")  # line2 → line23 完整行
        lines, offset = read_log_delta(f, offset)
        assert lines == ["line23"]
        assert offset == f.stat().st_size

    def test_chinese_lines(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("中文行1\n中文行2\n", encoding="utf-8")
        lines, _ = read_log_delta(f, 0)
        assert lines == ["中文行1", "中文行2"]

    def test_missing_file(self, tmp_path):
        lines, offset = read_log_delta(tmp_path / "nope.log", 10)
        assert lines == [] and offset == 10

    def test_offset_beyond_size(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("x\n", encoding="utf-8")
        lines, offset = read_log_delta(f, 9999)
        assert lines == [] and offset == f.stat().st_size


class TestFindSessionFile:
    """find_session_file：按 session_id 找 ~/.claude/projects/*/<sid>.jsonl。"""

    def test_find_in_projects(self, tmp_path, monkeypatch):
        proj = tmp_path / ".claude" / "projects" / "proj-a"
        proj.mkdir(parents=True)
        (proj / "sid-9.jsonl").write_text("{}", encoding="utf-8")
        (proj / "other.jsonl").write_text("{}", encoding="utf-8")
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        found = find_session_file("sid-9")
        assert found is not None
        assert found.name == "sid-9.jsonl"

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        assert find_session_file("no-such") is None


class TestPersistSessionDuringRun:
    """运行中首次出现 session_id 即落库（实时查看会话的前置条件）。"""

    def _mk_task(self, db, tmp_path) -> int:
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo = db.get_repo_by_project_id(42)
        return db.create_task(repo["id"], 42, 7, "demo issue", triggered_by="webhook")

    def test_persist_from_chunk(self, executor, tmp_path):
        task_id = self._mk_task(executor.db, tmp_path)
        # 带 session_id 的 JSON 行 → 立即落库并返回 True
        line = json.dumps({"type": "result", "session_id": "live-sid-1", "result": "x"})
        assert executor._persist_session_from_chunk(task_id, line) is True
        assert executor.db.get_task(task_id)["claude_session_id"] == "live-sid-1"

    def test_skip_non_json_chunk(self, executor, tmp_path):
        task_id = self._mk_task(executor.db, tmp_path)
        assert executor._persist_session_from_chunk(task_id, "Warning: no stdin") is False
        assert executor.db.get_task(task_id)["claude_session_id"] is None

    def test_skip_json_without_session_id(self, executor, tmp_path):
        task_id = self._mk_task(executor.db, tmp_path)
        line = json.dumps({"type": "user", "message": {"role": "user"}})
        assert executor._persist_session_from_chunk(task_id, line) is False
        assert executor.db.get_task(task_id)["claude_session_id"] is None

    def test_session_id_visible_while_process_running(self, executor, monkeypatch, tmp_path):
        """进程未结束时（读循环仍在等输出），DB 中 session_id 已可读。"""
        import botler.executor as executor_mod

        task_id = self._mk_task(executor.db, tmp_path)
        repo = executor.db.get_repo_by_project_id(42)
        issue = {"project_id": 42, "iid": 7}
        release = threading.Event()
        sid_line = json.dumps(
            {"type": "result", "session_id": "live-sid-2", "result": "running"})

        class _BlockingStdout:
            def __init__(self):
                self._sent = False

            def readline(self):
                if not self._sent:
                    self._sent = True
                    return sid_line
                release.wait(timeout=10)  # 阻塞模拟进程仍在运行
                return ""

        class _FakeProc:
            stdout = _BlockingStdout()
            _exit = 0

            def poll(self):
                return None if not release.is_set() else self._exit

            def wait(self, timeout=None):
                return self._exit

        monkeypatch.setattr(executor_mod.subprocess, "Popen",
                            lambda *a, **k: _FakeProc())
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo_, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_build_env", lambda repo_, issue_: {})

        t = threading.Thread(target=executor._run_once,
                             args=(task_id, dict(repo), issue))
        t.start()
        try:
            # 轮询等待运行中落库（最多 3s）
            deadline = time.time() + 3
            sid = None
            while time.time() < deadline:
                sid = executor.db.get_task(task_id)["claude_session_id"]
                if sid:
                    break
                time.sleep(0.05)
            assert sid == "live-sid-2"  # 进程仍阻塞（未结束）时已可读
        finally:
            release.set()
            t.join(timeout=10)
        assert not t.is_alive()
