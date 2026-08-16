"""执行引擎 / 消息通道插件化测试（issue #140）。

覆盖：
- 引擎名校验走插件注册表（claude / hermes / dsh 有效，未注册回退 claude）
- _run_once 按引擎名查插件委托执行（三引擎各自委托路径）
- 引擎插件元信息（描述 / 版本）
- notifier 插件分发：任务成功/失败遍历全部通道（in_app 记录事件、
  webhook 未启用跳过），单通道失败不阻塞其他通道
"""

from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.plugins import (
    PluginKind,
    get_plugin,
    has_plugin,
    list_plugins,
)
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


def _build_executor(tmp_path, worker_text: str = "worker: {}"):
    """按 worker 段文本构造 executor（engine 配置可控）。"""
    text = CONFIG_TEXT.replace("worker: {}", worker_text, 1)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    config.load()
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


class TestEngineSelection:
    """_engine 从插件注册表判定引擎名。"""

    def test_default_claude(self, tmp_path):
        """未配置 engine 时默认 claude。"""
        executor = _build_executor(tmp_path)
        assert executor._engine(executor.config.get()) == "claude"

    @pytest.mark.parametrize("name", ["claude", "hermes", "dsh"])
    def test_registered_engines_accepted(self, tmp_path, name):
        """注册表内的引擎名原样返回。"""
        executor = _build_executor(tmp_path, f"worker:\n  engine: {name}")
        assert executor._engine(executor.config.get()) == name

    @pytest.mark.parametrize("name", ["unknown", "", "CLAUDE", "  claude  "])
    def test_unregistered_falls_back_claude(self, tmp_path, name):
        """未注册 / 空 / 大小写 / 空白 engine 一律回退 claude。"""
        executor = _build_executor(tmp_path, f"worker:\n  engine: {name}")
        assert executor._engine(executor.config.get()) == "claude"


class TestRunOnceDispatch:
    """_run_once 按引擎插件委托执行。"""

    def test_claude_delegates_to_run_claude_once(self, tmp_path, monkeypatch):
        """claude 引擎委托 _run_claude_once（含断点续跑参数透传）。"""
        executor = _build_executor(tmp_path)
        captured = {}

        def fake_run(task_id, repo, issue, resume_session):
            captured["args"] = (task_id, repo, issue, resume_session)
            return (0, "claude-output")

        monkeypatch.setattr(executor, "_run_claude_once", fake_run)
        code, output = executor._run_once(1, {"name": "r"}, {"iid": 2}, resume_session="s1")
        assert (code, output) == (0, "claude-output")
        assert captured["args"] == (1, {"name": "r"}, {"iid": 2}, "s1")

    def test_hermes_delegates_to_run_hermes_once(self, tmp_path, monkeypatch):
        """hermes 引擎委托 _run_hermes_once（resume_history 显式传入优先）。"""
        executor = _build_executor(tmp_path, "worker:\n  engine: hermes")
        captured = {}

        def fake_run(task_id, repo, issue, messages, sid):
            captured["messages"] = messages
            captured["sid"] = sid
            return (0, "hermes-output")

        monkeypatch.setattr(executor, "_run_hermes_once", fake_run)
        monkeypatch.setattr(executor, "_hermes_resume_data",
                            lambda raw: (["旧历史"], "old-sid"))
        code, output = executor._run_once(1, {"name": "r"}, {"iid": 2},
                                          resume_history=["新历史"])
        assert (code, output) == (0, "hermes-output")
        assert captured["messages"] == ["新历史"], "显式 resume_history 优先"
        assert captured["sid"] == "old-sid"

    def test_hermes_uses_persisted_history_when_not_passed(self, tmp_path, monkeypatch):
        """hermes 未显式传 resume_history 时从任务落库 hermes_history 解析。"""
        executor = _build_executor(tmp_path, "worker:\n  engine: hermes")
        captured = {}

        def fake_run(task_id, repo, issue, messages, sid):
            captured["messages"] = messages
            return (0, "ok")

        # 模拟任务行含 hermes_history（SQLite 行 dict 语义）
        task_row = {"hermes_history": '["落库历史"]'}
        monkeypatch.setattr(executor, "_hermes_resume_data",
                            lambda raw: (raw, "sid-from-db"))
        monkeypatch.setattr(executor, "db",
                            SimpleNamespace(get_task=lambda tid: task_row))
        monkeypatch.setattr(executor, "_run_hermes_once", fake_run)
        executor._run_once(1, {"name": "r"}, {"iid": 2})
        assert captured["messages"] == '["落库历史"]'

    def test_dsh_delegates_to_run_dsh_once(self, tmp_path, monkeypatch):
        """dsh 引擎委托 _run_dsh_once（resume_session 透传）。"""
        executor = _build_executor(tmp_path, "worker:\n  engine: dsh")
        captured = {}

        def fake_run(task_id, repo, issue, resume_session):
            captured["resume"] = resume_session
            return (0, "dsh-output")

        monkeypatch.setattr(executor, "_run_dsh_once", fake_run)
        code, output = executor._run_once(1, {"name": "r"}, {"iid": 2}, resume_session="ds")
        assert (code, output) == (0, "dsh-output")
        assert captured["resume"] == "ds"

    def test_unknown_engine_delegates_claude(self, tmp_path, monkeypatch):
        """未注册引擎名回退 claude 插件执行。"""
        executor = _build_executor(tmp_path, "worker:\n  engine: bogus")
        monkeypatch.setattr(executor, "_run_claude_once",
                            lambda *a, **k: (0, "claude-fallback"))
        code, output = executor._run_once(1, {"name": "r"}, {"iid": 2})
        assert output == "claude-fallback"


