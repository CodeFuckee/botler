"""ClaudeExecutor hermes 引擎测试（issue #171：hermes agent SDK 进程内集成）。

覆盖：引擎分派（worker.engine 校验与回退）、_run_hermes_once 构造参数透传
（prompt 渲染 / 工作区 / 环境 / resume history + session_id）、停止与超时
（AIAgent.interrupt() → 125/124）、结果判定（success / unresolvable /
非 0 退出 / 非 JSON 输出）、conversation_history 落库与断点续跑恢复、
SSE 事件发布、SDK 未安装报错、以及 claude/dsh 现有路径不受影响。

HermesSdkRunner 的真实行为（线程/停止/事件回调）在 test_hermes_sdk_runner.py
覆盖，本文件用假 HermesSdkRunner 测 executor 侧的分派、判定与落库逻辑。
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
worker: {worker}
claude: {{}}
dsh: {{}}
templates: {{}}
repos: []
"""

# 直接传 _run_once 的 repo 字典（_build_prompt 需 prompt_template 键）
_REPO = {"name": "demo", "prompt_template": None}

_ISSUE = {"state": "opened", "title": "标题", "description": "正文",
          "web_url": "https://gitlab.example.com/x/-/issues/7",
          "project_id": 42, "iid": 7}

# hermes SDK runner 成功结果行样例
_HERMES_OUTPUT = json.dumps({
    "final_response": "已修复并推送，issue #7 处理完成",
    "messages": [{"role": "user", "content": "任务"},
                 {"role": "assistant", "content": "完成"}],
    "session_id": "hermes-sess-1",
    "error": None,
}, ensure_ascii=False)


