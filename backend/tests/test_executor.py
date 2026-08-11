"""ClaudeExecutor 详细失败原因测试：error_detail 落库、错误提取（trace）。

issue #4 新增「查看详细原因」按钮依赖 executor 在失败时把每次尝试的
退出码 + 提取的错误/trace 序列化写入 tasks.error_detail 字段。
issue #8 会话断点续跑：claude --resume 恢复上次会话 + 保留工作区。
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
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
        db.set_task_status(task_id, "running", claude_session_id=session_id)
        self._mk_session_file(tmp_path / "claude-home", session_id)
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": session_id}))
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("closed"),  # 一次尝试即成功
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
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
        db.set_task_status(task_id, "running", claude_session_id="ghost-sid")
        captured = self._session_toolkit(
            executor, monkeypatch, tmp_path,
            json.dumps({"result": "ok", "session_id": "s-fresh"}))
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("opened"),
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
        )

        executor.run_task(task_id)

        assert "--resume" not in captured["cmd"]
        # 无效 session_id 被清除，新会话 id 落库
        assert db.get_task(task_id)["claude_session_id"] == "s-fresh"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("降级" in m for m in logs)
