"""hermes 引擎 SDK runner 测试（issue #171：hermes agent SDK 进程内集成）。

覆盖：SDK 未安装探测、正常执行结果行（final_response/messages/session_id）、
prompt/history/session_id/task_id 透传、事件回调 → NDJSON 事件行（thinking/
tool_start/tool_complete/stream_delta/status）、停止（AIAgent.interrupt() 跨
线程中断阻塞中的 run_conversation）、SDK 异常容错、会话级 cwd 覆盖注册与
清理、进程环境注入（git 凭据/TERMINAL_CWD）与还原。

SDK 不在 requirements.txt（pm2 部署经 deploy/install-hermes-agent.sh
editable 安装，Docker 部署由 entrypoint 启动时安装），测试用 monkeypatch
注入假 run_agent 模块（与旧 test_hermes_runner.py 注入假 run_agent 同手法，
DshRunner 测试 test_dsh_runner.py 同模式）。
"""

import json
import os
import sys
import threading
import time
import types

import pytest

from botler import hermes_sdk_runner

# ---- 假 run_agent 模块（SDK）----


class FakeAIAgent:
    """可编程假 AIAgent：result / exception / block 三种行为。

    类级 preset（worker 线程启动前预设，避免与 run 的执行时序竞态）：
    preset_block = run_conversation 阻塞直到 interrupt()（模拟长时间执行）；
    preset_exc = run_conversation 直接抛出该异常；
    preset_result = run_conversation 正常返回的结果 dict。
    """

    created: list["FakeAIAgent"] = []
    preset_result: dict = {}
    preset_exc: Exception | None = None
    preset_block: bool = False
    # run_conversation 执行期间由 SDK 侧触发的回调（(回调名, 位置参数) 列表）
    preset_callbacks: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "hermes-sess-1"
        self._block = threading.Event() if FakeAIAgent.preset_block else None
        self.interrupt_calls = 0
        self.run_kwargs: dict = {}
        FakeAIAgent.created.append(self)

    def run_conversation(self, **kwargs):
        self.run_kwargs = kwargs
        # 模拟 SDK 执行期间逐条触发事件回调（与真实 AIAgent 一致）
        for cb_name, args in FakeAIAgent.preset_callbacks:
            cb = self.kwargs.get(cb_name)
            if callable(cb):
                cb(*args)
        if self._block is not None:
            self._block.wait(timeout=30)
            # 中断后 conversation loop 返回 interrupted 结果（无 final_response）
            return {"interrupted": True}
        if FakeAIAgent.preset_exc is not None:
            raise FakeAIAgent.preset_exc
        return FakeAIAgent.preset_result

    def interrupt(self):
        self.interrupt_calls += 1
        if self._block is not None:
            self._block.set()


# 假 tools.terminal_tool（会话级 cwd 覆盖注册/清理断言用）
class FakeTerminalTool:
    registered: list[tuple] = []
    cleared: list[str] = []

    @classmethod
    def register_task_env_overrides(cls, task_id, overrides):
        cls.registered.append((task_id, overrides))

    @classmethod
    def clear_task_env_overrides(cls, task_id):
        cls.cleared.append(task_id)


@pytest.fixture
def fake_sdk(monkeypatch):
    """注入假 run_agent 模块并重置假 AIAgent 状态。"""
    FakeAIAgent.created.clear()
    FakeAIAgent.preset_result = {
        "final_response": "已修复并推送，issue #7 处理完成",
        "messages": [{"role": "user", "content": "任务"},
                     {"role": "assistant", "content": "完成"}],
        "session_id": "hermes-sess-1",
    }
    FakeAIAgent.preset_exc = None
    FakeAIAgent.preset_block = False
    FakeAIAgent.preset_callbacks = []
    module = types.ModuleType("run_agent")
    module.AIAgent = FakeAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setattr(hermes_sdk_runner, "find_spec",
                        lambda name: object() if name == "run_agent" else None)
    # 假 tools.terminal_tool（run_agent 之外的工具模块路径）
    tmod = types.ModuleType("tools.terminal_tool")
    tmod.register_task_env_overrides = FakeTerminalTool.register_task_env_overrides
    tmod.clear_task_env_overrides = FakeTerminalTool.clear_task_env_overrides
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", tmod)
    FakeTerminalTool.registered.clear()
    FakeTerminalTool.cleared.clear()
    return module


@pytest.fixture
def sdk_missing(monkeypatch):
    """模拟 SDK 未安装（find_spec 返回 None）。"""
    sys.modules.pop("run_agent", None)
    monkeypatch.setattr(hermes_sdk_runner, "find_spec", lambda name: None)


