"""zcode 引擎测试（ZCode CLI 无头模式，与 Claude Code 同源）。

覆盖：
- 插件注册与 _run_once 分发（engine="zcode" → _run_zcode_once）；
- 命令构造：zcode_command/zcode_args 配置 + stream-json 自动补 --verbose +
  --dangerously-skip-permissions + prompt 收尾；
- 会话 id 落 tasks.zcode_session_id（运行中 init 行 + 结束 result 行），
  claude_session_id 不受污染；
- 断点续跑：--resume <sid>；~/.zcode 会话文件校验的宽容语义（目录缺失
  放行 / 文件缺失降级全新会话）；
- run_task 级：resume 分支走 zcode_session_id（含降级路径）；
- 健康探测 probe_zcode：正常 / 命令缺失 / 非零退出；
- 用量落库：result 行 usage 按 engine="zcode" 写入 task_usage。
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.engine_health import probe_engine
from botler.events import EventBus
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.plugins import PluginKind, list_plugins
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {precheck_enabled: false}
claude: {}
zcode:
  command: zc
  args: ["-p", "--output-format", "stream-json", "--verbose", "--model", "glm-5"]
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
                          workspace_root=str(tmp_path / "workspace"),
                          event_bus=EventBus())


class _MultiLineStdout:
    def __init__(self, lines: list[str]):
        self._lines = [l + "\n" for l in lines]

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class _FakeProc:
    def __init__(self, lines: list[str], exit_code: int = 0):
        self.stdout = _MultiLineStdout(lines)
        self._exit = exit_code
        self.stdin = None

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit


def _repo():
    return {"name": "demo", "url": "https://gitlab.example.com/group/demo.git",
            "prompt_template": None}


def _issue():
    return {"project_id": 42, "iid": 7, "title": "修复登录问题",
            "description": "登录报错"}


def _issue_dict(state):
    return {"project_id": 42, "iid": 7, "state": state, "title": "修复登录问题",
            "description": "登录报错", "labels": []}


def _stream_lines(session_id="sess-z"):
    """典型 zcode stream-json 输出（与 claude 同构：init + result 收尾）。"""
    return [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": session_id, "cwd": "/work/demo",
                    "model": "glm-5"}, ensure_ascii=False),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "我来修复。"},
                    ]}}, ensure_ascii=False),
        json.dumps({"type": "result", "subtype": "success",
                    "result": "修复完成，已推送。",
                    "exit_code": 0, "session_id": session_id,
                    "usage": {"input_tokens": 100, "output_tokens": 50}},
                   ensure_ascii=False),
    ]


def _toolkit(executor, monkeypatch, tmp_path, lines, exit_code=0):
    """fake Popen（捕获 cmd）+ workspace/日志桩。返回 captured dict。"""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(lines, exit_code)

    monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
    monkeypatch.setattr(executor, "prepare_workspace",
                        lambda repo, resume=False: (tmp_path / "ws", {}))
    monkeypatch.setattr(executor, "_log_file",
                        lambda tid: tmp_path / f"task_{tid}.log")
    return captured


class TestZcodePlugin:
    """插件注册与分发。"""

    def test_zcode_plugin_registered(self):
        names = [p.name for p in list_plugins(PluginKind.EXECUTOR)]
        assert "zcode" in names

    def test_run_once_dispatches_to_zcode(self, executor, monkeypatch):
        """_run_once(engine='zcode') 委托 _run_zcode_once 并透传 resume_session。"""
        calls = {}

        def fake_zcode(task_id, repo, issue, resume_session=None):
            calls["args"] = (task_id, repo, issue, resume_session)
            return 0, "ok"

        monkeypatch.setattr(executor, "_run_zcode_once", fake_zcode)
        exit_code, out = executor._run_once(9, _repo(), _issue(),
                                            "sess-r", None, "zcode")
        assert (exit_code, out) == (0, "ok")
        assert calls["args"] == (9, _repo(), _issue(), "sess-r")


class TestZcodeRunOnce:
    """命令构造与会话落库。"""

    def test_command_uses_zcode_config_and_flags(self, executor, monkeypatch, tmp_path):
        """cmd = zcode_command + zcode_args + skip-permissions + prompt 收尾。"""
        captured = _toolkit(executor, monkeypatch, tmp_path,
                            _stream_lines(), 0)

        exit_code, output = executor._run_once(1, _repo(), _issue(), None, None, "zcode")

        assert exit_code == 0
        cmd = captured["cmd"]
        assert cmd[0] == "zc"
        for flag in ["-p", "--output-format", "stream-json", "--verbose",
                     "--model", "glm-5", "--dangerously-skip-permissions"]:
            assert flag in cmd, f"cmd 缺少 {flag}"
        assert "修复登录问题" in cmd[-1], "prompt 应作为最后一个参数"

    def test_session_persisted_to_zcode_column(self, executor, monkeypatch, tmp_path):
        """会话 id 落 tasks.zcode_session_id；claude_session_id 不受污染。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo",
                                 "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        _toolkit(executor, monkeypatch, tmp_path, _stream_lines("sess-z"), 0)

        executor._run_once(task_id, _repo(), _issue(), None, None, "zcode")

        row = db.get_task(task_id)
        assert row["zcode_session_id"] == "sess-z"
        assert not row["claude_session_id"]

    def test_resume_flag_passed(self, executor, monkeypatch, tmp_path):
        """resume_session 非空 → cmd 含 --resume <sid>，prompt 为恢复引导语。"""
        captured = _toolkit(executor, monkeypatch, tmp_path,
                            _stream_lines("sess-z2"), 0)
        monkeypatch.setattr(executor, "_zcode_session_file", lambda sid: True)

        executor._run_once(1, _repo(), _issue(), "sess-z2", None, "zcode")

        cmd = captured["cmd"]
        assert cmd[cmd.index("--resume") + 1] == "sess-z2"
        assert "继续" in cmd[-1]

    def test_usage_persisted_with_zcode_engine(self, executor, monkeypatch, tmp_path):
        """result 行 usage 按 engine='zcode' 落 task_usage。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo",
                                 "https://gitlab.example.com/group/demo.git")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        _toolkit(executor, monkeypatch, tmp_path, _stream_lines(), 0)

        executor._run_once(task_id, _repo(), _issue(), None, None, "zcode")

        usage = db.get_task_usage(task_id)
        assert usage is not None and usage["engine"] == "zcode"
        assert usage["total_tokens"] == 150


class TestZcodeSessionFile:
    """~/.zcode 会话文件校验的宽容语义（直接测 SessionMixin 纯路径逻辑）。"""

    @staticmethod
    def _check(tmp_path, session_id="sess-check") -> bool:
        import types

        from botler.executor.session import SessionMixin
        monkey_home = tmp_path
        original = Path.home
        Path.home = staticmethod(lambda: monkey_home)
        try:
            return SessionMixin._zcode_session_file(
                types.SimpleNamespace(), session_id)
        finally:
            Path.home = original

    def test_tolerant_when_zcode_home_missing(self, tmp_path):
        """~/.zcode 目录不存在（会话根自定义 / 引擎跑远程）→ 放行续跑。"""
        assert self._check(tmp_path) is True

    def test_downgrade_when_session_file_missing(self, tmp_path):
        """~/.zcode 存在但会话文件不在 → False（降级全新会话）。"""
        (tmp_path / ".zcode").mkdir()
        assert self._check(tmp_path) is False

    def test_ok_when_session_file_exists(self, tmp_path):
        """~/.zcode/projects/*/<sid>.jsonl 存在 → True。"""
        proj = tmp_path / ".zcode" / "projects" / "-work-demo"
        proj.mkdir(parents=True)
        (proj / "sess-check.jsonl").write_text("{}\n", encoding="utf-8")
        assert self._check(tmp_path) is True


class TestZcodeRunTaskResume:
    """run_task 级 resume 分支（tasks.zcode_session_id）。"""

    def _run_task_env(self, executor, monkeypatch, tmp_path, lines):
        db = executor.db
        repo_id = db.upsert_repo(42, "demo",
                                 "https://gitlab.example.com/group/demo.git",
                                 engine="zcode")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        captured = _toolkit(executor, monkeypatch, tmp_path, lines, 0)
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: _issue_dict("closed"),
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )
        return db, repo_id, task_id, captured

    def test_run_task_resumes_zcode_session(self, executor, monkeypatch, tmp_path):
        db, repo_id, task_id, captured = self._run_task_env(
            executor, monkeypatch, tmp_path, _stream_lines("resume-z"))
        db.set_task_status(task_id, "retrying", zcode_session_id="resume-z")
        monkeypatch.setattr(executor, "_zcode_session_file", lambda sid: True)

        executor.run_task(task_id)

        assert "--resume" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--resume") + 1] == "resume-z"
        assert db.get_task(task_id)["status"] == "succeeded"
        assert db.get_task(task_id)["zcode_session_id"] == "resume-z"

    def test_run_task_missing_session_downgrades_to_fresh(self, executor, monkeypatch, tmp_path):
        db, repo_id, task_id, captured = self._run_task_env(
            executor, monkeypatch, tmp_path, _stream_lines("fresh-z"))
        db.set_task_status(task_id, "retrying", zcode_session_id="ghost-z")
        monkeypatch.setattr(executor, "_zcode_session_file", lambda sid: False)

        executor.run_task(task_id)

        assert "--resume" not in captured["cmd"]
        assert db.get_task(task_id)["zcode_session_id"] == "fresh-z"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("降级为全新会话" in m for m in logs)


class TestZcodeHealthProbe:
    """probe_zcode 健康探测。"""

    def _cfg(self, tmp_path, command="zcode"):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT.replace("command: zc", f"command: {command}"),
            encoding="utf-8")
        return ConfigManager(str(config_path)).get()

    def test_probe_ok(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="1.0.0\n", stderr="")
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = probe_engine("zcode", self._cfg(tmp_path))
        assert result["status"] == "ok"
        assert "1.0.0" in result["detail"]

    def test_probe_command_missing(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(cmd)
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = probe_engine("zcode", self._cfg(tmp_path))
        assert result["status"] == "fail"
        assert "找不到 zcode 命令" in result["detail"]

    def test_probe_nonzero_exit(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = probe_engine("zcode", self._cfg(tmp_path))
        assert result["status"] == "fail"
        assert "boom" in result["detail"]
