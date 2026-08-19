"""ClaudeExecutor 详细失败原因测试：error_detail 落库、错误提取（trace）。

issue #4 新增「查看详细原因」按钮依赖 executor 在失败时把每次尝试的
退出码 + 提取的错误/trace 序列化写入 tasks.error_detail 字段。
issue #8 会话断点续跑：claude --resume 恢复上次会话 + 保留工作区。
"""

import itertools
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor, format_display_line
from botler.gitlab_client import GitLabClient, GitLabError
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


def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "失败任务")


# 直接传 _run_once 的 repo 字典（_build_prompt 需 prompt_template 键）
_REPO = {"name": "demo", "prompt_template": None}


def _issue_dict(state: str = "opened") -> dict:
    """run_task 所需的完整 issue 字典（build_variables 依赖 project_id/iid）。"""
    return {"state": state, "title": "标题", "description": "正文",
            "web_url": "https://gitlab.example.com/x/-/issues/7",
            "project_id": 42, "iid": 7}


class TestExtractError:
    """_extract_error：从执行输出中提取 trace / 错误尾部。"""

    def test_traceback_from_json_result(self, executor):
        """claude JSON 输出：result 含 Traceback 时从其起始处截取。"""
        output = json.dumps({
            "result": "开始处理…\nTraceback (most recent call last):\n"
                      "  File \"x.py\", line 1, in <module>\n    raise ValueError('boom')\n"
                      "ValueError: boom",
            "session_id": "s1",
        })
        err = executor._extract_error(output)
        assert err.startswith("Traceback (most recent call last)")
        assert "ValueError: boom" in err

    def test_tail_when_no_traceback(self, executor):
        """无 Traceback 时取 result 尾部（错误通常出现在输出末尾）。"""
        output = json.dumps({"result": "第一步…\n第二步失败: network error"})
        err = executor._extract_error(output)
        assert "第二步失败: network error" in err
        assert "第一步" not in err or len(err) <= 3000

    def test_plain_text_output(self, executor):
        """非 JSON 输出直接按文本处理。"""
        output = "not json at all\nTailError: 挂了"
        err = executor._extract_error(output)
        assert "TailError: 挂了" in err

    def test_empty_output(self, executor):
        assert executor._extract_error("") == ""

    def test_truncated_to_max_chars(self, executor):
        long = "a" * 5000
        assert executor._extract_error(long, max_chars=100) == "a" * 100

    def test_nested_tool_calls_unescaped(self, executor):
        """result 内嵌工具调用记录（JSON 序列化文本）：\\n 等转义解码为真实字符（issue #16）。

        失败详情展示时不应出现 \\n / \\" 等字面转义符，而应显示真实换行与引号。
        """
        inner = json.dumps({"tool_name": "Bash", "tool_use_id": "call_00_x",
                            "tool_input": {"command": "cat <<EOF\nraise SystemExit\n"
                                                      "for i in data:\n    print(i['iid'], '|', i['title'])\nEOF"}})
        output = json.dumps({"result": inner, "session_id": "s1"})
        err = executor._extract_error(output)
        assert "print(i['iid'], '|', i['title'])" in err
        assert "\\n" not in err, "转义符 \\n 不应按字面量残留"
        assert "\\\"" not in err

    def test_mixed_text_and_json_unescaped(self, executor):
        """result = 普通文本 + JSON 片段（非纯 JSON）：宽松解码 \\n 字面量。"""
        inner = json.dumps({"tool_name": "Write",
                            "tool_input": {"content": "line1\nline2"}})
        output = json.dumps({"result": "脚本输出:\n" + inner, "session_id": "s1"})
        err = executor._extract_error(output)
        assert "脚本输出:" in err
        assert "line1\nline2" in err
        assert "\\n" not in err

    def test_plain_text_result_unchanged(self, executor):
        """result 为普通可读文本（真实换行）：解码保持原样不误伤。"""
        output = json.dumps({"result": "第一步…\n第二步失败: network error"})
        err = executor._extract_error(output)
        assert err == "第一步…\n第二步失败: network error"


class TestFormatDisplayLine:
    """format_display_line：claude 输出行重排（issue #16）。"""

    def test_json_line_decoded_and_noisy_fields_dropped(self):
        """JSON 行：result 解码换行，ttft_ms/uuid 等机器字段丢弃。"""
        line = json.dumps({"type": "result", "subtype": "success",
                           "session_id": "s1", "result": "a\\nb",
                           "ttft_ms": 4270, "uuid": "09b7"})
        out = format_display_line(line)
        assert out.startswith('type: "result"')
        assert 'session_id: "s1"' in out
        assert "a\nb" in out
        assert "ttft_ms" not in out and "uuid" not in out

    def test_non_json_line_unchanged(self):
        line = "Warning: no stdin data received... "
        assert format_display_line(line) == line

    def test_tail_output_decodes_lines(self, executor):
        """_tail_output：claude JSON 行解码后展示（失败评论/日志摘要数据源）。"""
        inner = json.dumps({"tool_name": "Bash", "tool_input": {"command": "a\nb"}})
        output = json.dumps({"result": inner})
        tail = executor._tail_output(output)
        assert "a\nb" in tail
        assert "\\n" not in tail


class TestDumpErrorDetail:
    def test_serializes_attempts(self, executor):
        detail = json.loads(executor._dump_error_detail(
            [{"attempt": 1, "exit_code": 1, "error": "boom"}], last_exit=-1))
        assert detail["summary"] == "重试耗尽后仍失败，最后退出码 -1"
        assert detail["attempts"] == [{"attempt": 1, "exit_code": 1, "error": "boom"}]

    def test_empty_attempts(self, executor):
        detail = json.loads(executor._dump_error_detail([], last_exit=124))
        assert detail["attempts"] == []


