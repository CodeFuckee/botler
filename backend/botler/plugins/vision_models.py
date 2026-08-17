"""识图（视觉理解）API 供应商插件（issue #152）。

设置页「识图模型」卡片配置的视觉理解模型统一调用封装，与生图模型
（issue #135，plugins/models.py）同插件体系：每个供应商是一个
``VisionProviderPlugin`` 插件，注册进全局注册表后，
``VisionModelClient.describe`` 按 provider 名查插件并委托调用。

内置插件：
- ``gemini_vision``：Google Gemini（generateContent 接口，默认模型
  ``gemini-2.5-flash``），图片经 inline_data（base64）输入，返回文本。
- ``openai_vision``：OpenAI（chat/completions 接口，默认模型
  ``gpt-4o``），图片经 image_url（data URL base64）输入，返回文本。
- ``custom``：自定义 OpenAI 兼容视觉模型（chat/completions 接口），
  无默认端点/模型，用户自填 Base URL / 模型（如硅基流动 / DeepSeek-VL /
  qwen-vl 等网关）；自定义 Base URL 作为完整请求地址直接使用（issue
  #150 语义），未配置 Base URL 时明确报错。

新增供应商：实现 ``VisionProviderPlugin.describe`` 并调用
``register_plugin`` 注册即可（或放入 ``worker.plugin_paths`` 外部加载）。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from .base import PluginKind, VisionProviderPlugin, register_plugin

logger = logging.getLogger("botler.plugins.vision_models")

# 识图请求默认超时（秒）：图片理解通常比纯文本接口慢，放宽到 60s
DEFAULT_TIMEOUT = 60.0

# 默认描述指令（测试按钮 / 调用方未传 prompt 时使用）
DEFAULT_VISION_PROMPT = "请详细描述这张图片的内容"


class VisionModelError(RuntimeError):
    """识图模型调用失败（缺 key / 缺图片 / 缺 Base URL / 网络异常 /
    非 2xx 响应 / 响应无文本等）。"""


def _parse_json_response(
    resp: httpx.Response, url: str, provider: str,
) -> dict[str, Any]:
    """解析识图接口 JSON 响应（复用生图 issue #151 的诊断语义）。

    网关/代理或自定义 Base URL 指向错误端点时，接口常返回 HTTP 2xx 但
    body 为空或为 HTML 错误页：直接 ``resp.json()`` 抛出的
    ``json.JSONDecodeError`` 无法定位问题。这里统一转为带状态码 /
    Content-Type / 响应片段 / 请求地址的 :class:`VisionModelError`。
    """
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 JSON 解析失败统一诊断
        raw = (resp.text or "").strip() or "（空响应体）"
        ctype = resp.headers.get("content-type") or "未知"
        raise VisionModelError(
            f"{provider} 接口返回内容不是有效 JSON（HTTP "
            f"{resp.status_code}，Content-Type: {ctype}）："
            f"{raw[:200]}（请求地址: {url}）。若使用自定义 Base URL "
            "请确认其指向正确的接口端点；若经网关/代理转发请检查网关返回内容"
        ) from exc


class GeminiVisionProvider(VisionProviderPlugin):
    """Google Gemini 视觉理解：POST /models/{model}:generateContent。

    图片通过 parts 的 inline_data（base64）输入，文本描述指令通过
    text part 输入；输出拼接 candidates[0].content.parts 中的全部
    text 片段返回。
    """

    name = "gemini_vision"
    display_name = "Gemini 视觉"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    default_model = "gemini-2.5-flash"
    description = ("Google Gemini（generateContent 接口，默认模型 "
                   "gemini-2.5-flash，支持图片输入 + 文本描述输出）")

    def describe(self, client: Any, image: bytes, *,
                 mime_type: str = "image/png",
                 prompt: str = "") -> str:
        parts: list[dict[str, Any]] = [{
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(image).decode("ascii"),
            },
        }]
        text = (prompt or "").strip() or DEFAULT_VISION_PROMPT
        parts.append({"text": text})
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
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接接口路径），请填写含完整"
                    "路径的地址" if resp.status_code == 404 else "")
            raise VisionModelError(
                f"Gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）{hint}")
        data = _parse_json_response(resp, url, "Gemini")
        try:
            parts_out = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"Gemini 响应缺少内容: {resp.text[:200]}") from exc
        texts = [str(p.get("text") or "").strip()
                 for p in parts_out if isinstance(p, dict) and p.get("text")]
        desc = "".join(t for t in texts if t)
        if not desc:
            raise VisionModelError(
                f"Gemini 响应未包含文本描述: {resp.text[:200]}")
        return desc


class OpenAIVisionProvider(VisionProviderPlugin):
    """OpenAI 视觉理解：POST /chat/completions。

    图片经 messages[].content 的 image_url（data URL base64）输入，
    文本描述指令经 type=text 部分输入；输出取 choices[0].message.content。
    """

    name = "openai_vision"
    display_name = "OpenAI 视觉"
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o"
    description = ("OpenAI（chat/completions 接口，默认模型 gpt-4o，"
                   "支持图片输入 + 文本描述输出）")

    def describe(self, client: Any, image: bytes, *,
                 mime_type: str = "image/png",
                 prompt: str = "") -> str:
        text = (prompt or "").strip() or DEFAULT_VISION_PROMPT
        content: list[dict[str, Any]] = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,"
                           f"{base64.b64encode(image).decode('ascii')}",
                },
            },
        ]
        payload = {
            "model": client.model,
            "messages": [{"role": "user", "content": content}],
        }
        # 自定义 base_url 视为完整端点直接使用（issue #150），否则按
        # 官方接口拼接 chat/completions 操作路径
        url = self.resolve_request_url(client.base_url, "/chat/completions")
        resp = client._http.post(
            url,
            headers={
                "Authorization": f"Bearer {client.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接 /chat/completions 接口"
                    "路径），请填写含完整路径的地址（如 "
                    "https://api.example.com/v1/chat/completions），"
                    "不能只填域名" if resp.status_code == 404 else "")
            raise VisionModelError(
                f"OpenAI 请求失败: HTTP {resp.status_code} {resp.text[:200]}"
                f"（请求地址: {url}）{hint}")
        data = _parse_json_response(resp, url, "OpenAI")
        try:
            content_out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"OpenAI 响应未包含文本描述: {resp.text[:200]}") from exc
        # content 可能是字符串或分段数组（部分兼容网关返回）
        if isinstance(content_out, str):
            desc = content_out.strip()
        elif isinstance(content_out, list):
            desc = "".join(
                str(p.get("text") or "").strip()
                for p in content_out
                if isinstance(p, dict) and p.get("text")
            ).strip()
        else:
            desc = ""
        if not desc:
            raise VisionModelError(
                f"OpenAI 响应未包含文本描述: {resp.text[:200]}")
        return desc


class CustomVisionProvider(VisionProviderPlugin):
    """自定义识图模型：OpenAI 兼容 chat/completions 接口。

    无默认端点/模型（default_base_url / default_model 均为空），用户自填
    Base URL / API Key / 模型（如硅基流动、DeepSeek-VL、qwen-vl 等网关）。
    自定义 Base URL 作为完整请求地址直接使用（issue #150 语义）；
    未配置 Base URL 时明确报错（无法确定请求地址）。
    """

    name = "custom"
    display_name = "自定义"
    default_base_url = ""
    default_model = ""
    description = ("自定义 OpenAI 兼容视觉模型（chat/completions 接口），"
                   "需自填 Base URL / 模型 / API Key；Base URL 作为完整"
                   "请求地址直接使用（如 https://api.example.com/v1/"
                   "chat/completions）")

    def describe(self, client: Any, image: bytes, *,
                 mime_type: str = "image/png",
                 prompt: str = "") -> str:
        if not client.base_url:
            raise VisionModelError(
                "自定义识图模型未配置 Base URL（请填写完整请求地址，如 "
                "https://api.example.com/v1/chat/completions）")
        text = (prompt or "").strip() or DEFAULT_VISION_PROMPT
        content: list[dict[str, Any]] = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,"
                           f"{base64.b64encode(image).decode('ascii')}",
                },
            },
        ]
        payload = {
            "model": client.model,
            "messages": [{"role": "user", "content": content}],
        }
        url = client.base_url  # 自定义完整端点直接使用（issue #150）
        resp = client._http.post(
            url,
            headers={
                "Authorization": f"Bearer {client.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接 /chat/completions 接口"
                    "路径），请填写含完整路径的地址（如 "
                    "https://api.example.com/v1/chat/completions）"
                    if resp.status_code == 404 else "")
            raise VisionModelError(
                f"自定义识图模型请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）{hint}")
        data = _parse_json_response(resp, url, "自定义识图模型")
        try:
            content_out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"自定义识图模型响应未包含文本描述: {resp.text[:200]}") from exc
        if isinstance(content_out, str):
            desc = content_out.strip()
        elif isinstance(content_out, list):
            desc = "".join(
                str(p.get("text") or "").strip()
                for p in content_out
                if isinstance(p, dict) and p.get("text")
            ).strip()
        else:
            desc = ""
        if not desc:
            raise VisionModelError(
                f"自定义识图模型响应未包含文本描述: {resp.text[:200]}")
        return desc


# 模块导入即注册内置识图供应商插件（注册顺序 = 设置页预设展示顺序）
register_plugin(GeminiVisionProvider())
register_plugin(OpenAIVisionProvider())
register_plugin(CustomVisionProvider())
