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
``/models/{model}:generateContent``、``/messages``）；自定义（不等于
预设默认，如代理网关 ``https://api.example.com/v1/chat/completions``）
→ 作为完整请求地址直接使用，不再拼接。custom 无默认地址，必须自填
Base URL（由 :meth:`ChatModelClient` 构造时明确报错）。

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


class ChatModelError(RuntimeError):
    """AI 对话模型调用失败（缺 key / 缺 Base URL / 不支持的 provider /
    网络异常 / 非 2xx 响应 / 响应无文本等）。"""


def _resolve_request_url(base_url: str, default_base_url: str,
                        api_path: str) -> str:
    """解析对话请求地址（issue #150 语义）。

    自定义 base_url（非空且不等于预设默认）→ 作为完整请求地址直接
    使用，不再拼接操作路径；否则在默认地址后拼接操作路径。
    """
    if base_url and base_url != default_base_url:
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
            self.base_url, DEFAULT_BASE_URLS[self.provider], "/chat/completions")
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


def resolve_chat_provider(settings) -> dict | None:
    """解析灵感对话使用的 AI 供应商配置（issue #166）。

    复用设置页「AI API 供应商」列表（ai_providers，issue #46）：取
    第一个 enabled 且 api_key 非空的项作为灵感对话模型。用户可通过
    调整列表顺序 / 启用开关选择灵感对话使用的模型；未配置返回 None
    （调用方报 400 引导设置页配置）。
    """
    for p in getattr(settings, "ai_providers", None) or []:
        if (isinstance(p, dict)
                and bool(p.get("enabled", True))
                and str(p.get("api_key") or "").strip()):
            return p
    return None