class TestRunTaskErrorDetail:
    """run_task 失败路径：error_detail 正确落库。"""

    def _install_mocks(self, executor, monkeypatch, tmp_path,
                       run_once, issue_state="opened"):
        calls = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": issue_state},
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
        )
        monkeypatch.setattr(executor, "_run_once", run_once)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return calls

    def test_retry_exhausted_writes_all_attempt_details(self, executor, monkeypatch, tmp_path):
        """max_retries=2 → 3 次尝试，每次失败的退出码与错误都进入 error_detail。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "处理失败: 构建超时"})
        self._install_mocks(executor, monkeypatch, tmp_path,
                            run_once=lambda *a: (1, output))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert "重试耗尽（2 次）后仍失败，最后退出码 1" in task["error_message"]
        detail = json.loads(task["error_detail"])
        assert len(detail["attempts"]) == 3
        for i, a in enumerate(detail["attempts"], start=1):
            assert a["attempt"] == i
            assert a["exit_code"] == 1
            assert "构建超时" in a["error"]

    def test_executor_error_recorded_without_trace(self, executor, monkeypatch, tmp_path):
        """executor 内部异常（非 claude 退出）也记录为一次失败详情。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)

        def boom(*_a):
            raise RuntimeError("claude 命令崩溃")

        self._install_mocks(executor, monkeypatch, tmp_path, run_once=boom)
        executor.run_task(task_id)

        task = db.get_task(task_id)
        detail = json.loads(task["error_detail"])
        assert len(detail["attempts"]) == 3
        assert "claude 命令崩溃" in detail["attempts"][0]["error"]

    def test_unresolvable_writes_single_attempt(self, executor, monkeypatch, tmp_path):
        """exit 0 但 Claude 自认无法解决：单次尝试详情落库且不重试。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        # ensure_ascii=False：真实 claude JSON 输出为 UTF-8 明文，unresolvable 正则才可命中
        output = json.dumps({"result": "抱歉，我无法解决该 issue，原因：权限不足"},
                            ensure_ascii=False)
        calls = self._install_mocks(executor, monkeypatch, tmp_path,
                                    run_once=lambda *a: (0, output),
                                    issue_state="opened")
        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["error_message"] == "Claude Code 报告无法解决该 issue"
        detail = json.loads(task["error_detail"])
        assert len(detail["attempts"]) == 1
        assert detail["attempts"][0]["exit_code"] == 0
        assert "无法解决" in detail["attempts"][0]["error"]
        # 失败评论 + bot-failed 标签各一次
        assert sum(1 for kind, _ in calls if kind == "comment") == 2  # 处理中 + 失败
        assert sum(1 for kind, _ in calls if kind == "labels") == 1


class TestRunTaskSuccessCriteria:
    """成功判定（issue #25 第二轮）：完成任务即成功，不再要求关闭 issue。

    模版库规范（docs/labels.md）：任务完成后不关闭 issue——留结果评论、
    打 bot-done，等用户确认后手动关闭。旧逻辑以 issue closed 为成功
    标志，exit 0 但 issue 仍 open 时判失败并重试，迫使 Claude 违规关闭
    issue（生产日志 task_30/31：#28 完成开发后 issue 被关闭）。新判定：
    Claude exit 0 且输出为正常 JSON result（非「无法解决」）即成功，
    issue 是否关闭不参与判定。
    """

    def _install(self, executor, monkeypatch, tmp_path, run_once,
                 issue_state="opened"):
        calls = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": issue_state},
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )
        monkeypatch.setattr(executor, "_run_once", run_once)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return calls

    def test_success_when_issue_stays_open(self, executor, monkeypatch, tmp_path):
        """exit 0 + 正常完成输出 + issue 未关闭 → 一次尝试即成功（修复前：判失败重试）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码，打了 bot-done 标签"},
                            ensure_ascii=False)
        calls = self._install(executor, monkeypatch, tmp_path,
                              run_once=lambda *a: (0, output), issue_state="opened")

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["attempt_count"] == 1  # 未因 issue 未关闭而重试
        assert task["exit_code"] == 0
        # issue #34：成功路径由平台代码打 bot-done（幂等），不打 bot-failed
        assert calls.count(("labels", (42, 1, ["bot-done"]))) == 1
        assert "无法完成此 issue" not in "".join(
            a[2] for kind, a in calls if kind == "comment")

    def test_success_when_issue_already_closed(self, executor, monkeypatch, tmp_path):
        """issue 已被关闭（兼容旧流程 / 用户指示关闭）：同样判成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "完成，issue 已关闭"}, ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output), issue_state="closed")

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["attempt_count"] == 1

    def test_unresolvable_still_fails_without_retry(self, executor, monkeypatch, tmp_path):
        """「无法解决」仍是失败终态（不重试），不受成功判定放宽影响。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "抱歉，我无法解决该 issue，原因：权限不足"},
                            ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output), issue_state="opened")

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_message"] == "Claude Code 报告无法解决该 issue"
        assert task["attempt_count"] == 1

    # ---- issue #120：执行引擎按任务落库（概览页右边栏按 issue 展示
    # 实际执行引擎，而非全局 worker.engine）----

    def test_run_task_persists_engine(self, executor, monkeypatch, tmp_path):
        """claude 引擎任务执行时把 engine 落库（默认配置）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"},
                            ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output), issue_state="opened")

        executor.run_task(task_id)

        assert db.get_task(task_id)["engine"] == "claude"

    def test_run_task_persists_engine_dsh(self, executor, monkeypatch, tmp_path):
        """dsh 引擎任务执行时同样把 engine 落库（非默认引擎路径）。"""
        executor.config.get().engine = "dsh"
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"final_response": "已修复并推送",
                             "finish_reason": "completed",
                             "session_id": "dsh-sess-1"}, ensure_ascii=False)
        self._install(executor, monkeypatch, tmp_path,
                      run_once=lambda *a: (0, output), issue_state="opened")

        executor.run_task(task_id)

        assert db.get_task(task_id)["engine"] == "dsh"


# ---- issue #8 会话断点续跑 ----

class _FakeStdout:
    """一次输出 + EOF；EOF 后 poll() 视为进程已退出。"""

    def __init__(self, text: str):
        self._lines = [text] if text else []

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def read(self) -> str:
        """subprocess.run 兼容：Popen 桩被 run() 以 communicate 读取时
        一次性返回全部剩余输出（如 git_remote.list_local_remotes）。"""
        text = "".join(self._lines)
        self._lines = []
        return text


class _FakeProc:
    def __init__(self, output: str, exit_code: int = 0):
        self.stdout = _FakeStdout(output)
        self.stdin = None
        self.stderr = None
        self.args = []  # subprocess.run 构造 CompletedProcess 需要
        self._exit = exit_code

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit

    # subprocess.run 兼容（run 内部用 with + communicate 包装 Popen）
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def communicate(self, input=None, timeout=None):
        out = self.stdout.read() if self.stdout else ""
        return out, ""


class TestSessionResume:
    """claude --resume 会话恢复：session_id 落库、恢复执行、降级回退。"""

    def _session_toolkit(self, executor, monkeypatch, tmp_path, output, exit_code=0):
        """构造 resume 测试环境：fake Popen 捕获 cmd、fake claude home 会话文件。"""
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(output, exit_code)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        claude_home = tmp_path / "claude-home"
        monkeypatch.setattr(executor, "_claude_home", lambda: claude_home)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return captured

    @staticmethod
    def _mk_session_file(claude_home: Path, session_id: str) -> Path:
        proj = claude_home / "projects" / "-proj"
        proj.mkdir(parents=True, exist_ok=True)
        f = proj / f"{session_id}.jsonl"
        f.write_text('{"type":"user","message":"hi"}\n', encoding="utf-8")
        return f

    def test_extract_session_id_from_output(self, executor):
        """JSON 输出含 session_id 时解析成功；无/非法 JSON 返回 None。"""
        assert executor._extract_session_id(
            json.dumps({"result": "ok", "session_id": "sid-abc"})) == "sid-abc"
        assert executor._extract_session_id(
            json.dumps({"result": "ok"})) is None
        assert executor._extract_session_id("not json") is None
        assert executor._extract_session_id("") is None

    def test_run_once_uses_resume_flag_and_guidance(self, executor, monkeypatch, tmp_path):
        """恢复执行：cmd 含 --resume <sid>，prompt 换成恢复引导语（含「继续」），工作区保留。"""
        session_id = "resume-sid-1"
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": session_id}))
        self._mk_session_file(tmp_path / "claude-home", session_id)

        executor._run_once(1, {"name": "demo"}, {"project_id": 42, "iid": 7}, "resume-sid-1")

        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == session_id
        prompt = cmd[-1]
        assert "继续" in prompt and "resume-sid-1" not in prompt
        assert "demo" in prompt and "7" in prompt  # 引导语渲染了仓库/issue 变量

    def test_run_once_fresh_without_resume_flag(self, executor, monkeypatch, tmp_path):
        """首次执行：不带 --resume，prompt 为完整模版。"""
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": "s-new"}))

        executor._run_once(1, _REPO, {"project_id": 42, "iid": 7})

        cmd = captured["cmd"]
        assert "--resume" not in cmd
        assert "你是" in cmd[-1]  # 默认模版文案

    def test_run_once_persists_session_id(self, executor, monkeypatch, tmp_path):
        """执行完成后 session_id 落库（供下次重试/重启恢复）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": "sid-persist"}))

        executor._run_once(task_id, _REPO, {"project_id": 42, "iid": 7})

        assert db.get_task(task_id)["claude_session_id"] == "sid-persist"

    def test_requeued_task_resumes_session(self, executor, monkeypatch, tmp_path):
        """平台重启恢复（requeue_interrupted 后任务重新入队）：run_task 走 resume。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        session_id = "requeue-sid"
        db.set_task_status(task_id, "retrying", claude_session_id=session_id)
        self._mk_session_file(tmp_path / "claude-home", session_id)
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": session_id}))
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("closed"),  # 一次尝试即成功
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )

        executor.run_task(task_id)

        assert "--resume" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--resume") + 1] == session_id
        assert db.get_task(task_id)["status"] == "succeeded"
        assert db.get_task(task_id)["claude_session_id"] == session_id
        # 日志记录了恢复执行
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("恢复" in m for m in logs)

    def test_missing_session_file_downgrades_to_fresh(self, executor, monkeypatch, tmp_path):
        """session 文件已丢失（如 ~/.claude 未持久化）：清掉 session_id，降级全新会话。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.set_task_status(task_id, "retrying", claude_session_id="ghost-sid")
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": "s-fresh"}))
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("opened"),
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )

        executor.run_task(task_id)

        assert "--resume" not in captured["cmd"]
        # 无效 session_id 被清除，新会话 id 落库
        assert db.get_task(task_id)["claude_session_id"] == "s-fresh"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("降级" in m for m in logs)


