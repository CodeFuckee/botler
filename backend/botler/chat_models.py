"""灵感 AI 对话模型调用封装（issue #166）。

概览页灵感板块「与 AI 对话」功能：用户围绕某条灵感与 AI agent 探讨，
后端复用设置页「AI API 供应商」（ai_providers，issue #46）配置的文本
对话模型完成回复。供应商支持三种协议：

- OpenAI 兼容 ``chat/completions``：deepseek / openai / moonshot /
  qwen / zhipu / siliconflow / ollama / openrouter / custom（与前端
  providers.jsx 的 AI_PROVIDER_PRESETS 对应）；
- Google Gemini ``generateContent``：gemini；
- Anthropic ``messages``：anthropic。

统一入口 :meth:`ChatModelClient.chat`：传入 OpenAI 兼容消息列表
（``[{"role": "system"|"user"|"assistant", "content": "..."}]``），按
provider 分发到对应协议实现，返回模型文本回复。

``base_url`` 语义（与生图/识图模型一致，issue #150）：留空 / 等于预设
默认 → 按官方接口在默认地址后拼接操作路径（``/chat/completions``、
``/models/{model}:generateContent``、``/messages``）；自定义地址如果已
包含操作路径则原样使用，否则按协议自动补拼操作路径（例如
``https://api.example.com/v1`` → ``.../v1/chat/completions``）。custom
无默认地址，必须自填 Base URL（由 :meth:`ChatModelClient` 构造时明确报错）。

错误统一抛 :class:`ChatModelError`：缺 API Key / 缺 Base URL / 不支持的
provider / 网络异常 / 非 2xx 响应 / 响应无文本等。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("botler.chat_models")

# 文本对话请求默认超时（秒）：对话接口通常 30s 内返回，放宽到 60s
# 应对慢网关 / 长回复场景
DEFAULT_TIMEOUT = 60.0

# 各 provider 默认接口地址 / 模型（与前端 providers.jsx 预设一致，
# 配置留空时兜底使用；custom 无默认地址，必须自填）
DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "ollama": "http://localhost:11434/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "custom": "",
}
DEFAULT_MODELS: dict[str, str] = {
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-pro",
    "moonshot": "moonshot-v1-8k",
    "qwen": "qwen-max",
    "zhipu": "glm-4-plus",
    "siliconflow": "deepseek-ai/DeepSeek-V3",
    "ollama": "llama3.1",
    "openrouter": "openai/gpt-4o",
    "custom": "",
}

# OpenAI 兼容 chat/completions 协议的 provider（其余 gemini / anthropic
# 走各自官方协议）
OPENAI_COMPAT_PROVIDERS = {
    "deepseek", "openai", "moonshot", "qwen", "zhipu",
    "siliconflow", "ollama", "openrouter", "custom",
}

# Anthropic messages 接口默认最大输出 token（非流式对话合理上限）
ANTHROPIC_MAX_TOKENS = 1024

# 支持供应商清单展示用（错误提示列出可选值）
SUPPORTED_PROVIDERS = sorted(DEFAULT_BASE_URLS)

# AI 供应商默认优先级（issue #495）：与仓库调度优先级 repos[].priority
# （issue #51）同语义——1~999 整数，数字越小优先级越高，缺省 100；
# 历史配置无 priority 字段时按 100 参与排序（全部同值 → 保持列表顺序，
# 与旧版「取第一个启用项」行为完全兼容）。
DEFAULT_PRIORITY = 100


class ChatModelError(RuntimeError):
    """AI 对话模型调用失败（缺 key / 缺 Base URL / 不支持的 provider /
    网络异常 / 非 2xx 响应 / 响应无文本等）。"""


def _resolve_request_url(base_url: str, default_base_url: str,
                        api_path: str, *, append_if_missing: bool = False) -> str:
    """解析对话请求地址（issue #150 / #413 语义）。

    自定义 base_url（非空且不等于预设默认）如果已经包含操作路径，
    则原样使用；启用 ``append_if_missing`` 时，缺少操作路径的 API
    前缀自动补拼，兼容只填写 ``https://.../v1`` 的自定义网关。
    未配置 / 等于预设默认时，在默认地址后拼接操作路径。
    """
    if base_url and base_url != default_base_url:
        if append_if_missing and not base_url.rstrip("/").endswith(api_path):
            return f"{base_url.rstrip('/')}{api_path}"
        return base_url
    return f"{default_base_url}{api_path}"


def _parse_json_response(resp: httpx.Response, url: str,
                         provider: str) -> dict[str, Any]:
    """解析对话接口 JSON 响应。

    网关/代理或自定义 Base URL 指向错误端点时，接口常返回 HTTP 2xx 但
    body 为空或为 HTML 错误页：统一转为带状态码 / Content-Type / 响应
    片段 / 请求地址的 :class:`ChatModelError`，便于用户对照网关返回
    确认配置是否正确（与生图/识图模型 issue #151 同诊断语义）。
    """
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 JSON 解析失败统一诊断
        raw = (resp.text or "").strip() or "（空响应体）"
        ctype = resp.headers.get("content-type") or "未知"
        raise ChatModelError(
            f"{provider} 接口返回内容不是有效 JSON（HTTP "
            f"{resp.status_code}，Content-Type: {ctype}）："
            f"{raw[:200]}（请求地址: {url}）。若使用自定义 Base URL "
            "请确认其指向正确的接口端点；若经网关/代理转发请检查网关返回内容"
        ) from exc


def _post_json(client: Any, url: str, headers: dict[str, str],
               payload: dict[str, Any]) -> httpx.Response:
    """向对话接口发起 POST JSON 请求，网络层异常统一转 ChatModelError。"""
    try:
        return client._http.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise ChatModelError(
            f"请求 AI 接口失败: {exc.__class__.__name__}（请求地址: {url}）"
        ) from exc


def _extract_text(content: Any) -> str:
    """归一化 OpenAI 兼容 content 字段为纯文本。

    新版 OpenAI 兼容接口 content 可能是字符串或部分列表
    （[{"type": "text", "text": "..."}]），统一拼接 text 部分。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content or "")


