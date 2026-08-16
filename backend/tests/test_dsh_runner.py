"""dsh 引擎 runner 测试（issue #84：集成 deepseek-harness 方案B）。

覆盖：SDK 未安装探测、正常执行结果行、停止（close 强制终止运行时）、
SDK 异常容错、通知→事件行映射（assistant 文本/思考/工具调用、回合结束、
会话状态、未知事件）、SDK 配置透传（provider/model/cwd/session_root/env）、
断点续跑 session_id 透传。

SDK 不在 requirements.txt（Docker 部署镜像已内置，issue #112；
开发机为可选依赖），测试用 monkeypatch 注入假 deepseek_harness 模块
（与 test_hermes_runner.py 注入假 run_agent 同手法）。
"""

import json
import sys
import threading
import time
import types
from dataclasses import dataclass, field

import pytest

from botler import dsh_runner


# ---- 假 SDK 模块 ----

@dataclass
class FakeRunResult:
    session_id: str = "session-1"
    final_response: str = "已完成任务"
    finish_reason: str = "completed"
    events: list = field(default_factory=list)
    notifications: list = field(default_factory=list)
    session_root: str | None = None


class FakeHarness:
    """可编程假 harness：result / exception / block 三种行为。

    类级 preset（worker 线程启动前预设，避免与 run 的执行时序竞态）：
    preset_block = run 阻塞直到 close 触发（模拟长时间执行）；
    preset_exc = run 直接抛出该异常。
    """

    created: list["FakeHarness"] = []
    closed: list["FakeHarness"] = []
    preset_block: bool = False
    preset_exc: Exception | None = None

    def __init__(self, config=None):
        self.config = config
        self._result = FakeRunResult()
        self._exc = FakeHarness.preset_exc
        self._block = threading.Event() if FakeHarness.preset_block else None
        self._run_kwargs = {}
        FakeHarness.created.append(self)

    def run(self, input, *, session_id=None, on_notification=None):
        self._run_kwargs = {"input": input, "session_id": session_id,
                            "on_notification": on_notification}
        if self._block is not None:
            self._block.wait(timeout=30)
            raise RuntimeError("transport closed: DeepSeek Harness runtime closed")
        if self._exc is not None:
            raise self._exc
        return self._result

    def close(self):
        FakeHarness.closed.append(self)
        if self._block is not None:
            self._block.set()


@dataclass
class FakeSdkConfig:
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    max_tokens: int | None = None
    cwd: str | None = None
    runtime_cwd: str | None = None
    session_root: str | None = None
    cordis: str | None = None
    env: dict = field(default_factory=dict)
    runtime_bin: str | None = None
    launch_args_override: tuple = None
    request_timeout_seconds: float | None = None
    shutdown_timeout_seconds: float | None = 1.0
    base_url: str | None = None
    api_key: str | None = None


@pytest.fixture
def fake_sdk(monkeypatch):
    """注入假 deepseek_harness 模块并重置假 harness 状态。"""
    FakeHarness.created.clear()
    FakeHarness.closed.clear()
    FakeHarness.preset_block = False
    FakeHarness.preset_exc = None
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = FakeHarness
    module.DeepSeekHarnessConfig = FakeSdkConfig
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    monkeypatch.setattr(dsh_runner, "find_spec",
                        lambda name: object() if name == "deepseek_harness" else None)
    return module


@pytest.fixture
def sdk_missing(monkeypatch):
    """模拟 SDK 未安装（find_spec 返回 None）。"""
    sys.modules.pop("deepseek_harness", None)
    monkeypatch.setattr(dsh_runner, "find_spec", lambda name: None)


def _mk_runner(**kwargs):
    """构造 DshRunner，on_line 收集行列表。"""
    lines: list[str] = []
    defaults = dict(prompt="任务 prompt", session_id=None, cwd="/work",
                    env={"GIT_ASKPASS": "/askpass"},
                    on_line=lines.append)
    defaults.update(kwargs)
    runner = dsh_runner.DshRunner(**defaults)
    return runner, lines


def _wait_done(runner, timeout=10.0):
    """等待 worker 线程结束。"""
    end = time.time() + timeout
    while time.time() < end and not runner.done():
        time.sleep(0.01)
    assert runner.done(), f"worker 线程 {timeout}s 内未结束"


