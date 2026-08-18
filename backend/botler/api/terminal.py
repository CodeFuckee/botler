"""Web 终端 API（issue #183）：token 签发 + 反向代理到独立终端服务进程。

设计（对应 issue 方案）：
- 主后端只负责「认证」：POST /api/terminal/token 用会话密钥签发短时效
  token（SSO 启用时需登录，SsoGuardMiddleware 已保护 /api/*）；
- 终端能力由**独立终端服务进程**提供（Tornado + terminado，见
  backend/terminal_service.py），默认只监听 127.0.0.1:8765（安全隔离，
  不对外暴露端口），对外统一经本路由反向代理：
    /api/terminal/ws/<name>  →  ws://<upstream>/terminal/ws/<name>
    /api/terminal/health     →  http://<upstream>/terminal/health
  这样浏览器始终与 Botler 主后端同源（Cookie/会话语义一致），也满足
  「Nginx 统一入口，代理到独立终端服务进程」的部署形态（部署方也可把
  nginx 直接指向终端服务进程，见 deploy/nginx-terminal.conf）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from ..auth import create_terminal_token

logger = logging.getLogger("botler.api.terminal")

router = APIRouter(prefix="/terminal", tags=["terminal"])

# 终端服务进程上游地址（环境变量可覆盖；docker 部署为 http://terminal:8765）
_TERM_UPSTREAM_DEFAULT = "http://127.0.0.1:8765"
# token 有效期（秒），与 auth.create_terminal_token 默认值同源
_TERM_TOKEN_TTL = int(os.environ.get("BOTLER_TERM_TOKEN_TTL", "60"))


def _term_upstream() -> str:
    """读取终端服务进程地址（每次读取，便于测试/运行时调整）。"""
    return os.environ.get("BOTLER_TERM_UPSTREAM", _TERM_UPSTREAM_DEFAULT)


def _term_ws_upstream() -> str:
    """终端服务 WebSocket 地址（http→ws / https→wss 转换，供 websockets 客户端）。"""
    return _term_upstream().replace("http", "ws", 1)


@router.post("/token")
def issue_terminal_token(request: Request):
    """签发终端 WebSocket 短时效 token（issue #183）。

    - SSO 启用：必须已登录（未登录 401），token 携带登录用户身份；
    - SSO 未启用：主后端本就开放访问，签发「本地用户」token（行为一致）。
    """
    sso = request.app.state.ctx.sso
    user = sso.current_user(request)
    if user is None:
        if sso.enabled():
            raise HTTPException(401, "未登录")
        user = {"sub": "local", "username": "local", "name": "本地用户", "email": ""}
    token = create_terminal_token(user, ttl_seconds=_TERM_TOKEN_TTL)
    return {"token": token, "expires_in": _TERM_TOKEN_TTL}


@router.get("/health")
async def terminal_health():
    """终端服务进程健康检查（反向代理，供前端/部署探活）。"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{_term_upstream()}/terminal/health")
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("终端服务健康检查失败: %s", e)
        return JSONResponse({"ok": False, "error": "终端服务不可达"}, status_code=503)


async def _pump(websocket: WebSocket, remote) -> None:
    """双向转发：浏览器 WebSocket ↔ 终端服务 WebSocket。

    任一方向结束（浏览器断开 / 终端服务关闭）即取消另一方，保证连接
    及时回收；文本与二进制消息原样透传（terminado 协议消息）。

    返回终端服务会话结束时的 WebSocket 关闭码：上游以非 1000 码关闭
    （如认证拒绝 4001）时原样返回，供调用方以相同语义关闭浏览器端。
    """

    close_code = 1000

    async def to_client():
        nonlocal close_code
        try:
            async for msg in remote:
                if isinstance(msg, (bytes, bytearray)):
                    await websocket.send_bytes(bytes(msg))
                else:
                    await websocket.send_text(str(msg))
        except websockets.exceptions.ConnectionClosed as e:
            # 终端服务侧主动关闭：保留其关闭码（4001 认证拒绝等）
            if e.rcvd is not None and e.rcvd.code:
                close_code = e.rcvd.code
            raise

    async def to_server():
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg["type"] == "websocket.receive":
                text = msg.get("text")
                if text is not None:
                    await remote.send(text)
                else:
                    await remote.send(msg.get("bytes") or b"")

    client_task = asyncio.create_task(to_client())
    server_task = asyncio.create_task(to_server())
    done, pending = await asyncio.wait(
        {client_task, server_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            await task
        except Exception:  # noqa: BLE001 —— 任一方向失败由另一方结束连接
            pass
    return close_code


@router.websocket("/ws/{name}")
async def terminal_ws_proxy(websocket: WebSocket, name: str):
    """WebSocket 反向代理：/api/terminal/ws/<name> → 终端服务进程。

    token 校验在终端服务进程侧完成（无效/过期以 close code 4001 拒绝）；
    本代理原样透传查询串（token 参数）与消息流。
    """
    await websocket.accept()
    query = (websocket.scope.get("query_string") or b"").decode("utf-8", errors="replace")
    upstream = (
        f"{_term_ws_upstream()}/terminal/ws/{urllib.parse.quote(name, safe='')}"
        + (f"?{query}" if query else "")
    )
    close_code = 1011
    close_reason = "终端服务连接失败"
    try:
        async with websockets.connect(upstream, ping_interval=20, ping_timeout=20) as remote:
            close_code = await _pump(websocket, remote)
            close_reason = "终端会话已结束"
    except websockets.exceptions.ConnectionClosed as e:
        # 上游连接异常关闭（如 token 校验失败 4001）：原样传递关闭码
        if e.rcvd is not None and e.rcvd.code:
            close_code = e.rcvd.code
            close_reason = e.rcvd.reason or close_reason
        logger.warning("终端 WebSocket 上游关闭: code=%s reason=%s", close_code, close_reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("终端 WebSocket 代理失败: %s", e)
        close_reason = f"终端服务连接失败: {e}"
    finally:
        # 无论上游会话如何结束，浏览器端连接都必须显式关闭（starlette
        # 在 endpoint 正常返回时不会自动发送 close，遗漏会导致客户端
        # 永远收不到断开事件而挂起）
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:  # noqa: BLE001
            pass
