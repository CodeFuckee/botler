"""hermes runner 脚本测试（issue #47：hermes 引擎）。

backend/hermes_runner.py 是独立脚本（不依赖 botler 包），由 hermes venv 的
python 运行：stdin 读 JSON 请求（prompt/history/session_id），进程内调用
run_agent.AIAgent（quiet_mode）处理任务，stdout 输出单行 JSON 结果
（final_response/messages/session_id/error），供 executor 落库断点续跑。

测试覆盖协议解析边界与 AIAgent 调用契约（mock run_agent 模块）。
"""

import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermes_runner


class TestLoadRequest:
    """load_request：stdin JSON 协议解析。"""

    def test_full_request(self):
        req = hermes_runner.load_request(io.StringIO(json.dumps({
            "prompt": "处理 issue #1",
            "history": [{"role": "user", "content": "上次任务"}],
            "session_id": "sess-123",
        })))
        assert req["prompt"] == "处理 issue #1"
        assert req["history"] == [{"role": "user", "content": "上次任务"}]
        assert req["session_id"] == "sess-123"

    def test_minimal_request_only_prompt(self):
        req = hermes_runner.load_request(io.StringIO(json.dumps(
            {"prompt": "p"})))
        assert req["prompt"] == "p"
        assert req.get("history") is None
        assert req.get("session_id") is None

    def test_empty_input_rejected(self):
        with pytest.raises(hermes_runner.RequestError):
            hermes_runner.load_request(io.StringIO(""))

    def test_invalid_json_rejected(self):
        with pytest.raises(hermes_runner.RequestError):
            hermes_runner.load_request(io.StringIO("{not json"))

    def test_missing_prompt_rejected(self):
        with pytest.raises(hermes_runner.RequestError):
            hermes_runner.load_request(io.StringIO(json.dumps(
                {"history": []})))

    def test_prompt_empty_string_rejected(self):
        with pytest.raises(hermes_runner.RequestError):
            hermes_runner.load_request(io.StringIO(json.dumps({"prompt": ""})))

    def test_history_wrong_type_rejected(self):
        """history 必须是列表（或缺失）——字符串等非法类型直接报错。"""
        with pytest.raises(hermes_runner.RequestError):
            hermes_runner.load_request(io.StringIO(json.dumps(
                {"prompt": "p", "history": "not-a-list"})))


class TestBuildResult:
    """build_result：结果 JSON 结构。"""

    def test_success_result(self):
        result = hermes_runner.build_result(
            "完成", [{"role": "assistant", "content": "完成"}], "sess-1")
        assert result["final_response"] == "完成"
        assert result["messages"] == [{"role": "assistant", "content": "完成"}]
        assert result["session_id"] == "sess-1"
        assert result["error"] is None

    def test_error_result(self):
        result = hermes_runner.build_result("", [], "", error="boom")
        assert result["error"] == "boom"
        assert result["final_response"] == ""

    def test_messages_default_empty_list(self):
        """messages 缺失时回退空列表（断点续跑恢复需可迭代）。"""
        result = hermes_runner.build_result("ok", None, "s1")
        assert result["messages"] == []


def _install_fake_run_agent(monkeypatch, agent_cls=None):
    """在 sys.modules 注入 fake run_agent 模块，供 runner import。"""
    fake = SimpleNamespace()
    fake.AIAgent = agent_cls or _FakeAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    return fake


class _FakeAIAgent:
    """记录构造参数并返回预设结果的 AIAgent 替身。"""

    init_kwargs = None
    conversation_calls = []

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "sess-test"

    def run_conversation(self, user_message, system_message=None,
                         conversation_history=None, **kwargs):
        type(self).conversation_calls.append({
            "user_message": user_message,
            "conversation_history": conversation_history,
        })
        return {"final_response": "已处理完成",
                "messages": [{"role": "user", "content": user_message},
                             {"role": "assistant", "content": "已处理完成"}]}