# ---- issue #11 复现：db 返回 sqlite3.Row，executor 却按 dict 用 .get() ----

class TestRepoSqlite3RowCompat:
    """issue #11：真实运行中 repo 来自 db.get_repo()（sqlite3.Row，无 .get() 方法），
    executor 的 _repo_workdir / prepare_workspace 对 repo 调 .get() → AttributeError，
    被 run_task 兜底捕获为「[executor] 未预期异常: 'sqlite3.Row' object has no
    attribute 'get'」，重试耗尽后退出码 -1（CI 日志 task_1.log 同款报错）。"""

    def test_repo_workdir_accepts_sqlite3_row(self, executor, tmp_path):
        """_repo_workdir 对 db 查出的 sqlite3.Row 不应抛 AttributeError。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo = db.get_repo(repo_id)
        assert isinstance(repo, sqlite3.Row)  # 前置条件：确认真实类型是 Row
        workdir = executor._repo_workdir(repo)
        assert workdir == tmp_path / "workspace" / "demo"

    def test_run_task_with_row_repo_succeeds(self, executor, monkeypatch, tmp_path):
        """端到端复现（与 CI 日志同调用路径）：run_task 拿到 Row 类型的 repo 后
        应正常走完流程并 succeeded；修复前会在 prepare_workspace 抛
        'sqlite3.Row' object has no attribute 'get'，任务 failed。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        # 预建工作区 .git 跳过 clone 分支；git 子命令与 claude 子进程全部 mock
        (tmp_path / "workspace" / "demo" / ".git").mkdir(parents=True)
        monkeypatch.setattr(executor, "_git", lambda *a, **k: None)
        monkeypatch.setattr(executor, "_askpass_script", lambda n: tmp_path / "askpass.sh")
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(json.dumps({"result": "ok", "session_id": "sid-row"}), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("closed"),
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert "未预期异常" not in (task["error_message"] or "")
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert not any("sqlite3.Row" in m for m in logs)


class TestAwaitingDecision:
    """issue #67：无人值守执行中 Claude 停在「需要用户决策」的提问节点。

    复现缺陷：claude -p 无人值守时，Claude 遇到需要用户决策的问题
    （如 fix-bug 流程评估多方案后提问「请选择 A 或 B」）只会把问题留在
    终端输出里，随后自行退出（exit 0）。旧判定 exit 0 + result 行即
    成功 → 任务被标 succeeded、issue 被打 bot-done，提问从未反馈给
    用户（生产任务 #90 处理 issue #66：约 4 分钟跑完，无任何提交、
    无 CI，却被判成功并打 bot-done）。
    修复后：提问结尾且无任务提交 → 任务 failed（未完成），Claude 的
    提问反馈到 issue 评论 + 打 blocked 标签（不在领取过滤标签中，
    用户回复后经重新指派/对账可再次入队按回复继续处理）。
    """

    QUESTION_OUTPUT = json.dumps({
        "result": "**第四阶段：评估修复方案，请用户决策**\n\n"
                  "| | 方案 A | 方案 B |\n"
                  "|做法| 断点降列 | auto-fit 自适应 |\n\n"
                  "**建议方案 B**。\n\n请选择 A 或 B（或提出其他要求）。",
    }, ensure_ascii=False)

    def _install(self, executor, monkeypatch, tmp_path, run_once,
                 commit_sha=None, issue_state="opened"):
        """fake _run_once + fake gitlab；commit_sha 控制提交查询结果。"""
        calls = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": issue_state},
            add_comment=lambda *a, **k: calls.append(("comment", a, k)),
            add_labels=lambda *a, **k: calls.append(("labels", a, k)),
            find_commit_for_issue=lambda pid, iid: commit_sha,
            get_latest_pipeline=lambda pid: {"id": 1, "status": "success",
                                             "sha": "other-sha"},
            last_note_author_id=lambda pid, iid: None,
        )
        monkeypatch.setattr(executor, "_run_once", run_once)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return calls

    def test_question_ending_without_commit_fails_and_posts_question(self, executor, monkeypatch, tmp_path):
        """提问结尾 + 无任务提交：任务判 failed（未完成），提问反馈到 issue，打 blocked。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = self._install(executor, monkeypatch, tmp_path,
                              run_once=lambda *a: (0, self.QUESTION_OUTPUT))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed", "任务未完成却被判成功（修复前为 succeeded）"
        assert "用户决策" in task["error_message"]
        assert task["attempt_count"] == 1
        # 提问内容反馈到 issue 评论（不再是只在终端里）
        comments = [a[2] for kind, a, k in calls if kind == "comment"]
        assert any("请选择 A 或 B" in c for c in comments), \
            f"提问未反馈到 issue 评论: {comments}"
        # 打 blocked 等用户回复，而不是 bot-done
        assert ("labels", (42, 1, ["blocked"]), {}) in calls or \
            any(kind == "labels" and a[2] == ["blocked"] for kind, a, k in calls), \
            f"未打 blocked 标签: {calls}"
        assert not any(kind == "labels" and a[2] == ["bot-done"]
                       for kind, a, k in calls), "不应打 bot-done"

    def test_normal_completion_without_commit_still_succeeds(self, executor, monkeypatch, tmp_path):
        """正常完成汇报结尾（非提问）+ 无提交（分析型任务）：仍判成功，不误伤。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps(
            {"result": "开发完成，已推送代码，打了 bot-done 标签，请确认后关闭本 issue。"},
            ensure_ascii=False)
        calls = self._install(executor, monkeypatch, tmp_path,
                              run_once=lambda *a: (0, output))
        _shorten_ci_timeouts(executor, monkeypatch)

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert any(kind == "labels" and a[2] == ["bot-done"]
                   for kind, a, k in calls)

    def test_question_ending_with_commit_still_succeeds(self, executor, monkeypatch, tmp_path):
        """提问结尾但有任务提交（已推送代码）：不判等待决策，走成功路径。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        sha = "deadbeef000111222333444555666777888999aa"
        calls = self._install(executor, monkeypatch, tmp_path,
                              run_once=lambda *a: (0, self.QUESTION_OUTPUT),
                              commit_sha=sha)
        _shorten_ci_timeouts(executor, monkeypatch)

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert any(kind == "labels" and a[2] == ["bot-done"]
                   for kind, a, k in calls)

    def test_output_ends_with_question_signal(self, executor):
        """提问信号检测：选项型提问命中，完成汇报/礼貌收尾不命中。"""
        assert executor._output_ends_with_question(json.dumps(
            {"result": "请选择 A 或 B（或提出其他要求）。"}, ensure_ascii=False))
        assert executor._output_ends_with_question(json.dumps(
            {"result": "方案如下，请问需要我继续实施吗？"}, ensure_ascii=False))
        assert executor._output_ends_with_question(json.dumps(
            {"result": "请回复 1 或 2 以确认后续步骤。"}, ensure_ascii=False))
        assert not executor._output_ends_with_question(json.dumps(
            {"result": "开发已完成，请确认后关闭本 issue。"}, ensure_ascii=False))
        assert not executor._output_ends_with_question(json.dumps(
            {"result": "如有问题请回复我。"}, ensure_ascii=False))
        assert not executor._output_ends_with_question(json.dumps(
            {"result": "修复完成，已推送并等待 CI。"}, ensure_ascii=False))


# ---- issue #19：任务成功时记录对应提交（任务页面 commit 链接） ----

class TestCommitRecording:
    """_finish_succeeded 成功后应查询并落库对应提交的 sha（issue #19）。

    任务页面展示 commit 链接依赖 tasks.commit_sha：Claude 按模板提交
    （message 含 "issue #N"）并关闭 issue 后，executor 用 GitLab commits
    API 匹配该提交。查询失败/找不到不应阻塞任务成功（页面不显示链接即可）。
    """

    def _commit_sha(self) -> str:
        return "deadbeef000111222333444555666777888999aa"

    def test_success_records_commit_sha(self, executor, tmp_path):
        """找到对应提交 → sha 落库，任务保持 succeeded。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: self._commit_sha(),
            add_labels=lambda *a, **k: None,
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["commit_sha"] == self._commit_sha()
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("已记录任务提交" in m for m in logs)

    def test_no_commit_found_keeps_success(self, executor, tmp_path):
        """查询不到对应提交（模板被改/提交信息不含 issue 号）→ 不落库、任务仍成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda *a, **k: None,
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["commit_sha"] is None

    def test_commit_query_error_keeps_success(self, executor, tmp_path):
        """GitLab API 查询失败（网络/权限）→ 记 warn 日志，任务仍成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: (_ for _ in ()).throw(
                GitLabError("GitLab API 错误 500: boom", 500)),
            add_labels=lambda *a, **k: None,
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["commit_sha"] is None
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("查询任务提交失败" in m for m in logs)

    def test_run_task_success_records_commit(self, executor, monkeypatch, tmp_path):
        """端到端：run_task 成功路径（exit 0 + issue closed）应记录对应提交。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        (tmp_path / "workspace" / "demo" / ".git").mkdir(parents=True)
        monkeypatch.setattr(executor, "_git", lambda *a, **k: None)
        monkeypatch.setattr(executor, "_askpass_script", lambda n: tmp_path / "askpass.sh")
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        def fake_popen(cmd, **kwargs):
            return _FakeProc(json.dumps({"result": "ok", "session_id": "sid-commit"}), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("closed"),
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: self._commit_sha(),
            last_note_author_id=lambda pid, iid: None,
            # issue #40：成功路径会探测任务触发的流水线；无匹配 sha → 不等待
            get_latest_pipeline=lambda pid: {"id": 1, "status": "success", "sha": "other"},
        )
        _shorten_ci_timeouts(executor, monkeypatch)

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["commit_sha"] == self._commit_sha()


class TestSucceededAddsBotDoneLabel:
    """issue #34：任务成功收尾时由 executor 代码直接打 bot-done 标签。

    此前 bot-done 依赖 Claude 按模板自行打标签——若 Claude 忘打，issue 缺
    终态标签会被 webhook/对账重复领取。改为平台代码写死：成功即打
    bot-done（幂等），打标签失败不阻塞任务成功。
    """

    def test_success_adds_bot_done_label(self, executor, tmp_path):
        """_finish_succeeded 应调用 add_labels 打 bot-done（issue #67：同步移除 in-progress）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        labels_calls = []
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda pid, iid, labels, remove=None: labels_calls.append(
                (pid, iid, labels, remove)),
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_succeeded(task_id, "ok")

        assert labels_calls == [(42, 1, ["bot-done"], ["in-progress"])]
        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("bot-done" in m for m in logs)

    def test_label_error_keeps_success(self, executor, tmp_path):
        """打标签时 GitLab API 报错：任务仍成功，仅记 warn 日志。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda pid, iid, labels, remove=None: (_ for _ in ()).throw(
                GitLabError("GitLab API 错误 500: boom", 500)),
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)
        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("bot-done 标签失败" in m for m in logs)

    def test_no_label_when_finish_skipped(self, executor, tmp_path):
        """条件终态（issue #24）：已被其他实例收尾时不打标签（避免重复）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        labels_calls = []
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda pid, iid, labels, remove=None: labels_calls.append(
                (pid, iid, labels, remove)),
            add_comment=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)
        db.finish_task(task_id, "succeeded")  # 模拟其他实例已收尾
        executor._finish_succeeded(task_id, "ok")

        assert labels_calls == []


