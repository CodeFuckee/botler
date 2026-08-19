"""生图模型调用接口（issue #135，插件化 issue #140）。

设置页「生图模型」卡片配置的图像模型统一调用封装。供应商实现已插件化：
每个 provider 是一个 ``ImageProviderPlugin``（见 ``botler.plugins.models``），
注册进全局插件注册表后，``ImageModelClient`` 按 provider 名查插件并委托
调用。本期内置两个供应商插件：

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

新增供应商：实现 ``ImageProviderPlugin.generate`` 并注册（内置模块或
``worker.plugin_paths`` 外部加载），无需改动本模块。
"""

from __future__ import annotations

import logging

import httpx

from .plugins import (
    DEFAULT_TIMEOUT,
    DEFAULT_SIZE,
    ImageModelError,
    ImageResult,
    PluginKind,
    PluginNotFoundError,
    get_plugin,
    list_plugins,
)

logger = logging.getLogger("botler.image_models")

# 内置预设（issue #135）：由 model_provider 插件注册表派生（issue #140）。
# key → 默认 base_url / model（设置页选择预设自动填充，均可修改），
# 与前端 providers.jsx 的 IMAGE_MODEL_PRESETS 对应。
IMAGE_MODEL_PRESETS: dict[str, dict[str, str]] = {
    plugin.name: {
        "name": plugin.display_name,
        "base_url": plugin.default_base_url,
        "model": plugin.default_model,
    }
    for plugin in list_plugins(PluginKind.MODEL_PROVIDER)
}


class ImageModelClient:
    """生图模型统一调用客户端。

    入参对应 Settings.image_models 列表项（``{name, provider, base_url,
    api_key, model, enabled}``）。``generate()`` 按 provider 查插件注册表
    分发到对应供应商实现。

    ``base_url`` 语义（issue #150）：留空 / 等于预设默认 → 按官方接口在
    默认地址后拼接操作路径（``/images/generations``、``/images/edits``、
    ``:generateContent``）；自定义（不等于预设默认，如代理网关
    ``https://grsai.dakka.com.cn/v1/draw/completions``）→ 作为完整请求
    地址直接使用，不再拼接。
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
        try:
            self._plugin = get_plugin(PluginKind.MODEL_PROVIDER, provider)
        except PluginNotFoundError as exc:
            raise ImageModelError(
                f"不支持的生成模型类型: {provider}（可选: "
                + ", ".join(IMAGE_MODEL_PRESETS) + "）") from exc
        preset = IMAGE_MODEL_PRESETS.get(provider, {})
        self.name = name or preset.get("name") or self._plugin.display_name
        self.provider = provider
        self.base_url = (base_url or self._plugin.default_base_url).rstrip("/")
        self.api_key = api_key
        self.model = model or self._plugin.default_model
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
            raise ImageModelError("生图模型调用缺少 prompt（生成指令不能为空）")
        if not self.api_key:
            raise ImageModelError(f"生图模型「{self.name}」未配置 API Key")
        try:
            # 按 provider 插件委托：供应商实现见 botler.plugins.models
            return self._plugin.generate(
                self, prompt, image,
                mime_type=mime_type, size=size, n=n)
        except ImageModelError:
            raise
        except httpx.TimeoutException as exc:
            raise ImageModelError(
                f"生图模型「{self.name}」请求超时（>{self.timeout}s）") from exc
        except httpx.HTTPError as exc:
            raise ImageModelError(
                f"生图模型「{self.name}」网络请求失败: {exc}") from exc


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
