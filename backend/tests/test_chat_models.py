"""灵感 AI 对话模型调用接口测试（issue #166）。

覆盖 ChatModelClient 三种协议实现（OpenAI 兼容 chat/completions /
Gemini generateContent / Anthropic messages）的请求构造、响应解析与
错误处理，以及 resolve_chat_provider 的供应商选择逻辑。用
httpx.MockTransport 模拟 HTTP，不做真实外呼。
"""

import json
from types import SimpleNamespace

import pytest
import httpx

from botler.chat_models import (
    DEFAULT_BASE_URLS,
    ChatModelClient,
    ChatModelError,
    resolve_chat_provider,
)


def _make_client(provider="deepseek", **kwargs):
    """构造客户端并注入 MockTransport（默认返回 OpenAI 兼容成功响应）。"""
    handler = kwargs.pop("handler", _ok_handler)
    client = ChatModelClient(
        name=kwargs.pop("name", "测试供应商"),
        provider=provider,
        api_key=kwargs.pop("api_key", "sk-test"),
        **kwargs)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """默认成功响应：OpenAI 兼容结构。"""
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "你好，我是 AI"}}],
    })


class TestConstructor:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ChatModelError, match="不支持的 AI 对话模型类型"):
            ChatModelClient(name="x", provider="unknown_provider", api_key="k")

    def test_empty_provider_rejected(self):
        with pytest.raises(ChatModelError, match="不支持的 AI 对话模型类型"):
            ChatModelClient(name="x", provider="", api_key="k")

    def test_custom_without_base_url_rejected(self):
        with pytest.raises(ChatModelError, match="未配置 Base URL"):
            ChatModelClient(name="x", provider="custom", api_key="k")

    def test_default_base_url_and_model_filled(self):
        """配置留空时兜底预设默认端点 / 模型。"""
        client = ChatModelClient(
            name="d", provider="deepseek", api_key="k")
        assert client.base_url == "https://api.deepseek.com/v1"
        assert client.model == "deepseek-chat"

    def test_custom_base_url_used_as_full_endpoint(self):
        """自定义 base_url（≠ 预设默认）作为完整请求地址直接使用。"""
        client = ChatModelClient(
            name="d", provider="deepseek", api_key="k",
            base_url="https://gw.example.com/v1/chat/completions")
        assert client.base_url == "https://gw.example.com/v1/chat/completions"

    def test_all_presets_have_defaults(self):
        """前端预设全部 provider 均有默认地址（custom 除外）。"""
        for provider, base in DEFAULT_BASE_URLS.items():
            if provider == "custom":
                continue
            assert base, f"{provider} 应有默认地址"


class TestChatCommon:
    def test_missing_api_key_rejected(self):
        client = ChatModelClient(
            name="d", provider="deepseek", api_key="", model="m")
        client._http = httpx.Client(transport=httpx.MockTransport(_ok_handler))
        with pytest.raises(ChatModelError, match="未配置 API Key"):
            client.chat([{"role": "user", "content": "hi"}])