class ChatModelClient:
    """AI 对话模型统一调用客户端（issue #166）。

    入参对应 Settings.ai_providers 列表项（``{name, provider, base_url,
    api_key, model, enabled}``）。``chat(messages)`` 按 provider 分发：
    OpenAI 兼容走 ``chat/completions``，gemini 走 ``generateContent``，
    anthropic 走 ``messages``。
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
        provider = (provider or "").strip()
        if provider not in DEFAULT_BASE_URLS:
            raise ChatModelError(
                f"不支持的 AI 对话模型类型: {provider or '（空）'}（可选: "
                + ", ".join(SUPPORTED_PROVIDERS) + "）")
        default_base = DEFAULT_BASE_URLS[provider]
        if not base_url and not default_base:
            raise ChatModelError(
                f"AI 供应商「{name}」未配置 Base URL（{provider} 无默认"
                "地址，请在设置页填写完整接口地址）")
        self.name = name or provider
        self.provider = provider
        self.base_url = (base_url or default_base).rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS[provider]
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # 实例级客户端：超时/SSL 统一管理，测试可注入 mock 传输
        self._http = httpx.Client(timeout=timeout, verify=verify_ssl)

    # ---- 统一入口 ----

    def chat(self, messages: list[dict[str, str]]) -> str:
        """按消息列表调用对话模型，返回文本回复。

        :param messages: OpenAI 兼容消息列表，每项 ``{"role":
            "system"|"user"|"assistant", "content": "..."}``；system 消息
            由供应商实现按协议转换（gemini / anthropic 无 system 角色）。
        """
        if not self.api_key:
            raise ChatModelError(f"AI 供应商「{self.name}」未配置 API Key")
        if self.provider == "gemini":
            return self._chat_gemini(messages)
        if self.provider == "anthropic":
            return self._chat_anthropic(messages)
        return self._chat_openai_compat(messages)

    # ---- OpenAI 兼容 chat/completions ----

    def _chat_openai_compat(self, messages: list[dict[str, str]]) -> str:
        payload = {"model": self.model, "messages": messages}
        url = _resolve_request_url(
            self.base_url, DEFAULT_BASE_URLS[self.provider],
            "/chat/completions", append_if_missing=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = _post_json(self, url, headers, payload)
        if resp.status_code >= 400:
            raise ChatModelError(
                f"{self.provider} 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）")
        data = _parse_json_response(resp, url, self.provider)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatModelError(
                f"{self.provider} 响应缺少消息内容: {resp.text[:200]}"
                f"（请求地址: {url}）") from exc
        text = _extract_text(content).strip()
        if not text:
            raise ChatModelError(
                f"{self.provider} 响应未包含文本内容: {resp.text[:200]}"
                f"（请求地址: {url}）")
        return text

    # ---- Google Gemini generateContent ----

    def _chat_gemini(self, messages: list[dict[str, str]]) -> str:
        # Gemini 无 system 角色：system 消息合并进首条 user 消息；
        # assistant 角色映射为 model。同一角色连续出现会 400，防御性
        # 在连续相同角色间插入空 user 消息（历史正常成对，理论不触发）。
        system_texts: list[str] = []
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "")
            if role == "system":
                system_texts.append(content)
                continue
            gemini_role = "model" if role == "assistant" else "user"
            if contents and contents[-1]["role"] == gemini_role:
                contents.append({"role": "user", "parts": [{"text": ""}]})
            contents.append({"role": gemini_role,
                             "parts": [{"text": content}]})
        if system_texts:
            system_prompt = "\n\n".join(t for t in system_texts if t.strip())
            if system_prompt:
                if contents and contents[0]["role"] == "model":
                    contents.insert(0, {"role": "user", "parts": [{"text": ""}]})
                contents.insert(0, {"role": "user",
                                    "parts": [{"text": system_prompt}]})
        payload = {"contents": contents}
        url = _resolve_request_url(
            self.base_url, DEFAULT_BASE_URLS["gemini"],
            f"/models/{self.model}:generateContent")
        headers = {
            "X-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        resp = _post_json(self, url, headers, payload)
        if resp.status_code >= 400:
            raise ChatModelError(
                f"gemini 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）")
        data = _parse_json_response(resp, url, "gemini")
        try:
            parts_out = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatModelError(
                f"gemini 响应缺少内容: {resp.text[:200]}"
                f"（请求地址: {url}）") from exc
        texts = [str(p.get("text") or "").strip()
                 for p in parts_out if isinstance(p, dict) and p.get("text")]
        reply = "".join(t for t in texts if t)
        if not reply:
            raise ChatModelError(
                f"gemini 响应未包含文本内容: {resp.text[:200]}"
                f"（请求地址: {url}）")
        return reply

    # ---- Anthropic messages ----

    def _chat_anthropic(self, messages: list[dict[str, str]]) -> str:
        # Anthropic 有 system 顶层字段：system 消息抽出，其余按角色直传
        # （assistant / user）；max_tokens 必填。
        system_texts = [str(m.get("content") or "")
                        for m in messages
                        if str(m.get("role") or "") == "system"]
        api_messages = [
            {"role": str(m["role"]), "content": str(m["content"])}
            for m in messages if str(m.get("role") or "") != "system"
        ]
        if not api_messages:
            api_messages = [{"role": "user", "content": "（空消息）"}]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
        }
        system_prompt = "\n\n".join(t for t in system_texts if t.strip())
        if system_prompt:
            payload["system"] = system_prompt
        url = _resolve_request_url(
            self.base_url, DEFAULT_BASE_URLS["anthropic"], "/messages")
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = _post_json(self, url, headers, payload)
        if resp.status_code >= 400:
            raise ChatModelError(
                f"anthropic 请求失败: HTTP {resp.status_code} "
                f"{resp.text[:200]}（请求地址: {url}）")
        data = _parse_json_response(resp, url, "anthropic")
        items = data.get("content") or []
        texts = [str(i.get("text") or "").strip()
                 for i in items if isinstance(i, dict)
                 and i.get("type") == "text"]
        reply = "".join(t for t in texts if t)
        if not reply:
            raise ChatModelError(
                f"anthropic 响应未包含文本内容: {resp.text[:200]}"
                f"（请求地址: {url}）")
        return reply


def _provider_priority(p: dict) -> int:
    """解析供应商 priority（issue #495）：缺省 DEFAULT_PRIORITY。

    历史配置无 priority 字段 / 值为 None 或非法时按默认 100 参与排序；
    校验层（_validate_ai_providers）已保证合法值，此处仅防御性兜底。
    """
    try:
        return int(p.get("priority", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def sorted_chat_providers(settings) -> list[dict]:
    """返回启用且有 API Key 的 AI 对话供应商，按优先级升序（issue #495）。

    过滤 enabled 且 api_key 非空的项；排序键为 priority（数字小优先级
    高，与 repos[].priority 同语义，issue #51），同优先级保持列表原有
    顺序（Python sorted 稳定排序）——历史配置全部默认 100 时行为与旧版
    「取第一个启用项」完全一致。
    """
    providers = [
        p for p in (getattr(settings, "ai_providers", None) or [])
        if (isinstance(p, dict)
            and bool(p.get("enabled", True))
            and str(p.get("api_key") or "").strip())
    ]
    return sorted(providers, key=_provider_priority)


def resolve_chat_provider(settings) -> dict | None:
    """解析灵感对话使用的 AI 供应商配置（issue #166 / #495）。

    复用设置页「AI API 供应商」列表（ai_providers，issue #46）：取
    优先级最高（priority 数字小，缺省 100）且 enabled、api_key 非空
    的项作为灵感对话模型。用户可通过调整优先级 / 启用开关选择灵感
    对话使用的模型；未配置返回 None（调用方报 400 引导设置页配置）。
    """
    providers = sorted_chat_providers(settings)
    return providers[0] if providers else None


def chat_with_fallback(
    providers: list[dict],
    messages: list[dict[str, str]],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    verify_ssl: bool = True,
    client_factory=None,
) -> tuple[str, dict]:
    """按优先级顺序调用 AI 对话供应商，失败自动切换下一个（issue #495）。

    需求语义：优先使用启用的高优先级供应商；当该供应商没有额度
    （余额不足 402 / 限流 429 / Key 无效 / 网络异常等任何调用失败）后，
    自动切换到下一个启用的低优先级供应商，直到成功或全部失败。

    :param providers: 已按优先级排序的候选供应商配置列表
        （由 :func:`sorted_chat_providers` 产出；显式指定供应商的场景
        可传单元素列表，失败即抛错保持「硬选择」语义）。
    :param messages: OpenAI 兼容消息列表，透传给 ChatModelClient.chat。
    :param client_factory: 测试注入用——自定义 ChatModelClient 构造器
        （接收 provider_cfg 返回客户端实例）；None 时按默认参数构造。
    :return: ``(reply, provider_cfg)``——第一个调用成功的供应商回复与其
        配置（调用方可用 provider 名提示用户实际由哪个供应商作答）。
    :raises ChatModelError: 无候选 / 全部供应商调用失败（错误信息逐条
        列出每个供应商的失败原因，便于用户对照配置诊断）。
    """
    if not providers:
        raise ChatModelError(
            "未配置可用的 AI 对话供应商：请先在设置页「AI API 供应商」"
            "添加并启用至少一个供应商（需填写 API Key）")
    errors: list[str] = []
    for cfg in providers:
        name = str(cfg.get("name") or cfg.get("provider") or "AI 供应商")
        try:
            if client_factory is not None:
                client = client_factory(cfg)
            else:
                client = ChatModelClient(
                    name=name,
                    provider=str(cfg.get("provider") or "custom").strip(),
                    base_url=str(cfg.get("base_url") or "").strip(),
                    api_key=str(cfg.get("api_key") or "").strip(),
                    model=str(cfg.get("model") or "").strip(),
                    timeout=timeout,
                    verify_ssl=verify_ssl)
            reply = client.chat(messages)
        except ChatModelError as e:
            errors.append(f"「{name}」: {e}")
            continue
        # 空回复属于「内容问题」而非「额度不足」：不触发切换，由调用方
        # 按业务语义自行检查（如「AI 回复为空」「提示词为空」）
        return (reply or "").strip(), cfg
    if len(providers) == 1:
        # 单供应商（显式指定 / 唯一候选）：直接透传原始错误（不带
        # 「所有供应商」包装），保持旧版错误消息语义
        raise ChatModelError(errors[0] if errors else "AI 调用失败")
    raise ChatModelError(
        "所有 AI 供应商调用失败：" + "；".join(errors))