def _mk_runner(**kwargs):
    """构造 HermesSdkRunner，on_line 收集行列表。"""
    lines: list[str] = []
    defaults = dict(prompt="任务 prompt", session_id=None, history=None,
                    task_id="42", cwd="/work",
                    env={"GIT_ASKPASS": "/askpass"},
                    on_line=lines.append)
    defaults.update(kwargs)
    runner = hermes_sdk_runner.HermesSdkRunner(**defaults)
    return runner, lines


def _wait_done(runner, timeout=10.0):
    """等待 worker 线程结束。"""
    end = time.time() + timeout
    while time.time() < end and not runner.done():
        time.sleep(0.01)
    assert runner.done(), f"worker 线程 {timeout}s 内未结束"


def _wait_agent(timeout=5.0) -> FakeAIAgent:
    """等待 AIAgent 构造（worker 异步），返回假 agent。"""
    end = time.time() + timeout
    while not FakeAIAgent.created and time.time() < end:
        time.sleep(0.01)
    assert FakeAIAgent.created, "AIAgent 未创建"
    return FakeAIAgent.created[-1]


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
        """SDK 未安装：start() 抛 HermesSdkNotInstalledError（含安装指引）。"""
        runner, _ = _mk_runner()
        with pytest.raises(hermes_sdk_runner.HermesSdkNotInstalledError) as ei:
            runner.start()
        assert "install-hermes-agent.sh" in str(ei.value)

    def test_start_ok_when_sdk_present(self, fake_sdk):
        """SDK 已安装：start() 启动 worker 并正常完成。"""
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 0


class TestSuccessRun:
    def test_success_returns_exit_zero_and_result_line(self, fake_sdk):
        """正常执行：退出码 0，结果行含 final_response/messages/session_id。"""
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 0
        result = _last_json(lines)
        assert result is not None
        assert result["error"] is None
        assert "已修复并推送" in result["final_response"]
        assert result["messages"] == FakeAIAgent.preset_result["messages"]
        assert result["session_id"] == "hermes-sess-1"

    def test_prompt_history_session_taskid_passed_to_sdk(self, fake_sdk):
        """prompt / history / session_id / task_id 透传给 run_conversation。"""
        runner, _ = _mk_runner(
            prompt="恢复会话继续执行", history=[{"role": "user", "content": "上次"}],
            session_id="hermes-sess-1", task_id="77")
        runner.start()
        _wait_done(runner)
        agent = _wait_agent()
        assert agent.run_kwargs["user_message"] == "恢复会话继续执行"
        assert agent.run_kwargs["conversation_history"] == [
            {"role": "user", "content": "上次"}]
        assert agent.run_kwargs["task_id"] == "77"
        # AIAgent 构造参数
        assert agent.kwargs["quiet_mode"] is True
        assert agent.kwargs["session_id"] == "hermes-sess-1"
        for cb in ("thinking_callback", "tool_start_callback",
                   "tool_complete_callback", "stream_delta_callback",
                   "status_callback"):
            assert callable(agent.kwargs.get(cb)), f"缺回调 {cb}"

    def test_fresh_run_passes_none_session_and_history(self, fake_sdk):
        """全新会话：session_id / history 传 None，task_id 透传。"""
        runner, _ = _mk_runner(session_id=None, history=None)
        runner.start()
        _wait_done(runner)
        agent = _wait_agent()
        assert agent.kwargs["session_id"] is None
        assert agent.run_kwargs["conversation_history"] is None

    def test_event_lines_emitted_before_result_line(self, fake_sdk):
        """回调 → 事件行（thinking/tool_start/tool_complete/stream_delta/status），
        结果行收尾。"""
        FakeAIAgent.preset_callbacks = [
            ("thinking_callback", ("思考中…",)),
            ("tool_start_callback", ("call_1", "bash", "ls -la")),
            ("tool_complete_callback", ("call_1", "bash", "ls -la", "文件列表")),
            ("stream_delta_callback", ("增量文本",)),
            ("status_callback", ("状态消息",)),
        ]
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        events = [json.loads(l) for l in lines if "final_response" not in l]
        kinds = [e["event"] for e in events]
        assert kinds == ["thinking", "tool_start", "tool_complete",
                         "stream_delta", "status"]
        assert events[1]["tool"] == "bash"
        # 结果行是最后一行
        assert "final_response" in json.loads(lines[-1])

    def test_callback_exception_does_not_break_result(self, fake_sdk):
        """单个回调抛异常不阻断结果输出（容错）。"""
        FakeAIAgent.preset_callbacks = [("tool_start_callback", (object(),))]
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert "final_response" in json.loads(lines[-1])


