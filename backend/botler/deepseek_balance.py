"""DeepSeek 账户余额查询（issue #138）。

概览页展示 DeepSeek 账户余额：当设置里配置了 DeepSeek API（dsh 段
api_key / AI API 供应商中 provider=deepseek 且启用的项 / 环境变量
DEEPSEEK_API_KEY）时，后端代调 ``GET https://api.deepseek.com/user/balance``
返回余额信息。API Key 只存在于服务端配置（config.yaml / 环境变量），
明文不流转到前端（与 ai_providers 掩码同安全策略）。

凭据解析优先级与 executor._dsh_credentials（issue #115）保持一致：
dsh 段显式配置 > 设置页「AI 供应商」deepseek 项 > 环境变量。
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("botler.deepseek_balance")

# 默认 DeepSeek 余额接口地址（issue #138 需求示例）
DEFAULT_BALANCE_URL = "https://api.deepseek.com/user/balance"

# 余额查询超时（秒）：余额接口为轻量 GET，10s 足够
BALANCE_TIMEOUT = 10.0

# 环境变量名（与 dsh 引擎 / deepseek-harness SDK 同源，部署机配置）
ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"


class DeepSeekBalanceError(RuntimeError):
    """余额查询失败（未配置 Key / 网络异常 / 非 2xx 响应等）。"""


def resolve_deepseek_credentials(settings) -> tuple[str, str]:
    """解析 DeepSeek API Key / Base URL，返回 (api_key, base_url)。

    优先级（与 executor._dsh_credentials 一致，issue #115/#138）：
    1. dsh 段显式配置（dsh.api_key / dsh.base_url）；
    2. 设置页「AI API 供应商」中 provider=deepseek 且 enabled、api_key
       非空的项（base_url 取该项，dsh 段已配 base_url 时优先 dsh）；
    3. 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL。
    未配置任何 Key 时 api_key 返回空串（调用方据此判定「未配置」）。
    """
    api_key = str(getattr(settings, "dsh_api_key", "") or "").strip()
    base_url = str(getattr(settings, "dsh_base_url", "") or "").strip()
    if not api_key:
        provider = next(
            (p for p in (getattr(settings, "ai_providers", None) or [])
             if isinstance(p, dict)
             and str(p.get("provider", "")).strip() == "deepseek"
             and bool(p.get("enabled", True))
             and str(p.get("api_key", "") or "").strip()),
            None)
        if provider is not None:
            api_key = str(provider.get("api_key", "") or "").strip()
            base_url = base_url or str(provider.get("base_url", "") or "").strip()
    if not api_key:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not base_url:
        base_url = os.environ.get(ENV_BASE_URL, "").strip()
    return api_key, base_url


def balance_url(base_url: str = "") -> str:
    """由配置的 base_url 派生余额接口地址。

    DeepSeek 官方余额接口为 ``https://api.deepseek.com/user/balance``
    （issue #138 需求示例）。预设 base_url 形如
    ``https://api.deepseek.com/v1``——``/v1`` 仅为 OpenAI 兼容前缀，
    余额接口不带该前缀，归一化去掉尾部 ``/v1`` 再拼 ``/user/balance``。
    未配置 base_url 时直接使用官方默认地址。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.deepseek.com"
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return f"{base}/user/balance"


class DeepSeekBalanceClient:
    """DeepSeek 余额查询客户端（服务端代调，Key 不外发）。

    入参为解析后的 api_key / base_url；``fetch()`` 调
    ``GET {balance_url}/user/balance``，返回 DeepSeek 余额响应体
    （``{is_available, balance_infos: [{currency, total_balance,
    granted_balance, topped_up_balance}]}``）。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        timeout: float = BALANCE_TIMEOUT,
        verify_ssl: bool = True,
    ) -> None:
        if not api_key:
            raise DeepSeekBalanceError("未配置 DeepSeek API Key，无法查询余额")
        self.api_key = api_key
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.url = balance_url(base_url)
        # 实例级客户端：超时/SSL 统一管理，测试可注入 mock 传输
        self._http = httpx.Client(timeout=timeout, verify=verify_ssl)

    def fetch(self) -> dict:
        """GET /user/balance，返回 DeepSeek 余额响应体（dict）。"""
        try:
            resp = self._http.get(
                self.url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
        except httpx.TimeoutException as exc:
            raise DeepSeekBalanceError(
                f"DeepSeek 余额查询超时（>{self.timeout}s）") from exc
        except httpx.HTTPError as exc:
            raise DeepSeekBalanceError(
                f"DeepSeek 余额查询网络请求失败: {exc}") from exc
        if resp.status_code >= 400:
            # 只带状态码 + 截断响应体（不回显 Authorization 头；错误响应
            # 不含请求头，但截断防异常响应携带意外内容）
            detail = (resp.text or "").strip()[:200]
            raise DeepSeekBalanceError(
                f"DeepSeek 余额查询失败: HTTP {resp.status_code}"
                + (f" {detail}" if detail else ""))
        return resp.json()
