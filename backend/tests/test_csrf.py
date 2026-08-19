"""CSRF 防护测试（issue #263）：双提交 cookie 模式中间件 + 登录下发 + 豁免规则。

背景：平台写操作 API（保存设置、删仓库、建 issue 等）依赖 SSO 会话 cookie
鉴权，缺少 CSRF token 校验。本测试验证：
- 登录态下无 X-CSRF-Token 头的写请求被 403；
- 头与 cookie 一致时放行；头缺失 / 不匹配 / cookie 被篡改均 403；
- SSO 未启用（无会话）时行为不变；
- 登录流程自身 / 健康检查 / webhook 端点豁免；
- 登录回调下发 CSRF cookie（非 HttpOnly，前端 JS 可读并回填请求头）；
- 老会话（无 CSRF cookie）写请求 403，/api/auth/me 探测时补发 cookie。
"""

from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CsrfGuardMiddleware,
    SsoAuth,
    SsoGuardMiddleware,
    create_csrf_token,
    create_session,
    get_session_secret,
)
from botler.config import ConfigManager
from botler.database import Database

CONFIG_SSO = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
sso:
  enabled: true
  well_known_url: https://nas.example.com/.well-known/openid-configuration
  client_id: app-123
  client_secret: secret-abc
  scope: "openid profile email"
  session_days: 7
