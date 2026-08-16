"""ClaudeExecutor dsh 引擎测试（issue #84：集成 deepseek-harness 方案B）。

覆盖：引擎分派（worker.engine=dsh 白名单）、_run_dsh_once 构造参数透传
（prompt 渲染 / 工作区 / 环境 / SDK 配置 / resume session_id）、停止与超时
（stop 强制终止运行时 → 125/124）、结果判定（_dsh_result 三态）、会话 id
落库（dsh_session_id）与断点续跑恢复、SSE 事件发布、SDK 未安装报错、
以及 claude/hermes 现有路径不受影响。

DshRunner 的真实行为（线程/停止/事件映射）在 test_dsh_runner.py 覆盖，
本文件用假 DshRunner 测 executor 侧的分派、判定与落库逻辑。
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
hermes: {hermes}
dsh: {dsh}
ai_providers: {ai_providers}
templates: {{}}
repos: []
"""

# 直接传 _run_once 的 repo 字典（_build_prompt 需 prompt_template 键）
_REPO = {"name": "demo", "prompt_template": None}

_ISSUE = {"state": "opened", "title": "标题", "description": "正文",
          "web_url": "https://gitlab.example.com/x/-/issues/7",
          "project_id": 42, "iid": 7}

# dsh runner 成功结果行样例
_RESULT_LINE = json.dumps({"final_response": "已修复并推送，issue #7 处理完成",
                           "finish_reason": "completed",
                           "session_id": "dsh-sess-1"}, ensure_ascii=False)

# 设置页「AI 供应商」里的 DeepSeek 项（issue #115：dsh 引擎凭据回退源）
_AI_PROVIDERS_DEEPSEEK = """
- name: deepseek
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: sk-ai-provider-key
  model: deepseek-v4-flash
  enabled: true
"""


def _mk_config(tmp_path, worker_extra="{}", dsh_extra="{}",
               hermes_extra="{}", ai_providers_extra="[]") -> ConfigManager:
    """worker_extra / dsh_extra / hermes_extra 为整段子键文本（非空时需自带前置换行）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.format(worker=worker_extra, dsh=dsh_extra,
                           hermes=hermes_extra,
                           ai_providers=ai_providers_extra),
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
    return _mk_executor(tmp_path, _mk_config(tmp_path))


@pytest.fixture
def dsh_executor(tmp_path):
    """engine=dsh + dsh 段配置的 executor。"""
    config = _mk_config(
        tmp_path, worker_extra="\n  engine: dsh",
        dsh_extra="\n  provider: deepseek-official"
                  "\n  model: deepseek-v4-flash"
                  "\n  max_tokens: 49152"
                  "\n  session_root: /var/dsh-sessions")
    return _mk_executor(tmp_path, config)


class _FakeRunner:
    """假 DshRunner：start 时同步回放 preset_lines（模拟 worker 输出）。

    实例由 _run_dsh_once 内部构造，测试通过类级 preset 预设行为：
    preset_lines = 新实例回放的行；preset_done = 新实例 done() 返回值。
    """

    instances: list["_FakeRunner"] = []
    preset_lines: list[str] = []
    preset_done: bool = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._exit = 0
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
        return self._exit

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def fake_runner(monkeypatch):
    """注入假 DshRunner 到 executor 模块命名空间并重置 preset。"""
    _FakeRunner.instances.clear()
    _FakeRunner.preset_lines = []
    _FakeRunner.preset_done = True
    monkeypatch.setattr("botler.executor.DshRunner", _FakeRunner)
    monkeypatch.setattr("botler.executor.DshSdkNotInstalledError",
                        type("DshSdkNotInstalledError", (Exception,), {}))
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
    """创建任务记录（session_id 落库需要真实 task 行）。"""
    db = executor.db
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    repo_id = db.get_repo_by_project_id(42)["id"]
    return db.create_task(repo_id, 42, 7, "标题")


def _fake_drain_proc(exit_code=0):
    """构造 claude/hermes 路径用的假 Popen 进程（立即 EOF + 退出）。"""
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
    """_engine：dsh 白名单与回退。"""

    def test_engine_dsh(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: dsh")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "dsh"

    def test_engine_uppercase_normalized(self, tmp_path):
        config = _mk_config(tmp_path, worker_extra="\n  engine: DSH")
        ex = _mk_executor(tmp_path, config)
        assert ex._engine(config.get()) == "dsh"

    def test_default_engine_is_claude(self, executor):
        assert executor._engine(executor.config.get()) == "claude"


class TestDshCredentials:
    """_dsh_credentials：dsh 段显式配置 > ai_providers deepseek 项 > 环境变量（SDK 默认）。

    issue #115 复现：用户仅设置页「AI 供应商」配过 DeepSeek key，dsh 段
    未配且部署机环境无 DEEPSEEK_API_KEY → 任务 #194 #195 全部 401 失败。
    """

    def test_explicit_dsh_settings_win(self, tmp_path):
        config = _mk_config(
            tmp_path, dsh_extra="\n  api_key: sk-dsh\n  base_url: https://x/v1",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == ("sk-dsh", "https://x/v1")

    def test_falls_back_to_ai_provider_deepseek(self, tmp_path):
        """dsh 段未配 → ai_providers 的 deepseek 项（enabled）回退。"""
        config = _mk_config(tmp_path, ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (
            "sk-ai-provider-key", "https://api.deepseek.com/v1")

    def test_ai_provider_fills_only_missing_fields(self, tmp_path):
        """dsh 段只配 base_url：api_key 回退、base_url 保留 dsh 段值。"""
        config = _mk_config(
            tmp_path, dsh_extra="\n  base_url: https://self-host/v1",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (
            "sk-ai-provider-key", "https://self-host/v1")

    def test_disabled_ai_provider_skipped(self, tmp_path):
        """deepseek 项 enabled=false → 不回退（key 不可用）。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: deepseek
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: sk-disabled
  model: deepseek-v4-flash
  enabled: false
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None)

    def test_non_deepseek_provider_skipped(self, tmp_path):
        """provider 非 deepseek 的项（如 openai）不回退。"""
        config = _mk_config(
            tmp_path,
            ai_providers_extra="""
- name: openai
  provider: openai
  base_url: https://api.openai.com/v1
  api_key: sk-openai
  model: gpt-4o
  enabled: true
""")
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None)

    def test_none_when_no_source(self, tmp_path):
        """dsh 段与 ai_providers 均无 deepseek 凭据 → (None, None)（SDK 读环境）。"""
        config = _mk_config(tmp_path)
        ex = _mk_executor(tmp_path, config)
        assert ex._dsh_credentials(config.get()) == (None, None)