def _mk_config(tmp_path, worker_extra="{precheck_enabled: false}") -> ConfigManager:
    """worker_extra 为整段子键文本（非空时需自带前置换行）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.format(worker=worker_extra), encoding="utf-8")
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
    """engine=hermes 的 executor（SDK 进程内模式，无需 command/args 配置）。"""
    config = _mk_config(tmp_path, worker_extra="\n  engine: hermes\n  precheck_enabled: false")
    return _mk_executor(tmp_path, config)


class _FakeRunner:
    """假 HermesSdkRunner：start 时同步回放 preset_lines（模拟 worker 输出）。

    实例由 _run_hermes_once 内部构造，测试通过类级 preset 预设行为：
    preset_lines = 新实例回放的行；preset_done = 新实例 done() 返回值。
    """

    instances: list["_FakeRunner"] = []
    preset_lines: list[str] = []
    preset_done: bool = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._done = _FakeRunner.preset_done
        self.stop_calls = 0
        self.result_lines = list(_FakeRunner.preset_lines)
        _FakeRunner.instances.append(self)

    def start(self):
        for line in self.result_lines:
            self.kwargs["on_line"](line)

    def done(self):
        return self._done

    def finish(self):
        return 0

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def fake_runner(monkeypatch):
    """注入假 HermesSdkRunner 到 executor 模块命名空间并重置 preset。"""
    _FakeRunner.instances.clear()
    _FakeRunner.preset_lines = []
    _FakeRunner.preset_done = True
    monkeypatch.setattr("botler.executor.HermesSdkRunner", _FakeRunner)
    monkeypatch.setattr("botler.executor.HermesSdkNotInstalledError",
                        type("HermesSdkNotInstalledError", (Exception,), {}))
    return _FakeRunner


def _patch_workspace(monkeypatch, executor, tmp_path):
    """替换 prepare_workspace / _log_file，避免真实 git/磁盘交互。"""
    calls: dict = {}

    def fake_prepare(repo, resume=False):
        calls["resume"] = resume
        return tmp_path / "work", {"GIT_ASKPASS": "/askpass"}

    monkeypatch.setattr(executor, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(executor, "_log_file",
                        lambda tid: tmp_path / f"task_{tid}.log")
    return calls


def _mk_task(executor) -> int:
    """创建任务记录（hermes_history 落库需要真实 task 行）。"""
    db = executor.db
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    repo_id = db.get_repo_by_project_id(42)["id"]
    return db.create_task(repo_id, 42, 7, "标题")


def _fake_drain_proc(exit_code=0):
    """构造 claude 路径用的假 Popen 进程（立即 EOF + 退出）。"""
    return type("Proc", (), {
        "stdout": type("SO", (), {"readline": lambda s: ""})(),
        "stderr": None,
        "stdin": type("SI", (), {"write": lambda s, t: None,
                                 "close": lambda s: None})(),
        "poll": lambda s: exit_code,
        "wait": lambda s, timeout=None: exit_code,
        "pid": 1,
    })()


class TestEngine:
    """_engine：hermes 白名单与回退。"""

    def test_default_engine_is_claude(self, executor):
        assert executor._engine(executor.config.get()) == "claude"

    def test_engine_hermes(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: hermes\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "hermes"

    def test_unknown_engine_falls_back_to_claude(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: gpt5\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "claude"


class TestRunHermesOnce:
    """_run_hermes_once（SDK 进程内模式）：构造参数、工作区、环境透传。"""

    def test_fresh_run_passes_prompt_and_workspace(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        code, output = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        assert code == 0
        assert _HERMES_OUTPUT in output
        assert calls["resume"] is False  # 全新执行：工作区重置
        kwargs = fake_runner.instances[0].kwargs
        assert "AI 维护者" in kwargs["prompt"]  # DEFAULT_TEMPLATE 渲染产物
        assert kwargs["session_id"] is None
        assert kwargs["history"] is None
        assert kwargs["task_id"] == "1"
        assert kwargs["cwd"] == str(tmp_path / "work")
        # git 凭据注入继承 _build_env（GIT_ASKPASS 为真实生成的脚本路径）
        assert kwargs["env"]["GIT_ASKPASS"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_resume_passes_history_and_session(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        history = [{"role": "user", "content": "上次任务"}]
        hermes_executor._run_hermes_once(
            1, _REPO, _ISSUE, history, resume_session_id="sess-9")
        assert calls["resume"] is True  # 恢复模式：工作区不清空
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["history"] == history
        assert kwargs["session_id"] == "sess-9"
        assert "继续处理" in kwargs["prompt"]  # RESUME_PROMPT 渲染

    def test_success_parses_result_from_output(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        code, output = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        assert code == 0
        assert output == _HERMES_OUTPUT
        assert hermes_executor._hermes_result(output) == "success"

    def test_multiline_output_parseable_success(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """事件行 + 结果行：拼接保留换行，结果判定与历史落库可解析。"""
        task_id = _mk_task(hermes_executor)
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "thinking", "text": "分析中…"},
                       ensure_ascii=False),
            _HERMES_OUTPUT,
        ]
        code, output = hermes_executor._run_hermes_once(task_id, _REPO, _ISSUE, None)
        assert code == 0
        assert hermes_executor._hermes_result(output) == "success"
        assert hermes_executor._hermes_history_from_output(output) == [
            {"role": "user", "content": "任务"},
            {"role": "assistant", "content": "完成"}]

    def test_stop_returns_stop_exit_code_and_calls_stop(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_done = False  # 运行中（done 返回 False 进入轮询循环）
        hermes_executor.request_stop(1)
        try:
            code, _ = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        finally:
            hermes_executor.clear_stop_request(1)
        assert code == 125
        assert fake_runner.instances[0].stop_calls == 1  # stop 被调用（请求中断）

    def test_timeout_returns_124_and_calls_stop(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_done = False
        # 超时秒数设为 0：进入轮询循环第一轮即超时（get 触发 load 后改内存值）
        hermes_executor.config.get().task_timeout_seconds = 0
        try:
            code, _ = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        finally:
            hermes_executor.config.get().task_timeout_seconds = 1800
        assert code == 124
        assert fake_runner.instances[0].stop_calls == 1

    def test_sdk_missing_raises_executor_error(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """SDK 未安装：HermesSdkNotInstalledError → ExecutorError（run_task 捕获重试）。"""
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        from botler.executor import HermesSdkNotInstalledError as _SDKErr

        class _AlwaysMissing(_FakeRunner):
            def start(self):
                raise _SDKErr("hermes-agent SDK 未安装")

        monkeypatch.setattr("botler.executor.HermesSdkRunner", _AlwaysMissing)
        from botler.executor import ExecutorError
        with pytest.raises(ExecutorError):
            hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)

    def test_sse_events_published_from_event_lines(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "stream_delta", "text": "正在处理…"},
                       ensure_ascii=False),
            _HERMES_OUTPUT,
        ]
        sub = hermes_executor.event_bus.subscribe(1, maxsize=10)
        hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        event = sub.get(timeout=2)
        assert event["kind"] == "text"
        assert event["text"] == "正在处理…"
        sub.close()

    def test_log_file_written_from_lines(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """事件行与结果行均落盘日志（SSE 回放数据源）。"""
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        log_text = (tmp_path / "task_1.log").read_text(encoding="utf-8")
        assert _HERMES_OUTPUT in log_text

    def test_claude_engine_untouched(
            self, executor, monkeypatch, tmp_path, fake_runner):
        """engine=claude（默认）时 hermes runner 不被调用（回归保护）。"""
        _patch_workspace(monkeypatch, executor, tmp_path)
        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kw: _fake_drain_proc())
        code, _ = executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 hermes 路径

    def test_dsh_engine_untouched(self, monkeypatch, tmp_path, fake_runner):
        """engine=dsh 时 hermes runner 不被调用（回归保护）。"""

        class _FakeDsh:
            def __init__(self, **kwargs):
                self.stop_calls = 0
            def start(self):
                pass
            def done(self):
                return True
            def finish(self):
                return 0
            def stop(self):
                self.stop_calls += 1

        monkeypatch.setattr("botler.executor.DshRunner", _FakeDsh)
        config = _mk_config(tmp_path, worker_extra="\n  engine: dsh\n  precheck_enabled: false")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        code, _ = ex._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 hermes 路径


class TestHermesResume:
    """断点续跑：hermes_history 落库与恢复（Q3-B 等价实现）。"""

    def test_history_persisted_after_run(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        history = hermes_executor._hermes_history_from_output(_HERMES_OUTPUT)
        assert history == [{"role": "user", "content": "任务"},
                           {"role": "assistant", "content": "完成"}]

    def test_resume_reads_history_and_passes_to_runner(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """显式传 resume_history 时：RESUME 提示 + history + 保留工作区。"""
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        history = [{"role": "user", "content": "上次任务"}]
        resumed = []

        def fake_prepare(repo, resume=False):
            resumed.append(resume)
            return tmp_path / "work", {"GIT_ASKPASS": "/askpass"}

        monkeypatch.setattr(hermes_executor, "prepare_workspace", fake_prepare)
        hermes_executor._run_hermes_once(1, _REPO, _ISSUE, history)
        assert resumed == [True]  # 恢复模式：工作区不清空
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["history"] == history
        assert "继续处理" in kwargs["prompt"]  # RESUME_PROMPT 渲染

    def test_resume_from_persisted_task_history(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """任务落库的 hermes_history：_run_once 内部经插件解析后恢复（含会话 id）。"""
        task_id = _mk_task(hermes_executor)
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        hermes_executor.db.set_task_status(
            task_id, None, hermes_history=json.dumps(
                {"session_id": "sess-99",
                 "messages": [{"role": "user", "content": "旧任务"}]}))
        hermes_executor._run_once(task_id, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["history"] == [{"role": "user", "content": "旧任务"}]
        assert kwargs["session_id"] == "sess-99"

    def test_corrupt_persisted_history_falls_back_to_fresh(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """落库历史损坏（非 JSON）→ 降级全新会话（不抛异常）。"""
        task_id = _mk_task(hermes_executor)
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        hermes_executor.db.set_task_status(task_id, None, hermes_history="{{{bad json")
        hermes_executor._run_once(task_id, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["history"] is None

    def test_fresh_run_cleans_workspace(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """无 history 的全新执行：工作区重置（与 claude 引擎一致）。"""
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        resumed = []
        monkeypatch.setattr(hermes_executor, "prepare_workspace",
                            lambda repo, resume=False: (
                                resumed.append(resume) or (
                                    tmp_path / "work", {"GIT_ASKPASS": "/x"})))
        hermes_executor._run_once(1, _REPO, _ISSUE)
        assert resumed == [False]

    def test_history_from_output_missing_messages(self):
        """输出无 messages 字段 → 空列表（不抛异常）。"""
        executor = ClaudeExecutor.__new__(ClaudeExecutor)  # 静态方法无需初始化
        output = json.dumps({"final_response": "ok", "error": None})
        assert executor._hermes_history_from_output(output) == []

    def test_history_from_output_non_json(self):
        executor = ClaudeExecutor.__new__(ClaudeExecutor)
        assert executor._hermes_history_from_output("garbage") == []


class TestHermesResultJudgement:
    """结果判定：成功 / 无法解决 / 失败重试（hermes 输出语义）。"""

    def test_success_accepts_json_result(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = [_HERMES_OUTPUT]
        code, output = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        assert code == 0
        assert hermes_executor._hermes_result(output) == "success"

    def test_unresolvable_detected_from_final_response(self, hermes_executor):
        output = json.dumps({
            "final_response": "抱歉，无法解决该 issue：依赖缺失",
            "messages": [], "session_id": "s2", "error": None})
        assert hermes_executor._hermes_result(output) == "unresolvable"

    def test_non_zero_exit_passthrough(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        """exit 非 0 → 原样透传，由 run_task 按失败重试（判定只看 exit 0 输出）。"""
        class _Exit1(_FakeRunner):
            def finish(self):
                return 1

        monkeypatch.setattr("botler.executor.HermesSdkRunner", _Exit1)
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        _FakeRunner.preset_lines = [_HERMES_OUTPUT]
        code, output = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        assert code == 1
        assert output == _HERMES_OUTPUT

    def test_error_field_marks_failure(self, hermes_executor):
        """runner 输出 error 字段（协议标记错误）→ 判定 failed。"""
        output = json.dumps({"final_response": "", "messages": [],
                             "session_id": "s3", "error": "agent 崩溃"})
        assert hermes_executor._hermes_result(output) == "failed"

    def test_non_json_output_is_failure(
            self, hermes_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, hermes_executor, tmp_path)
        fake_runner.preset_lines = ["no json here"]
        code, output = hermes_executor._run_hermes_once(1, _REPO, _ISSUE, None)
        assert code == 0
        assert hermes_executor._hermes_result(output) == "failed"

    def test_empty_final_response_is_failure(self, hermes_executor):
        """final_response 为空串 → failed（不允许空回复静默成功）。"""
        output = json.dumps({"final_response": "", "messages": [],
                             "session_id": "s4", "error": None})
        assert hermes_executor._hermes_result(output) == "failed"
