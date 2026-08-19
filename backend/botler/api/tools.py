"""MCP 工具管理 API（issue #172）：工具页面的后端接口。

- ``GET /api/tools``：全部工具 + 内置市场清单 + 已保存的远端市场索引地址；
- ``POST /api/tools``：创建自定义工具（body: 工具定义字段）；
- ``PUT /api/tools/{id}``：更新工具（任意定义字段 / enabled）；
- ``DELETE /api/tools/{id}``：删除工具；
- ``POST /api/tools/install``：安装内置市场工具（body: {name}）；
- ``POST /api/tools/import``：从 URL 导入（body: {url}，Git 仓库或 JSON 文件）；
- ``POST /api/tools/market-index``：拉取远端市场索引（body: {url}），
  返回候选清单并保存该地址供列表页展示。

安全约束（见 tools.validate_tool_def / import_from_url）：工具名仅字母
数字_-、stdio 必须 command、sse/http 必须 http(s) url、args/env 类型
校验、URL 仅 http/https、下载 ≤1MB。工具删除/启停只影响注入配置
（.mcp.json），不触碰仓库代码。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import tools

router = APIRouter(prefix="/tools", tags=["tools"])


def _bad(exc: ValueError) -> HTTPException:
    """ValueError → HTTP 400（业务校验错误统一提示）。"""
    return HTTPException(400, str(exc))


def _missing() -> HTTPException:
    """工具不存在 → HTTP 404。"""
    return HTTPException(404, "工具不存在")


class ToolPatch(BaseModel):
    """工具创建/更新请求体：全部字段可选（创建时按需必填校验）。"""

    name: str | None = None
    description: str | None = None
    kind: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    enabled: bool | None = None
    source: str | None = None


class NameBody(BaseModel):
    """内置市场安装请求体。"""

    name: str


class UrlBody(BaseModel):
    """URL 导入 / 市场索引请求体。"""

    url: str


@router.get("")
def list_tools_api(request: Request):
    """工具列表 + 内置市场 + 已保存的市场索引地址。"""
    c = request.app.state.ctx
    return {
        "tools": tools.list_tools(c.db),
        "market": tools.market_tools(),
        "market_index_url": tools.get_market_index_url(c.db),
    }


@router.post("")
def create_tool_api(request: Request, body: ToolPatch):
    """创建工具（source 缺省 custom；远端市场候选安装可传 market）。"""
    c = request.app.state.ctx
    payload = body.model_dump(exclude_none=True)
    source = payload.pop("source", None) or tools.SOURCE_CUSTOM
    try:
        return tools.create_tool(c.db, payload, source=source)
    except ValueError as exc:
        raise _bad(exc) from None


@router.put("/{tool_id}")
def update_tool_api(request: Request, tool_id: int, body: ToolPatch):
    """更新工具（部分字段；enabled 也可在此更新）。"""
    c = request.app.state.ctx
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(400, "没有可更新的字段")
    try:
        return tools.update_tool(c.db, tool_id, patch)
    except ValueError as exc:
        # 区分「工具不存在」（404）与「校验失败」（400）
        if str(exc) == "工具不存在":
            raise _missing() from None
        raise _bad(exc) from None


@router.delete("/{tool_id}")
def delete_tool_api(request: Request, tool_id: int):
    """删除工具；不存在 404。"""
    c = request.app.state.ctx
    if not tools.delete_tool(c.db, tool_id):
        raise _missing()
    return {"ok": True, "id": tool_id}


@router.post("/install")
def install_builtin_api(request: Request, body: NameBody):
    """安装内置市场工具。"""
    c = request.app.state.ctx
    try:
        return tools.install_builtin(c.db, body.name.strip())
    except ValueError as exc:
        raise _bad(exc) from None


@router.post("/import")
def import_tools_api(request: Request, body: UrlBody):
    """从 URL 导入工具（Git 仓库 / JSON 定义文件），返回导入的工具列表。"""
    c = request.app.state.ctx
    try:
        imported = tools.import_from_url(c.db, body.url)
    except ValueError as exc:
        raise _bad(exc) from None
    return {"imported": imported, "count": len(imported)}


@router.post("/market-index")
def market_index_api(request: Request, body: UrlBody):
    """拉取远端市场索引，返回候选清单并保存索引地址。"""
    c = request.app.state.ctx
    try:
        candidates = tools.fetch_market_index(body.url)
    except ValueError as exc:
        raise _bad(exc) from None
    tools.save_market_index_url(c.db, body.url)
    return {"candidates": candidates, "count": len(candidates),
            "market_index_url": tools.get_market_index_url(c.db)}