class TestSucceededResultComment:
    """issue #79：任务成功收尾由平台兜底写结果评论。

    此前结果评论依赖 Claude 按模板自行留言（DEFAULT_TEMPLATE 第 4 条）；
    全局 bot token 失效后 Claude 侧 API 401 失败，任务成功（bot-done 已打）
    但 issue 上没有任何报告评论。改为平台兜底：最后一条非系统评论非 bot
    本人发出时，从执行输出提取结果摘要写一条完成报告；写评论失败不阻塞
    任务成功（仅记 warn，与打标签一致）。
    """

    def _comment_mock(self, executor, tmp_path, last_author=None,
                      bot_id=7):
        comments = []
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: "deadbeef" * 5,
            add_labels=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: last_author,
            get_bot_id=lambda: bot_id,
            add_comment=lambda pid, iid, body: comments.append((pid, iid, body)))
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        return comments

    def test_success_leaves_result_comment_when_none_exists(self, executor,
                                                            tmp_path):
        """issue 无人评论（Claude 没写/写失败）→ 平台写完成报告评论。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = self._comment_mock(executor, tmp_path, last_author=None)

        db.claim_task(task_id)
        output = json.dumps({"result": "开发完成，全部测试通过"}, ensure_ascii=False)
        executor._finish_succeeded(task_id, output)

        assert len(comments) == 1
        pid, iid, body = comments[0]
        assert (pid, iid) == (42, 1)
        assert "任务已完成" in body
        assert "开发完成，全部测试通过" in body  # 结果摘要提取
        assert "deadbeef" in body  # commit sha
        assert db.get_task(task_id)["status"] == "succeeded"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("任务完成评论" in m for m in logs)

    def test_skips_comment_when_bot_already_commented(self, executor, tmp_path):
        """最后一条评论是 bot 本人（Claude 已留结果评论）→ 平台不重复写。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = self._comment_mock(executor, tmp_path, last_author=7,
                                      bot_id=7)

        db.claim_task(task_id)
        executor._finish_succeeded(task_id, "ok")

        assert comments == []
        assert db.get_task(task_id)["status"] == "succeeded"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("不重复写" in m for m in logs)

    def test_skips_comment_when_last_author_is_remote_token_bot(self, executor,
                                                                tmp_path):
        """最后评论作者是 remote token 账号（Claude 用兜底 token 写过）→ 不重复。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = self._comment_mock(executor, tmp_path, last_author=99,
                                      bot_id=99)

        db.claim_task(task_id)
        executor._finish_succeeded(task_id, "ok")

        assert comments == []

    def test_comment_error_keeps_success(self, executor, tmp_path):
        """写评论 GitLab API 报错：任务仍成功，仅记 warn 日志。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda *a, **k: None,
            last_note_author_id=lambda pid, iid: None,
            get_bot_id=lambda: 7,
            add_comment=lambda pid, iid, body: (_ for _ in ()).throw(
                GitLabError("GitLab API 错误 500: boom", 500)))
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)
        executor._finish_succeeded(task_id, "ok")

        assert db.get_task(task_id)["status"] == "succeeded"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("任务完成评论失败" in m for m in logs)

    def test_hermes_final_response_as_summary(self, executor, tmp_path):
        """hermes 引擎：摘要取 final_response（issue #47 输出协议）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = self._comment_mock(executor, tmp_path, last_author=None)

        db.claim_task(task_id)
        output = json.dumps({"final_response": "hermes 已完成处理"},
                            ensure_ascii=False)
        executor._finish_succeeded(task_id, output)

        assert len(comments) == 1
        assert "hermes 已完成处理" in comments[0][2]

    def test_no_comment_when_finish_skipped(self, executor, tmp_path):
        """条件终态（issue #24）：已被其他实例收尾时不写评论（避免重复）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        comments = self._comment_mock(executor, tmp_path, last_author=None)

        db.claim_task(task_id)
        db.finish_task(task_id, "succeeded")  # 模拟其他实例已收尾
        executor._finish_succeeded(task_id, "ok")

        assert comments == []