class TestMain:
    """main：完整执行流程（mock run_agent.AIAgent）。"""

    def _run(self, request: dict, monkeypatch, agent_cls=None) -> tuple[int, str]:
        _install_fake_run_agent(monkeypatch, agent_cls)
        out = io.StringIO()
        code = hermes_runner.main(
            stdin=io.StringIO(json.dumps(request)), stdout=out)
        return code, out.getvalue()

    def test_success_flow(self, monkeypatch):
        _FakeAIAgent.init_kwargs = None
        _FakeAIAgent.conversation_calls = []
        code, out = self._run({"prompt": "处理 issue #7"}, monkeypatch)
        assert code == 0
        result = json.loads(out)
        assert result["final_response"] == "已处理完成"
        assert result["error"] is None
        assert result["session_id"]  # 会话 id 非空（落库用）
        assert len(result["messages"]) == 2
        # 无人值守模式必须开启
        assert _FakeAIAgent.init_kwargs["quiet_mode"] is True
        # 提示词作为 user_message 传入
        call = _FakeAIAgent.conversation_calls[0]
        assert call["user_message"] == "处理 issue #7"
        assert call["conversation_history"] is None

    def test_history_passed_to_conversation(self, monkeypatch):
        _FakeAIAgent.conversation_calls = []
        history = [{"role": "user", "content": "上次内容"}]
        code, out = self._run(
            {"prompt": "继续", "history": history, "session_id": "sess-9"},
            monkeypatch)
        assert code == 0
        call = _FakeAIAgent.conversation_calls[0]
        # 断点续跑：上次消息历史作为 conversation_history 传入
        assert call["conversation_history"] == history
        # 会话 id 复用（hermes 侧 state.db 接续同一会话）
        assert _FakeAIAgent.init_kwargs["session_id"] == "sess-9"

    def test_import_failure_reports_error(self, monkeypatch):
        """hermes 未安装（import run_agent 失败）→ exit 1 + error JSON。"""
        monkeypatch.delitem(sys.modules, "run_agent", raising=False)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "run_agent":
                raise ModuleNotFoundError("No module named 'run_agent'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        out = io.StringIO()
        code = hermes_runner.main(
            stdin=io.StringIO(json.dumps({"prompt": "p"})), stdout=out)
        assert code == 1
        result = json.loads(out.getvalue())
        assert result["error"]
        assert "run_agent" in result["error"]

    def test_agent_exception_reports_error(self, monkeypatch):
        class _BoomAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, **kwargs):
                raise RuntimeError("agent 内部崩溃")

        code, out = self._run({"prompt": "p"}, monkeypatch, _BoomAgent)
        assert code == 1
        result = json.loads(out)
        assert "agent 内部崩溃" in result["error"]

    def test_request_error_reports_error(self, monkeypatch):
        """协议错误（缺 prompt）→ exit 1 + error JSON（不崩溃、不 trace）。"""
        out = io.StringIO()
        code = hermes_runner.main(stdin=io.StringIO("{}"), stdout=out)
        assert code == 1
        result = json.loads(out.getvalue())
        assert result["error"]

    def test_quiet_result_with_empty_messages(self, monkeypatch):
        """messages 为空列表的结果也合法（某些 provider 不返回消息历史）。"""

        class _NoHistoryAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, **kwargs):
                return {"final_response": "ok", "messages": []}

        code, out = self._run({"prompt": "p"}, monkeypatch, _NoHistoryAgent)
        assert code == 0
        result = json.loads(out)
        assert result["messages"] == []
        assert result["final_response"] == "ok"

    def test_missing_final_response_treated_as_error(self, monkeypatch):
        """结果缺少 final_response（异常返回结构）→ 报告错误而不是静默成功。"""

        class _NoFieldAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, **kwargs):
                return {"unexpected": "shape"}

        code, out = self._run({"prompt": "p"}, monkeypatch, _NoFieldAgent)
        assert code == 1
        result = json.loads(out)
        assert result["error"]