class TestOpenAICompat:
    def test_send_request_shape(self):
        """deepseek 走 chat/completions：路径、鉴权头与 payload 正确。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return _ok_handler(request)

        client = _make_client("deepseek", handler=handler)
        reply = client.chat([
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "你好"},
        ])
        assert reply == "你好，我是 AI"
        assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"] == {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "你好"},
            ],
        }

    def test_custom_base_url_without_chat_completions_appends_path(self):
        """custom Base URL 只填 API 前缀时自动补拼对话操作路径。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return _ok_handler(request)

        client = _make_client(
            "custom", handler=handler,
            base_url="https://new.s1.prod.gglohh.top/v1")
        client.chat([{"role": "user", "content": "生成提示词"}])
        assert seen["url"] == (
            "https://new.s1.prod.gglohh.top/v1/chat/completions")

    def test_custom_base_url_no_path_appended(self):
        """自定义 Base URL 原样使用（不再拼接 /chat/completions）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return _ok_handler(request)

        client = _make_client("custom", handler=handler,
                              base_url="https://gw.example.com/v1/chat/completions")
        client.chat([{"role": "user", "content": "hi"}])
        assert seen["url"] == "https://gw.example.com/v1/chat/completions"

    def test_list_content_extracted(self):
        """content 为部分列表（新版接口）时拼接 text 片段。"""
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "text", "text": "第二段"},
                ]}}],
            })

        client = _make_client("deepseek", handler=handler)
        assert client.chat([{"role": "user", "content": "hi"}]) == "第一段第二段"

    def test_http_error_status(self):
        """非 2xx 响应抛错并带状态码。"""
        def handler(request):
            return httpx.Response(500, text="server error")

        client = _make_client("deepseek", handler=handler)
        with pytest.raises(ChatModelError, match="HTTP 500"):
            client.chat([{"role": "user", "content": "hi"}])

    def test_non_json_response(self):
        """2xx 但返回 HTML / 空 body → 诊断错误。"""
        def handler(request):
            return httpx.Response(200, text="<html>gateway error</html>",
                                  headers={"content-type": "text/html"})

        client = _make_client("deepseek", handler=handler)
        with pytest.raises(ChatModelError, match="不是有效 JSON"):
            client.chat([{"role": "user", "content": "hi"}])

    def test_missing_choices(self):
        """响应缺少 choices → 明确报错。"""
        def handler(request):
            return httpx.Response(200, json={})

        client = _make_client("deepseek", handler=handler)
        with pytest.raises(ChatModelError, match="响应缺少消息内容"):
            client.chat([{"role": "user", "content": "hi"}])

    def test_empty_reply(self):
        """响应内容为空 → 报错。"""
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "  "}}],
            })

        client = _make_client("deepseek", handler=handler)
        with pytest.raises(ChatModelError, match="未包含文本内容"):
            client.chat([{"role": "user", "content": "hi"}])

    def test_network_error_wrapped(self):
        """网络层异常转 ChatModelError（带请求地址）。"""
        def handler(request):
            raise httpx.ConnectError("connection refused")

        client = _make_client("deepseek", handler=handler)
        with pytest.raises(ChatModelError, match="请求 AI 接口失败"):
            client.chat([{"role": "user", "content": "hi"}])


class TestGemini:
    def test_request_shape(self):
        """gemini 走 generateContent：system 并入首条 user、assistant→model。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["key"] = request.headers.get("X-goog-api-key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "candidates": [{"content": {"role": "model", "parts": [
                    {"text": "Gemini 回复"},
                ]}}],
            })

        client = _make_client("gemini", handler=handler)
        reply = client.chat([
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "之前回复"},
        ])
        assert reply == "Gemini 回复"
        assert "generateContent" in seen["url"]
        assert seen["key"] == "sk-test"
        contents = seen["body"]["contents"]
        assert contents[0] == {"role": "user", "parts": [{"text": "系统提示"}]}
        assert contents[1] == {"role": "user", "parts": [{"text": "你好"}]}
        assert contents[2] == {"role": "model", "parts": [{"text": "之前回复"}]}

    def test_consecutive_same_role_guard(self):
        """连续相同 role 时插入空 user 消息（Gemini 交替要求）。"""
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            })

        client = _make_client("gemini", handler=handler)
        client.chat([
            {"role": "user", "content": "一"},
            {"role": "user", "content": "二"},
        ])
        roles = [c["role"] for c in seen["body"]["contents"]]
        assert roles == ["user", "user", "user"]  # 中间插入空 user

    def test_no_candidates(self):
        def handler(request):
            return httpx.Response(200, json={"candidates": []})

        client = _make_client("gemini", handler=handler)
        with pytest.raises(ChatModelError, match="响应缺少内容"):
            client.chat([{"role": "user", "content": "hi"}])