class TestBuildEnvTokenFallback:
    """issue #79：任务会话 GITLAB_TOKEN 注入兜底（remote 内嵌 token 优先）。

    全局 bot token 失效后 Claude 侧 API（读 issue/写结果评论）401 失败；
    与平台 _call_with_fallback 的 per-repo 兜底（issue #65）对齐，会话
    GITLAB_TOKEN 优先取仓库 remote url 内嵌 token，无 token 时回退全局。
    """

    def _repo(self, tmp_path, url):
        local = tmp_path / "repo"
        local.mkdir()
        subprocess.run(["git", "init", "-q", str(local)], check=True)
        subprocess.run(["git", "-C", str(local), "remote", "add",
                        "origin", url], check=True)
        return {"name": "demo", "prompt_template": None,
                "local_path": str(local), "remote_name": "origin"}

    @staticmethod
    def _issue():
        return {"project_id": 42, "iid": 7}

    def test_env_uses_remote_token_when_embedded(self, executor, tmp_path):
        """remote url 内嵌 token → 会话 GITLAB_TOKEN 用 remote token。"""
        repo = self._repo(
            tmp_path,
            "https://user:remote-token-123@gitlab.example.com/group/demo.git")
        env = executor._build_env(repo, self._issue())
        assert env["GITLAB_TOKEN"] == "remote-token-123"

    def test_env_falls_back_to_global_token_without_embedded(self, executor,
                                                             tmp_path):
        """remote url 无内嵌 token → 回退全局 bot token。"""
        repo = self._repo(tmp_path,
                          "https://gitlab.example.com/group/demo.git")
        env = executor._build_env(repo, self._issue())
        assert env["GITLAB_TOKEN"] == "test-token"

    def test_env_falls_back_when_repo_has_no_remote(self, executor, tmp_path):
        """仓库无 remote（新 clone 前 / 非 git 目录）→ 回退全局 token。"""
        local = tmp_path / "empty"
        local.mkdir()
        subprocess.run(["git", "init", "-q", str(local)], check=True)
        repo = {"name": "demo", "prompt_template": None,
                "local_path": str(local), "remote_name": "origin"}
        env = executor._build_env(repo, self._issue())
        assert env["GITLAB_TOKEN"] == "test-token"


class TestRunTaskConcurrency:
    """issue #24：任务已被其他实例领取（running）时 run_task 直接跳过。

    双实例并存时同一任务可能被两个 worker 同时领取执行，修复后只有
    第一个成功 claim（queued/retrying → running）的实例继续，其余跳过。
    """

    def test_skip_when_already_claimed(self, executor, monkeypatch, tmp_path):
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.claim_task(task_id)  # 模拟其他实例已领取（任务已 running）

        called = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda *a: called.append("get_issue") or {"state": "opened"})
        monkeypatch.setattr(executor, "_run_once",
                            lambda *a: called.append("run_once") or (0, "x"))

        executor.run_task(task_id)

        # 未获取 issue、未执行 claude，任务状态保持 running 不被扰动
        assert called == []
        assert db.get_task(task_id)["status"] == "running"

    def test_terminal_task_skipped(self, executor, monkeypatch, tmp_path):
        """任务已终态（succeeded）→ 跳过，不覆盖成失败。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.set_task_status(task_id, "succeeded")

        called = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda *a: called.append("get_issue") or {"state": "opened"})
        monkeypatch.setattr(executor, "_run_once",
                            lambda *a: called.append("run_once") or (0, "x"))

        executor.run_task(task_id)

        assert called == []
        assert db.get_task(task_id)["status"] == "succeeded"


def _shorten_ci_timeouts(executor, monkeypatch):
    """把 CI 等待相关的配置超时缩到极小（测试用，避免真实等待秒级时间）。

    窗口留 1 秒余量（issue #42）：等待/探测的 deadline 在探测前计算，
    期间 db.add_log 真实写 SQLite，高负载机器上单次写入可达数十毫秒，
    20ms 窗口会被 add_log 耗尽导致误报 timeout（flaky）。测试里
    time.sleep 已 mock 为 no-op，窗口放大不拖慢正常路径；仅
    sha 永不匹配 / 永不终态两个用例以空转耗尽窗口（各约 1 秒 CPU）。
    """
    real_get = executor.config.get
    settings = real_get()
    monkeypatch.setattr(executor.config, "get", lambda: SimpleNamespace(
        **{**vars(settings),
           "ci_wait_detect_seconds": 1.0,
           "ci_wait_interval_seconds": 0.001,
           "ci_wait_timeout_seconds": 1.0}))


class TestWaitPipelineBeforeSucceed:
    """issue #40：任务成功收尾前等待任务提交触发的 CI 流水线到终态。

    复现缺陷：claude push 代码后退出，平台立即把任务标记 succeeded，
    此时流水线仍在运行（任务 #63 于 13:31:45 收尾，流水线 #737 的
    sync_to_github 到 13:48:34 才结束）。修复后：
    - 流水线 success/skipped → 任务 succeeded（打 bot-done）
    - 流水线 failed/canceled → 任务 failed（打 bot-failed + 失败评论）
    - 等待超时 → 任务 failed
    - 无匹配流水线（仓库无 CI / 未推送）→ 直接成功，不等待
    - 等待期间用户停止 → interrupted
    """

    SHA = "abc123def456"

    def _install(self, executor, monkeypatch, tmp_path, run_once,
                 latest_pipeline, pipeline_status, issue_state="opened"):
        """构造 run_task 环境：fake _run_once + fake gitlab（含流水线桩）。"""
        calls = []
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": issue_state},
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: self.SHA,
            last_note_author_id=lambda pid, iid: None,
            get_latest_pipeline=latest_pipeline,
            get_pipeline=lambda pid, pid2: {"id": pid2, "status": pipeline_status(),
                                            "sha": self.SHA},
        )
        monkeypatch.setattr(executor, "_run_once", run_once)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return calls

    def test_pipeline_success_before_task_succeeded(self, executor, monkeypatch, tmp_path):
        """流水线先 running 后 success：任务等到流水线终态才 succeeded。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"}, ensure_ascii=False)
        states = iter(["success"])
        calls = self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
            pipeline_status=lambda: next(states),
        )

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert calls.count(("labels", (42, 1, ["bot-done"]))) == 1

    def test_pipeline_failed_marks_task_failed(self, executor, monkeypatch, tmp_path):
        """流水线 failed：任务判失败并打 bot-failed（不再仅凭 claude exit 0 判成功）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"}, ensure_ascii=False)
        states = iter(["failed"])
        calls = self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
            pipeline_status=lambda: next(states),
        )

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert "流水线" in task["error_message"]
        assert calls.count(("labels", (42, 1, ["bot-failed"]))) == 1
        assert any("无法完成此 issue" in a[2] for kind, a in calls if kind == "comment")

    def test_pipeline_canceled_marks_task_failed(self, executor, monkeypatch, tmp_path):
        """流水线 canceled 同样视为失败（CI 未通过，任务不算完成）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "已推送"}, ensure_ascii=False)
        states = iter(["canceled"])
        self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
            pipeline_status=lambda: next(states),
        )

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "failed"

    def test_pipeline_skipped_counts_as_success(self, executor, monkeypatch, tmp_path):
        """流水线 skipped（无 job 需要执行）：任务成功收尾。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "已推送"}, ensure_ascii=False)
        states = iter(["skipped"])
        self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "pending", "sha": self.SHA},
            pipeline_status=lambda: next(states),
        )

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"

    def test_no_matching_pipeline_skips_wait(self, executor, monkeypatch, tmp_path):
        """仓库无 CI（最新流水线 sha 始终不匹配）：不等待，直接成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "完成"}, ensure_ascii=False)
        self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 1, "status": "success", "sha": "other-sha"},
            pipeline_status=lambda: "success",
        )
        # 探测窗口缩到极小，避免默认 120s 拖慢测试
        _shorten_ci_timeouts(executor, monkeypatch)

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"

    def test_pipeline_timeout_marks_task_failed(self, executor, monkeypatch, tmp_path):
        """流水线一直不结束（超时）：任务判失败，不再无限等待。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "已推送"}, ensure_ascii=False)
        self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
            pipeline_status=lambda: "running",
        )
        # 用极小超时模拟"流水线迟迟不完成"
        monkeypatch.setattr(executor, "_wait_pipeline_for_commit",
                            lambda *a, **k: "timeout")

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert "超时" in task["error_message"]

    def test_stop_during_pipeline_wait_interrupts_task(self, executor, monkeypatch, tmp_path):
        """等待流水线期间用户一键停止：任务 interrupted，不判成功也不判失败。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "已推送"}, ensure_ascii=False)
        self._install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, output),
            latest_pipeline=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
            pipeline_status=lambda: "running",
        )
        monkeypatch.setattr(executor, "_wait_pipeline_for_commit",
                            lambda *a, **k: "stopped")

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "interrupted"


