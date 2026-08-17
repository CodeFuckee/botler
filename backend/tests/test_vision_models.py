"""识图模型调用接口测试（issue #152）。

覆盖 Gemini 视觉（generateContent）与 OpenAI 兼容视觉（chat/completions）
两个 provider 的请求构造、响应解析与错误处理，以及自定义 provider
（OpenAI 兼容接口）的完整请求路径。用 httpx.MockTransport 模拟 HTTP，
不做真实外呼。
"""

import base64
import json

import pytest
import httpx

from botler.plugins.vision_models import format_request_info
from botler.vision_models import (
    VISION_MODEL_PRESETS,
    VisionModelClient,
    VisionModelError,
    client_from_config,
    find_enabled,
)


class TestPresets:
    def test_builtin_providers(self):
        """内置预设包含 gemini_vision / openai_vision / custom。"""
        assert set(VISION_MODEL_PRESETS) >= {
            "gemini_vision", "openai_vision", "custom"}

    def test_gemini_preset_defaults(self):
        """Gemini 预设默认端点与模型符合官方 generateContent 接口。"""
        gemini = VISION_MODEL_PRESETS["gemini_vision"]
        assert gemini["base_url"].startswith(
            "https://generativelanguage.googleapis.com")
        assert gemini["model"]

    def test_openai_preset_defaults(self):
        """OpenAI 预设默认端点与模型符合 chat/completions 接口。"""
        openai = VISION_MODEL_PRESETS["openai_vision"]
        assert openai["base_url"].startswith("https://api.openai.com")
        assert openai["model"]

    def test_custom_preset_empty_defaults(self):
        """自定义预设无默认端点/模型（用户自填，走 OpenAI 兼容接口）。"""
        custom = VISION_MODEL_PRESETS["custom"]
        assert custom["base_url"] == ""
        assert custom["model"] == ""

    def test_unknown_provider_rejected(self):
        """不支持的 provider 构造时报错。"""
        with pytest.raises(VisionModelError, match="不支持的识图模型类型"):
            VisionModelClient(name="x", provider="unknown_provider",
                              api_key="k", model="m")


