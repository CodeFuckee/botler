"""Synology SSO 登录（issue #27）：OIDC 授权码流程 + 签名会话 + API 保护。

- 群晖 SSO Server 以 OIDC（OAuth2 授权码模式）接入：discovery → 授权码 →
  token 交换 → userinfo 取用户信息（不解析/验签 id_token，免引入 JWT 依赖）。
- 会话为签名 cookie（标准库 hmac-sha256，密钥懒生成持久化在
  <backend>/data/session_secret.key，容器部署由 docker-compose 挂载持久化）。
- SSO 启用后由 SsoGuardMiddleware 保护 /api/*（登录流程自身与健康检查除外），
  未登录返回 401；前端经 /api/auth/status 感知并跳转登录页。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("botler.auth")

# 会话 / state cookie 名称
SESSION_COOKIE = "botler_session"
STATE_COOKIE = "botler_sso_state"
# CSRF 防护 cookie（issue #263）：双提交 cookie 模式，值由会话密钥派生
# 并绑定具体会话；非 HttpOnly——前端 api.js 需读取该 cookie 回填
# X-CSRF-Token 请求头（同源 JS 可读、跨站攻击者不可读，构成防护基础）。
CSRF_COOKIE = "botler_csrf"
# state 有效期（秒）：用户停在群晖登录页过久则失效，防 CSRF 回放
STATE_TTL_SECONDS = 300

# 会话签名密钥路径：环境变量覆盖（测试用），默认 backend/data/session_secret.key
_BACKEND_DIR = Path(__file__).resolve().parents[1]
SESSION_SECRET_PATH = os.environ.get(
    "BOTLER_SESSION_SECRET",
    str(_BACKEND_DIR / "data" / "session_secret.key"),
)


def get_session_secret(secret_path: str | None = None) -> str:
    """读取会话签名密钥；不存在时生成 32 字节随机密钥并持久化（重启不丢）。"""
    path = Path(secret_path or SESSION_SECRET_PATH)
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info("已生成会话签名密钥: %s", path)
    return secret


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def create_session(user: dict[str, Any], days: int, secret: str, now: float | None = None) -> str:
    """签发会话 cookie：base64url(JSON payload).HMAC-SHA256 签名。"""
    now = time.time() if now is None else now
    payload = {k: user.get(k) for k in ("sub", "username", "name", "email", "picture")}
    # picture（OIDC claims，issue #271）：头像链接随会话携带，前端 /api/auth/me
    # 读取展示；缺失/加载失败由前端回退首字母占位
    payload["exp"] = int(now) + int(days) * 86400
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{data}.{_sign(data, secret)}"


def verify_session(cookie: str, secret: str, now: float | None = None) -> dict[str, Any] | None:
    """校验会话 cookie：签名一致且未过期 → 返回 payload；否则 None。"""
    if not cookie or "." not in cookie:
        return None
    data, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(data, secret)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(data.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < (time.time() if now is None else now):
        return None
    return payload


def create_csrf_token(secret: str, session: str) -> str:
    """派生 CSRF token（issue #263）：HMAC(session_secret, 会话 cookie)。

    双提交 cookie 模式的「与 session_secret 派生」实现：
    - token 由会话签名密钥 + 具体会话值派生，攻击者无法伪造；
    - 绑定会话：重新登录（会话 cookie 变化）后 token 自然失效；
    - 校验规则：请求头 X-CSRF-Token == cookie == 派生期望值。
    """
    return hmac.new(
        secret.encode(), f"botler-csrf-v1:{session}".encode(), hashlib.sha256
    ).hexdigest()


def sso_settings(s) -> dict[str, Any]:
    """从 Settings 提取 sso 配置段（OIDC 客户端与登录流程使用）。"""
    return {
        "enabled": s.sso_enabled,
        "well_known_url": s.sso_well_known_url,
        "client_id": s.sso_client_id,
        "client_secret": s.sso_client_secret,
        "scope": s.sso_scope or "openid",
        "session_days": s.sso_session_days,
        "redirect_uri": s.sso_redirect_uri,
        "verify_ssl": s.sso_verify_ssl,
    }


class OidcClient:
    """群晖 SSO Server OIDC 客户端（httpx，transport 可注入便于测试）。"""

    def __init__(self, cfg: dict[str, Any], transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._client = httpx.Client(
            transport=transport,
            verify=bool(cfg.get("verify_ssl", True)),
            timeout=15.0,
        )
        self._discovery: dict[str, Any] | None = None

    def discover(self) -> dict[str, Any]:
        """从 well-known URL 拉取 OIDC 端点信息（结果缓存于实例）。"""
        if self._discovery is None:
            resp = self._client.get(self.cfg["well_known_url"])
            resp.raise_for_status()
            self._discovery = resp.json()
        return self._discovery

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        """构造授权码请求 URL（引导浏览器跳群晖登录页）。"""
        d = self.discover()
        params = {
            "response_type": "code",
            "client_id": self.cfg["client_id"],
            "redirect_uri": redirect_uri,
            "scope": self.cfg.get("scope") or "openid",
            "state": state,
        }
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        return f"{d['authorization_endpoint']}?{qs}"

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """授权码换 access_token。"""
        d = self.discover()
        resp = self._client.post(d["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
        })
        resp.raise_for_status()
        return resp.json()["access_token"]

    def fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        """用 access_token 拉取用户信息（sub/username/name/email）。"""
        d = self.discover()
        resp = self._client.get(
            d["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


class SsoAuth:
    """SSO 认证服务：配置读取、登录 URL 构造、回调完成、会话校验。"""

    def __init__(self, config, secret_path: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.config = config
        self.secret_path = secret_path
        self._transport = transport
        self._oidc: OidcClient | None = None

    def enabled(self) -> bool:
        return bool(self.config.get().sso_enabled)

    def _secret(self) -> str:
        return get_session_secret(self.secret_path)

    def _oidc_client(self) -> OidcClient:
        if self._oidc is None:
            self._oidc = OidcClient(sso_settings(self.config.get()), transport=self._transport)
        return self._oidc

    def current_user(self, request: Request) -> dict[str, Any] | None:
        """从请求 cookie 解析当前登录用户；未启用 SSO 或未登录 → None。"""
        if not self.enabled():
            return None
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        return verify_session(cookie, self._secret())

    def csrf_token_for(self, session: str) -> str:
        """派生指定会话的 CSRF token（issue #263）：登录下发 / 老会话补发。"""
        return create_csrf_token(self._secret(), session)

    def build_login(self, request: Request) -> tuple[str, str]:
        """构造群晖授权 URL 与 state（state 以 cookie 落盘，回调时校验）。"""
        state = secrets.token_hex(16)
        return self._oidc_client().authorization_url(self._redirect_uri(request), state), state

    def _redirect_uri(self, request: Request) -> str:
        """回调地址：优先配置值，否则按浏览器地址动态生成（支持任意域名/IP）。"""
        cfg = sso_settings(self.config.get())
        if cfg.get("redirect_uri"):
            return cfg["redirect_uri"]
        scheme = request.url.scheme
        host = request.headers.get("host") or "localhost"
        return f"{scheme}://{host}/api/auth/callback"

    def complete_login(self, request: Request, code: str | None, state: str | None) -> str | None:
        """回调完成登录：校验 state → 换 token → 取用户信息 → 签发会话 cookie。

        任一步失败返回 None（前端跳登录页并提示）。
        """
        expected = request.cookies.get(STATE_COOKIE)
        if not expected or not state or not hmac.compare_digest(str(expected), str(state)):
            logger.warning("SSO 回调 state 校验失败（可能被回放）")
            return None
        if not code:
            return None
        try:
            redirect_uri = self._redirect_uri(request)
            token = self._oidc_client().exchange_code(code, redirect_uri)
            info = self._oidc_client().fetch_userinfo(token)
        except Exception as e:  # noqa: BLE001
            logger.warning("SSO 登录失败: %s", e)
            return None
        if not info.get("sub"):
            logger.warning("SSO userinfo 缺少 sub 字段: %s", info)
            return None
        cfg = sso_settings(self.config.get())
        return create_session(info, cfg["session_days"], self._secret())


