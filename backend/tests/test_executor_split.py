"""executor 包拆分（issue #192）后新模块的单元测试。

覆盖拆分出的四个职责模块的纯函数与可独立调用的 mixin 方法：
- executor/workspace.py：git 工作区工具（_force_remove / _untracked_paths /
  _is_pull_conflict / _conflict_handoff_instructions）
- executor/process.py：引擎输出解析与结果判定（_last_json_object /
  _result_text / _extract_error / _is_unresolvable / _output_ends_with_question /
  _extract_question / _hermes_result / _dsh_result / _dsh_collision）
- executor/session.py：会话文件解析（find_session_file / read_session_prompt /
  parse_transcript / read_log_delta / _truncate_text）
- executor/prompt.py：脱敏与转义解码（_strip_credential_sections /
  _decode_escapes / format_display_line）
"""

import json
import subprocess
from pathlib import Path

import pytest

from botler.executor import (
    format_display_line, find_session_file, parse_transcript, read_log_delta,
    read_session_prompt,
)
from botler.executor.common import ExecutorError
from botler.executor.process import ProcessMixin
from botler.executor.prompt import _decode_escapes, _strip_credential_sections
from botler.executor.session import (
    _first_user_line_index, _truncate_text,
)
from botler.executor.workspace import WorkspaceMixin


# ---------------- prompt.py：脱敏与转义解码 ----------------

class TestStripCredentialSections:
    def test_removes_credential_section(self):
        text = "[user]\n\tname = bot\n[credential]\n\thelper = store\n[core]\n\tautocrlf = false\n"
        out = _strip_credential_sections(text)
        assert "[credential]" not in out
        assert "[user]" in out and "[core]" in out

    def test_removes_credential_subsection(self):
        text = '[credential "https://gitlab.example.com"]\n\tusername = oauth2\n[user]\n\tname = bot\n'
        out = _strip_credential_sections(text)
        assert "credential" not in out
        assert "[user]" in out

    def test_no_credential_returns_same(self):
        text = "[user]\n\tname = bot\n"
        # 原实现按行 join，尾部换行被去除（拆分前行为一致）
        assert _strip_credential_sections(text) == text.rstrip("\n")


class TestDecodeEscapes:
    def test_nested_json_decode(self):
        text = json.dumps(json.dumps({"a": 1}))  # 双重序列化
        out = _decode_escapes(text)
        assert "a" in out and "1" in out

    def test_plain_text_passthrough(self):
        assert _decode_escapes("普通文本") == "普通文本"


class TestFormatDisplayLine:
    def test_json_line_reformat(self):
        line = json.dumps({"type": "result", "session_id": "s1",
                           "result": "hello\\nworld", "uuid": "noise"})
        out = format_display_line(line)
        assert "type: \"result\"" in out
        assert "session_id: \"s1\"" in out
        assert "result:" in out and "hello" in out
        assert "uuid" not in out  # 机器噪音字段被丢弃

    def test_non_json_passthrough(self):
        assert format_display_line("普通输出行") == "普通输出行"


# ---------------- session.py：会话文件解析 ----------------

def _write_session_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8")


class TestFindSessionFile:
    def test_found(self, tmp_path):
        claude_home = tmp_path / ".claude"
        sid = "abc123"
        f = claude_home / "projects" / "demo" / f"{sid}.jsonl"
        _write_session_jsonl(f, [])
        assert find_session_file(sid, claude_home) == f

    def test_missing(self, tmp_path):
        assert find_session_file("nope", tmp_path / ".claude") is None


class TestReadSessionPrompt:
    def test_reads_first_user_message(self, tmp_path):
        f = tmp_path / "s.jsonl"
        _write_session_jsonl(f, [
            {"type": "system", "message": {"role": "system", "content": "x"}},
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "text", "text": "完整提示词"}]}},
        ])
        assert read_session_prompt(f) == "完整提示词"

    def test_missing_file(self, tmp_path):
        assert read_session_prompt(tmp_path / "nope.jsonl") is None