class TestWaitPipelineForCommit:
    """_wait_pipeline_for_commit 内部轮询逻辑（issue #40）。"""

    SHA = "abc123def456"

    def _install(self, executor, monkeypatch, latest, statuses):
        """fake gitlab + 缩短超时；statuses 为 get_pipeline 依序返回的状态迭代。"""
        executor.gitlab = SimpleNamespace(
            get_latest_pipeline=latest,
            get_pipeline=lambda pid, pid2: {"id": pid2, "sha": self.SHA,
                                            "status": next(statuses)},
        )
        _shorten_ci_timeouts(executor, monkeypatch)
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)

    def test_waits_until_terminal_state(self, executor, monkeypatch):
        """命中 sha 匹配的流水线后，轮询直到 success 返回。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
                      statuses=iter(["success"]))

        state = executor._wait_pipeline_for_commit(1, 42, self.SHA)

        assert state == "success"

    def test_returns_failed_state(self, executor, monkeypatch):
        """流水线 failed → 返回 failed（run_task 据此判任务失败）。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
                      statuses=iter(["failed"]))

        assert executor._wait_pipeline_for_commit(1, 42, self.SHA) == "failed"

    def test_returns_no_pipeline_when_sha_never_matches(self, executor, monkeypatch):
        """探测窗口内最新流水线 sha 始终不匹配（仓库无 CI）→ no_pipeline。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 1, "status": "success", "sha": "other"},
                      statuses=iter([]))

        state = executor._wait_pipeline_for_commit(1, 42, self.SHA)

        assert state == "no_pipeline"

    def test_returns_timeout_when_pipeline_never_terminal(self, executor, monkeypatch):
        """流水线一直 running 直到总超时 → timeout。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
                      statuses=itertools.repeat("running"))

        state = executor._wait_pipeline_for_commit(1, 42, self.SHA)

        assert state == "timeout"

    def test_stop_during_wait_returns_stopped(self, executor, monkeypatch):
        """等待期间收到停止请求 → stopped（run_task 据此走停止收尾）。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 900, "status": "running", "sha": self.SHA},
                      statuses=itertools.repeat("running"))
        monkeypatch.setattr(executor, "_stop_requested", lambda tid: True)

        state = executor._wait_pipeline_for_commit(1, 42, self.SHA)

        assert state == "stopped"

    def test_stop_during_detect_returns_stopped(self, executor, monkeypatch):
        """探测阶段收到停止请求同样返回 stopped。"""
        self._install(executor, monkeypatch,
                      latest=lambda pid: {"id": 1, "status": "success", "sha": "other"},
                      statuses=iter([]))
        monkeypatch.setattr(executor, "_stop_requested", lambda tid: True)

        state = executor._wait_pipeline_for_commit(1, 42, self.SHA)

        assert state == "stopped"


class TestGitlabFallbackOnGlobalTokenFailure:
    """issue #65 补充：全局 bot token 失效（401/403）时，executor 的
    issue 查询 / 评论 / 打标签应像对账一样用仓库 remote 内嵌 token 兜底。

    此前兜底仅覆盖对账扫描与 webhook 身份判定，executor 全部 GitLab 操作
    只走全局 client——全局 token 被撤销后，任务领取（get_issue）与
    「处理中」评论、失败评论、bot-done/bot-failed 标签全部 401
    （生产任务 #88/#89 因此 1 秒内失败，issue 上收不到任何评论）。
    """

    @staticmethod
    def _boom(*args, **kwargs):
        raise GitLabError("token 无效或已过期（401）", 401)

    def _install(self, executor, monkeypatch, tmp_path, fallback,
                 run_once=None):
        """全局 client 全部 401；fallback 为 remote token 客户端桩。"""
        executor.gitlab = SimpleNamespace(
            get_issue=self._boom, add_comment=self._boom,
            add_labels=self._boom, find_commit_for_issue=self._boom,
            get_latest_pipeline=self._boom, get_pipeline=self._boom,
            last_note_author_id=self._boom)
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")
        monkeypatch.setattr("botler.executor.build_repo_client_with_username",
                            lambda repo, verify_ssl: (fallback, "agent"))
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        if run_once is not None:
            monkeypatch.setattr(executor, "_run_once", run_once)

    def test_run_task_401_falls_back_and_succeeds(self, executor,
                                                  monkeypatch, tmp_path):
        """全局 get_issue 401：用 remote token 兜底领取，任务照常执行成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"},
                            ensure_ascii=False)
        calls = []
        fallback = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("opened"),
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
            get_bot_id=lambda: 7,
            get_latest_pipeline=lambda pid: {"id": 1, "status": "success",
                                             "sha": "other-sha"},
        )
        self._install(executor, monkeypatch, tmp_path, fallback,
                      run_once=lambda *a: (0, output))
        _shorten_ci_timeouts(executor, monkeypatch)

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded", \
            f"全局 token 失效不应导致任务失败: {task['error_message']}"
        # 领取评论与 bot-done 标签经 remote token 兜底客户端发出
        assert any("已收到该 issue" in a[2]
                   for kind, a in calls if kind == "comment")
        assert ("labels", (42, 1, ["bot-done"])) in calls

    def test_call_with_fallback_401_uses_remote_client(self, executor,
                                                       monkeypatch):
        """_call_with_fallback：全局 401 → remote token 客户端重试成功。"""
        repo = {"name": "demo"}
        fallback = SimpleNamespace(probe=lambda: "fallback-ok")
        monkeypatch.setattr("botler.executor.build_repo_client_with_username",
                            lambda r, verify_ssl: (fallback, "agent"))
        executor.gitlab = SimpleNamespace(probe=self._boom)

        result, client = executor._call_with_fallback(repo,
                                                      lambda c: c.probe())

        assert result == "fallback-ok"
        assert client is fallback

    def test_call_with_fallback_non_auth_error_no_fallback(self, executor,
                                                           monkeypatch):
        """非 401/403（如 404）不触发兜底，原样抛出。"""
        built = []
        monkeypatch.setattr("botler.executor.build_repo_client_with_username",
                            lambda r, verify_ssl: built.append(1) or (None, None))
        executor.gitlab = SimpleNamespace(
            probe=lambda: (_ for _ in ()).throw(GitLabError("资源不存在（404）", 404)))

        with pytest.raises(GitLabError):
            executor._call_with_fallback({"name": "demo"},
                                         lambda c: c.probe())
        assert built == []

    def test_call_with_fallback_401_without_remote_token_raises(self,
                                                                executor,
                                                                monkeypatch):
        """全局 401 且仓库 remote 无可用 token：抛出原 401 错误。"""
        monkeypatch.setattr("botler.executor.build_repo_client_with_username",
                            lambda r, verify_ssl: (None, None))
        executor.gitlab = SimpleNamespace(probe=self._boom)

        with pytest.raises(GitLabError):
            executor._call_with_fallback({"name": "demo"},
                                         lambda c: c.probe())

    def test_finish_failed_401_falls_back_comment_and_labels(self, executor,
                                                             monkeypatch,
                                                             tmp_path):
        """失败收尾：全局 401 时评论与 bot-failed 标签经 remote token 发出。"""
        db = executor.db
        repo_id = _mk_repo(db)
        repo = db.get_repo_by_project_id(42)
        task_id = _mk_task(db, repo_id)
        calls = []
        fallback = SimpleNamespace(
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)))
        self._install(executor, monkeypatch, tmp_path, fallback)
        db.claim_task(task_id)

        executor._finish_failed(task_id, "重试耗尽", repo=repo)

        assert any("无法完成此 issue" in a[2]
                   for kind, a in calls if kind == "comment")
        assert ("labels", (42, 1, ["bot-failed"])) in calls

    def test_finish_succeeded_401_falls_back_bot_done_label(self, executor,
                                                            monkeypatch,
                                                            tmp_path):
        """成功收尾：全局 401 时 bot-done 标签经 remote token 打出。"""
        db = executor.db
        repo_id = _mk_repo(db)
        repo = db.get_repo_by_project_id(42)
        task_id = _mk_task(db, repo_id)
        calls = []
        fallback = SimpleNamespace(
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: None,
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            last_note_author_id=lambda pid, iid: None,
            get_bot_id=lambda: 7)
        self._install(executor, monkeypatch, tmp_path, fallback)
        db.claim_task(task_id)

        executor._finish_succeeded(task_id, "ok", repo=repo)

        assert ("labels", (42, 1, ["bot-done"])) in calls
        assert any("任务已完成" in a[2]
                   for kind, a in calls if kind == "comment")
        assert db.get_task(task_id)["status"] == "succeeded"