class TestClientCommon:
    def test_empty_image_rejected(self):
        """图片为空直接报错（不发请求）。"""
        client = VisionModelClient(
            name="n", provider="openai_vision", api_key="k")
        with pytest.raises(VisionModelError, match="图片"):
            client.describe(b"")

    def test_missing_api_key_rejected(self):
        """未配置 API Key 时明确报错（不发请求）。"""
        client = VisionModelClient(
            name="n", provider="gemini_vision", api_key="")
        with pytest.raises(VisionModelError, match="未配置 API Key"):
            client.describe(b"\x89PNG-test")

    def test_custom_without_base_url_rejected(self):
        """自定义 provider 未配置 Base URL 时明确报错（不发请求）。"""
        client = VisionModelClient(
            name="n", provider="custom", api_key="k", model="m")
        with pytest.raises(VisionModelError, match="Base URL"):
            client.describe(b"\x89PNG-test")

    def test_timeout_error_includes_request_url(self):
        """请求超时（issue #156）：错误信息应包含实际请求地址，用户可
        据此判断 POST 到底发到了哪个 URL（超时前无法拿到响应体）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            raise httpx.ReadTimeout("timed out", request=request)

        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="Gemini 视觉", provider="gemini_vision",
            api_key="AIza-timeout-key", timeout=5)
        client._http = httpx.Client(transport=transport)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "请求超时" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # 超时前拿不到响应体：POST 出去的请求头（密钥掩码）与请求体
        # 是唯一可诊断线索，必须带进错误提示（issue #156）
        assert "请求头" in msg
        assert "请求体" in msg
        assert "已掩码" in msg
        # API Key 明文不泄漏（只展示掩码）
        assert "AIza-timeout-key" not in msg

    def test_connect_error_includes_request_url(self):
        """网络连接失败（DNS / 拒绝连接 / SSL 等，issue #156）：错误信息
        应带实际请求地址，而不是只有晦涩的 httpx 异常文本（此时无响应体，
        请求地址是唯一可诊断线索）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            raise httpx.ConnectError("connection refused", request=request)

        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="Gemini 视觉", provider="gemini_vision",
            api_key="k", timeout=5)
        client._http = httpx.Client(transport=transport)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "网络请求失败" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # 网络层失败同样带 POST 请求头（密钥掩码）与请求体（issue #156）
        assert "请求头" in msg
        assert "请求体" in msg
        assert "已掩码" in msg

    def test_timeout_error_includes_request_body(self):
        """请求超时（issue #156）：错误信息除请求地址外，还带「后端 POST
        给上游 API 的请求体」（含描述指令文本，base64 图片数据截断展示），
        超时前拿不到响应体时这是唯一可诊断线索。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            raise httpx.ReadTimeout("timed out", request=request)

        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="Gemini 视觉", provider="gemini_vision",
            api_key="AIza-secret-key", timeout=5)
        client._http = httpx.Client(transport=transport)
        big = b"\x89PNG" + b"0" * 300  # 超长图片：base64 超过截断阈值
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "请求超时" in msg
        assert "请求体" in msg
        # 实际 POST 的载荷内容带进错误：描述指令文本可见
        text = captured["body"]["contents"][0]["parts"][1]["text"]
        assert text in msg
        # base64 图片数据截断展示，不刷屏
        assert "已截断" in msg
        assert "已掩码" in msg
        assert "AIza-secret-key" not in msg  # API Key 明文不泄漏

    def test_connect_error_includes_request_body(self):
        """网络连接失败（issue #156）：错误信息同样带 POST 请求体（含
        data URL 前缀），用户可确认图片编码与载荷结构是否正确。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            raise httpx.ConnectError("connection refused", request=request)

        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="OpenAI 视觉", provider="openai_vision",
            api_key="sk-secret-key", timeout=5)
        client._http = httpx.Client(transport=transport)
        big = b"\x89PNG" + b"0" * 300
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "网络请求失败" in msg
        assert "请求体" in msg
        assert "已截断" in msg
        # data URL 前缀可见，说明图片以 base64 data URL 编码进载荷
        assert "data:image/png;base64," in msg
        assert "sk-secret-key" not in msg


