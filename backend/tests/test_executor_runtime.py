"""生产环境任务运行失败回归测试（部署后任务一直失败，pm2 日志 task_7/8/9）。

现象：claude 每次执行都因 permission_denials 全部操作被拒，Claude 自行
终止（exit 0）→ 重试耗尽 → 任务 failed。
根因 1：claude -p 无头模式未加 --dangerously-skip-permissions，权限系统
        拦截 Bash/Read/MCP 等一切操作，无人值守下无法交互授权；
根因 2：claude 无 stdin 时 stderr 打印 "Warning: no stdin data received
        ..."（executor 把 stderr 合并进 stdout），_extract_session_id
        对整个输出 json.loads 失败 → session_id 永不落库 → 断点续跑失效。
"""

import json

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


class TestClaudeSkipPermissions:
    """claude -p 无头模式必须带 --dangerously-skip-permissions（无人值守）。"""

    def test_run_once_cmd_includes_skip_permissions(self, executor, monkeypatch, tmp_path):
        """_run_once 构造的 claude 命令必须含 --dangerously-skip-permissions；
        否则 GIT_ASKPASS/GITLAB_TOKEN 只解决凭据，Bash/curl/Read 等操作
        仍被权限系统拒绝（与 task_7/8 日志的 permission_denials 一致）。"""
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(json.dumps({"result": "ok", "session_id": "s1"}), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        executor._run_once(1, {"name": "demo", "prompt_template": None}, {"project_id": 42, "iid": 7})

        assert "--dangerously-skip-permissions" in captured["cmd"]

    def test_run_once_resume_keeps_skip_permissions(self, executor, monkeypatch, tmp_path):
        """断点续跑（--resume）时同样保留 --dangerously-skip-permissions。"""
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(json.dumps({"result": "ok", "session_id": "s2"}), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        monkeypatch.setattr(executor, "_session_file", lambda sid: tmp_path / "x.jsonl")

        executor._run_once(1, {"name": "demo", "prompt_template": None}, {"project_id": 42, "iid": 7}, "resume-sid")

        assert "--dangerously-skip-permissions" in captured["cmd"]
        assert "--resume" in captured["cmd"]


class TestExtractWithStderrPrefix:
    """claude 无 stdin 警告（stderr）混入 stdout 时的容错。

    真实日志 task_8.log 第一行：Warning: no stdin data received in 3s,
    proceeding without it... 后接 JSON。executor 将 stderr 合并进 stdout，
    直接对整个 output json.loads 必然失败。
    """

    WARNING = ("Warning: no stdin data received in 3s, proceeding without it. "
               "If piping from a slow command, redirect stdin explicitly: "
               "< /dev/null to skip, or wait longer.\n")

    def test_extract_session_id_tolerates_warning_prefix(self, executor):
        """带警告前缀的输出仍能解析出 session_id（修复前返回 None，续跑失效）。"""
        output = self.WARNING + json.dumps({"result": "ok", "session_id": "sid-warn"})
        assert executor._extract_session_id(output) == "sid-warn"

    def test_extract_error_tolerates_warning_prefix(self, executor):
        """带警告前缀的输出仍能提取 result 中的错误信息。"""
        output = self.WARNING + json.dumps({"result": "处理失败: 构建超时"})
        assert "构建超时" in executor._extract_error(output)

    def test_run_once_persists_session_id_with_warning(self, executor, monkeypatch, tmp_path):
        """端到端：真实带警告前缀的输出，执行后 session_id 必须落库。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "失败任务")

        def fake_popen(cmd, **kwargs):
            return _FakeProc(self.WARNING + json.dumps(
                {"result": "ok", "session_id": "sid-warn"}), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        executor._run_once(task_id, {"name": "demo", "prompt_template": None},
                           {"project_id": 42, "iid": 7})

        assert db.get_task(task_id)["claude_session_id"] == "sid-warn"
