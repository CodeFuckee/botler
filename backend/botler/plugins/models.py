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
import json
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


def _parse_json_response(
    resp: httpx.Response, url: str, provider: str,
    *,
    show_raw: bool = False,
) -> dict[str, Any]:
    """解析生图接口 JSON 响应（issue #151）。

    网关/代理或自定义 Base URL 指向错误端点时，接口常返回 HTTP 2xx 但
    body 为空或为 HTML 错误页：直接 ``resp.json()`` 抛出的
    ``json.JSONDecodeError``（"Expecting value: line 1 column 1 (char 0)"）
    无法定位问题（设置页只能看到「生图测试失败: Expecting value ...」）。

    ``show_raw=False``（默认，Gemini 等）统一转为带状态码 / Content-Type /
    响应片段 / 请求地址的 :class:`ImageModelError`，用户可据此判断是网关
    拦截还是 Base URL 配错端点。

    ``show_raw=True``（OpenAI，issue #151 后续反馈）：错误信息直接完整
    展示接口原始返回内容（不再截断到 200 字符、不包裹冗长诊断提示），
    用户可直接看到接口到底返回了什么（如网关错误页、纯文本提示）。
    """
    try:
        return resp.json()
    except json.JSONDecodeError as exc:  # 空 body / 非 JSON 内容统一诊断
        raw = (resp.text or "").strip() or "（空响应体）"
        ctype = resp.headers.get("content-type") or "未知"
        if show_raw:  # OpenAI：直接把接口原始返回内容展示给用户
            raise ImageModelError(
                f"{provider} 接口返回内容不是有效 JSON（HTTP "
                f"{resp.status_code}，Content-Type: {ctype}），"
                f"接口原始返回内容如下：\n{raw}"
            ) from exc
        snippet = raw[:200]
        raise ImageModelError(
            f"{provider} 接口返回内容不是有效 JSON（HTTP "
            f"{resp.status_code}，Content-Type: {ctype}）："
            f"{snippet}（请求地址: {url}）。若使用自定义 Base URL "
            "请确认其指向正确的接口端点；若经网关/代理转发请检查网关返回内容"
        ) from exc


def _parse_sse_events(text: str) -> list[dict[str, Any]]:
    """解析 SSE（text/event-stream）响应文本，返回全部 data 事件 JSON 载荷。

    issue #151 用户反馈：配置的生图接口（聚合网关类）真实返回为 SSE 流——
    多行 ``data: {json}`` 事件逐步上报进度（progress/status），最终事件
    ``status: "succeeded"`` 且 ``results[0].url`` 为生成图片地址。解析策略：

    - 每条 ``data:`` 行先单独尝试 JSON 解析（部分网关省略事件间空行，
      逐行即可拆出独立事件）；
    - 单行无法解析时按 SSE 规范累积连续多行 data，以换行拼接后再解析
      （兼容标准 SSE 的多行 JSON 字段）；
    - ``data: [DONE]`` 流结束标记与空 payload 跳过；
    - 解析失败的 data 内容直接丢弃（调用方无事件可解析时会报错展示原始
      内容兜底，用户仍能看到接口到底返回了什么）。
    """
    events: list[dict[str, Any]] = []
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            # 空行 / 其它字段 = 事件结束，清空未拼完的多行缓冲
            if pending:
                _flush_sse_pending(pending, events)
                pending = []
            continue
        payload = stripped[len("data:"):].lstrip()
        if not payload or payload == "[DONE]":
            continue
        if pending:
            # 已有未解析完的多行 JSON：继续累积，能拼出完整 JSON 即落地
            pending.append(payload)
            if _flush_sse_pending(pending, events):
                pending = []
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            pending.append(payload)  # 多行 JSON 事件的起始行
    if pending:
        _flush_sse_pending(pending, events)
    return events