class TestStop:
    def test_stop_with_blocked_run(self, fake_sdk):
        """运行中 stop()：interrupt 中断阻塞的 run_conversation，线程结束。"""
        FakeAIAgent.preset_block = True
        runner, lines = _mk_runner()
        runner.start()
        agent = _wait_agent()
        assert not runner.done()
        runner.stop()
        _wait_done(runner, timeout=10)
        assert agent.interrupt_calls == 1
        # 中断结果行带 error 说明
        result = _last_json(lines)
        assert result is not None
        assert result["error"] == "任务被用户停止"
        assert runner.finish() == 1

    def test_stop_before_start_then_start(self, fake_sdk):
        """agent 未构造时 stop() 仅置标志；worker 启动后检查标志直接退出。"""
        FakeAIAgent.preset_block = True
        runner, lines = _mk_runner()
        runner.stop()  # agent 尚未构造
        runner.start()
        _wait_done(runner, timeout=10)
        assert runner.done()
        # 未构造 AIAgent（未发起执行），输出中断说明结果行
        assert FakeAIAgent.created == []
        result = _last_json(lines)
        assert result is not None and result["error"] == "任务被用户停止"

    def test_double_stop_idempotent(self, fake_sdk):
        """重复 stop() 幂等（interrupt 调用不报错）。"""
        FakeAIAgent.preset_block = True
        runner, _ = _mk_runner()
        runner.start()
        _wait_agent()
        runner.stop()
        runner.stop()
        _wait_done(runner, timeout=10)
        assert runner.done()


class TestFailure:
    def test_sdk_exception_produces_error_line(self, fake_sdk):
        """run_conversation 抛异常：结果行带 error，finish 返回 1。"""
        FakeAIAgent.preset_exc = RuntimeError("agent 崩了")
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 1
        result = _last_json(lines)
        assert result["error"] is not None
        assert "agent 崩了" in result["error"]

    def test_import_failure_produces_error_line(self, monkeypatch):
        """run_agent 可 find_spec 但 import 失败（残缺安装）：error 结果行。"""
        class _BadModule(types.ModuleType):
            def __getattr__(self, name):
                raise ImportError(f"broken run_agent: {name}")
        monkeypatch.setitem(sys.modules, "run_agent", _BadModule("run_agent"))
        monkeypatch.setattr(hermes_sdk_runner, "find_spec",
                            lambda name: object())
        runner, lines = _mk_runner()
        runner.start()
        _wait_done(runner)
        assert runner.finish() == 1
        result = _last_json(lines)
        assert "无法导入 run_agent.AIAgent" in result["error"]

    def test_finish_returns_one_before_start(self, fake_sdk):
        """未 start() 直接 finish()：返回 1（不崩溃）。"""
        runner, _ = _mk_runner()
        assert runner.finish() == 1


class TestEnvAndCwd:
    def test_env_applied_during_run_and_restored(self, fake_sdk):
        """env（git 凭据等）worker 内注入 os.environ，结束后还原。"""
        FakeAIAgent.preset_block = True  # run 阻塞中观察 env
        os.environ["GIT_ASKPASS"] = "/old-askpass"
        os.environ["SOME_ORIG"] = "orig"
        runner, _ = _mk_runner(env={"GIT_ASKPASS": "/new-askpass",
                                    "SOME_ORIG": "new",
                                    "NEW_KEY": "1"})
        runner.start()
        _wait_agent()
        # run 期间 env 已生效（AIAgent 构造发生在 env 注入之后）
        assert os.environ.get("GIT_ASKPASS") == "/new-askpass"
        assert os.environ.get("SOME_ORIG") == "new"
        assert os.environ.get("NEW_KEY") == "1"
        runner.stop()
        _wait_done(runner)
        # 结束后还原（含删除 NEW_KEY）
        assert os.environ.get("GIT_ASKPASS") == "/old-askpass"
        assert os.environ.get("SOME_ORIG") == "orig"
        assert os.environ.get("NEW_KEY") is None

    def test_terminal_cwd_set_and_restored(self, fake_sdk):
        """TERMINAL_CWD 注入工作区路径，结束后还原。"""
        FakeAIAgent.preset_block = True  # run 阻塞中观察 TERMINAL_CWD
        os.environ["TERMINAL_CWD"] = "/old-cwd"
        runner, _ = _mk_runner(cwd="/work/repo")
        runner.start()
        _wait_agent()
        assert os.environ.get("TERMINAL_CWD") == "/work/repo"
        runner.stop()
        _wait_done(runner)
        assert os.environ.get("TERMINAL_CWD") == "/old-cwd"

    def test_cwd_override_registered_and_cleared(self, fake_sdk):
        """会话级 cwd 覆盖（register/clear_task_env_overrides）按 task_id 调用。"""
        runner, _ = _mk_runner(task_id="42", cwd="/work/repo")
        runner.start()
        _wait_done(runner)
        assert ("42", {"cwd": "/work/repo"}) in FakeTerminalTool.registered
        assert "42" in FakeTerminalTool.cleared