class TestGeminiVisionClient:
    def _client(self, handler, api_key="AIza-test", **kw):
        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="Gemini 视觉", provider="gemini_vision",
            api_key=api_key, timeout=5, **kw)
        client._http = httpx.Client(transport=transport)  # 注入 mock 传输
        return client

    def test_describe_request_and_parse(self):
        """上传图片：POST generateContent，inline_data 携带图片 base64，
        X-goog-api-key 认证，响应文本拼接返回。"""
        png = b"\x89PNG-vision-input"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["key"] = request.headers.get("X-goog-api-key")
            captured["json"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [
                    {"text": "图片里有一只橘色的猫"},
                    {"text": "坐在窗台上"},
                ]}}],
            })

        client = self._client(handler)
        desc = client.describe(png, mime_type="image/png",
                               prompt="请描述这张图片的内容")
        assert captured["method"] == "POST"
        assert captured["url"].endswith(
            "/models/gemini-2.5-flash:generateContent")
        assert captured["key"] == "AIza-test"
        parts = captured["json"]["contents"][0]["parts"]
        inline = parts[0]["inline_data"]
        assert inline["mime_type"] == "image/png"
        assert base64.b64decode(inline["data"]) == png
        assert parts[1]["text"] == "请描述这张图片的内容"
        assert desc == "图片里有一只橘色的猫坐在窗台上"

    def test_describe_default_prompt(self):
        """prompt 缺省时使用内置默认描述指令。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["parts"] = json.loads(
                request.read().decode())["contents"][0]["parts"]
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "描述结果"}]}}],
            })

        client = self._client(handler)
        desc = client.describe(b"\x89PNG-x")
        assert desc == "描述结果"
        # 默认 prompt 应为中文描述指令（不传图片外的多余文本以外，
        # 至少请求体含描述指令文本 part）
        assert any("描述" in str(p.get("text", ""))
                   for p in captured["parts"])

    def test_error_response_raises(self):
        """非 2xx 响应（如 401）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text='{"error": "API key invalid"}')

        client = self._client(handler)
        with pytest.raises(VisionModelError, match="401"):
            client.describe(b"\x89PNG-x")

    def test_missing_text_in_response_raises(self):
        """响应不含文本结果时报错。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [
                    {"inlineData": {"mimeType": "image/png",
                                    "data": base64.b64encode(b"x").decode()}},
                ]}}],
            })

        client = self._client(handler)
        with pytest.raises(VisionModelError, match="未包含文本"):
            client.describe(b"\x89PNG-x")

    def test_error_response_includes_request_url(self):
        """非 2xx 响应错误信息应包含实际请求地址（便于用户诊断
        Base URL 路径段缺失导致的 404 page not found）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(404, text="404 page not found")

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        assert "请求地址" in str(exc.value)
        assert captured["url"] in str(exc.value)

    def test_error_response_includes_request_body(self):
        """非 2xx 响应（issue #156）：错误信息带「后端 POST 给上游 API
        的信息」——请求地址 + 请求头（API Key 掩码）+ 请求体（图片 base64
        截断），用户可对照网关返回内容确认配置是否正确。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["key"] = request.headers.get("X-goog-api-key")
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(401, text='{"error": "invalid key"}')

        client = self._client(handler)
        big = b"\x89PNG" + b"0" * 300
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "401" in msg
        assert "请求地址" in msg and captured["url"] in msg
        assert "请求头" in msg
        assert "请求体" in msg
        # 实际 POST 的载荷内容带进错误（用户可见）
        text = captured["body"]["contents"][0]["parts"][1]["text"]
        assert text in msg
        # API Key 掩码展示，明文不泄漏
        assert "已掩码" in msg
        assert captured["key"] not in msg
        # base64 图片数据截断展示
        assert "已截断" in msg

    def test_missing_content_error_includes_request_url(self):
        """响应缺少内容（issue #156）：错误信息应同时带响应片段与实际
        请求地址，方便用户对照网关返回内容定位问题。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"candidates": [{}]})

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "缺少内容" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # issue #156：同时带 POST 请求体（用户可确认载荷结构）
        assert "请求体" in msg
        assert "请详细描述这张图片的内容" in msg

class TestOpenAIVisionClient:
    def _client(self, handler, api_key="sk-test", provider="openai_vision", **kw):
        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="OpenAI 视觉", provider=provider,
            api_key=api_key, timeout=5, **kw)
        client._http = httpx.Client(transport=transport)
        return client

    def test_describe_request_and_parse(self):
        """上传图片：POST chat/completions，Bearer 认证，image_url 携带
        data URL（base64），响应 choices[0].message.content 返回。"""
        png = b"\x89PNG-vision-openai"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                         "content": "这是一张夕阳下的海滩照片"}}],
            })

        client = self._client(handler)
        desc = client.describe(png, mime_type="image/png",
                               prompt="请描述这张图片的内容")
        assert captured["url"].endswith("/chat/completions")
        assert captured["auth"] == "Bearer sk-test"
        assert captured["body"]["model"] == "gpt-4o"
        content = captured["body"]["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "请描述这张图片的内容"
        img = content[1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"].startswith("data:image/png;base64,")
        assert base64.b64decode(
            img["image_url"]["url"].split(",", 1)[1]) == png
        assert desc == "这是一张夕阳下的海滩照片"

    def test_describe_no_image_url_in_second_part(self):
        """不带图片时只发文本内容（虽然本功能始终要求图片，防御性校验）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
            })

        client = self._client(handler)
        desc = client.describe(b"\x89PNG-x")
        assert desc == "ok"
        content = captured["body"]["messages"][0]["content"]
        assert content[0]["type"] == "text"

    def test_error_response_raises(self):
        """非 2xx 响应（如 429 限流）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limit exceeded")

        client = self._client(handler)
        with pytest.raises(VisionModelError, match="429"):
            client.describe(b"\x89PNG-x")

    def test_missing_content_in_response_raises(self):
        """响应 choices 为空时报错。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        client = self._client(handler)
        with pytest.raises(VisionModelError, match="未包含文本"):
            client.describe(b"\x89PNG-x")

    def test_error_429_includes_request_body(self):
        """非 2xx 响应（issue #156，429 限流）：错误信息带「后端 POST 给
        上游 API 的信息」，请求头 Bearer 密钥掩码展示、请求体可见。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(429, text="rate limit exceeded")

        client = self._client(handler)
        big = b"\x89PNG" + b"0" * 300
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "429" in msg
        assert "请求头" in msg
        assert "请求体" in msg
        # Bearer 认证方式保留、密钥掩码：明文不泄漏
        assert "Bearer ***" in msg
        assert "已掩码" in msg
        assert captured["auth"] not in msg
        # 请求体可见：模型名 + 描述指令
        assert captured["body"]["model"] in msg
        assert "请详细描述这张图片的内容" in msg

    def test_error_404_includes_request_url(self):
        """404（如 Base URL 缺 /v1 路径段的 page not found）错误信息
        应包含实际请求地址，帮助用户定位配置问题。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(404, text="404 page not found")

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        assert "404" in str(exc.value)
        assert captured["url"] in str(exc.value)
        assert "Base URL" in str(exc.value)

    def test_missing_content_error_includes_request_url(self):
        """响应 choices 为空（issue #156）：错误信息应同时带响应片段与
        实际请求地址。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"choices": []})

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "未包含文本" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # issue #156：同时带 POST 请求体（用户可确认载荷结构）
        assert "请求体" in msg
        assert "gpt-4o" in msg

