"""识图模型调用接口（issue #152，插件体系 issue #140 同模式）。

设置页「识图模型」卡片配置的视觉理解模型统一调用封装。供应商实现已
插件化：每个 provider 是一个 ``VisionProviderPlugin``（见
``botler.plugins.vision_models``），注册进全局插件注册表后，
``VisionModelClient`` 按 provider 名查插件并委托调用。本期内置三个
供应商插件：

- ``gemini_vision``：Google Gemini（默认模型 ``gemini-2.5-flash``），
  走 ``generateContent`` 接口，图片经 inline_data（base64）输入、
  文本描述输出。
- ``openai_vision``：OpenAI（默认模型 ``gpt-4o``），走
  ``chat/completions`` 接口，图片经 image_url（data URL）输入、
  文本描述输出。
- ``custom``：自定义 OpenAI 兼容视觉模型（chat/completions 接口），
  无默认端点/模型，用户自填 Base URL / 模型 / API Key（如硅基流动、
  DeepSeek-VL、qwen-vl 等网关）；Base URL 作为完整请求地址直接使用。

统一入口 :meth:`VisionModelClient.describe`：图片字节 + 可选描述指令 →
文本描述。配置来自 ``Settings.vision_models``（设置页 / config.yaml），
API Key 支持 ``${ENV}`` 引用（config 加载时已展开为明文），落盘
config.yaml、API 只返回掩码，与 image_models（issue #135）同模式。

新增供应商：实现 ``VisionProviderPlugin.describe`` 并注册（内置模块或
``worker.plugin_paths`` 外部加载），无需改动本模块。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .plugins import (
    VISION_DEFAULT_TIMEOUT,
    VisionModelError,
    PluginKind,
    PluginNotFoundError,
    format_request_info,
    get_plugin,
    list_plugins,
)

logger = logging.getLogger("botler.vision_models")

# 内置预设（issue #152）：由识图供应商插件注册表派生。
# key → 默认 base_url / model（设置页选择预设自动填充，均可修改），
# 与前端 providers.jsx 的 VISION_MODEL_PRESETS 对应。
VISION_MODEL_PRESETS: dict[str, dict[str, str]] = {
    plugin.name: {
        "name": plugin.display_name,
        "base_url": plugin.default_base_url,
        "model": plugin.default_model,
    }
    for plugin in list_plugins(PluginKind.VISION_MODEL_PROVIDER)
}


class VisionModelClient:
    """识图模型统一调用客户端。

    入参对应 Settings.vision_models 列表项（``{name, provider, base_url,
    api_key, model, enabled}``）。``describe()`` 按 provider 查插件注册表
    分发到对应供应商实现。

    ``base_url`` 语义（issue #150，与生图模型一致）：留空 / 等于预设
    默认 → 按官方接口在默认地址后拼接操作路径（``/chat/completions``、
    ``:generateContent``）；自定义（不等于预设默认，如代理网关
    ``https://api.example.com/v1/chat/completions``）→ 作为完整请求
    地址直接使用，不再拼接。issue #321 起 OpenAI 兼容识图供应商
    （openai_vision / custom）对自定义 base_url 增加补拼：未以
    ``/chat/completions`` 结尾时自动补拼操作路径（如阿里云百炼兼容网关
    ``https://.../compatible-mode/v1`` → 实际请求
    ``https://.../compatible-mode/v1/chat/completions``）——此前只填
    API 前缀的配置会直接 POST 到网关根路径，被网关以 400 ``url error``
    拒绝（issue #321 现象）；已含完整路径的地址保持原样直用。自定义
    provider（无默认地址）未配置 Base URL 时由插件明确报错。
    """

    def __init__(
        self,
        *,
        name: str,
        provider: str,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = VISION_DEFAULT_TIMEOUT,
        verify_ssl: bool = True,
        image_store: Any | None = None,
    ) -> None:
        try:
            self._plugin = get_plugin(PluginKind.VISION_MODEL_PROVIDER, provider)
        except PluginNotFoundError as exc:
            raise VisionModelError(
                f"不支持的识图模型类型: {provider}（可选: "
                + ", ".join(VISION_MODEL_PRESETS) + "）") from exc
        preset = VISION_MODEL_PRESETS.get(provider, {})
        self.name = name or preset.get("name") or self._plugin.display_name
        self.provider = provider
        self.base_url = (base_url or self._plugin.default_base_url).rstrip("/")
        self.api_key = api_key
        self.model = model or self._plugin.default_model
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # issue #163：识图图片对象存储（MinIO）。非 None 且供应商支持
        # http URL 时，describe() 先把图片哈希上传 MinIO，识图请求改传
        # http URL；未配置 / 供应商不支持时保持 base64 内联输入。
        self.image_store = image_store
        # 实例级客户端：超时/SSL 统一管理，测试可注入 mock 传输
        self._http = httpx.Client(timeout=timeout, verify=verify_ssl)

    # ---- 统一入口 ----

    def describe(
        self,
        image: bytes,
        *,
        mime_type: str = "image/png",
        prompt: str = "",
    ) -> str:
        """按上传图片调用识图模型，返回图片内容的文本描述。

        :param image: 图片原始字节
        :param mime_type: 图片 MIME 类型（如 image/png / image/jpeg）
        :param prompt: 可选描述指令；留空使用内置默认（"请详细描述这张
            图片的内容"）
        """
        if not image:
            raise VisionModelError("识图模型调用缺少图片（请上传一张图片）")
        if not self.api_key:
            raise VisionModelError(f"识图模型「{self.name}」未配置 API Key")
        # issue #163：MinIO 图片上传模式——图片先计算哈希上传 MinIO，
        # 识图请求传 http(s) URL（OpenAI 兼容 image_url），替代 base64。
        # issue #164：OpenAI 兼容供应商（openai_vision / custom）禁止
        # base64 内联（网关会拒绝 data: URL，如阿里云百炼 qwen 报
        # "url error"）——未启用 MinIO 时明确报错引导配置，不再静默回退；
        # Gemini 官方接口不支持 http URL（仅 base64 inline_data，
        # Google API 限制），保持 base64 内联输入。
        if self.image_store is not None and self._plugin.supports_image_url:
            try:
                image = self.image_store.put_image(image, mime_type=mime_type)
            except Exception as exc:  # noqa: BLE001 上传失败统一转识图错误
                raise VisionModelError(f"图片上传 MinIO 失败: {exc}") from exc
        elif self._plugin.supports_image_url:
            raise VisionModelError(
                f"识图模型「{self.name}」要求图片以 http URL 传入（不再支持"
                "base64 内联，OpenAI 兼容网关会拒绝 data: URL），但未启用"
                "MinIO 图片上传。请在设置页/配置启用 MinIO（minio.enabled"
                " + endpoint + access_key + secret_key + public_base_url），"
                "图片将自动上传至 MinIO 的 public 桶并经 nginx 代理地址"
                "访问")
        elif self.image_store is not None:
            logger.warning(
                "识图模型「%s」（%s）供应商不支持 http 图片 URL（Gemini 官方"
                "接口仅支持 base64 inline_data），本次图片仍以 base64 内联"
                "输入", self.name, self.provider)
        try:
            # 按 provider 插件委托：供应商实现见 botler.plugins.vision_models
            return self._plugin.describe(
                self, image, mime_type=mime_type, prompt=prompt)
        except VisionModelError:
            raise
        except httpx.TimeoutException as exc:
            # issue #156：失败提示带上「后端 POST 给上游 API 的信息」——
            # 请求地址 + 请求头（API Key 掩码）+ 请求体（base64 图片截断）。
            # 超时前拿不到响应体，POST 出去的载荷是唯一可诊断线索；请求头/
            # 请求体由供应商 _post_json 附加到异常对象上
            req = getattr(exc, "request", None)
            hint = format_request_info(
                str(req.url) if req is not None else "",
                getattr(exc, "request_headers", None),
                getattr(exc, "request_payload", None))
            raise VisionModelError(
                f"识图模型「{self.name}」请求超时（>{self.timeout}s）（{hint}）"
            ) from exc
        except httpx.HTTPError as exc:
            # issue #156：网络层失败（连接/DNS/SSL 等）同样带「后端 POST
            # 给上游 API 的信息」（地址 + 请求头 + 请求体）；若异常携带
            # 响应（如 HTTPStatusError）则附接口返回状态码与内容
            req = getattr(exc, "request", None)
            hint = format_request_info(
                str(req.url) if req is not None else "",
                getattr(exc, "request_headers", None),
                getattr(exc, "request_payload", None))
            resp = getattr(exc, "response", None)
            resp_hint = (f"，接口返回: HTTP {resp.status_code} {resp.text[:200]}"
                         if resp is not None else "")
            raise VisionModelError(
                f"识图模型「{self.name}」网络请求失败: {exc}（{hint}）{resp_hint}"
            ) from exc


def find_enabled(models: list[dict] | None, provider: str) -> dict | None:
    """从 Settings.vision_models 列表中找指定 provider 的启用项。"""
    for m in models or []:
        if (isinstance(m, dict)
                and str(m.get("provider", "")).strip() == provider
                and bool(m.get("enabled", True))
                and str(m.get("api_key", "") or "").strip()):
            return m
    return None


def client_from_config(cfg: dict) -> VisionModelClient:
    """按配置项构造客户端（provider 归一、缺省值由客户端补）。"""
    provider = str(cfg.get("provider") or "").strip() or "custom"
    return VisionModelClient(
        name=str(cfg.get("name") or "").strip(),
        provider=provider,
        base_url=str(cfg.get("base_url") or "").strip(),
        api_key=str(cfg.get("api_key") or "").strip(),
        model=str(cfg.get("model") or "").strip(),
    )