class TestRunDshOnce:
    """_run_dsh_once：构造参数、工作区、环境、SDK 配置透传。"""

    def test_fresh_run_passes_prompt_and_workspace(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        code, output = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        assert code == 0
        assert _RESULT_LINE in output
        assert calls["resume"] is False  # 全新执行：工作区重置
        kwargs = fake_runner.instances[0].kwargs
        assert "AI 维护者" in kwargs["prompt"]  # DEFAULT_TEMPLATE 渲染产物
        assert kwargs["session_id"] is None
        assert kwargs["cwd"] == str(tmp_path / "work")
        # git 凭据注入继承 _build_env（GIT_ASKPASS 为真实生成的脚本路径）
        assert kwargs["env"]["GIT_ASKPASS"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_sdk_config_from_dsh_settings(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["provider"] == "deepseek-official"
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["max_tokens"] == 49152
        assert kwargs["session_root"] == "/var/dsh-sessions"

    def test_ai_provider_credentials_passed_to_runner(
            self, monkeypatch, tmp_path, fake_runner):
        """issue #115：dsh 段无 key 时，ai_providers 的 deepseek 凭据传给 runner。"""
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: dsh",
            ai_providers_extra=_AI_PROVIDERS_DEEPSEEK)
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        ex._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["api_key"] == "sk-ai-provider-key"
        assert kwargs["base_url"] == "https://api.deepseek.com/v1"

    def test_ai_provider_credentials_none_passed_as_none(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """无任何凭据源时传 None（SDK 读环境 DEEPSEEK_API_KEY 兜底，行为不变）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["api_key"] is None
        assert kwargs["base_url"] is None

    def test_resume_passes_session_id_and_keeps_workspace(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        calls = _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE, resume_session="sess-9")
        assert calls["resume"] is True  # 恢复模式：工作区不清空
        kwargs = fake_runner.instances[0].kwargs
        assert kwargs["session_id"] == "sess-9"
        assert "继续处理" in kwargs["prompt"]  # RESUME_PROMPT 渲染

    def test_success_persists_dsh_session_id(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_multiline_output_parseable_success_and_session_persist(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """issue #119：多行事件输出拼接必须保留换行，否则结果行解析失败。

        复现：DshRunner 的 on_line 回调逐条收到事件行（行尾无换行符），
        _run_dsh_once 若用 ''.join 拼接，output 无换行分隔，_last_json_object
        按行扫描只拿到首个事件对象（finish_reason 缺失）→ _dsh_result 误判
        failed → 触发重试；且 _persist_dsh_session_id 同样解析不到 session_id
        → 断点续跑失效 → 每次重试都是全新会话（重复开发任务），重试耗尽后
        任务显示失败（任务 #198 #199 日志：引擎 exit 0、结果行 completed 仍失败）。
        """
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        # 事件行 + 结果行（真实 DshRunner 回调逐行 on_line 的真实形态）
        fake_runner.preset_lines = [
            json.dumps({"event": "raw", "type": "step/end"},
                       ensure_ascii=False),
            json.dumps({"event": "status", "message": "回合结束: completed"},
                       ensure_ascii=False),
            _RESULT_LINE,
        ]
        code, output = dsh_executor._run_dsh_once(task_id, _REPO, _ISSUE)
        assert code == 0
        # 结果行必须可解析：成功判定 + 会话 id 落库（断点续跑）
        assert dsh_executor._dsh_result(output) == "success"
        row = dsh_executor.db.get_task(task_id)
        assert row["dsh_session_id"] == "dsh-sess-1"

    def test_stop_returns_stop_exit_code_and_calls_stop(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_done = False  # 运行中（done 返回 False 进入轮询循环）
        dsh_executor.request_stop(1)
        try:
            code, _ = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        finally:
            dsh_executor.clear_stop_request(1)
        assert code == 125
        assert fake_runner.instances[0].stop_calls == 1  # stop 被调用（终止运行时）

    def test_timeout_returns_124_and_calls_stop(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_done = False
        # 超时秒数设为 0：进入轮询循环第一轮即超时（get 触发 load 后改内存值）
        dsh_executor.config.get().task_timeout_seconds = 0
        try:
            code, _ = dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        finally:
            dsh_executor.config.get().task_timeout_seconds = 1800
        assert code == 124
        assert fake_runner.instances[0].stop_calls == 1

    def test_sdk_missing_raises_executor_error(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """SDK 未安装：DshSdkNotInstalledError → ExecutorError（run_task 捕获重试）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        from botler.executor import DshSdkNotInstalledError as _SDKErr

        class _AlwaysMissing(_FakeRunner):
            def start(self):
                raise _SDKErr("deepseek-harness-sdk 未安装")

        monkeypatch.setattr("botler.executor.DshRunner", _AlwaysMissing)
        from botler.executor import ExecutorError
        with pytest.raises(ExecutorError):
            dsh_executor._run_dsh_once(1, _REPO, _ISSUE)

    def test_sse_events_published_from_event_lines(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [
            json.dumps({"event": "stream_delta", "text": "正在处理…"},
                       ensure_ascii=False),
            _RESULT_LINE,
        ]
        sub = dsh_executor.event_bus.subscribe(1, maxsize=10)
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        event = sub.get(timeout=2)
        assert event["kind"] == "text"
        assert event["text"] == "正在处理…"
        sub.close()

    def test_log_file_written_from_lines(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        """事件行与结果行均落盘日志（SSE 回放数据源）。"""
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        fake_runner.preset_lines = [_RESULT_LINE]
        dsh_executor._run_dsh_once(1, _REPO, _ISSUE)
        log_text = (tmp_path / "task_1.log").read_text(encoding="utf-8")
        assert _RESULT_LINE in log_text

    def test_claude_engine_untouched(self, executor, monkeypatch, tmp_path,
                                     fake_runner):
        """engine=claude（默认）时 dsh runner 不被调用（回归保护）。"""
        _patch_workspace(monkeypatch, executor, tmp_path)
        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kw: _fake_drain_proc())
        code, _ = executor._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 dsh 路径

    def test_hermes_engine_untouched(self, monkeypatch, tmp_path, fake_runner):
        """engine=hermes 时 dsh runner 不被调用（回归保护）。"""
        config = _mk_config(
            tmp_path, worker_extra="\n  engine: hermes",
            hermes_extra="\n  command: /opt/hermes/venv/bin/python"
                         "\n  args: [\"/app/backend/hermes_runner.py\"]")
        ex = _mk_executor(tmp_path, config)
        _patch_workspace(monkeypatch, ex, tmp_path)
        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kw: _fake_drain_proc())
        code, _ = ex._run_once(1, _REPO, _ISSUE)
        assert code == 0
        assert fake_runner.instances == []  # 未走 dsh 路径


class TestDshResult:
    """_dsh_result：成功 / 无法解决 / 失败重试（dsh 输出语义）。"""

    def _out(self, final_response, finish_reason="completed", error=None,
             session_id="s1"):
        data = {"final_response": final_response,
                "finish_reason": finish_reason,
                "session_id": session_id}
        if error:
            data["error"] = error
        return json.dumps(data, ensure_ascii=False)

    def test_completed_with_response_is_success(self, executor):
        assert executor._dsh_result(
            self._out("任务完成")) == "success"

    def test_unresolvable_detected(self, executor):
        assert executor._dsh_result(
            self._out("抱歉，无法解决该 issue：依赖缺失")) == "unresolvable"

    def test_max_tokens_is_failure(self, executor):
        """finish_reason=max-tokens（输出截断，未完成）→ 失败重试。"""
        assert executor._dsh_result(
            self._out("做了一半", finish_reason="max-tokens")) == "failed"

    def test_error_finish_reason_is_failure(self, executor):
        assert executor._dsh_result(
            self._out("", finish_reason="error")) == "failed"

    def test_none_finish_reason_is_failure(self, executor):
        """无回合结束（finish_reason=None）→ 失败重试。"""
        assert executor._dsh_result(
            self._out("文本", finish_reason=None)) == "failed"

    def test_error_field_marks_failure(self, executor):
        assert executor._dsh_result(
            self._out("", error="runtime crashed")) == "failed"

    def test_empty_final_response_is_failure(self, executor):
        assert executor._dsh_result(
            self._out("", finish_reason="completed")) == "failed"

    def test_non_json_output_is_failure(self, executor):
        assert executor._dsh_result("no json here") == "failed"

    def test_unknown_finish_reason_is_failure(self, executor):
        """SDK 版本演进产生未知 reason：不静默成功，按失败重试。"""
        assert executor._dsh_result(
            self._out("文本", finish_reason="mystery")) == "failed"


class TestDshSessionPersist:
    def test_persist_valid_session_id(self, executor):
        task_id = _mk_task(executor)
        executor._persist_dsh_session_id(task_id, _RESULT_LINE)
        assert executor.db.get_task(task_id)["dsh_session_id"] == "dsh-sess-1"

    def test_persist_invalid_output_keeps_value(self, executor):
        """输出无结果行（非 JSON）→ 不落库、不抛异常。"""
        task_id = _mk_task(executor)
        executor._persist_dsh_session_id(task_id, "garbage")
        assert executor.db.get_task(task_id)["dsh_session_id"] is None

    def test_persist_missing_session_id_keeps_value(self, executor):
        task_id = _mk_task(executor)
        executor.db.set_task_status(task_id, None, dsh_session_id="old")
        executor._persist_dsh_session_id(
            task_id, json.dumps({"final_response": "x",
                                 "finish_reason": "completed"}))
        assert executor.db.get_task(task_id)["dsh_session_id"] == "old"


class TestRunTaskResume:
    """run_task 断点续跑：dsh_session_id 恢复为 resume_session。"""

    def _run_task(self, dsh_executor, monkeypatch, captured):
        monkeypatch.setattr(dsh_executor, "_call_with_fallback",
                            lambda repo, fn, **kw: (_ISSUE, None))
        monkeypatch.setattr(dsh_executor, "_await_pipeline_and_finish_succeeded",
                            lambda *a, **kw: None)

        def fake_run_once(task_id, repo, issue, resume_session=None,
                          resume_history=None):
            captured["resume_session"] = resume_session
            return 0, _RESULT_LINE

        monkeypatch.setattr(dsh_executor, "_run_once", fake_run_once)

    def test_run_task_resumes_from_dsh_session_id(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        dsh_executor.db.set_task_status(task_id, None, dsh_session_id="sess-9")
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        captured = {}
        self._run_task(dsh_executor, monkeypatch, captured)
        dsh_executor.run_task(task_id)
        assert captured["resume_session"] == "sess-9"

    def test_run_task_fresh_when_no_session(
            self, dsh_executor, monkeypatch, tmp_path, fake_runner):
        task_id = _mk_task(dsh_executor)
        _patch_workspace(monkeypatch, dsh_executor, tmp_path)
        captured = {}
        self._run_task(dsh_executor, monkeypatch, captured)
        dsh_executor.run_task(task_id)
        assert captured["resume_session"] is None