class TestEnginePluginMeta:
    """引擎插件元信息与注册表一致性。"""

    def test_builtin_engine_plugins_registered(self):
        """三个内置引擎插件全部注册且描述非空。"""
        for name in ("claude", "hermes", "dsh"):
            plugin = get_plugin(PluginKind.EXECUTOR, name)
            assert plugin.description != ""
            assert plugin.version == "1.0"
            assert plugin.kind == PluginKind.EXECUTOR

    def test_engine_accepts_all_registered(self, tmp_path):
        """_engine 接受注册表内全部引擎名（插件体系驱动引擎校验）。"""
        executor = _build_executor(tmp_path)
        for plugin in list_plugins(PluginKind.EXECUTOR):
            cfg = SimpleNamespace(engine=plugin.name)
            assert executor._engine(cfg) == plugin.name

    def test_has_plugin_by_name(self, tmp_path):
        """has_plugin 按插件名查询（参数为名字字符串，非插件对象）。"""
        _build_executor(tmp_path)
        assert has_plugin(PluginKind.EXECUTOR, "claude")
        assert not has_plugin(PluginKind.EXECUTOR, "不存在")


class TestNotifierDispatch:
    """任务收尾 notifier 插件分发（issue #140）。"""

    @pytest.fixture
    def executor(self, tmp_path):
        return _build_executor(tmp_path)

    @pytest.fixture
    def db(self, tmp_path):
        return Database(str(tmp_path / "test.db"))

    def test_success_dispatch_records_in_app(self, executor, db, tmp_path):
        """成功分发：in_app 通道记录 task_succeeded 事件。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=7, title="修复登录 bug")
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.find_commit_for_issue = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.last_note_author_id = lambda *a, **k: None  # type: ignore[method-assign]
        db.claim_task(task_id)
        executor._finish_succeeded(task_id, "ok")
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "task_succeeded"
        assert events[0]["task_id"] == task_id

    def test_failed_dispatch_records_in_app(self, executor, db, tmp_path):
        """失败分发：in_app 通道记录 task_failed 事件（含原因）。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=8, title="接口报错")
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        db.claim_task(task_id)
        executor._finish_failed(task_id, "Claude Code 报告无法解决该 issue")
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "task_failed"
        assert "无法解决" in events[0]["body"]

    def test_one_channel_failure_does_not_block_others(self, executor, db,
                                                      monkeypatch, tmp_path):
        """webhook 通道失败（如网络错误）不影响 in_app 通道记录事件。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="容错任务")
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        # webhook 插件发送抛异常（模拟网络故障）
        webhook_plugin = get_plugin(PluginKind.NOTIFIER, "webhook")

        def boom(*a, **k):
            raise RuntimeError("webhook 网络故障")

        monkeypatch.setattr(webhook_plugin, "send_task_succeeded", boom)
        db.claim_task(task_id)
        executor._finish_failed(task_id, "重试耗尽")
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "task_failed"


# ---- 复用 test_notifications.py 的辅助函数 ----

def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1, title: str = "测试任务") -> int:
    return db.create_task(repo_id, 42, issue_iid, title)