class TestParseTranscript:
    def _session(self, tmp_path):
        return tmp_path / "s.jsonl"

    def test_basic_parse(self, tmp_path):
        f = self._session(tmp_path)
        _write_session_jsonl(f, [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
            ]}},
        ])
        messages, truncated = parse_transcript(f)
        assert truncated is False
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "tool"]
        assert messages[0]["text"] == "hi"
        assert messages[2]["tool"] == "bash"

    def test_truncation_keeps_first_user(self, tmp_path):
        f = self._session(tmp_path)
        records = [{"type": "user", "message": {"role": "user", "content": "提示词"}}]
        for i in range(50):
            records.append({"type": "user",
                            "message": {"role": "user", "content": f"m{i}"}})
        _write_session_jsonl(f, records)
        messages, truncated = parse_transcript(f, max_messages=10)
        assert truncated is True
        assert len(messages) <= 10
        assert messages[0]["text"] == "提示词"  # 首条 user 消息（提示词）保留

    def test_missing_file(self, tmp_path):
        assert parse_transcript(tmp_path / "nope.jsonl") == ([], False)


class TestReadLogDelta:
    def test_incremental_read(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("line1\nline2\n", encoding="utf-8")
        lines, offset = read_log_delta(f, 0)
        assert lines == ["line1", "line2"]
        with open(f, "a", encoding="utf-8") as fh:  # 追加而非覆盖
            fh.write("line3\n")
        lines2, offset2 = read_log_delta(f, offset)
        assert lines2 == ["line3"]
        assert offset2 > offset

    def test_missing_file(self, tmp_path):
        assert read_log_delta(tmp_path / "nope.log", 0) == ([], 0)

    def test_half_line_rollback(self, tmp_path):
        f = tmp_path / "task.log"
        f.write_text("line1\n", encoding="utf-8")
        lines, offset = read_log_delta(f, 0)
        # 写入半行：下一轮应回退到行首，不返回撕裂行
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("par")
        lines2, offset2 = read_log_delta(f, offset)
        assert lines2 == []
        assert offset2 == offset  # offset 回退


class TestTruncateText:
    def test_truncate(self):
        assert _truncate_text("12345", 3) == ("123", True)

    def test_no_truncate(self):
        assert _truncate_text("12", 3) == ("12", False)


class TestFirstUserLineIndex:
    def test_finds_first_user_line(self):
        lines = [
            json.dumps({"type": "system", "message": {"role": "system"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}),
        ]
        assert _first_user_line_index(lines) == 1

    def test_none_when_no_user(self):
        lines = [json.dumps({"type": "assistant", "message": {"role": "assistant"}})]
        assert _first_user_line_index(lines) is None


# ---------------- workspace.py：git 工作区工具 ----------------

@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    return repo


class TestUntrackedPaths:
    def test_lists_untracked(self, git_repo):
        (git_repo / "tracked.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "init"], check=True)
        (git_repo / "untracked.txt").write_text("y", encoding="utf-8")
        paths = WorkspaceMixin._untracked_paths(git_repo, {})
        assert paths == ["untracked.txt"]

    def test_empty_when_clean(self, git_repo):
        (git_repo / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "init"], check=True)
        assert WorkspaceMixin._untracked_paths(git_repo, {}) == []


class TestForceRemove:
    def test_removes_file(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        assert WorkspaceMixin._force_remove(f) is True
        assert not f.exists()

    def test_removes_dir(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "inner").write_text("x", encoding="utf-8")
        assert WorkspaceMixin._force_remove(d) is True
        assert not d.exists()


class TestIsPullConflict:
    def test_rebase_marker_detected(self, git_repo):
        (git_repo / ".git" / "rebase-merge").mkdir()
        exc = ExecutorError("git pull 失败")
        assert WorkspaceMixin()._is_pull_conflict(git_repo, {}, exc) is True

    def test_unrelated_error_not_conflict(self, git_repo):
        exc = ExecutorError("网络错误: could not resolve host")
        assert WorkspaceMixin()._is_pull_conflict(git_repo, {}, exc) is False


class TestConflictHandoffInstructions:
    def test_contains_merge_guidance(self):
        text = WorkspaceMixin._conflict_handoff_instructions()
        assert "先手工解决" in text
        assert "git rebase --continue" in text
        assert "push --force" in text


# ---------------- process.py：输出解析与结果判定 ----------------

class TestLastJsonObject:
    def test_extracts_first_json_with_prefix_noise(self):
        out = "Warning: no stdin data received...\n" + json.dumps({"a": 1}) + "\ntail"
        assert ProcessMixin()._last_json_object(out) == {"a": 1}

    def test_none_for_non_json(self):
        assert ProcessMixin()._last_json_object("无 JSON 内容") is None


class TestResultText:
    def test_claude_result(self):
        out = json.dumps({"type": "result", "result": "完成\\n工作"})
        assert ProcessMixin()._result_text(out) == "完成\n工作"

    def test_hermes_final_response(self):
        out = json.dumps({"final_response": "done"})
        assert ProcessMixin()._result_text(out) == "done"

    def test_non_json_passthrough(self):
        assert ProcessMixin()._result_text("raw text") == "raw text"


class TestExtractError:
    def test_traceback_extracted(self):
        out = json.dumps({"type": "result",
                          "result": "前置信息\\nTraceback (most recent call last):\\n  File x\\nError: boom"})
        err = ProcessMixin()._extract_error(out)
        assert err.startswith("Traceback (most recent call last)")
        assert "Error: boom" in err


class TestIsUnresolvable:
    @pytest.mark.parametrize("text", ["无法解决该问题", "cannot fix this", "out of scope"])
    def test_detects_unresolvable(self, text):
        assert ProcessMixin()._is_unresolvable(text) is True

    def test_normal_text_not_unresolvable(self):
        assert ProcessMixin()._is_unresolvable("已完成开发") is False


class TestDecisionQuestion:
    def test_ends_with_question(self):
        out = json.dumps({"type": "result", "result": "请选择 A 或 B？"})
        assert ProcessMixin()._output_ends_with_question(out) is True

    def test_no_question(self):
        out = json.dumps({"type": "result", "result": "任务已完成，请确认后关闭。"})
        assert ProcessMixin()._output_ends_with_question(out) is False

    def test_extract_question(self):
        out = json.dumps({"type": "result",
                          "result": "遇到问题。\\n请选择 A 或 B？"})
        q = ProcessMixin()._extract_question(out)
        assert "请选择 A 或 B" in q


class TestHermesResult:
    def test_success(self):
        out = json.dumps({"final_response": "done", "error": ""})
        assert ProcessMixin()._hermes_result(out) == "success"

    def test_unresolvable(self):
        out = json.dumps({"final_response": "无法解决该问题", "error": ""})
        assert ProcessMixin()._hermes_result(out) == "unresolvable"

    def test_failed_non_json(self):
        assert ProcessMixin()._hermes_result("非 JSON") == "failed"


class TestDshResult:
    def test_success(self):
        out = json.dumps({"finish_reason": "completed", "final_response": "ok", "error": ""})
        assert ProcessMixin()._dsh_result(out) == "success"

    def test_failed_incomplete(self):
        out = json.dumps({"finish_reason": "max_tokens", "final_response": "ok", "error": ""})
        assert ProcessMixin()._dsh_result(out) == "failed"


class TestDshCollision:
    def test_detects_collision(self):
        out = json.dumps({"finish_reason": "error", "error": ""})
        assert ProcessMixin()._dsh_collision("id collision " + out) is True

    def test_no_collision(self):
        out = json.dumps({"finish_reason": "completed", "final_response": "ok"})
        assert ProcessMixin()._dsh_collision(out) is False


# ---------------- 对外再导出（引用方兼容） ----------------

class TestModuleReExports:
    def test_public_names_available(self):
        from botler.executor import (
            ClaudeExecutor, ExecutorError, format_display_line,
            find_session_file, parse_transcript, read_log_delta,
            read_session_prompt, DshRunner, DshSdkNotInstalledError,
            HermesSdkRunner, HermesSdkNotInstalledError,
        )
        assert all(x is not None for x in [
            ClaudeExecutor, ExecutorError, format_display_line,
            find_session_file, parse_transcript, read_log_delta,
            read_session_prompt, DshRunner, DshSdkNotInstalledError,
            HermesSdkRunner, HermesSdkNotInstalledError,
        ])
