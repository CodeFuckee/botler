"""执行引擎不设任务时限的回归测试（issue #424）。"""

from pathlib import Path

from botler.config import ConfigManager
from botler.executor import ClaudeExecutor
from botler.database import Database
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer


CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
worker: {worker}
claude: {{}}
templates: {{}}
repos: []
"""


def _config(tmp_path, worker="{}"):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT.format(worker=worker), encoding="utf-8")
    return ConfigManager(str(path))


def test_default_task_execution_timeout_is_disabled(tmp_path):
    """未配置时，三种执行引擎共享的任务时限必须为 None（无限制）。"""
    assert _config(tmp_path).get().task_timeout_seconds is None


def test_legacy_task_timeout_setting_cannot_restore_execution_limit(tmp_path):
    """历史配置即使保留 1800，也不能重新给执行引擎加时间上限。"""
    config = _config(tmp_path, "{task_timeout_seconds: 1800}")

    assert config.get().task_timeout_seconds is None


def test_process_output_drain_accepts_no_deadline(tmp_path):
    """Claude 子进程输出循环不接收 deadline，仍应正常读完。"""
    config = _config(tmp_path)
    executor = ClaudeExecutor(
        config, Database(str(tmp_path / "test.db")),
        GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False),
        TemplateRenderer(config), workspace_root=str(tmp_path / "workspace"))

    class _Stdout:
        def __init__(self):
            self.lines = ["完成\n", ""]

        def readline(self):
            return self.lines.pop(0)

    class _Proc:
        stdout = _Stdout()

        def poll(self):
            return None if self.stdout.lines else 0

    try:
        stopped, chunks = executor._drain_process_output(
            _Proc(), 1, Path(tmp_path / "task.log"))
    finally:
        executor.db.close()

    assert (stopped, chunks) == (False, ["完成\n"])
