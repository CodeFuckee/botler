"""生图模型调用接口测试（issue #135）。

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
        with pytest.raises(ImageModelError, match="不支持的生成模型类型"):
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

    def test_error_response_includes_request_url(self):
        """非 2xx 响应错误信息应包含实际请求地址（便于用户诊断
        Base URL 路径段缺失导致的 404 page not found）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="404 page not found")

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        assert "请求地址" in str(exc.value)
        # 404 时附加 Base URL 提示（issue #150 语义），请求地址仍完整展示
        assert ("/models/gemini-3-pro-image:generateContent）"
                in str(exc.value))

    def test_error_404_includes_base_url_hint(self):
        """404（自定义 Base URL 缺完整路径的 page not found）应附带
        Base URL 检查提示（与 OpenAI 一致，issue #150 语义）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="404 page not found")

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        assert "Base URL" in str(exc.value)
        assert "完整请求地址" in str(exc.value)


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

    def test_generations_requests_b64_json_response_format(self):
        """请求体携带 response_format=b64_json：OpenAI 默认只返回
        url 字段，不显式要求则拿不到 b64_json，成功也无法回传图片。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"x").decode()}],
            })

        client = self._client(handler)
        client.generate("生成一张图")
        assert captured["body"]["response_format"] == "b64_json"

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

    def test_edits_requests_b64_json_response_format(self):
        """编辑接口 multipart 表单同样携带 response_format=b64_json。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"x").decode()}],
            })

        client = self._client(handler)
        client.generate("把背景换成星空", image=b"\x89PNG-in",
                        mime_type="image/png")
        raw = captured["body"]
        assert b'name="response_format"' in raw
        assert b"b64_json" in raw

    def test_error_response_raises(self):
        """非 2xx 响应（如 429 限流）抛出带状态码的错误。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limit exceeded")

        client = self._client(handler)
        with pytest.raises(ImageModelError, match="429"):
            client.generate("画一只猫")

    def test_error_404_includes_request_url(self):
        """404（如 Base URL 缺 /v1 路径段的 page not found）错误信息
        应包含实际请求地址，帮助用户定位配置问题。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(404, text="404 page not found")

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        assert "404" in str(exc.value)
        assert captured["url"] in str(exc.value)
        # 404 应附带 Base URL 检查提示（缺 /v1 路径段常见）
        assert "Base URL" in str(exc.value)

    def test_404_hint_explains_custom_url_verbatim(self):
        """自定义 Base URL（含完整路径）404 时：请求地址原样直用，且
        错误提示明确「自定义 Base URL 作为完整请求地址直接使用、不再
        拼接接口路径」（issue #150 语义的用户可诊断提示）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(404, text="404 page not found")

        client = ImageModelClient(
            name="GPT Image", provider="openai_gpt_image",
            base_url="https://grsai.dakka.com.cn/v1/draw/completions",
            api_key="sk-test", timeout=5)
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        # 请求地址 = 自定义完整 URL，原样直用（issue #150 核心语义）
        assert captured["url"] == "https://grsai.dakka.com.cn/v1/draw/completions"
        assert "完整请求地址" in str(exc.value)
        assert "不再拼接" in str(exc.value)
        assert "grsai.dakka.com.cn/v1/draw/completions" in str(exc.value)

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


