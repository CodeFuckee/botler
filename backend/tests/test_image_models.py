"""识图模型调用接口测试（issue #135）。

覆盖 Gemini Nano Banana Pro（generateContent）与 OpenAI GPT Image 2
（images/generations + images/edits）两个 provider 的请求构造、响应
解析与错误处理。用 httpx.MockTransport 模拟 HTTP，不做真实外呼。
"""

import base64

import pytest
import httpx

from botler.image_models import (
    ImageModelClient,
    ImageModelError,
    IMAGE_MODEL_PRESETS,
    client_from_config,
    find_enabled,
)


class TestPresets:
    def test_two_builtin_providers(self):
        """内置预设包含 gemini_nano_banana 与 openai_gpt_image。"""
        assert set(IMAGE_MODEL_PRESETS) == {
            "gemini_nano_banana", "openai_gpt_image"}

    def test_preset_defaults(self):
        """预设默认端点与模型符合两 provider 官方接口。"""
        gemini = IMAGE_MODEL_PRESETS["gemini_nano_banana"]
        assert gemini["base_url"].startswith(
            "https://generativelanguage.googleapis.com")
        assert gemini["model"]
        openai = IMAGE_MODEL_PRESETS["openai_gpt_image"]
        assert openai["base_url"].startswith("https://api.openai.com")
        assert openai["model"]

    def test_unknown_provider_rejected(self):
        """不支持的 provider 构造时报错。"""
        with pytest.raises(ImageModelError, match="不支持的识图模型类型"):
            ImageModelClient(name="x", provider="unknown_provider",
                             api_key="k", model="m")


class TestClientCommon:
    def test_empty_prompt_rejected(self):
        """prompt 为空直接报错（不发请求）。"""
        client = ImageModelClient(
            name="n", provider="openai_gpt_image", api_key="k")
        with pytest.raises(ImageModelError, match="prompt"):
            client.generate("   ")

    def test_missing_api_key_rejected(self):
        """未配置 API Key 时明确报错（不发请求）。"""
        client = ImageModelClient(
            name="n", provider="gemini_nano_banana", api_key="")
        with pytest.raises(ImageModelError, match="未配置 API Key"):
            client.generate("画一只猫")


class TestGeminiClient:
    def _client(self, handler, api_key="AIza-test"):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="Gemini", provider="gemini_nano_banana",
            api_key=api_key, timeout=5)
        client._http = httpx.Client(transport=transport)  # 注入 mock 传输
        return client

    def test_generate_content_request_and_parse(self):
        """文本 prompt：POST generateContent，X-goog-api-key 认证，
        响应 inlineData base64 解码为字节。"""
        png = b"\x89PNG-fake-image"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["key"] = request.headers.get("X-goog-api-key")
            captured["json"] = request.read().decode()
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [
                    {"inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(png).decode(),
                    }},
                ]}}],
            })

        client = self._client(handler)
        results = client.generate("画一只蓝色猫咪")
        assert captured["method"] == "POST"
        assert captured["url"].endswith(
            "/models/gemini-3-pro-image:generateContent")
        assert captured["key"] == "AIza-test"
        import json
        body = json.loads(captured["json"])
        assert body["contents"][0]["parts"][0]["text"] == "画一只蓝色猫咪"
        assert len(results) == 1
        assert results[0].data == png
        assert results[0].mime_type == "image/png"

    def test_generate_with_image_input(self):
        """带参考图片：inline_data 携带 base64 图片字节。"""
        png = b"\x89PNG-input"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["parts"] = json.loads(request.read().decode())["contents"][0]["parts"]
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [
                    {"inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(b"\x89PNG-out").decode(),
                    }},
                ]}}],
            })

        client = self._client(handler)
        results = client.generate("把图片里的猫换成狗", image=png,
                                  mime_type="image/png")
        assert captured["parts"][0]["text"] == "把图片里的猫换成狗"
        inline = captured["parts"][1]["inline_data"]
        assert inline["mime_type"] == "image/png"
        assert base64.b64decode(inline["data"]) == png
        assert results[0].data == b"\x89PNG-out"

    def test_error_response_raises(self):
        """非 2xx 响应（如 401）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text='{"error": "API key invalid"}')

        client = self._client(handler)
        with pytest.raises(ImageModelError, match="401"):
            client.generate("画一只猫")

    def test_missing_image_in_response_raises(self):
        """响应不含图像数据时报错。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"text": "抱歉，我无法生成图片"}]}}]})

        client = self._client(handler)
        with pytest.raises(ImageModelError, match="未包含图像数据"):
            client.generate("画一只猫")


