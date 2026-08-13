"""hermes runner 流式协议测试（实时输出功能第三轮）。

需求：hermes 引擎任务执行期间逐事件实时可见。
- runner 构造 AIAgent 时注册回调（thinking/tool_start/tool_complete/
  stream_delta/status），回调把事件行实时写到 stdout（逐行 flush）
- stdout 输出协议：NDJSON 事件行在前，最后一行结果 JSON 收尾
  （与旧单行协议兼容：无回调触发时只有结果行）
- 回调异常不阻断任务执行与结果输出
"""

import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermes_runner


def _install_fake_run_agent(monkeypatch, agent_cls):
    """在 sys.modules 注入 fake run_agent 模块，供 runner import。"""
    fake = SimpleNamespace()
    fake.AIAgent = agent_cls or _FakeAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    return fake


class _FakeAIAgent:
    """记录构造参数（含回调）并可手动触发回调的 AIAgent 替身。"""

    init_kwargs = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "sess-test"

    def run_conversation(self, user_message, system_message=None,
                         conversation_history=None, **kwargs):
        # 模拟 hermes 执行过程中的回调触发（按 hermes-agent 真实签名：
        # tool_start_callback(id, name, args)、tool_complete_callback(id, name, args, result)）
        kws = type(self).init_kwargs
        kws["thinking_callback"]("分析登录报错原因")
        kws["tool_start_callback"]("tc1", "bash", "pytest tests/")
        kws["tool_complete_callback"]("tc1", "bash", "pytest tests/", "42 passed")
        kws["stream_delta_callback"]("已完成修复。")
        kws["status_callback"]("任务进入收尾阶段")
        return {"final_response": "已处理完成",
                "messages": [{"role": "user", "content": user_message},
                             {"role": "assistant", "content": "已处理完成"}]}


class _BoomCallbackAgent(_FakeAIAgent):
    """回调抛异常的替身：验证异常不阻断结果输出。"""

    def run_conversation(self, user_message, system_message=None,
                         conversation_history=None, **kwargs):
        kws = type(self).init_kwargs
        kws["thinking_callback"]("正常思考")
        # 不可 JSON 序列化的结果 → 事件行序列化抛异常（回调包装器需容错）
        kws["tool_complete_callback"]("tc1", "bash", "ls", {"set", "不可序列化"})
        return {"final_response": "已处理完成",
                "messages": [{"role": "assistant", "content": "完成"}]}


class TestStreamEvents:
    """runner 注册回调并把事件行实时写到 stdout。"""

    def _run(self, monkeypatch, agent_cls=None) -> tuple[int, list[str]]:
        _install_fake_run_agent(monkeypatch, agent_cls)
        out = io.StringIO()
        code = hermes_runner.main(
            stdin=io.StringIO(json.dumps({"prompt": "处理 issue #7"})),
            stdout=out)
        return code, out.getvalue().splitlines()

    def test_agent_constructed_with_callbacks(self, monkeypatch):
        _FakeAIAgent.init_kwargs = None
        self._run(monkeypatch)
        kws = _FakeAIAgent.init_kwargs
        for name in ("thinking_callback", "tool_start_callback",
                     "tool_complete_callback", "stream_delta_callback",
                     "status_callback"):
            assert callable(kws.get(name)), f"缺少回调 {name}"

    def test_event_lines_then_result_line(self, monkeypatch):
        _FakeAIAgent.init_kwargs = None
        code, lines = self._run(monkeypatch)
        assert code == 0
        # 前 N-1 行是事件行，最后一行是结果 JSON
        events = [json.loads(l) for l in lines[:-1]]
        result = json.loads(lines[-1])
        assert result["final_response"] == "已处理完成"
        kinds = [e["event"] for e in events]
        assert kinds == ["thinking", "tool_start", "tool_complete",
                         "stream_delta", "status"]
        assert events[0]["text"] == "分析登录报错原因"
        assert events[1]["tool"] == "bash"
        assert events[2]["output"] == "42 passed"
        assert events[2]["is_error"] is False
        assert events[3]["text"] == "已完成修复。"

    def test_no_callbacks_means_single_result_line(self, monkeypatch):
        """回调从未触发（安静执行）→ 输出只有结果行（旧协议兼容）。"""
        class _QuietAgent(_FakeAIAgent):
            def run_conversation(self, user_message, **kwargs):
                return {"final_response": "安静完成", "messages": [],
                        "session_id": self.session_id}

        code, lines = self._run(monkeypatch, agent_cls=_QuietAgent)
        assert code == 0
        assert len(lines) == 1
        assert json.loads(lines[0])["final_response"] == "安静完成"

    def test_callback_exception_does_not_break_result(self, monkeypatch):
        """回调内部异常（如非字符串参数）不阻断结果输出。"""
        code, lines = self._run(monkeypatch, agent_cls=_BoomCallbackAgent)
        assert code == 0
        result = json.loads(lines[-1])
        assert result["final_response"] == "已处理完成"
        # 异常回调前的正常事件行仍在
        events = [json.loads(l) for l in lines[:-1]]
        assert events[0]["event"] == "thinking"
