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


class FakeImageStore:
    """MinIO 图片存储替身（issue #163）：put_image 返回固定 http URL。"""

    def __init__(self, url="http://img.example.com:9000/public/abc"):
        self.url = url
        self.calls: list[tuple] = []

    def put_image(self, data, mime_type="image/png"):
        self.calls.append((data, mime_type))
        return self.url


class FailingImageStore:
    """上传失败的存储替身（模拟 MinIO 不可用）。"""

    def put_image(self, data, mime_type="image/png"):
        raise RuntimeError("connection refused")


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
        """自定义 provider 未配置 Base URL 时明确报错（不发请求）。

        issue #164：未启用 MinIO 时先报「要求 http URL」的引导错误；
        本用例注入 image_store 使检查走到 Base URL 校验（两者都是
        describe 前置校验，顺序不影响正确性）。
        """
        client = VisionModelClient(
            name="n", provider="custom", api_key="k", model="m")
        client.image_store = FakeImageStore()
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
        # issue #164：OpenAI 兼容识图模型禁止 base64 内联，须走 MinIO
        # http URL 模式（无 image_store 时 describe 直接报错不发请求）
        client.image_store = FakeImageStore(
            url="http://img.example.com:9000/public/abc")
        client._http = httpx.Client(transport=transport)
        big = b"\x89PNG" + b"0" * 300
        with pytest.raises(VisionModelError) as exc:
            client.describe(big)
        msg = str(exc.value)
        assert "网络请求失败" in msg
        assert "请求体" in msg
        # 载荷为 http URL 图片（不再有超长 base64，无截断）
        assert "http://img.example.com:9000/public/abc" in msg
        assert "data:image" not in msg
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

    def test_describe_with_image_store_keeps_inline_data(self):
        """issue #163：Gemini 官方接口不支持 http URL 图片输入——
        即使配置 MinIO 图片存储，仍以 base64 inline_data 内联输入，
        且不触发上传。"""
        png = b"\x89PNG-gemini-minio"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            })

        client = self._client(handler)
        client.image_store = FakeImageStore()
        desc = client.describe(png, mime_type="image/png")
        assert desc == "ok"
        inline = captured["json"]["contents"][0]["parts"][0]["inline_data"]
        assert base64.b64decode(inline["data"]) == png

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
        # issue #164：OpenAI 兼容识图模型禁止 base64 内联，图片须先
        # 上传 MinIO 以 http URL 传入
        client.image_store = FakeImageStore(
            url="http://img.example.com:9000/public/abc")
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
        # 图片以 http URL 传入（MinIO public 桶），不再塞 base64
        assert img["image_url"]["url"] == "http://img.example.com:9000/public/abc"
        assert "data:image" not in img["image_url"]["url"]
        assert desc == "这是一张夕阳下的海滩照片"

    def test_describe_with_image_store_uses_http_url(self):
        """issue #163：配置 MinIO 图片存储后，OpenAI 兼容请求的
        image_url.url 使用 http URL（而非 base64 data URL），图片先
        经 put_image 上传（对象名 = 哈希值）。"""
        png = b"\x89PNG-openai-minio"
        store = FakeImageStore(url="http://img.example.com:9000/public/"
                                    "abc123")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "http 图片描述"}}],
            })

        client = self._client(handler)
        client.image_store = store
        desc = client.describe(png, mime_type="image/png",
                               prompt="描述")
        assert desc == "http 图片描述"
        img = captured["body"]["messages"][0]["content"][1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"] == store.url
        assert img["image_url"]["url"].startswith("http://")
        assert "data:image" not in img["image_url"]["url"]
        # 上传入参：图片字节 + MIME 类型
        assert store.calls == [(png, "image/png")]

    def test_describe_image_store_upload_failure_raises(self):
        """issue #163：MinIO 上传失败统一转 VisionModelError，错误信息
        可诊断（不抛底层异常）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
            })

        client = self._client(handler)
        client.image_store = FailingImageStore()
        with pytest.raises(VisionModelError, match="上传 MinIO 失败"):
            client.describe(b"\x89PNG-x")

    def test_describe_no_image_url_in_second_part(self):
        """不带图片时只发文本内容（虽然本功能始终要求图片，防御性校验）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
            })

        client = self._client(handler)
        client.image_store = FakeImageStore()
        desc = client.describe(b"\x89PNG-x")
        assert desc == "ok"
        content = captured["body"]["messages"][0]["content"]
        assert content[0]["type"] == "text"

    def test_error_response_raises(self):
        """非 2xx 响应（如 429 限流）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limit exceeded")

        client = self._client(handler)
        client.image_store = FakeImageStore()
        with pytest.raises(VisionModelError, match="429"):
            client.describe(b"\x89PNG-x")

    def test_missing_content_in_response_raises(self):
        """响应 choices 为空时报错。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        client = self._client(handler)
        client.image_store = FakeImageStore()
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
        client.image_store = FakeImageStore()
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
        client.image_store = FakeImageStore()
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
        client.image_store = FakeImageStore()
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "未包含文本" in msg
        assert "请求地址" in msg
        assert captured["url"] in msg
        # issue #156：同时带 POST 请求体（用户可确认载荷结构）
        assert "请求体" in msg
        assert "gpt-4o" in msg

    def test_openai_requires_minio_no_base64_fallback(self):
        """issue #164：OpenAI 兼容识图模型禁止 base64 内联——未启用
        MinIO（image_store=None）时 describe 直接报错引导启用 MinIO，
        绝不把图片 base64 塞进请求体。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("不应发出任何请求（未启用 MinIO 时禁止 base64）")

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x", mime_type="image/png")
        msg = str(exc.value)
        assert "MinIO" in msg
        assert "http URL" in msg
        assert "base64" in msg

    def test_url_error_hint_for_base64_data_url(self):
        """_data_url_rejected_hint：字节图片（base64 内联模式）且网关返回
        图片 URL 类错误时，给出可操作的诊断提示（issue #164 遗留防御：
        直接传字节给插件的场景仍能命中提示；正常流程已禁止 base64 内联）。
        """
        from botler.plugins.vision_models import _data_url_rejected_hint
        hint = _data_url_rejected_hint(
            b"\x89PNG-x", "url error, please check url！")
        assert "base64" in hint
        assert "MinIO" in hint
        assert "http URL" in hint
        # 非图片 URL 类错误不加提示
        assert _data_url_rejected_hint(
            b"\x89PNG-x", "rate limit exceeded") == ""
        # http URL 字符串图片（MinIO 模式）不加提示
        assert _data_url_rejected_hint(
            "http://img.example.com:9000/public/abc", "url error") == ""

    def test_url_error_no_hint_when_http_url_mode(self):
        """MinIO http URL 模式（图片已是 http URL 字符串）下网关报 url
        错误时，不加 base64 提示（避免误导——此时与图片编码无关）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "code": "InvalidParameter",
                "message": "url error, please check url！",
            })

        client = self._client(handler)
        # MinIO http URL 模式：图片经 put_image 上传后以 URL 字符串传入
        # 插件，网关报 url 错误时与图片编码无关，不加 base64 提示
        client.image_store = FakeImageStore(
            url="http://img.example.com:9000/public/abc")
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x", mime_type="image/png",
                            prompt="描述")
        msg = str(exc.value)
        assert "url error" in msg
        assert "base64" not in msg
        assert "MinIO" not in msg

    def test_url_error_hint_not_added_for_unrelated_errors(self):
        """非图片 URL 类 400 错误（如限流）不加 base64/MinIO 提示。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "code": "Throttling",
                "message": "rate limit exceeded, retry later",
            })

        client = self._client(handler)
        client.image_store = FakeImageStore()
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x", mime_type="image/png")
        msg = str(exc.value)
        assert "rate limit" in msg
        assert "MinIO" not in msg

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

    def test_custom_requires_minio_no_base64_fallback(self):
        """issue #164：自定义 OpenAI 兼容网关同样禁止 base64 内联——未
        启用 MinIO 时 describe 报错引导配置，不发请求。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("不应发出任何请求（未启用 MinIO 时禁止 base64）")

        client = self._client(handler)
        with pytest.raises(VisionModelError) as exc:
            client.describe(b"\x89PNG-x")
        msg = str(exc.value)
        assert "MinIO" in msg
        assert "http URL" in msg
        assert "base64" in msg

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
        client.image_store = FakeImageStore()
        desc = client.describe(b"\x89PNG-x")
        assert captured["url"] == self.CUSTOM_URL
        assert captured["body"]["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
        assert desc == "图中有高楼与蓝天"

    def test_custom_with_image_store_uses_http_url(self):
        """issue #163：自定义 OpenAI 兼容网关同样走 http URL 图片模式
        （Base URL 完整地址直用，图片不塞 base64）。"""
        store = FakeImageStore(url="http://img.example.com:9000/public/"
                                    "def456")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "自定义网关描述"}}],
            })

        client = self._client(handler)
        client.image_store = store
        desc = client.describe(b"\x89PNG-custom-minio")
        assert captured["url"] == self.CUSTOM_URL
        img = captured["body"]["messages"][0]["content"][1]
        assert img["image_url"]["url"] == store.url
        assert "data:image" not in img["image_url"]["url"]
        assert desc == "自定义网关描述"

    def test_missing_content_error_includes_request_url(self):
        """自定义网关响应 choices 为空（issue #156）：错误信息应带实际
        请求地址，帮助用户对照网关返回内容定位问题。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"choices": []})

        client = self._client(handler)
        client.image_store = FakeImageStore()
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
        client.image_store = FakeImageStore()
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
        # issue #164：图片为 http URL 模式（不再塞超长 base64），请求体
        # 完整可见且不触发截断
        assert "http://img.example.com:9000/public/abc" in msg
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
