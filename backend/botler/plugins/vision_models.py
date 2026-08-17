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

诊断约定（issue #156）：设置页「测试」失败时，错误提示统一带上
「后端 POST 给上游 API 的信息」——实际请求地址 + 请求头（API Key 掩码）
+ 请求体（base64 图片数据截断），用户可据此对照网关/供应商确认配置。
网络层异常（超时/连接失败，无响应体可看）由 :func:`_post_json` 把请求
载荷附加到异常对象，交 ``VisionModelClient.describe`` 统一带进错误；
HTTP 层错误 / JSON 解析失败 / 响应缺内容等路径由各供应商与
:func:`format_request_info` 直接拼进错误文案。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from .base import PluginKind, VisionProviderPlugin, register_plugin

logger = logging.getLogger("botler.plugins.vision_models")

# 识图请求默认超时（秒）：图片理解通常比纯文本接口慢，放宽到 60s
DEFAULT_TIMEOUT = 60.0

# 默认描述指令（测试按钮 / 调用方未传 prompt 时使用）
DEFAULT_VISION_PROMPT = "请详细描述这张图片的内容"


def _image_url_value(image: bytes | str, mime_type: str) -> str:
    """构造 OpenAI 兼容 image_url 的 url 字段（issue #163）。

    图片为 http(s) URL 字符串（MinIO 上传模式）时原样使用——识图请求
    传 http URL 而非 base64 data URL；为字节时回退 data URL（base64）
    内联输入（未配置 MinIO 的部署保持原行为）。
    """
    if isinstance(image, str):
        return image
    return (f"data:{mime_type};base64,"
            f"{base64.b64encode(image).decode('ascii')}")

# ---- 请求信息脱敏 / 摘要（issue #156） ----
# 错误提示要展示「后端 POST 给上游 API 的信息」，但请求体里包含 base64
# 图片数据（可长达数十万字符）与认证头里的 API Key：展示前必须脱敏，
# 否则错误提示会被图片数据刷屏、并泄漏密钥。
_REQUEST_BODY_MAX_FIELD = 200   # 请求体字段超长阈值：超过即截断
_REQUEST_BODY_PREVIEW = 80      # 截断后保留的前缀长度
_SENSITIVE_KEY_PARTS = ("key", "token", "secret", "password")
_MASKED_HEADER_KEYS = {"authorization", "x-goog-api-key", "api-key",
                       "proxy-authorization"}


def _mask_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """请求头脱敏：认证类头的密钥值掩码展示。

    Authorization 保留 ``Bearer `` 前缀便于用户区分认证方式（如 401 时
    确认是否走了 Bearer 认证），密钥部分统一掩码；其余请求头原样展示。
    """
    masked: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _MASKED_HEADER_KEYS:
            if key.lower() == "authorization" and value.startswith("Bearer "):
                masked[key] = "Bearer ***（已掩码）"
            else:
                masked[key] = "***（已掩码）"
        else:
            masked[key] = value
    return masked


