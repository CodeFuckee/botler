"""AI 配置辅助 API（issue #499）：设置页「获取模型」按钮的后端代理。

前端无法直连 OpenAI 等云端供应商（浏览器 CORS 拦截），由本机 Botler
服务代为请求 OpenAI 兼容的 ``GET {base_url}/models`` 端点，返回模型 id
列表供用户在设置页选择。API Key 复用设置页保存语义：前端传掩码值或
留空时，按供应商 name 匹配已保存配置的明文 Key（明文不流转前端）。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# 获取模型列表超时：比对话调用（DEFAULT_TIMEOUT=60）短，列表接口普遍
# 较快，避免用户在设置页长时间等待。
MODELS_TIMEOUT = 15.0

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


class ModelListError(RuntimeError):
    """获取模型列表失败：上游非 2xx / 网络异常 / 响应格式不符。

    ``is_network`` 标记网络层异常（连接失败/超时等），路由层映射为
    502；其余（上游业务错误、格式不符）映射为 400。
    """

    def __init__(self, message: str, *, is_network: bool = False) -> None:
        super().__init__(message)
        self.is_network = is_network


class ListModelsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    name: str = ""


def ctx_of(request: Request):
    """从请求中取全局依赖容器（与其它 api 模块一致）。"""
    return request.app.state.ctx


def _make_http_client() -> httpx.Client:
    """创建上游请求客户端（独立函数便于测试注入 MockTransport）。"""
    return httpx.Client(timeout=MODELS_TIMEOUT, verify=True)


def fetch_model_ids(
    *,
    base_url: str,
    api_key: str = "",
    http: httpx.Client | None = None,
) -> list[str]:
    """请求 OpenAI 兼容 ``GET {base_url}/models``，返回模型 id 列表。

    - base_url 已含 ``/models`` 时原样使用，否则自动补拼（与
      ``chat_models._resolve_request_url`` 同语义）；
    - api_key 非空时带 ``Authorization: Bearer <key>``，空则不带（本地
      服务如 Ollama 无鉴权场景）；
    - 解析 ``data[].id`` 提取模型 id，去重保序；
    - 上游非 2xx / 网络异常 / 非 JSON / 缺 data 列表均抛 ModelListError。
    """
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        url += "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = http or _make_http_client()
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("获取模型列表网络异常: %s %s", url, exc)
        raise ModelListError(
            f"获取模型失败: 网络请求异常（{exc.__class__.__name__}），"
            "请检查 Base URL 是否可达", is_network=True) from exc
    if resp.status_code >= 400:
        logger.warning("获取模型列表上游非 2xx: %s HTTP %s %s",
                       url, resp.status_code, resp.text[:200])
        raise ModelListError(
            f"获取模型失败: HTTP {resp.status_code} "
            f"{resp.text[:200]}（请求地址: {url}）")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ModelListError(
            "获取模型失败: 供应商响应不是合法 JSON") from exc
    raw = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ModelListError(
            "获取模型失败: 响应缺少 data 模型列表（不符合 OpenAI 兼容格式）")
    models: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            models.append(mid)
    return models


@router.post("/list-models")
def list_models(req: ListModelsRequest, request: Request) -> dict:
    """通过 OpenAI 兼容接口获取供应商可用模型 id 列表（issue #499）。

    请求体 ``{base_url, api_key, name}``：
    - base_url 必填且以 http(s):// 开头；
    - api_key 留空或为掩码值（含 *）时，按 name 匹配已保存配置的明文
      Key（与设置保存 ``_validate_ai_providers`` 同语义），匹配不到则
      不带认证头（本地无鉴权服务场景）。
    """
    c = ctx_of(request)
    base_url = (req.base_url or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url 必须以 http(s):// 开头")
    api_key = (req.api_key or "").strip()
    if not api_key or "*" in api_key:
        # 掩码 / 留空 = 保持现有：按 name 匹配已保存配置
        api_key = ""
        name = (req.name or "").strip()
        for p in (c.config.get().ai_providers or []):
            if str(p.get("name") or "") == name:
                api_key = str(p.get("api_key") or "")
                break
    try:
        models = fetch_model_ids(base_url=base_url, api_key=api_key)
    except ModelListError as exc:
        raise HTTPException(502 if exc.is_network else 400, str(exc)) from exc
    return {"models": models}