def _flush_sse_pending(pending: list[str],
                       events: list[dict[str, Any]]) -> bool:
    """尝试把累积的多行 data 拼接解析为一个 SSE 事件。

    成功追加到 events 并返回 True；拼接后仍不是有效 JSON 返回 False
    （调用方保留缓冲继续累积后续行）。
    """
    payload = "\n".join(pending).strip()
    if not payload:
        return True
    try:
        events.append(json.loads(payload))
        return True
    except json.JSONDecodeError:
        return False


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
            # 带上实际请求地址便于诊断；404 多为自定义 Base URL 只填域名、
            # 缺少完整路径（自定义 Base URL 原样直用语义，issue #150）
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接接口路径），请填写含完整"
                    "路径的地址" if resp.status_code == 404 else "")
            raise ImageModelError(
                f"Gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）{hint}")
        data = _parse_json_response(resp, url, "Gemini")
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
            # 带上实际请求地址便于诊断；404（page not found）多为自定义
            # Base URL 只填域名、缺少完整路径（issue #150 语义下自定义
            # Base URL 会原样作为请求地址），提示用户补全完整地址
            hint = ("；若为 404 page not found：自定义 Base URL 将作为完整"
                    "请求地址直接使用（不再拼接 /images/generations 等接口"
                    "路径），请填写含完整路径的地址（如 "
                    "https://grsai.dakka.com.cn/v1/draw/completions），"
                    "不能只填域名" if resp.status_code == 404 else "")
            raise ImageModelError(
                f"OpenAI 请求失败: HTTP {resp.status_code} {resp.text[:200]}"
                f"（请求地址: {url}）{hint}")
        # 兼容网关/聚合服务返回 SSE 流式响应（issue #151 用户反馈）：
        # Content-Type 为 text/event-stream 或 body 以 data: 开头即按
        # SSE 解析（部分网关不返回标准 Content-Type，用 body 形态兜底），
        # 成功事件里的 results[].url 下载为图片返回；失败/无结果走诊断错误
        if ("text/event-stream" in (resp.headers.get("content-type") or "").lower()
                or resp.text.lstrip().startswith("data:")):
            return self._generate_from_sse(client, resp, url)
        # OpenAI 非 JSON 响应直接把原始返回内容完整展示给用户（issue #151 后续反馈）
        data = _parse_json_response(resp, url, "OpenAI", show_raw=True)
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

    def _generate_from_sse(self, client: Any, resp: httpx.Response,
                           url: str) -> list[ImageResult]:
        """处理 OpenAI 兼容接口返回的 SSE（text/event-stream）流式响应。

        issue #151 用户反馈：配置的生图接口真实返回为 SSE 流（多行
        ``data: {json}`` 事件逐步上报进度），最终事件 ``status:
        "succeeded"`` 且 ``results[0].url`` 为生成图片地址。解析
        data 事件后下载图片返回；任务失败（status=failed）给出
        failure_reason / error；流未完成或无结果给出可诊断错误。
        """
        events = _parse_sse_events(resp.text)
        if not events:
            raw = (resp.text or "").strip() or "（空响应体）"
            raise ImageModelError(
                f"OpenAI 接口返回内容不是有效 JSON（HTTP {resp.status_code}，"
                f"Content-Type: {resp.headers.get('content-type') or '未知'}），"
                f"接口原始返回内容如下：\n{raw}")
        # 最终状态：向上回溯找最后一个 succeeded 事件（部分网关末尾
        # 可能补发心跳/空事件，取最近一个成功结果）
        final = next(
            (e for e in reversed(events)
             if str(e.get("status") or "").strip().lower() == "succeeded"),
            None)
        if final is not None:
            results = final.get("results") or []
            image_results: list[ImageResult] = []
            for item in results:
                img_url = (str(item.get("url") or "").strip()
                           if isinstance(item, dict) else "")
                if not img_url:
                    continue
                # 用同一 http 客户端下载图片（继承 verify_ssl / 超时配置）；
                # CDN 常做 302 跳转，显式允许跟随
                img_resp = client._http.get(img_url, follow_redirects=True)
                if img_resp.status_code >= 400:
                    raise ImageModelError(
                        f"OpenAI 生图结果图片下载失败: HTTP "
                        f"{img_resp.status_code}（{img_url}）")
                mime = ((img_resp.headers.get("content-type") or "image/png")
                        .split(";")[0].strip() or "image/png")
                image_results.append(ImageResult(mime_type=mime,
                                                 data=img_resp.content))
            if image_results:
                return image_results
        # 失败事件：优先展示任务失败原因（failure_reason / error）
        last_failed = next(
            (e for e in reversed(events)
             if str(e.get("status") or "").strip().lower() == "failed"),
            None)
        if last_failed is not None:
            reason = str(last_failed.get("failure_reason") or "").strip()
            err = str(last_failed.get("error") or "").strip()
            detail = "；".join(x for x in (reason, err) if x) or "未知原因"
            raise ImageModelError(f"OpenAI 生图任务失败: {detail}")
        # 无 succeeded / failed：流未完成（仍 running）或事件缺结果字段
        last_status = str((events[-1].get("status") or "")).strip() or "未知"
        raise ImageModelError(
            f"OpenAI 接口 SSE 流中未找到 succeeded 事件（最终事件 "
            f"status: {last_status}，共 {len(events)} 个事件，请求地址: "
            f"{url}）。若自定义 Base URL 请确认其指向正确的接口端点；"
            "若经网关/代理转发请检查网关返回内容")


# 模块导入即注册内置供应商插件（注册顺序 = 设置页预设展示顺序）
register_plugin(GeminiNanoBananaProvider())
register_plugin(OpenAIGptImageProvider())
