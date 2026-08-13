"""ClaudeExecutor hermes 引擎测试（issue #47：集成 hermes）。

覆盖：引擎分派（worker.engine 校验与回退）、hermes 子进程命令/环境构造、
stdin 协议（prompt/history/session_id）、结果判定（成功 / 无法解决 /
非 0 退出 / 非 JSON 输出）、conversation_history 落库与断点续跑恢复、
以及 engine=claude 时现有路径不受影响。
"""

import io
import json
from pathlib import Path

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
worker: {worker}
claude: {{}}
hermes: {hermes}
templates: {{}}
repos: []
"""

# 直接传 _run_once 的 repo 字典（_build_prompt 需 prompt_template 键）
_REPO = {"name": "demo", "prompt_template": None}

_ISSUE = {"state": "opened", "title": "标题", "description": "正文",
          "web_url": "https://gitlab.example.com/x/-/issues/7",
          "project_id": 42, "iid": 7}

# hermes runner 成功输出样例
_HERMES_OUTPUT = json.dumps({
    "final_response": "已修复并推送，issue #7 处理完成",
    "messages": [{"role": "user", "content": "任务"},
                 {"role": "assistant", "content": "完成"}],
    "session_id": "hermes-sess-1",
    "error": None,
})


def _mk_config(tmp_path, worker_extra="{}", hermes_extra="{}") -> ConfigManager:
    """worker_extra / hermes_extra 为整段子键文本（非空时需自带前置换行）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.format(worker=worker_extra, hermes=hermes_extra),
        encoding="utf-8")
    return ConfigManager(str(config_path))


def _mk_executor(tmp_path, config: ConfigManager) -> ClaudeExecutor:
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


@pytest.fixture
def executor(tmp_path):
    config = _mk_config(tmp_path)
    return _mk_executor(tmp_path, config)


@pytest.fixture
def hermes_executor(tmp_path):
    """engine=hermes + hermes command/args 的 executor。"""
    config = _mk_config(
        tmp_path, worker_extra="\n  engine: hermes",
        hermes_extra="\n  command: /opt/hermes/venv/bin/python"
                     "\n  args: [\"/app/backend/hermes_runner.py\"]")
    return _mk_executor(tmp_path, config)


class _FakeStdout:
    """一次输出 + EOF；EOF 后 poll() 视为进程已退出（与 test_executor_runtime 同模式）。"""

    def __init__(self, text: str):
        self._lines = [text] if text else []

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _FakeStdin:
    """捕获 runner 请求的假 stdin；close 为 no-op（close 后仍可读回内容）。"""

    def __init__(self):
        self._buf = io.StringIO()

    def write(self, text):
        return self._buf.write(text)

    def close(self):
        pass

    def getvalue(self):
        return self._buf.getvalue()


class _FakeProc:
    def __init__(self, output: str, exit_code: int = 0):
        self.stdout = _FakeStdout(output)
        self._exit = exit_code
        self.stdin = _FakeStdin()

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit


def _patch_run(monkeypatch, executor, tmp_path, output: str, exit_code: int = 0):
    """替换 Popen 为假进程并捕获启动参数，返回 captured 列表。"""
    captured = {}

    def fake_popen(cmd, **kwargs):
        proc = _FakeProc(output, exit_code)
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        captured["proc"] = proc
        return proc

    monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
    monkeypatch.setattr(executor, "prepare_workspace",
                        lambda repo, resume=False: (
                            tmp_path / "work", {"GIT_ASKPASS": "/askpass"}))
    monkeypatch.setattr(executor, "_log_file",
                        lambda tid: tmp_path / f"task_{tid}.log")
    return captured


def _stdin_request(captured) -> dict:
    """取 executor 写给 hermes runner stdin 的请求 JSON。"""
    return json.loads(captured["proc"].stdin.getvalue())