class TestCustomVisionProvider:
    """自定义 provider：走 OpenAI 兼容 chat/completions 接口（issue #152）。

    用户可配置任意 OpenAI 兼容视觉模型（如硅基流动 / DeepSeek-VL /
    qwen-vl 等网关），Base URL 作为完整请求地址直接使用（issue #150
    语义），模型 / API Key 由用户填写。
    """

    CUSTOM_URL = "https://api.siliconflow.cn/v1/chat/completions"

    def _client(self, handler):
        transport = httpx.MockTransport(handler)
        client = VisionModelClient(
            name="自定义视觉", provider="custom",
            base_url=self.CUSTOM_URL, api_key="sk-custom",
            model="Qwen/Qwen2.5-VL-7B-Instruct", timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def test_custom_uses_base_url_verbatim(self):
        """自定义 Base URL 作为完整请求地址直接使用，不拼接接口路径。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "图中有高楼与蓝天"}}],
            })

        client = self._client(handler)
        desc = client.describe(b"\x89PNG-x")
        assert captured["url"] == self.CUSTOM_URL
        assert captured["body"]["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
        assert desc == "图中有高楼与蓝天"

    def test_missing_content_error_includes_request_url(self):
        """自定义网关响应 choices 为空（issue #156）：错误信息应带实际
        请求地址，帮助用户对照网关返回内容定位问题。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"choices": []})

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "未包含文本" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # issue #156：同时带 POST 请求体（自定义网关同样可见载荷）
        assert "请求体" in msg
        assert "Qwen/Qwen2.5-VL-7B-Instruct" in msg

    def test_invalid_json_error_includes_request_body(self):
        """接口返回非 JSON（issue #156）：错误信息带响应片段 + POST 请求
        信息（地址 + 请求头掩码 + 请求体截断），用户可对照网关返回定位
        Base URL 指向错误的场景。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, text="<html>gateway error page</html>")

        client = self._client(handler)
        big = b"\x89PNG" + b"0" * 300
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert "gateway error page" in msg
        assert "请求地址" in msg and self.CUSTOM_URL in msg
        assert "请求头" in msg
        assert "请求体" in msg
        assert "已掩码" in msg
        assert "已截断" in msg
        assert captured["body"]["model"] in msg
        assert "sk-custom" not in msg  # API Key 明文不泄漏

class TestRequestInfo:
    """format_request_info 脱敏摘要单元测试（issue #156）。

    错误提示要展示「后端 POST 给上游 API 的信息」，但必须脱敏：认证头
    密钥掩码（Authorization 保留 Bearer 前缀）、请求体超长字符串（base64
    图片数据）截断、key/token 类字段整体掩码。
    """

    def test_formats_url_headers_body(self):
        """正常路径：地址 + 掩码请求头 + 请求体 JSON 摘要。"""
        info = format_request_info(
            "https://api.example.com/v1/chat/completions",
            headers={"Authorization": "Bearer sk-real-123",
                     "Content-Type": "application/json"},
            payload={"model": "gpt-4o", "messages": [{"role": "user"}]})
        assert "请求地址: https://api.example.com" in info
        assert '"Authorization": "Bearer ***（已掩码）"' in info
        assert '"Content-Type": "application/json"' in info
        assert '"model": "gpt-4o"' in info
        assert "sk-real-123" not in info  # 密钥不泄漏

    def test_masks_api_key_header_values(self):
        """X-goog-api-key 与 api-key 头整体掩码（无 Bearer 前缀）。"""
        info = format_request_info(
            "https://generativelanguage.googleapis.com/v1beta",
            headers={"X-goog-api-key": "AIza-secret", "api-key": "k2"},
            payload={})
        assert "AIza-secret" not in info
        assert "k2" not in info
        assert '"X-goog-api-key": "***（已掩码）"' in info
        assert '"api-key": "***（已掩码）"' in info

    def test_truncates_long_image_base64(self):
        """超长字符串（base64 图片数据）截断展示并标注总长度。"""
        big = "b" * 5000
        info = format_request_info(
            "https://api.example.com/v1",
            headers={"Authorization": "Bearer k"},
            payload={"data": big})
        assert "已截断" in info
        assert "共 5000 字符" in info
        assert "b" * 200 not in info  # 未完整展示
        assert info.count("b") <= 80  # 只保留预览前缀（请求头无小写 b）

    def test_masks_sensitive_payload_fields(self):
        """请求体中的 api_key / token 类字段整体掩码。"""
        info = format_request_info(
            "https://api.example.com/v1",
            headers={},
            payload={"model": "m", "api_key": "sk-x", "token": "t1"})
        assert "sk-x" not in info
        assert "t1" not in info
        assert '"api_key": "***（已掩码）"' in info
        assert '"token": "***（已掩码）"' in info
        assert '"model": "m"' in info

    def test_payload_none_omits_body(self):
        """payload 为 None（无请求体可展示）时不输出请求体段。"""
        info = format_request_info("https://api.example.com/v1")
        assert info == "请求地址: https://api.example.com/v1"
        assert "请求体" not in info

class TestHelpers:
    def test_find_enabled_returns_matching(self):
        """find_enabled 找指定 provider 且启用且有 key 的项。"""
        models = [
            {"name": "A", "provider": "openai_vision",
             "api_key": "k1", "enabled": True},
            {"name": "B", "provider": "gemini_vision",
             "api_key": "", "enabled": True},
            {"name": "C", "provider": "gemini_vision",
             "api_key": "k3", "enabled": False},
            {"name": "D", "provider": "gemini_vision",
             "api_key": "k4", "enabled": True},
        ]
        found = find_enabled(models, "gemini_vision")
        assert found is not None and found["name"] == "D"
        assert find_enabled(models, "custom") is None
        assert find_enabled(None, "gemini_vision") is None

    def test_client_from_config(self):
        """按配置项构造客户端，缺省值由客户端补全。"""
        cfg = {"name": "Gemini 视觉生产", "provider": "gemini_vision",
               "base_url": "", "api_key": "k", "model": ""}
        client = client_from_config(cfg)
        assert client.name == "Gemini 视觉生产"
        assert client.base_url == VISION_MODEL_PRESETS["gemini_vision"]["base_url"]
        assert client.model == "gemini-2.5-flash"

    def test_client_from_config_unknown_provider_raises(self):
        """配置 provider 未知时构造报错（配置错误尽早暴露）。"""
        with pytest.raises(VisionModelError):
            client_from_config({"name": "x", "provider": "nope",
                                "api_key": "k"})