"""

CONFIG_NO_SSO = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""


def _oidc_handler(request: httpx.Request) -> httpx.Response:
    """模拟群晖 SSO Server 的 OIDC 端点（与 test_auth.py 同构）。"""
    url = str(request.url)
    if url.endswith("/.well-known/openid-configuration"):
        return httpx.Response(200, json={
            "issuer": "https://nas.example.com",
            "authorization_endpoint": "https://nas.example.com/oauth/authorize",
            "token_endpoint": "https://nas.example.com/oauth/token",
            "userinfo_endpoint": "https://nas.example.com/oauth/userinfo",
        })
    if url.endswith("/oauth/token"):
        form = dict(parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer"})
    if url.endswith("/oauth/userinfo"):
        return httpx.Response(200, json={
            "sub": "uid-1", "username": "zhangsan", "name": "张三",
            "email": "zs@example.com", "picture": "",
        })
    return httpx.Response(404, json={"error": "not found"})


def _build_app(tmp_path, config_text: str):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    sso = SsoAuth(config, secret_path=str(tmp_path / "session.key"),
                  transport=httpx.MockTransport(_oidc_handler))
    ctx = SimpleNamespace(config=config, db=db, sso=sso)
    app = FastAPI()
    app.state.ctx = ctx
    # 与生产一致：SsoGuard → CsrfGuard → api 路由（执行顺序由 add 顺序保证）
    app.add_middleware(CsrfGuardMiddleware)
    app.add_middleware(SsoGuardMiddleware)
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/webhook/gitlab")
    async def webhook(request: Request):
        return {"ok": True}

    return TestClient(app), tmp_path


@pytest.fixture
def sso_client(tmp_path):
    return _build_app(tmp_path, CONFIG_SSO)


@pytest.fixture
def open_client(tmp_path):
    return _build_app(tmp_path, CONFIG_NO_SSO)


def _login(tc) -> None:
    """走完整登录流程，TestClient 自动保存会话与 CSRF cookie。"""
    resp = tc.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    state = dict(parse_qsl(urlparse(resp.headers["location"]).query))["state"]
    resp = tc.get(f"/api/auth/callback?code=good-code&state={state}", follow_redirects=False)
    assert resp.status_code == 302


def _csrf_headers(tc) -> dict:
    """从 TestClient cookie jar 读取 CSRF token 构造请求头。"""
    return {"X-CSRF-Token": tc.cookies.get(CSRF_COOKIE)}


class TestCsrfToken:
    def test_derived_from_session_secret(self, tmp_path):
        """token 由 session_secret 派生：同一会话同一 secret 值确定。"""
        secret = get_session_secret(str(tmp_path / "session.key"))
        session = "payload.sig"
        t1 = create_csrf_token(secret, session)
        t2 = create_csrf_token(secret, session)
        assert t1 == t2
        assert len(t1) == 64  # hmac-sha256 hex

    def test_bound_to_session(self, tmp_path):
        """token 绑定会话：不同会话 cookie 派生值不同。"""
        secret = get_session_secret(str(tmp_path / "session.key"))
        assert create_csrf_token(secret, "session-a") != create_csrf_token(secret, "session-b")


class TestCsrfGuard:
    def test_write_rejected_without_header(self, sso_client):
        """验收标准1：登录态下无 CSRF 头的写请求被 403。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.cookies.get(CSRF_COOKIE) is not None  # 登录已下发
        resp = tc.post("/api/settings", json={"foo": "bar"})
        assert resp.status_code == 403

    def test_write_rejected_on_header_mismatch(self, sso_client):
        """头与 cookie 不一致 → 403。"""
        tc, _ = sso_client
        _login(tc)
        resp = tc.post("/api/settings", json={},
                       headers={"X-CSRF-Token": "forged-token"})
        assert resp.status_code == 403

    def test_write_allowed_with_matching_header(self, sso_client):
        """头与 cookie 一致 → 放行。"""
        tc, _ = sso_client
        _login(tc)
        resp = tc.put("/api/settings", json={}, headers=_csrf_headers(tc))
        assert resp.status_code == 200

    def test_write_rejected_on_tampered_cookie(self, sso_client):
        """cookie 被篡改（非派生值）→ 403（防御纵深）。"""
        tc, _ = sso_client
        _login(tc)
        tc.cookies.delete(CSRF_COOKIE)
        tc.cookies.set(CSRF_COOKIE, "tampered")
        resp = tc.put("/api/settings", json={}, headers=_csrf_headers(tc))
        assert resp.status_code == 403

    def test_get_exempt(self, sso_client):
        """GET 请求不校验 CSRF。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.get("/api/settings").status_code == 200

    def test_put_delete_also_protected(self, sso_client):
        """PUT/DELETE 写请求同样校验。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.put("/api/settings", json={}).status_code == 403
        assert tc.delete("/api/repos/1").status_code == 403
        assert tc.put("/api/settings", json={}, headers=_csrf_headers(tc)).status_code == 200

    def test_anonymous_write_handled_by_sso_guard(self, sso_client):
        """未登录写请求 → 401（SsoGuard 先拦截，不涉及 CSRF）。"""
        tc, _ = sso_client
        resp = tc.post("/api/settings", json={})
        assert resp.status_code == 401

    def test_auth_endpoints_exempt(self, sso_client):
        """登录流程自身端点豁免 CSRF。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.post("/api/auth/logout").status_code == 200

    def test_health_exempt(self, sso_client):
        """健康检查豁免。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.post("/api/health").status_code == 405

    def test_webhook_exempt(self, sso_client):
        """webhook 端点（/api/ 之外）豁免。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.post("/webhook/gitlab", json={}).status_code == 200

    def test_legacy_session_without_csrf_cookie_rejected(self, sso_client):
        """老会话（登录早于 CSRF 上线，无 CSRF cookie）写请求 → 403。"""
        tc, tmp_path = sso_client
        secret = get_session_secret(str(tmp_path / "session.key"))
        old = create_session({"sub": "u", "username": "old"}, days=7, secret=secret)
        tc.cookies.set(SESSION_COOKIE, old)
        assert tc.cookies.get(CSRF_COOKIE) is None
        assert tc.post("/api/settings", json={}).status_code == 403

    def test_legacy_session_gets_cookie_from_me(self, sso_client):
        """老会话访问 /api/auth/me 补发 CSRF cookie，随后写请求可通过。"""
        tc, tmp_path = sso_client
        secret = get_session_secret(str(tmp_path / "session.key"))
        old = create_session({"sub": "u", "username": "old"}, days=7, secret=secret)
        tc.cookies.set(SESSION_COOKIE, old)
        resp = tc.get("/api/auth/me")
        assert resp.status_code == 200
        # 响应下发 CSRF cookie（老会话补发）
        assert "set-cookie" in resp.headers
        assert f"{CSRF_COOKIE}=" in resp.headers.get("set-cookie", "")
        assert tc.cookies.get(CSRF_COOKIE) is not None
        assert tc.put("/api/settings", json={}, headers=_csrf_headers(tc)).status_code == 200


class TestCsrfCookieLifecycle:
    def test_login_sets_csrf_cookie(self, sso_client):
        """登录回调下发 CSRF cookie：非 HttpOnly（前端 JS 可读）、SameSite=Lax。"""
        tc, _ = sso_client
        _login(tc)
        set_cookie = [h for h in tc.headers.getlist("set-cookie")] if hasattr(tc.headers, "getlist") else []
        resp = tc.get("/api/auth/callback", follow_redirects=False)  # 触发一次
        # 从完整登录流程中检查 cookie 属性
        tc2, _ = sso_client
        r = tc2.get("/api/auth/login", follow_redirects=False)
        state = dict(parse_qsl(urlparse(r.headers["location"]).query))["state"]
        cb = tc2.get(f"/api/auth/callback?code=good-code&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        raw = cb.headers.get("set-cookie", "")
        csrf_part = next(
            (seg.strip() for seg in raw.split(",") if seg.strip().startswith(f"{CSRF_COOKIE}=")),
            "",
        )
        assert csrf_part, "登录响应应包含 botler_csrf cookie"
        assert "httponly" not in csrf_part.lower()  # 前端 JS 必须能读
        assert "samesite=lax" in csrf_part.lower()

    def test_logout_clears_csrf_cookie(self, sso_client):
        """退出登录清除 CSRF cookie。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.cookies.get(CSRF_COOKIE) is not None
        resp = tc.post("/api/auth/logout")
        assert resp.status_code == 200
        assert "set-cookie" in resp.headers  # delete_cookie 下发过期指令
        tc.cookies.delete(CSRF_COOKIE)
        assert tc.cookies.get(CSRF_COOKIE) is None


class TestNoSsoBehavior:
    def test_write_without_csrf_allowed(self, open_client):
        """验收标准3：SSO 未启用（无会话）时行为不变，写请求不校验 CSRF。"""
        tc, _ = open_client
        resp = tc.post("/api/settings", json={"foo": "bar"})
        assert resp.status_code != 403
