"""ClaudeExecutor 详细失败原因测试：error_detail 落库、错误提取（trace）。

issue #4 新增「查看详细原因」按钮依赖 executor 在失败时把每次尝试的
退出码 + 提取的错误/trace 序列化写入 tasks.error_detail 字段。
issue #8 会话断点续跑：claude --resume 恢复上次会话 + 保留工作区。
"""

import io
import itertools
import json
import sqlite3
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

        calls = self._install_mocks(executor, monkeypatch, tmp_path, run_once=boom)
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


# ---- issue #8 会话断点续跑 ----

class _FakeStdout:
    """一次输出 + EOF；EOF 后 poll() 视为进程已退出。"""

    def __init__(self, text: str):
        self._lines = [text] if text else []

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class _FakeProc:
    def __init__(self, output: str, exit_code: int = 0):
        self.stdout = _FakeStdout(output)
        self._exit = exit_code

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit


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
        captured = self._session_toolkit(
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
            add_labels=lambda *a, **k: None)
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
            add_labels=lambda *a, **k: None)
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
            add_labels=lambda *a, **k: None)
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
                (pid, iid, labels, remove)))
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
                GitLabError("GitLab API 错误 500: boom", 500)))
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
                (pid, iid, labels, remove)))
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        db.claim_task(task_id)
        db.finish_task(task_id, "succeeded")  # 模拟其他实例已收尾
        executor._finish_succeeded(task_id, "ok")

        assert labels_calls == []


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
            get_latest_pipeline=self._boom, get_pipeline=self._boom)
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
        repo = db.get_repo_by_project_id(42)
        task_id = _mk_task(db, repo_id)
        output = json.dumps({"result": "开发完成，已推送代码"},
                            ensure_ascii=False)
        calls = []
        fallback = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("opened"),
            add_comment=lambda *a, **k: calls.append(("comment", a)),
            add_labels=lambda *a, **k: calls.append(("labels", a)),
            find_commit_for_issue=lambda pid, iid: None,
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
            find_commit_for_issue=lambda pid, iid: None)
        self._install(executor, monkeypatch, tmp_path, fallback)
        db.claim_task(task_id)

        executor._finish_succeeded(task_id, "ok", repo=repo)

        assert ("labels", (42, 1, ["bot-done"])) in calls
        assert db.get_task(task_id)["status"] == "succeeded"