class TestResumePromptTemplate:
    """_resume_prompt 渲染来源（issue #116）：内置默认 + config 自定义。

    中断恢复引导语改为从 config（templates.resume）读取，未配置/清空时
    回退内置默认；claude/hermes/dsh 三引擎共用 _resume_prompt 统一入口。
    """

    def test_resume_prompt_uses_builtin_default(self, executor):
        """未配置自定义模版时用内置默认，占位符正常替换。"""
        prompt = executor._resume_prompt(_REPO, _issue_dict())
        assert "继续处理（中断恢复）" in prompt
        assert "demo" in prompt          # {repo_name}
        assert "#7" in prompt            # {issue_iid}
        assert "标题" in prompt          # {issue_title}
        assert "https://gitlab.example.com/x/-/issues/7" in prompt  # {issue_url}

    def test_resume_prompt_uses_configured_template(self, executor):
        """配置自定义恢复模版后渲染使用自定义文本。"""
        executor.config.update_section("templates", {"resume": "恢复 {repo_name} #{issue_iid}，继续。"})
        prompt = executor._resume_prompt(_REPO, _issue_dict())
        assert prompt == "恢复 demo #7，继续。"

    def test_resume_prompt_blank_config_falls_back_to_builtin(self, executor):
        """自定义被清空后回退内置默认。"""
        executor.config.update_section("templates", {"resume": "临时自定义"})
        executor.config.update_section("templates", {"resume": "   "})
        prompt = executor._resume_prompt(_REPO, _issue_dict())
        assert "继续处理（中断恢复）" in prompt