class SsoGuardMiddleware(BaseHTTPMiddleware):
    """SSO 启用时保护 /api/*（登录流程自身与健康检查除外），未登录 → 401。

    webhook 路径不在 /api/ 前缀下，天然放行（GitLab 外部调用）。
    """

    # 放行前缀：登录流程自身 / 健康检查（部署监控；/api/terminal/health
    # 为终端服务探活端点，issue #183：与 /api/health 同语义，SSO 场景放行）
    PUBLIC_API_PREFIXES = ("/api/auth/", "/api/health", "/api/terminal/health")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path.startswith("/api/") and not path.startswith(self.PUBLIC_API_PREFIXES):
            ctx = getattr(request.app.state, "ctx", None)
            sso = getattr(ctx, "sso", None)
            if sso is not None and sso.enabled() and sso.current_user(request) is None:
                # 401 响应体区分未登录与会话过期（issue #221）：无会话 cookie
                # → 首次访问未登录；有 cookie 但签名无效/已过期 → 明确提示
                # 重新登录（前端据此跳登录页并展示对应文案）
                if request.cookies.get(SESSION_COOKIE):
                    return JSONResponse(
                        {"error": "登录已过期，请重新登录"}, status_code=401
                    )
                return JSONResponse({"error": "未登录（SSO 已启用）"}, status_code=401)
        return await call_next(request)


