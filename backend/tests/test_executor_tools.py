"""executor MCP 工具注入测试（issue #172）。

覆盖：
- _inject_mcp_tools：启用工具 → 工作区 .mcp.json 写入 + 日志；
  无启用工具 → 清理残留 + 「跳过注入」日志；异常不阻塞（记 warn）。
- _run_claude_once：prepare_workspace 之后、claude 启动之前注入
  .mcp.json（fake Popen 捕获 cwd 验证注入文件存在）。
"""

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
    gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "ws"
    (workdir / ".git" / "info").mkdir(parents=True)
    return workdir


def _add_tool(executor, name: str, enabled: bool = True,
              kind: str = "stdio") -> int:
    from botler import tools
    definition = {
        "name": name, "description": "工具", "kind": kind,
        "command": "python3", "args": ["-m", "demo"], "env": {},
        "url": "https://x.example/mcp" if kind != "stdio" else "",
    }
    tool = tools.create_tool(executor.db, definition)
    if not enabled:
        tools.set_tool_enabled(executor.db, tool["id"], False)
    return tool["id"]


class TestInjectMcpTools:
    def test_injects_when_enabled(self, executor, tmp_path):
        _add_tool(executor, "srv")
        workdir = _mk_workdir(tmp_path)
        executor._inject_mcp_tools(1, workdir)
        payload = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
        assert payload["mcpServers"]["srv"]["command"] == "python3"
        logs = executor.db.list_logs(1)
        assert any("MCP 工具已注入" in l["message"] for l in logs)

    def test_skips_when_none_enabled(self, executor, tmp_path):
        _add_tool(executor, "off", enabled=False)
        workdir = _mk_workdir(tmp_path)
        executor._inject_mcp_tools(1, workdir)
        assert not (workdir / ".mcp.json").exists()
        logs = executor.db.list_logs(1)
        assert any("跳过注入" in l["message"] for l in logs)

    def test_inject_failure_does_not_block(self, executor, tmp_path, monkeypatch):
        _add_tool(executor, "srv")
        workdir = _mk_workdir(tmp_path)
        monkeypatch.setattr(
            "botler.tools.write_workspace_mcp_config",
            lambda db, wd: (_ for _ in ()).throw(RuntimeError("boom")))
        # 不应抛异常
        executor._inject_mcp_tools(1, workdir)
        logs = executor.db.list_logs(1)
        assert any("注入失败" in l["message"] for l in logs)


class TestRunClaudeInject:
    def test_run_claude_once_injects_mcp(self, executor, monkeypatch, tmp_path):
        """_run_claude_once 在启动 claude 前注入 .mcp.json（fake Popen）。"""
        _add_tool(executor, "demo-tool")
        captured = {}

        class _FakeProc:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["cwd"] = kwargs.get("cwd")
                self.stdout = None
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def communicate(self, input=None, timeout=None):
                return "{}", ""

        monkeypatch.setattr("botler.executor.subprocess.Popen", _FakeProc)
        workdir = _mk_workdir(tmp_path)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (workdir, {}))
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")
        monkeypatch.setattr(executor, "_capture_env_snapshot",
                            lambda tid, wd: None)
        monkeypatch.setattr(executor, "_capture_base_sha",
                            lambda tid, wd, env: None)
        monkeypatch.setattr(executor, "_persist_session_id",
                            lambda tid, out: None)
        monkeypatch.setattr(executor, "_persist_claude_usage",
                            lambda tid, out: None)
        monkeypatch.setattr(executor, "_drain_process_output",
                            lambda *a, **k: (False, ['{"result":"ok"}']))

        executor._run_claude_once(1, {"name": "demo", "prompt_template": None},
                                  {"project_id": 42, "iid": 7})

        assert captured["cwd"] == workdir
        payload = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
        assert "demo-tool" in payload["mcpServers"]