class TestCaptureEnvSnapshot:
    """任务执行环境快照（issue #276）：_capture_env_snapshot 采集落库。"""

    def _mk_git_workdir(self, tmp_path, name: str = "demo") -> Path:
        """在 workspace_root 下构造真实 git 仓库工作区，返回 workdir。"""
        workdir = tmp_path / "workspace" / name
        workdir.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(workdir)], check=True)
        subprocess.run(["git", "-C", str(workdir), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(workdir), "config", "user.name", "t"],
                       check=True)
        subprocess.run(["git", "-C", str(workdir), "commit", "--allow-empty",
                        "-m", "init"], check=True)
        return workdir

    def test_capture_writes_environment_json(self, executor, monkeypatch, tmp_path):
        """采集成功：tasks.environment 落库 JSON（引擎/起始提交/配置 hash）。"""
        workdir = self._mk_git_workdir(tmp_path)
        monkeypatch.setattr(
            "botler.env_snapshot.detect_tool",
            lambda tool, timeout: {"version": "2.1.226", "installed": True})
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=31)
        executor._capture_env_snapshot(task_id, workdir)
        env = json.loads(db.get_task(task_id)["environment"])
        assert env["engine"] == {"name": "claude", "version": "2.1.226"}
        assert env["git"]["branch"]
        assert len(env["git"]["commit_sha"]) == 40
        assert env["config_hash"]
        assert env["captured_at"]

    def test_capture_only_once(self, executor, monkeypatch, tmp_path):
        """只采一次：重复调用不覆盖首次快照（重试/断点续跑语义）。"""
        workdir = self._mk_git_workdir(tmp_path)
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=32)
        first = {"engine": {"name": "claude", "version": "1.0.0"},
                 "git": {"branch": "main", "commit_sha": "x"}}
        db.set_task_status(task_id, None,
                           environment=json.dumps(first, ensure_ascii=False))
        called = []
        monkeypatch.setattr(
            "botler.executor.collect_env_snapshot",
            lambda **kw: called.append(1) or {"engine": {"name": "claude"}})
        executor._capture_env_snapshot(task_id, workdir)
        assert called == []  # 已有快照 → 不再采集
        assert json.loads(db.get_task(task_id)["environment"]) == first

    def test_capture_failure_writes_error_marker(self, executor, monkeypatch,
                                                 tmp_path):
        """采集抛异常：落库「环境快照获取失败」标记，任务照常执行（不抛）。"""
        workdir = self._mk_git_workdir(tmp_path)
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=33)

        def boom(**kw):
            raise RuntimeError("采集链路故障")
        monkeypatch.setattr("botler.executor.collect_env_snapshot", boom)
        executor._capture_env_snapshot(task_id, workdir)  # 不应抛出
        env = json.loads(db.get_task(task_id)["environment"])
        assert env["error"] == "环境快照获取失败"

    def test_capture_persist_failure_does_not_raise(self, executor, monkeypatch,
                                                    tmp_path):
        """落库失败（db 异常）不阻塞任务执行。"""
        workdir = self._mk_git_workdir(tmp_path)
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=34)
        monkeypatch.setattr(
            "botler.executor.collect_env_snapshot",
            lambda **kw: {"engine": {"name": "claude"}})
        monkeypatch.setattr(db, "set_task_status",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        executor._capture_env_snapshot(task_id, workdir)  # 不应抛出


class TestRunTaskTransientIssueFetch:
    """issue #280：任务启动拉取 issue 遇 GitLab 瞬时故障（502 等）不应立即判失败。

    08-17 生产事故：GitLab 短暂不可用返回 502，44 个排队任务 get_issue
    一次 502 即全部打成 failed，且失败评论同样发不出。修复后按指数退避
    重试（ISSUE_FETCH_MAX_ATTEMPTS 次），重试耗尽或非瞬时错误才判失败。
    """

    def _install(self, executor, monkeypatch, tmp_path, get_issue):
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.gitlab = SimpleNamespace(
            get_issue=get_issue,
            add_comment=lambda *a, **k: {"id": 1},
            add_labels=lambda *a, **k: {},
        )
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        return task_id

    def test_transient_502_retried_then_proceeds(self, executor, monkeypatch, tmp_path):
        """前两次 get_issue 502、第三次成功 → 任务继续执行（不以获取失败收尾）。"""
        from botler.executor import ExecutorError, ISSUE_FETCH_MAX_ATTEMPTS
        calls = {"n": 0}

        def flaky_get_issue(pid, iid):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise GitLabError("GitLab API 错误 502: boom", 502)
            return {"state": "opened"}

        def run_once(*a, **k):
            # 让执行阶段失败（进入重试分支），仅验证「已越过 issue 拉取阶段」
            raise ExecutorError("boom")

        task_id = self._install(executor, monkeypatch, tmp_path, flaky_get_issue)
        monkeypatch.setattr(executor, "_run_once", run_once)
        executor.run_task(task_id)

        task = executor.db.get_task(task_id)
        assert calls["n"] == 3, "502 应重试到成功为止"
        assert task["status"] == "failed"
        assert "获取 issue" not in task["error_message"], \
            "瞬时故障重试成功不应以「获取 issue 失败」收尾"
        assert ISSUE_FETCH_MAX_ATTEMPTS >= 3

    def test_transient_always_502_fails_after_attempts(self, executor, monkeypatch, tmp_path):
        """持续 502 → 重试耗尽后判失败，原因含获取 issue 失败。"""
        from botler.executor import ISSUE_FETCH_MAX_ATTEMPTS
        calls = {"n": 0}

        def always_502(pid, iid):
            calls["n"] += 1
            raise GitLabError("GitLab API 错误 502: boom", 502)

        task_id = self._install(executor, monkeypatch, tmp_path, always_502)
        executor.run_task(task_id)

        task = executor.db.get_task(task_id)
        assert calls["n"] == ISSUE_FETCH_MAX_ATTEMPTS, "应重试到次数上限"
        assert task["status"] == "failed"
        assert "获取 issue" in task["error_message"]

    def test_permanent_404_fails_immediately(self, executor, monkeypatch, tmp_path):
        """404 非瞬时 → 只请求一次即判失败，不重试。"""
        calls = {"n": 0}

        def always_404(pid, iid):
            calls["n"] += 1
            raise GitLabError("资源不存在（404）: /projects/42/issues/1", 404)

        task_id = self._install(executor, monkeypatch, tmp_path, always_404)
        executor.run_task(task_id)

        task = executor.db.get_task(task_id)
        assert calls["n"] == 1
        assert task["status"] == "failed"
        assert "获取 issue" in task["error_message"]


class TestFinishFailedTransientRetry:
    """issue #280：失败收尾评论/标签遇瞬时故障应退避重试，确保用户收到反馈。

    08-17 事故中「没有任何的回复和评论」的直接原因：失败评论与 bot-failed
    标签调用各试一次即遇 502 失败放弃。修复后瞬时故障按指数退避重试。
    """

    def _mk_running_task(self, executor, tmp_path):
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.set_task_status(task_id, "running")
        return task_id

    def test_failure_comment_retried_on_transient_502(self, executor, monkeypatch, tmp_path):
        """add_comment 前两次 502、第三次成功 → 失败评论最终发出。"""
        task_id = self._mk_running_task(executor, tmp_path)
        comment_calls = {"n": 0}

        def add_comment(pid, iid, body):
            comment_calls["n"] += 1
            if comment_calls["n"] <= 2:
                raise GitLabError("GitLab API 错误 502: boom", 502)
            return {"id": 1}

        executor.gitlab = SimpleNamespace(add_comment=add_comment,
                                          add_labels=lambda *a, **k: {})
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        executor._finish_failed(task_id, "测试失败原因")

        task = executor.db.get_task(task_id)
        assert task["status"] == "failed"
        assert comment_calls["n"] == 3, "失败评论应重试到成功"
        logs = executor.db.list_logs(task_id)
        assert any("已在 issue 上留失败评论" in l["message"] for l in logs)

    def test_failure_comment_permanent_error_no_retry(self, executor, monkeypatch, tmp_path):
        """403 非瞬时 → 失败评论只试一次即放弃。"""
        task_id = self._mk_running_task(executor, tmp_path)
        comment_calls = {"n": 0}

        def add_comment(pid, iid, body):
            comment_calls["n"] += 1
            raise GitLabError("权限不足（403）: denied", 403)

        executor.gitlab = SimpleNamespace(add_comment=add_comment,
                                          add_labels=lambda *a, **k: {})
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        executor._finish_failed(task_id, "测试失败原因")

        task = executor.db.get_task(task_id)
        assert task["status"] == "failed"
        assert comment_calls["n"] == 1, "403 永久性错误不应重试"


# ---- issue #302：dsh 执行前会话根目录 zstd 编码归一化接线 ----

class TestDshSessionRootNormalizationWiring:
    """dsh 引擎执行前把会话根目录遗留明文 session.jsonl 归一化到 zstd。

    真实调用 _run_dsh_once（stub prepare_workspace 与 DshRunner），
    预置旧版部署遗留的明文会话文件，断言：归一化真实执行（明文删除、
    zstd 生成）且任务日志记录；根因见 botler/dsh_sessions.py 模块注释
    （runtime 根级编码检查遇明文 artifact 拒绝启动，任务 #415 反复失败）。
    """

    def test_run_dsh_once_normalizes_legacy_session_root(
            self, executor, monkeypatch, tmp_path):
        import botler.executor as executor_module
        from botler import dsh_sessions

        workdir = tmp_path / "workspace" / "demo"
        workdir.mkdir(parents=True, exist_ok=True)
        # 旧版部署遗留的明文会话文件（runtime zstd 模式下根级编码检查
        # 会拒绝启动的形态）
        session_dir = workdir / ".sessions" / "--proj--" / "sess-legacy"
        session_dir.mkdir(parents=True)
        plain = session_dir / "session.jsonl"
        plain.write_bytes(
            b'{"type":"session","version":0,"id":"sess-legacy",'
            b'"createdAt":1,"cwd":"/x","delegationDepth":0}\n'
            b'{"type":"agent/inbox/spliced","seq":0,"time":1,"data":{}}\n')

        def fake_prepare(repo, resume=False):
            return workdir, {}

        class FakeDshRunner:
            def __init__(self, **kw):
                self.on_line = kw["on_line"]
                self.session_id = kw.get("session_id")
                self.usage = None

            def start(self):
                self.on_line(json.dumps({
                    "final_response": "已处理",
                    "finish_reason": "completed",
                    "session_id": self.session_id or "sid-new"},
                    ensure_ascii=False))

            def done(self):
                return True

            def stop(self):
                pass

            def finish(self, join_timeout: float = 60.0):
                return 0

        monkeypatch.setattr(executor_module, "DshRunner", FakeDshRunner)
        monkeypatch.setattr(executor, "prepare_workspace", fake_prepare)
        executor.config.get().engine = "dsh"

        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        repo = {"name": "demo", "prompt_template": None,
                "url": "https://gitlab.example.com/group/demo.git"}
        issue = {"state": "opened", "title": "标题", "description": "正文",
                 "web_url": "https://gitlab.example.com/x/-/issues/7",
                 "project_id": 42, "iid": 7}

        exit_code, _output = executor._run_dsh_once(task_id, repo, issue)

        assert exit_code == 0
        # 遗留明文被转换删除、zstd artifact 生成（真实归一化逻辑生效）
        assert not plain.exists()
        zstd_path = session_dir / dsh_sessions.SESSION_ARTIFACT_ZSTD
        assert zstd_path.is_file()
        # 任务日志记录了归一化
        logs = db.list_logs(task_id)
        assert any("归一化到 zstd 压缩" in row["message"] for row in logs)
        assert any("遗留明文 session.jsonl" in row["message"] for row in logs)

    def test_run_dsh_once_skips_log_when_no_legacy(
            self, executor, monkeypatch, tmp_path):
        """根目录无明文遗留时不写归一化日志（正常路径不刷屏）。"""
        import botler.executor as executor_module

        workdir = tmp_path / "workspace" / "demo"
        workdir.mkdir(parents=True, exist_ok=True)

        def fake_prepare(repo, resume=False):
            return workdir, {}

        class FakeDshRunner:
            def __init__(self, **kw):
                self.on_line = kw["on_line"]
                self.session_id = kw.get("session_id")
                self.usage = None

            def start(self):
                self.on_line(json.dumps({
                    "final_response": "已处理",
                    "finish_reason": "completed",
                    "session_id": self.session_id or "sid-new"},
                    ensure_ascii=False))

            def done(self):
                return True

            def stop(self):
                pass

            def finish(self, join_timeout: float = 60.0):
                return 0

        monkeypatch.setattr(executor_module, "DshRunner", FakeDshRunner)
        monkeypatch.setattr(executor, "prepare_workspace", fake_prepare)
        executor.config.get().engine = "dsh"

        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        repo = {"name": "demo", "prompt_template": None,
                "url": "https://gitlab.example.com/group/demo.git"}
        issue = {"state": "opened", "title": "标题", "description": "正文",
                 "web_url": "https://gitlab.example.com/x/-/issues/7",
                 "project_id": 42, "iid": 7}

        exit_code, _output = executor._run_dsh_once(task_id, repo, issue)

        assert exit_code == 0
        logs = db.list_logs(task_id)
        assert not any("归一化到 zstd 压缩" in row["message"] for row in logs)