class TestEngine:
    """_engine：引擎名读取与回退。"""

    def test_default_engine_is_claude(self, executor):
        assert executor._engine(executor.config.get()) == "claude"

    def test_engine_hermes(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: hermes")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "hermes"

    def test_unknown_engine_falls_back_to_claude(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: gpt5")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "claude"


class TestRunHermesOnce:
    """_run_once 的 hermes 引擎路径：命令、环境、stdin 协议、结果解析。"""

    def test_command_from_hermes_config(self, executor, monkeypatch, tmp_path):
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: hermes",
            hermes_extra="\n  command: /opt/hermes/venv/bin/python"
                         "\n  args: [\"/app/backend/hermes_runner.py\"]")
        ex = _mk_executor(tmp_path, config)
        captured = _patch_run(monkeypatch, ex, tmp_path, _HERMES_OUTPUT)
        code, output = ex._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert captured["cmd"] == ["/opt/hermes/venv/bin/python",
                                   "/app/backend/hermes_runner.py"]

    def test_stdin_carries_prompt_only_for_fresh_run(
            self, hermes_executor, monkeypatch, tmp_path):
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        hermes_executor._run_once(1, _REPO, _ISSUE)
        # 全新执行：stdin 只有 prompt（渲染后的任务模板），无 history / session_id
        request = _stdin_request(captured)
        assert "AI 维护者" in request["prompt"]  # DEFAULT_TEMPLATE 渲染产物
        assert request.get("history") is None
        assert request.get("session_id") is None

    def test_env_carries_workdir_and_git_credentials(
            self, hermes_executor, monkeypatch, tmp_path):
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        hermes_executor._run_once(1, _REPO, _ISSUE)
        env = captured["kwargs"]["env"]
        # hermes terminal 工具在 botler 工作区执行（TERMINAL_CWD）
        assert env["TERMINAL_CWD"] == str(tmp_path / "work")
        # git 凭据注入继承 _build_env（GIT_ASKPASS）
        assert env["GIT_ASKPASS"]
        # 子进程 cwd 即仓库工作区
        assert captured["kwargs"]["cwd"] == tmp_path / "work"

    def test_result_parsed_from_json_output(
            self, hermes_executor, monkeypatch, tmp_path):
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        code, output = hermes_executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert output == _HERMES_OUTPUT

    def test_non_json_output_passthrough(
            self, hermes_executor, monkeypatch, tmp_path):
        """runner 输出非 JSON（如异常前的 stderr 噪声）→ 原样返回，判定交给上层。"""
        _patch_run(monkeypatch, hermes_executor, tmp_path, "oops\n", exit_code=1)
        code, output = hermes_executor._run_once(1, _REPO, _ISSUE)
        assert code == 1
        assert "oops" in output

    def test_claude_engine_untouched(self, executor, monkeypatch, tmp_path):
        """engine=claude（默认）时命令仍是 claude CLI（回归保护）。"""
        captured = _patch_run(monkeypatch, executor, tmp_path,
                              json.dumps({"result": "ok",
                                          "session_id": "s1"}))
        code, _ = executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert captured["cmd"][0] == "claude"
        assert "--dangerously-skip-permissions" in captured["cmd"]


class TestHermesResume:
    """断点续跑：hermes_history 落库与恢复（Q3-B 等价实现）。"""

    def test_history_persisted_after_run(
            self, hermes_executor, monkeypatch, tmp_path):
        _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        hermes_executor._run_once(1, _REPO, _ISSUE)
        history = hermes_executor._hermes_history_from_output(_HERMES_OUTPUT)
        assert history == [{"role": "user", "content": "任务"},
                           {"role": "assistant", "content": "完成"}]

    def test_resume_reads_history_and_passes_to_stdin(
            self, hermes_executor, monkeypatch, tmp_path):
        """显式传 resume_history 时：RESUME 提示 + history + 保留工作区。"""
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        history = [{"role": "user", "content": "上次任务"}]
        resumed = []

        def fake_prepare(repo, resume=False):
            resumed.append(resume)
            return tmp_path / "work", {"GIT_ASKPASS": "/askpass"}

        monkeypatch.setattr(hermes_executor, "prepare_workspace", fake_prepare)
        hermes_executor._run_once(1, _REPO, _ISSUE, resume_history=history)
        assert resumed == [True]  # 恢复模式：工作区不清空
        request = _stdin_request(captured)
        assert request["history"] == history
        assert "继续处理" in request["prompt"]  # RESUME_PROMPT 渲染

    def _mk_task(self, hermes_executor) -> int:
        """创建任务记录（hermes_history 落库需要真实 task 行）。"""
        db = hermes_executor.db
        db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        repo_id = db.get_repo_by_project_id(42)["id"]
        return db.create_task(repo_id, 42, 7, "标题")

    def test_resume_from_persisted_task_history(
            self, hermes_executor, monkeypatch, tmp_path):
        """任务落库的 hermes_history：_run_once 内部解析后恢复（含会话 id）。"""
        task_id = self._mk_task(hermes_executor)
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        hermes_executor.db.set_task_status(
            task_id, None, hermes_history=json.dumps(
                {"session_id": "sess-99",
                 "messages": [{"role": "user", "content": "旧任务"}]}))
        hermes_executor._run_once(task_id, _REPO, _ISSUE)
        request = _stdin_request(captured)
        assert request["history"] == [{"role": "user", "content": "旧任务"}]
        assert request["session_id"] == "sess-99"

    def test_corrupt_persisted_history_falls_back_to_fresh(
            self, hermes_executor, monkeypatch, tmp_path):
        """落库历史损坏（非 JSON）→ 降级全新会话（不抛异常）。"""
        task_id = self._mk_task(hermes_executor)
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        hermes_executor.db.set_task_status(task_id, None, hermes_history="{{{bad json")
        hermes_executor._run_once(task_id, _REPO, _ISSUE)
        request = _stdin_request(captured)
        assert request.get("history") is None

    def test_fresh_run_cleans_workspace(
            self, hermes_executor, monkeypatch, tmp_path):
        """无 history 的全新执行：工作区重置（与 claude 引擎一致）。"""
        captured = _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        resumed = []
        monkeypatch.setattr(hermes_executor, "prepare_workspace",
                            lambda repo, resume=False: (
                                resumed.append(resume) or (
                                    tmp_path / "work", {"GIT_ASKPASS": "/x"})))
        hermes_executor._run_once(1, _REPO, _ISSUE)
        assert resumed == [False]

    def test_history_from_output_missing_messages(self):
        """输出无 messages 字段 → 空列表（不抛异常）。"""
        from botler.executor import _load_json_output
        executor = ClaudeExecutor.__new__(ClaudeExecutor)  # 静态方法无需初始化
        output = json.dumps({"final_response": "ok", "error": None})
        assert executor._hermes_history_from_output(output) == []

    def test_history_from_output_non_json(self):
        executor = ClaudeExecutor.__new__(ClaudeExecutor)
        assert executor._hermes_history_from_output("garbage") == []


class TestHermesResultJudgement:
    """结果判定：成功 / 无法解决 / 失败重试（hermes 输出语义）。"""

    def test_success_accepts_json_result(
            self, hermes_executor, monkeypatch, tmp_path):
        _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT)
        code, output = hermes_executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert hermes_executor._hermes_result(output) == "success"

    def test_unresolvable_detected_from_final_response(
            self, hermes_executor, monkeypatch, tmp_path):
        output = json.dumps({
            "final_response": "抱歉，无法解决该 issue：依赖缺失",
            "messages": [], "session_id": "s2", "error": None})
        assert hermes_executor._hermes_result(output) == "unresolvable"

    def test_non_zero_exit_passthrough(
            self, hermes_executor, monkeypatch, tmp_path):
        """exit 非 0 → 原样透传，由 run_task 按失败重试（判定只看 exit 0 输出）。"""
        _patch_run(monkeypatch, hermes_executor, tmp_path, _HERMES_OUTPUT, exit_code=1)
        code, output = hermes_executor._run_once(1, _REPO, _ISSUE)
        assert code == 1
        assert output == _HERMES_OUTPUT

    def test_error_field_marks_failure(
            self, hermes_executor, monkeypatch, tmp_path):
        """runner 输出 error 字段（协议标记错误）→ 判定 failed。"""
        output = json.dumps({"final_response": "", "messages": [],
                             "session_id": "s3", "error": "agent 崩溃"})
        _patch_run(monkeypatch, hermes_executor, tmp_path, output, exit_code=1)
        hermes_executor._run_once(1, _REPO, _ISSUE)
        assert hermes_executor._hermes_result(output) == "failed"

    def test_non_json_output_is_failure(
            self, hermes_executor, monkeypatch, tmp_path):
        _patch_run(monkeypatch, hermes_executor, tmp_path, "no json here", exit_code=0)
        code, output = hermes_executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert hermes_executor._hermes_result(output) == "failed"

    def test_empty_final_response_is_failure(
            self, hermes_executor, monkeypatch, tmp_path):
        """final_response 为空串 → failed（不允许空回复静默成功）。"""
        output = json.dumps({"final_response": "", "messages": [],
                             "session_id": "s4", "error": None})
        assert hermes_executor._hermes_result(output) == "failed"