class TestCustomBaseUrlVerbatim:
    """自定义 base_url 视为完整调用地址直接使用（issue #150）。

    用户配置完整端点（如代理网关 https://grsai.dakka.com.cn/v1/draw/completions）
    时，请求必须原样打到该地址，不再拼接 /images/generations 等操作路径；
    未配置 / 等于预设默认时仍按官方接口拼接（既有行为保持）。
    """

    CUSTOM_OPENAI = "https://grsai.dakka.com.cn/v1/draw/completions"
    CUSTOM_GEMINI = ("https://grsai.dakka.com.cn/v1beta/models/"
                     "gemini-3-pro-image:generateContent")

    def _openai(self, handler, base_url=CUSTOM_OPENAI):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="GPT Image", provider="openai_gpt_image",
            base_url=base_url, api_key="sk-test", timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def _gemini(self, handler, base_url=CUSTOM_GEMINI):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="Gemini", provider="gemini_nano_banana",
            base_url=base_url, api_key="AIza-test", timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def test_openai_generations_uses_custom_url_verbatim(self):
        """无参考图：自定义完整 URL 直接作为请求地址，不拼接
        /images/generations。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"\x89PNG-x").decode()}],
            })

        client = self._openai(handler)
        client.generate("画一只猫")
        assert captured["url"] == self.CUSTOM_OPENAI
        assert not captured["url"].endswith("/images/generations")

    def test_openai_edits_uses_custom_url_verbatim(self):
        """带参考图：自定义完整 URL 直接作为请求地址，不拼接
        /images/edits。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"\x89PNG-x").decode()}],
            })

        client = self._openai(handler)
        client.generate("把背景换成星空", image=b"\x89PNG-in",
                        mime_type="image/png")
        assert captured["url"] == self.CUSTOM_OPENAI
        assert not captured["url"].endswith("/images/edits")

    def test_gemini_uses_custom_url_verbatim(self):
        """Gemini：自定义完整 URL（含 :generateContent 端点）直接使用，
        不再重复拼接 /models/{model}:generateContent。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [
                    {"inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(b"\x89PNG-x").decode(),
                    }},
                ]}}],
            })

        client = self._gemini(handler)
        client.generate("画一只猫")
        assert captured["url"] == self.CUSTOM_GEMINI
        assert not captured["url"].endswith("/models/")

    def test_default_base_url_with_trailing_slash_still_appends_path(self):
        """等于预设默认的 base_url（含尾斜杠，rstrip 归一）仍按官方
        接口拼接操作路径，不误判为自定义完整地址。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(b"\x89PNG-x").decode()}],
            })

        client = self._openai(handler, base_url="https://api.openai.com/v1/")
        client.generate("画一只猫")
        assert captured["url"].endswith("/images/generations")
        assert captured["url"].startswith("https://api.openai.com/v1")