class CsrfGuardMiddleware(BaseHTTPMiddleware):
    """写操作 CSRF 防护（issue #263）：双提交 cookie 模式校验。

    与 SsoGuardMiddleware 共用放行前缀（登录流程自身 / 健康检查），webhook
    在 /api/ 前缀之外天然豁免。规则：
    - 仅 SSO 启用且已登录（存在有效会话 cookie）时生效——无会话则无
      CSRF 风险面，SSO 未启用 / 未登录行为与现状完全一致；
    - 仅校验非 GET/HEAD/OPTIONS 写请求（读请求无副作用，不校验）；
    - 已登录写请求：请求头 X-CSRF-Token == botler_csrf cookie ==
      session_secret 派生期望值，三者任一缺失/不一致 → 403；
    - 老会话（登录早于 CSRF 上线、无 CSRF cookie）写请求 → 403 但不补发
      cookie（避免给攻击者放行窗口），前端启动探测 /api/auth/me 时后端
      补发 cookie，随后写请求恢复正常。
    """

    # 放行前缀：登录流程自身 / 健康检查（与 SsoGuardMiddleware 同源）
    PUBLIC_API_PREFIXES = ("/api/auth/", "/api/health", "/api/terminal/health")
    # 无副作用的读方法：不产生状态变更，无需 CSRF 校验
    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    @staticmethod
    def _csrf_ok(request: Request, sso: SsoAuth) -> bool:
        """双提交校验：header == cookie == 派生期望值（三者缺一不可）。"""
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie:
            return False
        session = request.cookies.get(SESSION_COOKIE)
        header = request.headers.get("X-CSRF-Token")
        if not header:
            return False
        expected = sso.csrf_token_for(session)
        # 恒定时间比较，防时序侧信道
        return (
            hmac.compare_digest(header, cookie)
            and hmac.compare_digest(cookie, expected)
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        path = request.url.path
        # 读请求 / 登录流程 / 健康检查 / /api/ 之外的端点（webhook）均放行
        if request.method in self.SAFE_METHODS or path.startswith(self.PUBLIC_API_PREFIXES):
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)
        ctx = getattr(request.app.state, "ctx", None)
        sso = getattr(ctx, "sso", None)
        # SSO 未启用（无会话机制）→ 保持现状，不校验
        if sso is None or not sso.enabled():
            return await call_next(request)
        session = request.cookies.get(SESSION_COOKIE)
        # 未登录：SsoGuardMiddleware 先行返回 401，CSRF 不重复拦截
        if not session or sso.current_user(request) is None:
            return await call_next(request)
        if not self._csrf_ok(request, sso):
            logger.warning("CSRF 校验失败: %s %s", request.method, path)
            return JSONResponse(
                {"error": "CSRF 校验失败（X-CSRF-Token 缺失或不一致）"},
                status_code=403,
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# 终端服务短时效 token（issue #183）：Web 终端 WebSocket 的认证凭证
#
# 与主后端「共享用户验证」的实现方式：
#   - 主后端 /api/terminal/token 用与会话 cookie 相同的 HMAC 会话密钥签发
#     短时效 token（payload 携带 SSO 登录用户信息，默认 60 秒过期）；
#   - 独立终端服务进程（Tornado + terminado，backend/terminal_service.py）
#     用同一密钥校验 token（同一份 session_secret.key），校验通过才托管 PTY。
# 因此 token 结构/签名算法与会话 cookie 完全同构，只是有效期短、仅用于
# WebSocket 握手，避免把长会话 cookie 直接暴露给独立进程。
# ---------------------------------------------------------------------------

# 终端 token 默认有效期（秒）：WebSocket 握手后连接已建立，无需长时效
TERMINAL_TOKEN_TTL_SECONDS = int(os.environ.get("BOTLER_TERM_TOKEN_TTL", "60"))


def create_terminal_token(
    user: dict[str, Any],
    ttl_seconds: int = TERMINAL_TOKEN_TTL_SECONDS,
    secret: str | None = None,
    now: float | None = None,
) -> str:
    """签发终端 WebSocket 短时效 token（issue #183）。

    payload 携带 typ=term 声明 + 用户信息（sub/username/name/email）+ exp；
    签名算法与会话 cookie 相同（HMAC-SHA256）。typ 声明把终端 token 与
    会话 cookie 隔离：结构同构但用途互不复用（cookie 有效期长，不能当
    终端 token 用），终端服务用 verify_terminal_token 校验。
    """
    secret = secret or get_session_secret()
    now = time.time() if now is None else now
    payload = {"typ": "term"}
    payload.update({k: user.get(k) for k in ("sub", "username", "name", "email", "picture")})
    payload["exp"] = int(now) + int(ttl_seconds)
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{data}.{_sign(data, secret)}"


def verify_terminal_token(
    token: str | None,
    secret: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """校验终端 token：签名一致且未过期 → payload；否则 None（issue #183）。"""
    if not token or "." not in token:
        return None
    secret = secret or get_session_secret()
    data, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(data, secret)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(data.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("typ") != "term":
        return None
    if payload.get("exp", 0) < (time.time() if now is None else now):
        return None
    return payload