def _wait_harness(timeout=5.0) -> FakeHarness:
    """等待 harness 创建（worker 异步），返回假 harness。"""
    end = time.time() + timeout
    while not FakeHarness.created and time.time() < end:
        time.sleep(0.01)
    assert FakeHarness.created, "harness 未创建"
    return FakeHarness.created[-1]


def _last_json(lines):
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            continue
        return data
    return None


# ---- SDK 探测与生命周期 ----

class TestSdkProbe:
    def test_start_raises_when_sdk_missing(self, sdk_missing):
        runner, _ = _mk_runner()
        with pytest.raises(dsh_runner.DshSdkNotInstalledError) as exc:
            runner.start()
        # 错误消息含安装指引（部署机直接可照做）
        assert "deepseek-harness-sdk" in str(exc.value)

    def test_start_ok_when_sdk_present(self, fake_sdk):
        runner, _ = _mk_runner()
        runner.start()
        _wait_done(runner)


class TestSuccessRun:
    def test_success_returns_exit_zero_and_result_line(self, fake_sdk):
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 0
        data = _last_json(lines)
        assert data is not None
        assert data["final_response"] == "已完成任务"
        assert data["finish_reason"] == "completed"
        assert data["session_id"] == "session-1"
        assert "error" not in data

    def test_prompt_and_session_id_passed_to_sdk(self, fake_sdk):
        runner, _ = _mk_runner(prompt="P", session_id="resume-sess-9")
        runner.start()
        _wait_done(runner)
        harness = FakeHarness.created[0]
        assert harness._run_kwargs["input"] == "P"
        assert harness._run_kwargs["session_id"] == "resume-sess-9"

    def test_fresh_run_passes_none_session_id(self, fake_sdk):
        """全新执行不传 session_id（SDK 自生成，结果行回传实际 id）。"""
        runner, _ = _mk_runner(session_id=None)
        runner.start()
        _wait_done(runner)
        assert FakeHarness.created[0]._run_kwargs["session_id"] is None

    def test_sdk_config_transparent_passthrough(self, fake_sdk):
        """botler 配置字段原样传给 SDK（含 env 注入 GIT_ASKPASS 等）。"""
        runner, _ = _mk_runner(
            provider="deepseek-official", model="deepseek-v4-flash",
            max_tokens=49152, cwd="/work", session_root="/sessions",
            cordis="/conf/cordis.yml", runtime_bin="/bin/dsh-agent",
            base_url="https://api.example.com", api_key="sk-test")
        runner.start()
        _wait_done(runner)
        config = FakeHarness.created[0].config
        assert config.provider == "deepseek-official"
        assert config.model == "deepseek-v4-flash"
        assert config.max_tokens == 49152
        assert config.cwd == "/work"
        assert config.session_root == "/sessions"
        assert config.cordis == "/conf/cordis.yml"
        assert config.runtime_bin == "/bin/dsh-agent"
        assert config.base_url == "https://api.example.com"
        assert config.api_key == "sk-test"
        # 子进程环境注入：GIT_ASKPASS 凭据随 env 传给 SDK
        assert config.env["GIT_ASKPASS"] == "/askpass"

    def test_empty_env_ok(self, fake_sdk):
        runner, _ = _mk_runner(env=None)
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 0


class TestStop:
    def test_stop_with_blocked_run(self, fake_sdk):
        """run 阻塞时 stop()：close 被调用、worker 退出、结果行标记 stopped。"""
        FakeHarness.preset_block = True  # run 阻塞直到 close（模拟长时间执行）
        runner, lines = _mk_runner()
        runner.start()
        harness = _wait_harness()
        runner.stop()
        _wait_done(runner)
        assert harness in FakeHarness.closed
        data = _last_json(lines)
        assert data is not None
        assert data.get("error") == "stopped"

    def test_stop_before_start_then_start(self, fake_sdk):
        """停止请求先于 start 到达：worker 直接输出 stopped 行退出。"""
        runner, lines = _mk_runner()
        runner.stop()  # harness 未创建：仅置标志
        runner.start()
        _wait_done(runner)
        data = _last_json(lines)
        assert data is not None and data.get("error") == "stopped"

    def test_double_stop_idempotent(self, fake_sdk):
        """重复 stop 不抛异常（close 幂等）。"""
        runner, _ = _mk_runner()
        runner.start()
        _wait_done(runner)
        runner.stop()
        runner.stop()