class TestJsonDecodeErrorDiagnosis:
    """接口返回 200 + 空 body / 非 JSON 内容时的可诊断错误（issue #151）。

    生图接口（或自定义 Base URL 指向的网关/代理）常见返回 HTTP 200 但
    body 为空或为 HTML 错误页：直接 resp.json() 抛出的
    "Expecting value: line 1 column 1 (char 0)" 无法定位问题。修复前
    该异常不是 ImageModelError，会穿透到设置页显示成
    「生图测试失败: Expecting value: ...」；修复后：

    - Gemini：转为带状态码 / 响应片段 / 请求地址的 ImageModelError，
      用户可据此判断是网关拦截还是 Base URL 配错端点；
    - OpenAI（issue #151 后续反馈）：把接口原始返回内容直接完整展示
      在错误信息中（不再截断到 200 字符、不再包裹冗长提示），用户
      可直接看到接口到底返回了什么。
    """

    def _gemini(self, handler):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="Gemini", provider="gemini_nano_banana",
            api_key="AIza-test", timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def _openai(self, handler):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="GPT Image", provider="openai_gpt_image",
            api_key="sk-test", timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def test_gemini_empty_body_raises_helpful_error(self):
        """Gemini：200 + 空 body → ImageModelError 带响应片段与请求地址。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200)  # 空响应体

        client = self._gemini(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert "空响应体" in msg
        assert captured["url"] in msg
        assert "Expecting value" not in msg

    def test_gemini_html_body_raises_helpful_error(self):
        """Gemini：200 + HTML 错误页 → ImageModelError 带内容片段提示。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200, text="<html><body>502 Bad Gateway</body></html>",
                headers={"Content-Type": "text/html"})

        client = self._gemini(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert "502 Bad Gateway" in msg
        assert captured["url"] in msg

    def test_openai_empty_body_raises_error_directly_shows_empty_note(self):
        """OpenAI：200 + 空 body → 错误信息直接说明「空响应体」，不再截断隐藏。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = self._openai(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert "空响应体" in msg
        assert "Expecting value" not in msg

    def test_openai_plain_text_body_shows_full_raw_content(self):
        """OpenAI：200 + 纯文本（非 JSON）→ 直接完整展示接口原始返回内容。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok, no json here",
                                  headers={"Content-Type": "text/plain"})

        client = self._openai(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert "ok, no json here" in msg
        assert "Expecting value" not in msg

    def test_openai_long_text_body_not_truncated(self):
        """OpenAI：非 JSON 内容超过 200 字符也不截断，完整展示（用户反馈
        「直接将接口返回的内容显示出来」，issue #151 后续）。"""
        raw = "gateway said: " + "x" * 300
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=raw,
                                  headers={"Content-Type": "text/plain"})

        client = self._openai(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert raw in msg  # 完整内容直接展示，不允许 200 字符截断

    def test_openai_html_body_shows_full_raw_content(self):
        """OpenAI：200 + HTML 错误页 → 完整 HTML 内容直接展示。"""
        html = ("<html><body><h1>502 Bad Gateway</h1>"
                "<p>upstream error details here</p></body></html>")
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html,
                                  headers={"Content-Type": "text/html"})

        client = self._openai(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "不是有效 JSON" in msg
        assert html in msg
        assert "Expecting value" not in msg


class TestOpenAISseResponse:
    """OpenAI 接口返回 SSE（text/event-stream）流式响应（issue #151 用户反馈）。

    用户配置的生图接口（如 aitohumanize 类聚合网关）真实返回为 SSE 流：
    多行 ``data: {json}`` 事件逐步上报进度（progress/status），最终事件
    ``status: "succeeded"`` 且 ``results[0].url`` 为生成图片地址。修复前
    该内容被当作普通 JSON 解析失败，错误信息只能展示原始流内容，无法
    拿到图片；修复后应解析 data 事件、下载 results 中的图片 URL 并返回
    图片结果。
    """

    IMG_URL = "https://file7.aitohumanize.com/file/0c593022c7fe4ec2a43515a91cade7a6.png"
    PNG = b"\x89PNG-sse-downloaded"

    SSE_SUCCEEDED = (
        'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549","task_id":"",'
        '"url":"","width":0,"height":0,"progress":1,"status":"running",'
        '"failure_reason":"","error":"","results":null,"callback_url":"",'
        '"start_time":1786947802,"end_time":0}\n'
        'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549","task_id":"",'
        '"url":"","width":0,"height":0,"progress":50,"status":"running",'
        '"failure_reason":"","error":"","results":null,"callback_url":"",'
        '"start_time":1786947802,"end_time":0}\n'
        'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549","task_id":"",'
        '"url":"","width":0,"height":0,"progress":100,"status":"succeeded",'
        '"failure_reason":"","error":"","results":[{"url":"'
        + IMG_URL + '","width":0,"height":0}],"callback_url":"",'
        '"start_time":1786947802,"end_time":0}\n'
    )

    SSE_FAILED = (
        'data: {"id":"t1","progress":10,"status":"running","error":"",'
        '"failure_reason":"","results":null}\n'
        'data: {"id":"t1","progress":100,"status":"failed",'
        '"failure_reason":"图片包含违规内容","error":"bad prompt",'
        '"results":null}\n'
    )

    def _client(self, handler, api_key="sk-test"):
        transport = httpx.MockTransport(handler)
        client = ImageModelClient(
            name="GPT Image", provider="openai_gpt_image",
            api_key=api_key, timeout=5)
        client._http = httpx.Client(transport=transport)
        return client

    def test_sse_succeeded_downloads_image_from_results_url(self):
        """SSE 流最终 status=succeeded：解析 data 事件，下载 results 中
        图片 URL 并返回 ImageResult（Content-Type 作为 mime）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                captured["url"] = str(request.url)
                return httpx.Response(
                    200, text=self.SSE_SUCCEEDED,
                    headers={"Content-Type": "text/event-stream"})
            # GET：下载生成图片
            captured["download_url"] = str(request.url)
            return httpx.Response(
                200, content=self.PNG,
                headers={"Content-Type": "image/png"})

        client = self._client(handler)
        results = client.generate("画一只猫")
        assert captured["url"].endswith("/images/generations")
        assert captured["download_url"] == self.IMG_URL
        assert len(results) == 1
        assert results[0].data == self.PNG
        assert results[0].mime_type == "image/png"

    def test_sse_failed_raises_error_with_reason(self):
        """SSE 流最终 status=failed：错误信息包含 failure_reason / error，
        不再把原始流内容当普通 JSON 报「不是有效 JSON」。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=self.SSE_FAILED,
                headers={"Content-Type": "text/event-stream"})

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        # 结构化错误：明确「任务失败」+ failure_reason / error 字段，
        # 而不是把整段 SSE 原始流当普通 JSON 解析失败的原始内容展示
        assert "生图任务失败" in msg
        assert "图片包含违规内容" in msg  # failure_reason 展示
        assert "bad prompt" in msg         # error 展示
        assert "data: {" not in msg        # 不再整体倾倒原始 SSE 流

    def test_sse_no_succeeded_result_raises_diagnostic(self):
        """SSE 流只有 running 事件、没有 succeeded 结果：报可诊断错误，
        错误信息包含事件状态说明（而非「Expecting value」）。"""
        running_only = (
            'data: {"id":"t1","progress":1,"status":"running",'
            '"results":null,"error":"","failure_reason":""}\n'
            'data: {"id":"t1","progress":50,"status":"running",'
            '"results":null,"error":"","failure_reason":""}\n'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=running_only,
                headers={"Content-Type": "text/event-stream"})

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "Expecting value" not in msg
        assert "succeeded" in msg  # 说明流中没有成功事件

    def test_sse_download_failure_raises(self):
        """SSE 成功事件给出图片 URL，但下载失败（如 404）：报错指明
        下载失败与地址，不静默返回空结果。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200, text=self.SSE_SUCCEEDED,
                    headers={"Content-Type": "text/event-stream"})
            return httpx.Response(404, text="not found")

        client = self._client(handler)
        with pytest.raises(ImageModelError) as exc:
            client.generate("画一只猫")
        msg = str(exc.value)
        assert "下载失败" in msg
        assert self.IMG_URL in msg

    def test_sse_multiline_data_event_parsed(self):
        """SSE 单事件多行 data（JSON 跨行）按规范以换行拼接解析（兼容
        标准 SSE 实现，而非仅单行 data）。"""
        # 事件1: 单行 running；事件2: 多行 JSON（results 跨行）
        sse = (
            'data: {"id":"t1","progress":10,"status":"running",'
            '"results":null,"error":"","failure_reason":""}\n'
            "\n"
            'data: {"id":"t1","progress":100,"status":"succeeded",\n'
            'data: "results":[{"url":"' + self.IMG_URL + '","width":0,"height":0}],\n'
            'data: "error":"","failure_reason":""}\n'
            "\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200, text=sse,
                    headers={"Content-Type": "text/event-stream"})
            return httpx.Response(
                200, content=self.PNG,
                headers={"Content-Type": "image/png"})

        client = self._client(handler)
        results = client.generate("画一只猫")
        assert len(results) == 1
        assert results[0].data == self.PNG

    def test_sse_with_done_marker_ignored(self):
        """SSE 流结束标记 ``data: [DONE]`` 应被忽略，不影响结果解析。"""
        sse = self.SSE_SUCCEEDED + "data: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200, text=sse,
                    headers={"Content-Type": "text/event-stream"})
            return httpx.Response(
                200, content=self.PNG,
                headers={"Content-Type": "image/png"})

        client = self._client(handler)
        results = client.generate("画一只猫")
        assert len(results) == 1
        assert results[0].data == self.PNG

    def test_sse_detected_by_body_when_content_type_missing(self):
        """网关未返回 text/event-stream Content-Type（或类型缺失）但
        body 是 data: 行时，同样按 SSE 解析（避免退化为「不是有效
        JSON」的原始内容展示）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, text=self.SSE_SUCCEEDED)
            return httpx.Response(
                200, content=self.PNG,
                headers={"Content-Type": "image/png"})

        client = self._client(handler)
        results = client.generate("画一只猫")
        assert len(results) == 1
        assert results[0].data == self.PNG

    def test_sse_inline_multiple_events_on_one_line(self):
        """网关把多个 data 事件挤在同一行、以空格分隔（issue #151 用户
        实际粘贴的返回内容形态：``data: {...} data: {...}`` 无换行），
        同样应按事件解析并下载 results 中的图片。修复前逐行解析拿不到
        任何事件，只能报「不是有效 JSON」并倾倒整行原始内容。"""
        # 用户 note 中真实网关返回的单行形态：多个 data: {json} 空格分隔
        inline_sse = (
            'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549",'
            '"task_id":"","url":"","width":0,"height":0,"progress":1,'
            '"status":"running","failure_reason":"","error":"",'
            '"results":null,"callback_url":"","start_time":1786947802,'
            '"end_time":0} '
            'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549",'
            '"task_id":"","url":"","width":0,"height":0,"progress":50,'
            '"status":"running","failure_reason":"","error":"",'
            '"results":null,"callback_url":"","start_time":1786947802,'
            '"end_time":0} '
            'data: {"id":"16-18ce737e-9692-455d-89a9-be79f437c549",'
            '"task_id":"","url":"","width":0,"height":0,"progress":100,'
            '"status":"succeeded","failure_reason":"","error":"",'
            '"results":[{"url":"' + self.IMG_URL + '","width":0,'
            '"height":0}],"callback_url":"","start_time":1786947802,'
            '"end_time":1786947840}'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200, text=inline_sse,
                    headers={"Content-Type": "text/event-stream"})
            # GET：下载生成图片
            return httpx.Response(
                200, content=self.PNG,
                headers={"Content-Type": "image/png"})

        client = self._client(handler)
        results = client.generate("画一只猫")
        assert len(results) == 1
        assert results[0].data == self.PNG
