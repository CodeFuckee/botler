"""识图模型调用接口（issue #135）。

设置页「识图模型」卡片配置的图像模型统一调用封装。本期实现两个 provider：

- ``gemini_nano_banana``：Google Gemini Nano Banana Pro（默认模型
  ``gemini-3-pro-image``），走 ``generateContent`` 接口，支持文本
  prompt + 可选图片输入（base64 inline_data）、图像输出（inlineData）。
- ``openai_gpt_image``：OpenAI GPT Image 2（默认模型 ``gpt-image-2``），
  走 OpenAI images API：无图片输入走 ``images/generations``（JSON），
  带图片输入走 ``images/edits``（multipart）；输出统一为 base64 图片。

统一入口 :meth:`ImageModelClient.generate`：文本 prompt + 可选图片 →
图片结果列表。配置来自 ``Settings.image_models``（设置页 / config.yaml），
API Key 支持 ``${ENV}`` 引用（config 加载时已展开为明文），落盘
config.yaml、API 只返回掩码，与 ai_providers（issue #46）同模式。

本期仅实现调用接口封装（为后续 AI 功能消费做准备），不接入具体业务。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("botler.image_models")

# 图像生成请求默认超时（秒）：图像生成/编辑耗时通常远高于文本接口
DEFAULT_TIMEOUT = 120.0

# 默认输出尺寸（OpenAI images API 要求；Gemini 由模型侧决定）
DEFAULT_SIZE = "1024x1024"

# 内置预设（issue #135）：与前端 providers.jsx 的 IMAGE_MODEL_PRESETS 对应。
# key → 默认 base_url / model（设置页选择预设自动填充，均可修改）。
IMAGE_MODEL_PRESETS: dict[str, dict[str, str]] = {
    "gemini_nano_banana": {
        "name": "Gemini Nano Banana Pro",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3-pro-image",
    },
    "openai_gpt_image": {
        "name": "GPT Image 2",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-image-2",
    },
}


class ImageModelError(RuntimeError):
    """识图模型调用失败（缺 key / 网络异常 / 非 2xx 响应等）。"""


@dataclass
class ImageResult:
    """单张图片结果：MIME 类型 + 原始字节。"""

    mime_type: str
    data: bytes


class ImageModelClient:
    """识图模型统一调用客户端。

    入参对应 Settings.image_models 列表项（``{name, provider, base_url,
    api_key, model, enabled}``）。``generate()`` 按 provider 分发到
    Gemini generateContent 或 OpenAI images API。
    """

    def __init__(
        self,
        *,
        name: str,
        provider: str,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        verify_ssl: bool = True,
    ) -> None:
        if provider not in IMAGE_MODEL_PRESETS:
            raise ImageModelError(
                f"不支持的识图模型类型: {provider}（可选: "
                + ", ".join(IMAGE_MODEL_PRESETS) + "）")
        preset = IMAGE_MODEL_PRESETS[provider]
        self.name = name or preset["name"]
        self.provider = provider
        self.base_url = (base_url or preset["base_url"]).rstrip("/")
        self.api_key = api_key
        self.model = model or preset["model"]
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # 实例级客户端：超时/SSL 统一管理，测试可注入 mock 传输
        self._http = httpx.Client(timeout=timeout, verify=verify_ssl)

    # ---- 统一入口 ----

    def generate(
        self,
        prompt: str,
        image: bytes | None = None,
        *,
        mime_type: str = "image/png",
        size: str = DEFAULT_SIZE,
        n: int = 1,
    ) -> list[ImageResult]:
        """按 prompt（+ 可选参考图片）生成图片，返回图片结果列表。

        :param prompt: 文本指令（生成/编辑描述）
        :param image: 可选参考图片字节（Gemini 作为 inline_data 输入；
            OpenAI 走 images/edits 编辑接口）
        :param mime_type: 输入图片 MIME 类型（image 非空时生效）
        :param size: 输出尺寸（OpenAI 生效，如 1024x1024）
        :param n: 生成张数（OpenAI 生效；Gemini 默认单张）
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageModelError("识图模型调用缺少 prompt（生成指令不能为空）")
        if not self.api_key:
            raise ImageModelError(f"识图模型「{self.name}」未配置 API Key")
        try:
            if self.provider == "gemini_nano_banana":
                return self._generate_gemini(prompt, image, mime_type)
            return self._generate_openai(prompt, image, mime_type, size, n)
        except ImageModelError:
            raise
        except httpx.TimeoutException as exc:
            raise ImageModelError(
                f"识图模型「{self.name}」请求超时（>{self.timeout}s）") from exc
        except httpx.HTTPError as exc:
            raise ImageModelError(
                f"识图模型「{self.name}」网络请求失败: {exc}") from exc

    # ---- Gemini generateContent ----

    def _generate_gemini(
        self, prompt: str, image: bytes | None, mime_type: str
    ) -> list[ImageResult]:
        """Gemini Nano Banana Pro：POST /models/{model}:generateContent。

        图片输入通过 parts 的 inline_data（base64）传递；输出取
        candidates[0].content.parts 中的 inlineData 解码为字节。
        """
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image is not None:
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image).decode("ascii"),
                },
            })
        payload = {"contents": [{"parts": parts}]}
        url = f"{self.base_url}/models/{self.model}:generateContent"
        resp = self._http.post(
            url,
            headers={
                "X-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            raise ImageModelError(
                f"Gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}")
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

    # ---- OpenAI images API ----

    def _generate_openai(
        self,
        prompt: str,
        image: bytes | None,
        mime_type: str,
        size: str,
        n: int,
    ) -> list[ImageResult]:
        """OpenAI GPT Image 2：images/generations（无参考图）或
        images/edits（带参考图，multipart）。输出统一取 b64_json。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        if image is None:
            url = f"{self.base_url}/images/generations"
            payload = {
                "model": self.model,
                "prompt": prompt,
                "n": max(1, int(n)),
                "size": size,
            }
            resp = self._http.post(url, headers=headers, json=payload)
        else:
            url = f"{self.base_url}/images/edits"
            files = {
                "image": ("image", image, mime_type),
                "prompt": (None, prompt),
                "model": (None, self.model),
                "n": (None, str(max(1, int(n)))),
                "size": (None, size),
            }
            resp = self._http.post(url, headers=headers, files=files)
        if resp.status_code >= 400:
            raise ImageModelError(
                f"OpenAI 请求失败: HTTP {resp.status_code} {resp.text[:200]}")
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


def find_enabled(models: list[dict] | None, provider: str) -> dict | None:
    """从 Settings.image_models 列表中找指定 provider 的启用项。"""
    for m in models or []:
        if (isinstance(m, dict)
                and str(m.get("provider", "")).strip() == provider
                and bool(m.get("enabled", True))
                and str(m.get("api_key", "") or "").strip()):
            return m
    return None


def client_from_config(cfg: dict) -> ImageModelClient:
    """按配置项构造客户端（provider 归一、缺省值由客户端补）。"""
    provider = str(cfg.get("provider") or "").strip() or "custom"
    return ImageModelClient(
        name=str(cfg.get("name") or "").strip(),
        provider=provider,
        base_url=str(cfg.get("base_url") or "").strip(),
        api_key=str(cfg.get("api_key") or "").strip(),
        model=str(cfg.get("model") or "").strip(),
    )