class TestAnthropic:
    def test_request_shape(self):
        """anthropic 走 messages：system 抽到顶层、带 max_tokens 与版本头。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["version"] = request.headers.get("anthropic-version")
            seen["key"] = request.headers.get("x-api-key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "Claude 回复"}],
            })

        client = _make_client("anthropic", handler=handler)
        reply = client.chat([
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "你好"},
        ])
        assert reply == "Claude 回复"
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["version"] == "2023-06-01"
        assert seen["key"] == "sk-test"
        assert seen["body"]["system"] == "系统提示"
        assert seen["body"]["max_tokens"] == 1024
        assert seen["body"]["messages"] == [{"role": "user", "content": "你好"}]

    def test_no_text_content(self):
        def handler(request):
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "t", "name": "x",
                 "input": {}},
            ]})

        client = _make_client("anthropic", handler=handler)
        with pytest.raises(ChatModelError, match="未包含文本内容"):
            client.chat([{"role": "user", "content": "hi"}])


class TestResolveChatProvider:
    def test_first_enabled_with_key(self):
        """取第一个启用且 Key 非空的项。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "a", "provider": "deepseek", "api_key": "sk-a",
             "enabled": True},
            {"name": "b", "provider": "openai", "api_key": "sk-b",
             "enabled": True},
        ])
        assert resolve_chat_provider(settings)["name"] == "a"

    def test_skips_disabled_and_missing_key(self):
        settings = SimpleNamespace(ai_providers=[
            {"name": "off", "provider": "deepseek", "api_key": "sk",
             "enabled": False},
            {"name": "nokey", "provider": "openai", "api_key": "",
             "enabled": True},
            {"name": "ok", "provider": "deepseek", "api_key": "sk-ok",
             "enabled": True},
        ])
        assert resolve_chat_provider(settings)["name"] == "ok"

    def test_empty_returns_none(self):
        assert resolve_chat_provider(SimpleNamespace(ai_providers=[])) is None
        assert resolve_chat_provider(SimpleNamespace(ai_providers=None)) is None

    def test_enabled_default_true(self):
        """enabled 缺省视为启用（与设置页默认一致）。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "a", "provider": "deepseek", "api_key": "sk"},
        ])
        assert resolve_chat_provider(settings)["name"] == "a"


class TestPriorityAndFallback:
    """AI 供应商优先级排序与额度不足自动切换（issue #495）。

    覆盖：priority 数字小优先（与 repos[].priority 语义一致，issue #51）、
    同优先级保持列表顺序、历史配置缺省 100 兼容、禁用/无 Key 过滤、
    调用失败自动切换下一供应商、成功即停、全部失败汇总错误。
    """

    @staticmethod
    def _fallback_factory(handler_map: dict) -> callable:
        """按供应商 name 分发 HTTP handler 的 client 工厂（注入 MockTransport）。"""
        def factory(cfg: dict):
            handler = handler_map.get(cfg["name"], _ok_handler)
            client = ChatModelClient(
                name=cfg["name"],
                provider=str(cfg.get("provider") or "deepseek"),
                api_key=str(cfg.get("api_key") or "sk-test"),
                model=str(cfg.get("model") or ""),
            )
            client._http = httpx.Client(transport=httpx.MockTransport(handler))
            return client
        return factory

    @staticmethod
    def _fail_handler(status_code: int = 402, text: str = ""):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text=text or "boom")
        return handler

    # ---- 优先级排序 ----

    def test_priority_smaller_first(self):
        """priority 数字小优先级高（与 repos[].priority 语义一致）。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "low", "provider": "deepseek", "api_key": "sk-a",
             "priority": 200},
            {"name": "high", "provider": "openai", "api_key": "sk-b",
             "priority": 50},
        ])
        assert resolve_chat_provider(settings)["name"] == "high"

    def test_same_priority_keeps_list_order(self):
        """同优先级按列表顺序（第一个启用项优先，与旧版行为一致）。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "first", "provider": "deepseek", "api_key": "sk-a"},
            {"name": "second", "provider": "openai", "api_key": "sk-b"},
        ])
        assert resolve_chat_provider(settings)["name"] == "first"

    def test_missing_priority_defaults_100(self):
        """历史配置无 priority 字段默认 100，与显式 100 同优先级保持顺序。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "no-pri", "provider": "deepseek", "api_key": "sk-a"},
            {"name": "pri-100", "provider": "openai", "api_key": "sk-b",
             "priority": 100},
        ])
        assert resolve_chat_provider(settings)["name"] == "no-pri"

    def test_invalid_priority_falls_back_to_default(self):
        """priority 非法值（None / 非数字）按默认 100 参与排序。"""
        settings = SimpleNamespace(ai_providers=[
            {"name": "bad", "provider": "deepseek", "api_key": "sk-a",
             "priority": "abc"},
            {"name": "ok", "provider": "openai", "api_key": "sk-b",
             "priority": 99},
        ])
        assert resolve_chat_provider(settings)["name"] == "ok"

    def test_sorted_filters_disabled_and_missing_key(self):
        """排序结果只含启用且有 Key 的供应商。"""
        from botler.chat_models import sorted_chat_providers
        providers = sorted_chat_providers(SimpleNamespace(ai_providers=[
            {"name": "off", "provider": "deepseek", "api_key": "sk",
             "enabled": False, "priority": 1},
            {"name": "nokey", "provider": "openai", "api_key": "",
             "priority": 2},
            {"name": "ok1", "provider": "deepseek", "api_key": "sk-1",
             "priority": 5},
            {"name": "ok2", "provider": "openai", "api_key": "sk-2",
             "priority": 3},
        ]))
        assert [p["name"] for p in providers] == ["ok2", "ok1"]

    def test_sorted_empty(self):
        """空列表 / None 返回空列表。"""
        from botler.chat_models import sorted_chat_providers
        assert sorted_chat_providers(SimpleNamespace(ai_providers=[])) == []
        assert sorted_chat_providers(SimpleNamespace(ai_providers=None)) == []

    # ---- 失败自动切换 ----

    def test_fallback_first_failure_then_success(self):
        """第一个供应商额度不足（402）失败，自动切换第二个并成功。"""
        from botler.chat_models import chat_with_fallback
        providers = [
            {"name": "a", "provider": "deepseek", "api_key": "sk-a",
             "priority": 1},
            {"name": "b", "provider": "openai", "api_key": "sk-b",
             "priority": 2},
        ]
        reply, used = chat_with_fallback(
            providers, [{"role": "user", "content": "hi"}],
            client_factory=self._fallback_factory(
                {"a": self._fail_handler(402, "Insufficient Balance")}))
        assert reply == "你好，我是 AI"
        assert used["name"] == "b"

    def test_fallback_stops_at_first_success(self):
        """成功即停：不再调用后续供应商（按优先级顺序尝试）。"""
        from botler.chat_models import chat_with_fallback
        calls: list[str] = []

        def handler_a(request: httpx.Request) -> httpx.Response:
            calls.append("a")
            return httpx.Response(500, text="boom")

        def handler_b(request: httpx.Request) -> httpx.Response:
            calls.append("b")
            return _ok_handler(request)

        def handler_c(request: httpx.Request) -> httpx.Response:
            calls.append("c")
            return _ok_handler(request)

        providers = [
            {"name": "a", "provider": "deepseek", "api_key": "sk-a",
             "priority": 1},
            {"name": "b", "provider": "openai", "api_key": "sk-b",
             "priority": 2},
            {"name": "c", "provider": "anthropic", "api_key": "sk-c",
             "priority": 3},
        ]
        reply, used = chat_with_fallback(
            providers, [{"role": "user", "content": "hi"}],
            client_factory=self._fallback_factory({
                "a": handler_a, "b": handler_b, "c": handler_c}))
        assert reply == "你好，我是 AI"
        assert used["name"] == "b"
        assert calls == ["a", "b"], "第三个供应商不应被调用"

    def test_fallback_all_failed_raises_summary(self):
        """全部失败抛汇总错误，逐供应商标注失败原因。"""
        from botler.chat_models import chat_with_fallback
        providers = [
            {"name": "甲", "provider": "deepseek", "api_key": "sk-a",
             "priority": 1},
            {"name": "乙", "provider": "openai", "api_key": "sk-b",
             "priority": 2},
        ]
        with pytest.raises(ChatModelError) as ei:
            chat_with_fallback(
                providers, [{"role": "user", "content": "hi"}],
                client_factory=self._fallback_factory({
                    "甲": self._fail_handler(402, "Insufficient Balance"),
                    "乙": self._fail_handler(429, "Rate limit exceeded"),
                }))
        msg = str(ei.value)
        assert "甲" in msg and "乙" in msg, "汇总错误应包含每个供应商名"
        assert "所有 AI 供应商" in msg

    def test_fallback_empty_providers_raises(self):
        """空候选列表抛可读错误。"""
        from botler.chat_models import chat_with_fallback
        with pytest.raises(ChatModelError, match="未配置"):
            chat_with_fallback([], [{"role": "user", "content": "hi"}])

    def test_fallback_single_provider_success(self):
        """单供应商直接成功（显式指定场景，不切换）。"""
        from botler.chat_models import chat_with_fallback
        providers = [
            {"name": "only", "provider": "deepseek", "api_key": "sk-a",
             "priority": 100},
        ]
        reply, used = chat_with_fallback(
            providers, [{"role": "user", "content": "hi"}],
            client_factory=self._fallback_factory({}))
        assert reply == "你好，我是 AI"
        assert used["name"] == "only"