class TestFailure:
    def test_sdk_exception_produces_error_line(self, fake_sdk):
        """SDK run 抛异常 → exit 1 + 结果行含 error（判定交给 executor）。"""
        FakeHarness.preset_exc = RuntimeError("model endpoint 500")
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 1
        data = _last_json(lines)
        assert data is not None
        assert "RuntimeError" in data["error"]
        assert data["final_response"] == ""

    def test_finish_returns_one_before_start(self, fake_sdk):
        """未 start 就 finish：不抛异常，返回失败码。"""
        runner, _ = _mk_runner()
        assert runner.finish() == 1


# ---- 通知 → 事件行映射 ----

def _note(method, payload):
    """构造与 SDK Notification 等价的简单对象。"""
    return types.SimpleNamespace(method=method, payload=payload)


class TestFormatNotification:
    def test_text_block_becomes_stream_delta(self):
        lines = dsh_runner.format_dsh_notification("session.event", {
            "sessionId": "s1",
            "event": {"type": "assistant/message",
                      "data": {"message": {"content": [
                          {"type": "text", "text": "正在修复…"}]}}}})
        assert lines == [json.dumps({"event": "stream_delta",
                                     "text": "正在修复…"}, ensure_ascii=False)]

    def test_thinking_block_becomes_thinking(self):
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/message",
                      "data": {"message": {"content": [
                          {"type": "thinking", "thinking": "分析中"}]}}}})
        assert lines == [json.dumps({"event": "thinking",
                                     "text": "分析中"}, ensure_ascii=False)]

    def test_tool_use_block_becomes_tool_start(self):
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/message",
                      "data": {"message": {"content": [
                          {"type": "tool_use", "name": "bash",
                           "input": {"command": "pytest"}}]}}}})
        assert lines == [json.dumps({"event": "tool_start", "tool": "bash",
                                     "input": {"command": "pytest"}},
                                    ensure_ascii=False)]

    def test_multi_block_keeps_order(self):
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/message",
                      "data": {"message": {"content": [
                          {"type": "text", "text": "第一步"},
                          {"type": "tool_use", "name": "bash",
                           "input": {"command": "ls"}},
                          {"type": "text", "text": "完成"}]}}}})
        assert len(lines) == 3
        assert json.loads(lines[0])["event"] == "stream_delta"
        assert json.loads(lines[1])["event"] == "tool_start"
        assert json.loads(lines[2])["event"] == "stream_delta"

    def test_turn_end_becomes_status(self):
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "turn/end",
                      "data": {"reason": {"kind": "completed"}}}})
        assert lines == [json.dumps({"event": "status",
                                     "message": "回合结束: completed"},
                                    ensure_ascii=False)]

    def test_turn_end_error_includes_failure_message(self):
        """issue #115：turn/end 的 error 细节透传（401 等失败原因可见）。

        任务 #194 #195 失败详情只有「回合结束: error」，看不到 AUTH 401
        原因；error.message 必须拼进状态行供日志/SSE/error_detail 诊断。
        """
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "turn/end",
                      "data": {"reason": {
                          "kind": "error",
                          "error": {"message": "Authentication Fails, "
                                              "Your api key is invalid",
                                    "code": "AUTH", "status": 401}}}}})
        data = json.loads(lines[0])
        assert data["event"] == "status"
        assert data["message"] == ("回合结束: error（Authentication Fails, "
                                   "Your api key is invalid）")

    def test_turn_end_error_with_failure_field(self):
        """assistant/chunk 风格的 failure 字段同样透传。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "turn/end",
                      "data": {"reason": {
                          "kind": "error",
                          "failure": {"message": "model timeout",
                                      "code": "TIMEOUT"}}}}})
        data = json.loads(lines[0])
        assert data["message"] == "回合结束: error（model timeout）"

    def test_turn_end_max_tokens_plain(self):
        """max-tokens 等无 message 的 kind：不附加细节（行为不变）。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "turn/end",
                      "data": {"reason": {"kind": "max-tokens"}}}})
        assert lines == [json.dumps({"event": "status",
                                     "message": "回合结束: max-tokens"},
                                    ensure_ascii=False)]

    def test_assistant_chunk_finish_error_becomes_status(self):
        """assistant/chunk 的 finish/error 块：透传 failure message 为状态行。

        真实运行中 LLM 调用失败的细节在这个块里（turn/end 只带摘要），
        此前整块落 raw 丢 message，任务日志无法诊断。
        """
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/chunk",
                      "data": {"turn": 1, "step": 1,
                               "chunk": {"type": "finish",
                                         "reason": {"kind": "error",
                                                    "failure": {
                                                        "message": "model "
                                                                   "overloaded",
                                                        "code": "BUSY",
                                                        "status": 503}}}}}})
        data = json.loads(lines[0])
        assert data["event"] == "status"
        assert data["message"] == "模型调用失败: model overloaded"

    def test_assistant_chunk_non_error_stays_raw(self):
        """assistant/chunk 的未识别块类型仍落 raw（行为不变）。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/chunk",
                      "data": {"chunk": {"type": "delta",
                                         "text": "流式增量"}}}})
        assert json.loads(lines[0]) == {"event": "raw",
                                        "type": "assistant/chunk"}

    def test_reasoning_block_becomes_thinking(self):
        """真实 SDK 的思考块类型是 reasoning（非 thinking）：必须透传为
        thinking 事件行（任务 #194 #195 诊断发现：思考内容从未出现在
        SSE/任务日志中，诊断链路缺失过程可见性）。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/message",
                      "data": {"message": {"content": [
                          {"type": "reasoning",
                           "text": "先检查 401 根因"}]}}}})
        assert lines == [json.dumps({"event": "thinking",
                                     "text": "先检查 401 根因"},
                                    ensure_ascii=False)]

    def test_assistant_chunk_text_delta_becomes_stream_delta(self):
        """真实 SDK 流式增量块 text-delta：透传为 stream_delta（实时流）。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/chunk",
                      "data": {"turn": 1, "step": 1,
                               "chunk": {"type": "text-delta",
                                         "index": 1, "text": "正在修复"}}}})
        assert lines == [json.dumps({"event": "stream_delta",
                                     "text": "正在修复"},
                                    ensure_ascii=False)]

    def test_assistant_chunk_reasoning_delta_becomes_thinking(self):
        """真实 SDK 思考增量块 reasoning-delta：透传为 thinking 事件行。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "assistant/chunk",
                      "data": {"turn": 1, "step": 1,
                               "chunk": {"type": "reasoning-delta",
                                         "index": 0, "text": "分析配置链路"}}}})
        assert lines == [json.dumps({"event": "thinking",
                                     "text": "分析配置链路"},
                                    ensure_ascii=False)]

    def test_session_status_notification(self):
        lines = dsh_runner.format_dsh_notification(
            "session.status", {"sessionId": "s1", "status": "idle"})
        assert lines == [json.dumps({"event": "status",
                                     "message": "dsh 会话状态: idle"},
                                    ensure_ascii=False)]

    def test_unknown_session_event_becomes_raw(self):
        """未知 session.event 类型：raw 行落日志（parse 不发布 SSE，仅诊断）。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "mystery/event", "data": {}}})
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"event": "raw", "type": "mystery/event"}

    def test_unknown_notification_method_becomes_raw(self):
        lines = dsh_runner.format_dsh_notification("subagent.heartbeat",
                                                   {"childSessionId": "c1"})
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "raw"
        assert json.loads(lines[0])["method"] == "subagent.heartbeat"

    def test_inbox_receipt_skipped(self):
        """SDK 内部簿记通知（agent/inbox/spliced）→ 无行。"""
        lines = dsh_runner.format_dsh_notification("session.event", {
            "event": {"type": "agent/inbox/spliced", "data": {}}})
        assert lines == []

    def test_malformed_payload_tolerated(self):
        """payload 畸形（缺 event/data 字段）→ 不抛异常。"""
        assert dsh_runner.format_dsh_notification("session.event", {}) == []
        assert dsh_runner.format_dsh_notification("session.event",
                                                  {"event": "not-a-dict"}) == []
        assert dsh_runner.format_dsh_notification(
            "session.event",
            {"event": {"type": "assistant/message"}}) == []  # 缺 data


class TestResultLine:
    def test_build_result_line(self):
        line = dsh_runner.build_result_line(
            "完成", "completed", "session-7")
        assert json.loads(line) == {"final_response": "完成",
                                    "finish_reason": "completed",
                                    "session_id": "session-7"}

    def test_build_result_line_with_error(self):
        line = dsh_runner.build_result_line(None, None, "s", error="boom")
        data = json.loads(line)
        assert data["error"] == "boom"
        assert data["final_response"] == ""