def _mask_request_body(payload: Any) -> Any:
    """请求体脱敏（递归）：超长字符串（典型为 base64 图片数据）截断展示；
    key / token / secret / password 类字段整体掩码；其余原样保留。"""
    if isinstance(payload, dict):
        return {
            key: ("***（已掩码）"
                  if (isinstance(value, str)
                      and any(p in key.lower() for p in _SENSITIVE_KEY_PARTS))
                  else _mask_request_body(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_mask_request_body(item) for item in payload]
    if isinstance(payload, str):
        if len(payload) > _REQUEST_BODY_MAX_FIELD:
            return (f"{payload[:_REQUEST_BODY_PREVIEW]}…"
                    f"（已截断，共 {len(payload)} 字符）")
        return payload
    return payload


def format_request_info(
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """把「后端 POST 给上游 API 的信息」格式化为诊断摘要（issue #156）。

    设置页「测试识图模型」失败时，错误提示带上实际发给 AI 供应商的
    POST 请求信息：请求地址 + 请求头（API Key 掩码）+ 请求体（base64
    图片数据截断），便于用户对照网关/供应商返回确认配置是否正确。

    :param url: 实际请求地址（含拼接/自定义路径）
    :param headers: 请求头（Authorization / X-goog-api-key 等掩码）
    :param payload: 请求体（超长字符串如 base64 图片截断展示）
    :return: "请求地址: ...，请求头: {...}，请求体: {...}" 形式的摘要
    """
    parts = [f"请求地址: {url}"]
    if headers:
        parts.append("请求头: " + json.dumps(
            _mask_request_headers(dict(headers)), ensure_ascii=False))
    if payload is not None:
        parts.append("请求体: " + json.dumps(
            _mask_request_body(payload), ensure_ascii=False))
    return "，".join(parts)


def _post_json(
    client: Any,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    """向识图接口发起 POST JSON 请求（issue #156 统一入口）。

    网络层失败（超时 / 连接失败 / SSL 等，此时拿不到响应体）时把本次
    请求的请求体与请求头附加到异常对象上，由上层
    ``VisionModelClient.describe`` 统一带进错误提示——POST 出去的载荷
    是唯一可诊断线索。HTTP 非 2xx 响应不在此抛异常（供应商自行拼错误）。
    """
    try:
        return client._http.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        # 附加属性供上层读取（httpx 异常对象允许挂载自定义属性）
        exc.request_payload = payload  # type: ignore[attr-defined]
        exc.request_headers = headers  # type: ignore[attr-defined]
        raise


class VisionModelError(RuntimeError):
    """识图模型调用失败（缺 key / 缺图片 / 缺 Base URL / 网络异常 /
    非 2xx 响应 / 响应无文本等）。"""


def _parse_json_response(
    resp: httpx.Response,
    url: str,
    provider: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析识图接口 JSON 响应（复用生图 issue #151 的诊断语义）。

    网关/代理或自定义 Base URL 指向错误端点时，接口常返回 HTTP 2xx 但
    body 为空或为 HTML 错误页：直接 ``resp.json()`` 抛出的
    ``json.JSONDecodeError`` 无法定位问题。这里统一转为带状态码 /
    Content-Type / 响应片段 / POST 请求信息（issue #156）的
    :class:`VisionModelError`。
    """
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 JSON 解析失败统一诊断
        raw = (resp.text or "").strip() or "（空响应体）"
        ctype = resp.headers.get("content-type") or "未知"
        raise VisionModelError(
            f"{provider} 接口返回内容不是有效 JSON（HTTP "
            f"{resp.status_code}，Content-Type: {ctype}）："
            f"{raw[:200]}（{format_request_info(url, headers, payload)}）。"
            "若使用自定义 Base URL 请确认其指向正确的接口端点；若经"
            "网关/代理转发请检查网关返回内容"
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
    # issue #163：Gemini 官方 generateContent 接口只接受 inline_data
    # （base64）与 file_data（Files API / GCS），不支持任意 http(s) 公网
    # URL 图片输入 → 不参与 MinIO URL 上传模式，图片仍以 base64 内联
    supports_image_url: bool = False

    def describe(self, client: Any, image: bytes, *,
                 mime_type: str = "image/png",
                 prompt: str = "") -> str:
        if isinstance(image, str):
            # issue #163：官方接口不支持 http URL 图片输入，明确报错
            # 而不是把 URL 当字节 base64 编码
            raise VisionModelError(
                "Gemini 官方 generateContent 接口不支持 http 图片 URL 输入"
                "（仅支持 base64 inline_data / file_data），请改用 OpenAI "
                "兼容识图模型（openai_vision / custom）或关闭 MinIO 图片上传")
        parts: list[dict[str, Any]] = [{
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(image).decode("ascii"),
            },
        }]
        text = (prompt or "").strip() or DEFAULT_VISION_PROMPT
        parts.append({"text": text})
        payload = {"contents": [{"parts": parts}]}
        headers = {
            "X-goog-api-key": client.api_key,
            "Content-Type": "application/json",
        }
        # 自定义 base_url 视为完整端点直接使用（issue #150），否则按
        # 官方接口拼接 generateContent 操作路径
        url = self.resolve_request_url(
            client.base_url, f"/models/{client.model}:generateContent")
        resp = _post_json(client, url, headers, payload)
        if resp.status_code >= 400:
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接接口路径），请填写含完整"
                    "路径的地址" if resp.status_code == 404 else "")
            raise VisionModelError(
                f"Gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（"
                f"{format_request_info(url, headers, payload)}）{hint}")
        data = _parse_json_response(resp, url, "Gemini", headers, payload)
        try:
            parts_out = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"Gemini 响应缺少内容: {resp.text[:200]}（"
                f"{format_request_info(url, headers, payload)}）") from exc
        texts = [str(p.get("text") or "").strip()
                 for p in parts_out if isinstance(p, dict) and p.get("text")]
        desc = "".join(t for t in texts if t)
        if not desc:
            raise VisionModelError(
                f"Gemini 响应未包含文本描述: {resp.text[:200]}（"
                f"{format_request_info(url, headers, payload)}）")
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
                "image_url": {"url": _image_url_value(image, mime_type)},
            },
        ]
        payload = {
            "model": client.model,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
        }
        # 自定义 base_url 视为完整端点直接使用（issue #150），否则按
        # 官方接口拼接 chat/completions 操作路径
        url = self.resolve_request_url(client.base_url, "/chat/completions")
        resp = _post_json(client, url, headers, payload)
        if resp.status_code >= 400:
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接 /chat/completions 接口"
                    "路径），请填写含完整路径的地址（如 "
                    "https://api.example.com/v1/chat/completions），"
                    "不能只填域名" if resp.status_code == 404 else "")
            raise VisionModelError(
                f"OpenAI 请求失败: HTTP {resp.status_code} {resp.text[:200]}"
                f"（{format_request_info(url, headers, payload)}）{hint}")
        data = _parse_json_response(resp, url, "OpenAI", headers, payload)
        try:
            content_out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"OpenAI 响应未包含文本描述: {resp.text[:200]}"
                f"（{format_request_info(url, headers, payload)}）") from exc
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
                f"OpenAI 响应未包含文本描述: {resp.text[:200]}"
                f"（{format_request_info(url, headers, payload)}）")
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
                "image_url": {"url": _image_url_value(image, mime_type)},
            },
        ]
        payload = {
            "model": client.model,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
        }
        url = client.base_url  # 自定义完整端点直接使用（issue #150）
        resp = _post_json(client, url, headers, payload)
        if resp.status_code >= 400:
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接 /chat/completions 接口"
                    "路径），请填写含完整路径的地址（如 "
                    "https://api.example.com/v1/chat/completions）"
                    if resp.status_code == 404 else "")
            raise VisionModelError(
                f"自定义识图模型请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（"
                f"{format_request_info(url, headers, payload)}）{hint}")
        data = _parse_json_response(resp, url, "自定义识图模型",
                                    headers, payload)
        try:
            content_out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionModelError(
                f"自定义识图模型响应未包含文本描述: {resp.text[:200]}"
                f"（{format_request_info(url, headers, payload)}）") from exc
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
                f"自定义识图模型响应未包含文本描述: {resp.text[:200]}"
                f"（{format_request_info(url, headers, payload)}）")
        return desc


# 模块导入即注册内置识图供应商插件（注册顺序 = 设置页预设展示顺序）
register_plugin(GeminiVisionProvider())
register_plugin(OpenAIVisionProvider())
register_plugin(CustomVisionProvider())
