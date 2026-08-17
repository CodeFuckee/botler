"""大模型 API 供应商插件（issue #140）。

把现有生图模型调用（issue #135，image_models.py）的 provider 分发迁移为
插件体系：每个供应商是一个 ``ImageProviderPlugin`` 插件，注册进全局注册表
后，``ImageModelClient.generate`` 按 provider 名查插件并委托调用。

内置插件：
- ``gemini_nano_banana``：Google Gemini Nano Banana Pro（generateContent
  接口，支持文本 prompt + 可选图片输入、图像输出）
- ``openai_gpt_image``：OpenAI GPT Image 2（images/generations 或
  images/edits 接口，输出统一为 base64 图片）

新增供应商：实现 ``ImageProviderPlugin.generate`` 并调用
``register_plugin`` 注册即可（或放入 ``worker.plugin_paths`` 外部加载）。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ImageProviderPlugin, PluginKind, register_plugin

logger = logging.getLogger("botler.plugins.models")

# 图像生成请求默认超时（秒）：图像生成/编辑耗时通常远高于文本接口
DEFAULT_TIMEOUT = 120.0

# 默认输出尺寸（OpenAI images API 要求；Gemini 由模型侧决定）
DEFAULT_SIZE = "1024x1024"


class ImageModelError(RuntimeError):
    """生图模型调用失败（缺 key / 网络异常 / 非 2xx 响应等）。"""


@dataclass
class ImageResult:
    """单张图片结果：MIME 类型 + 原始字节。"""

    mime_type: str
    data: bytes


class GeminiNanoBananaProvider(ImageProviderPlugin):
    """Gemini Nano Banana Pro：POST /models/{model}:generateContent。

    图片输入通过 parts 的 inline_data（base64）传递；输出取
    candidates[0].content.parts 中的 inlineData 解码为字节。
    """

    name = "gemini_nano_banana"
    display_name = "Gemini Nano Banana Pro"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    default_model = "gemini-3-pro-image"
    description = ("Google Gemini Nano Banana Pro（generateContent 接口，"
                   "支持文本 prompt + 可选图片输入、图像输出）")

    def generate(
        self,
        client: Any,
        prompt: str,
        image: bytes | None = None,
        *,
        mime_type: str = "image/png",
        size: str = DEFAULT_SIZE,
        n: int = 1,
    ) -> list[ImageResult]:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image is not None:
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image).decode("ascii"),
                },
            })
        payload = {"contents": [{"parts": parts}]}
        # 自定义 base_url 视为完整端点直接使用（issue #150），否则按
        # 官方接口拼接 generateContent 操作路径
        url = self.resolve_request_url(
            client.base_url, f"/models/{client.model}:generateContent")
        resp = client._http.post(
            url,
            headers={
                "X-goog-api-key": client.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            # 带上实际请求地址，便于用户诊断 Base URL 路径段缺失
            # （如官方域名少了 /v1beta）导致的 404 page not found
            raise ImageModelError(
                f"Gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）")
        data = resp.json()
        try:
            parts_out = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ImageModelError(
                f"Gemini 响应缺少图像结果: {resp.text[:200]}") from exc
        results: list[ImageResult] = []
        for part in parts_out:
            inline = part.get("inlineData") or {}
            b64 = inline.get("data")
            if not b64:
                continue  # 文本/其他 part 跳过（图像模型通常只回图像）
            results.append(ImageResult(
                mime_type=inline.get("mimeType", "image/png"),
                data=base64.b64decode(b64),
            ))
        if not results:
            raise ImageModelError(
                f"Gemini 响应未包含图像数据: {resp.text[:200]}")
        return results


class OpenAIGptImageProvider(ImageProviderPlugin):
    """OpenAI GPT Image 2：images/generations（无参考图）或
    images/edits（带参考图，multipart）。输出统一取 b64_json。"""

    name = "openai_gpt_image"
    display_name = "GPT Image 2"
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-image-2"
    description = ("OpenAI GPT Image 2（images/generations / images/edits "
                   "接口，输出统一为 base64 图片）")

    def generate(
        self,
        client: Any,
        prompt: str,
        image: bytes | None = None,
        *,
        mime_type: str = "image/png",
        size: str = DEFAULT_SIZE,
        n: int = 1,
    ) -> list[ImageResult]:
        headers = {
            "Authorization": f"Bearer {client.api_key}",
        }
        if image is None:
            # 自定义 base_url 视为完整端点直接使用（issue #150），
            # 否则按官方接口拼接 images/generations 操作路径
            url = self.resolve_request_url(client.base_url, "/images/generations")
            payload = {
                "model": client.model,
                "prompt": prompt,
                "n": max(1, int(n)),
                "size": size,
                # 默认只返回 url，显式要求 base64 才能回传图片给前端展示
                "response_format": "b64_json",
            }
            resp = client._http.post(url, headers=headers, json=payload)
        else:
            url = self.resolve_request_url(client.base_url, "/images/edits")
            files = {
                "image": ("image", image, mime_type),
                "prompt": (None, prompt),
                "model": (None, client.model),
                "n": (None, str(max(1, int(n)))),
                "size": (None, size),
                "response_format": (None, "b64_json"),
            }
            resp = client._http.post(url, headers=headers, files=files)
        if resp.status_code >= 400:
            # 带上实际请求地址便于诊断；404（page not found）多为
            # Base URL 缺 /v1 路径段或域名不对，给出针对性提示
            hint = ("；若为 404 page not found，请检查 Base URL 是否完整"
                    "（OpenAI 官方地址通常以 /v1 结尾，例如 "
                    "https://api.openai.com/v1）" if resp.status_code == 404 else "")
            raise ImageModelError(
                f"OpenAI 请求失败: HTTP {resp.status_code} {resp.text[:200]}"
                f"（请求地址: {url}）{hint}")
        data = resp.json()
        items = data.get("data") or []
        results: list[ImageResult] = []
        for item in items:
            b64 = item.get("b64_json")
            if not b64:
                continue
            results.append(ImageResult(
                mime_type="image/png", data=base64.b64decode(b64)))
        if not results:
            raise ImageModelError(
                f"OpenAI 响应未包含图像数据: {resp.text[:200]}")
        return results


# 模块导入即注册内置供应商插件（注册顺序 = 设置页预设展示顺序）
register_plugin(GeminiNanoBananaProvider())
register_plugin(OpenAIGptImageProvider())