class TestOpenAIClient:
    def _client(self, handler, api_key="sk-test"):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="GPT Image", provider="openai_gpt_image",
            api_key=api_key, timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def test_generations_request_and_parse(self):
        """无参考图：POST images/generations，Bearer 认证，b64_json 解析。"""
        img = b"\x89PNG-openai"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(img).decode()}],
            })

        client = self._client(handler)
        results = client.generate("赛博朋克风格的城市夜景",
                                  size="1536x1024", n=2)
        assert captured["url"].endswith("/images/generations")
        assert captured["auth"] == "Bearer sk-test"
        import json
        body = json.loads(captured["body"])
        assert body["model"] == "gpt-image-2"
        assert body["prompt"] == "赛博朋克风格的城市夜景"
        assert body["size"] == "1536x1024"
        assert body["n"] == 2
        assert len(results) == 1
        assert results[0].data == img

    def test_edits_request_multipart_with_image(self):
        """带参考图：POST images/edits，multipart 携带 image + prompt。"""
        png = b"\x89PNG-input-openai"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("Content-Type", "")
            captured["body"] = request.read()
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"\x89PNG-out").decode()}],
            })

        client = self._client(handler)
        results = client.generate("把背景换成星空", image=png,
                                  mime_type="image/png")
        assert captured["url"].endswith("/images/edits")
        assert "multipart/form-data" in captured["content_type"]
        raw = captured["body"]
        assert b"\x89PNG-input-openai" in raw
        assert "把背景换成星空".encode() in raw
        assert results[0].data == b"\x89PNG-out"

    def test_error_response_raises(self):
        """非 2xx 响应（如 429 限流）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limit exceeded")

        client = self._client(handler)
        with pytest.raises(ImageModelError, match="429"):
            client.generate("画一只猫")

    def test_missing_image_in_response_raises(self):
        """响应 data 为空时报错。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        client = self._client(handler)
        with pytest.raises(ImageModelError, match="未包含图像数据"):
            client.generate("画一只猫")


class TestHelpers:
    def test_find_enabled_returns_matching(self):
        """find_enabled 找指定 provider 且启用且有 key 的项。"""
        models = [
            {"name": "A", "provider": "openai_gpt_image",
             "api_key": "k1", "enabled": True},
            {"name": "B", "provider": "gemini_nano_banana",
             "api_key": "", "enabled": True},
            {"name": "C", "provider": "gemini_nano_banana",
             "api_key": "k3", "enabled": False},
            {"name": "D", "provider": "gemini_nano_banana",
             "api_key": "k4", "enabled": True},
        ]
        found = find_enabled(models, "gemini_nano_banana")
        assert found is not None and found["name"] == "D"
        assert find_enabled(models, "custom") is None
        assert find_enabled(None, "gemini_nano_banana") is None

    def test_client_from_config(self):
        """按配置项构造客户端，缺省值由客户端补全。"""
        cfg = {"name": "Gemini 生产", "provider": "gemini_nano_banana",
               "base_url": "", "api_key": "k", "model": ""}
        client = client_from_config(cfg)
        assert client.name == "Gemini 生产"
        assert client.base_url == IMAGE_MODEL_PRESETS["gemini_nano_banana"]["base_url"]
        assert client.model == "gemini-3-pro-image"

    def test_client_from_config_unknown_provider_raises(self):
        """配置 provider 未知时构造报错（配置错误尽早暴露）。"""
        with pytest.raises(ImageModelError):
            client_from_config({"name": "x", "provider": "nope",
                                "api_key": "k"})
