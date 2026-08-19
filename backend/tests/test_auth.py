"""SSO 认证测试：OIDC 授权码流程 + 签名会话 + API 保护（issue #27）。

用 httpx.MockTransport 模拟群晖 SSO Server 的 OIDC 端点
（discovery / token / userinfo），不依赖真实服务器。
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
    CsrfGuardMiddleware,
    SsoAuth,
    SsoGuardMiddleware,
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
    """模拟群晖 SSO Server 的 OIDC 端点。"""
    url = str(request.url)
    if url.endswith("/.well-known/openid-configuration"):
        return httpx.Response(200, json={
            "issuer": "https://nas.example.com",
            "authorization_endpoint": "https://nas.example.com/oauth/authorize",
            "token_endpoint": "https://nas.example.com/oauth/token",
            "userinfo_endpoint": "https://nas.example.com/oauth/userinfo",
        })
    if url.endswith("/oauth/token"):
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        form = dict(parse_qsl(request.content.decode()))
        # 授权码交换必须带 client_id + client_secret
        assert form.get("client_id") == "app-123"
        assert form.get("client_secret") == "secret-abc"
        assert form.get("grant_type") == "authorization_code"
        return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer"})
    if url.endswith("/oauth/userinfo"):
        assert request.headers["authorization"] == "Bearer at-1"
        return httpx.Response(200, json={
            "sub": "uid-1",
            "username": "zhangsan",
            "name": "张三",
            "email": "zs@example.com",
            "picture": "https://nas.example.com/avatar/zhangsan.png",
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
    # 与生产一致：guard 中间件 + 完整 api 路由（auth 路由已并入 api_router）
    # 执行顺序与 main.create_app 相同：SsoGuard → CsrfGuard（issue #263）
    app.add_middleware(CsrfGuardMiddleware)
    app.add_middleware(SsoGuardMiddleware)
    app.include_router(api_router)

    # main.create_app 中定义的端点（最小 app 里补 stub，验证不被 401 拦截即可）
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
    """走一遍完整登录流程，TestClient 自动保存会话 cookie。

    follow_redirects=False：302 不跟随，避免 TestClient 去访问 mock 的
    群晖授权页（那不在模拟范围内）。
    """
    resp = tc.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    state = dict(parse_qsl(urlparse(resp.headers["location"]).query))["state"]
    resp = tc.get(f"/api/auth/callback?code=good-code&state={state}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/")


class TestSsoStatus:
    def test_disabled_reports_disabled(self, open_client):
        """SSO 未启用：status 返回 enabled=false 且无用户。"""
        tc, _ = open_client
        data = tc.get("/api/auth/status").json()
        assert data["enabled"] is False
        assert data["user"] is None

    def test_enabled_reports_disabled_for_anonymous(self, sso_client):
        tc, _ = sso_client
        data = tc.get("/api/auth/status").json()
        assert data["enabled"] is True
        assert data["user"] is None


class TestApiGuard:
    def test_api_open_when_sso_disabled(self, open_client):
        """SSO 未启用：API 无需登录。"""
        tc, _ = open_client
        assert tc.get("/api/settings").status_code == 200

    def test_api_protected_when_enabled(self, sso_client):
        """SSO 启用：未登录访问 API → 401。"""
        tc, _ = sso_client
        assert tc.get("/api/settings").status_code == 401
        assert tc.get("/api/tasks").status_code == 401

    def test_auth_endpoints_exempt(self, sso_client):
        """登录流程自身端点不受保护。"""
        tc, _ = sso_client
        assert tc.get("/api/auth/status").status_code == 200

    def test_health_exempt(self, sso_client):
        """健康检查不要求登录（部署监控用）。"""
        tc, _ = sso_client
        assert tc.get("/api/health").status_code == 200

    def test_webhook_exempt(self, sso_client):
        """GitLab webhook 不要求登录（外部调用）。"""
        tc, _ = sso_client
        assert tc.post("/webhook/gitlab").status_code == 200

    def test_api_accessible_after_login(self, sso_client):
        """登录后 API 可访问。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.get("/api/settings").status_code == 200


class TestLoginFlow:
    def test_login_redirects_to_provider(self, sso_client):
        """/api/auth/login 302 到群晖授权端点，带齐参数与 state。"""
        tc, _ = sso_client
        resp = tc.get("/api/auth/login", follow_redirects=False)
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert loc.startswith("https://nas.example.com/oauth/authorize?")
        params = dict(parse_qsl(urlparse(loc).query))
        assert params["response_type"] == "code"
        assert params["client_id"] == "app-123"
        assert params["redirect_uri"] == "http://testserver/api/auth/callback"
        assert params["scope"] == "openid profile email"
        assert params["state"]
        # state 以 httponly cookie 落盘，防 CSRF
        set_cookie = resp.headers.get("set-cookie", "")
        assert "botler_sso_state=" in set_cookie

    def test_login_rejected_when_disabled(self, open_client):
        tc, _ = open_client
        assert tc.get("/api/auth/login").status_code == 400

    def test_callback_without_code_redirects_login_error(self, sso_client):
        tc, _ = sso_client
        resp = tc.get("/api/auth/callback?state=whatever", follow_redirects=False)
        assert resp.status_code == 302
        assert "error" in resp.headers["location"]

    def test_callback_with_bad_state_rejected(self, sso_client):
        """state 与 cookie 不符 → 拒绝（防 CSRF 回放）。"""
        tc, _ = sso_client
        tc.get("/api/auth/login")
        resp = tc.get("/api/auth/callback?code=good-code&state=forged-state", follow_redirects=False)
        assert resp.status_code == 302
        assert "error" in resp.headers["location"]

    def test_callback_missing_state_rejected(self, sso_client):
        tc, _ = sso_client
        resp = tc.get("/api/auth/callback?code=good-code", follow_redirects=False)
        assert resp.status_code == 302
        assert "error" in resp.headers["location"]

    def test_full_flow_sets_session(self, sso_client):
        """完整流程：登录 → 回调换 token/userinfo → 会话建立，me 返回用户。"""
        tc, _ = sso_client
        _login(tc)
        assert tc.get("/api/auth/me").status_code == 200
        me = tc.get("/api/auth/me").json()
        assert me["sub"] == "uid-1"
        assert me["username"] == "zhangsan"
        assert me["name"] == "张三"
        assert me["email"] == "zs@example.com"
        # picture（issue #271）：OIDC claims 头像随会话携带，前端右上角展示
        assert me["picture"] == "https://nas.example.com/avatar/zhangsan.png"
        # CSRF 防护（issue #263）：登录回调同时下发非 HttpOnly 的 CSRF cookie，
        # 前端 api.js 读取回填 X-CSRF-Token 请求头
        assert tc.cookies.get(CSRF_COOKIE) is not None


class TestSessionSecurity:
    def test_tampered_cookie_rejected(self, sso_client):
        """篡改会话 cookie → 签名校验失败 → 401。"""
        tc, tmp_path = sso_client
        _login(tc)
        jar = tc.cookies
        tampered = jar["botler_session"][:-4] + "dead"
        jar.set("botler_session", tampered)
        assert tc.get("/api/settings").status_code == 401

    def test_expired_cookie_rejected(self, sso_client):
        """过期会话 cookie → 401。"""
        tc, tmp_path = sso_client
        secret_path = str(tmp_path / "session.key")
        secret = get_session_secret(secret_path)
        # now=0 → exp 落在 1970 年，必然已过期
        expired = create_session({"sub": "u", "username": "old"},
                                 days=7, secret=secret, now=0)
        tc.cookies.set("botler_session", expired)
        assert tc.get("/api/settings").status_code == 401

    def test_logout_clears_session(self, sso_client):
        """退出登录后会话失效。"""
        tc, _ = sso_client
        _login(tc)
        resp = tc.post("/api/auth/logout")
        assert resp.status_code == 200
        # 删除 cookie 后请求不再带会话
        tc.cookies.delete("botler_session")
        assert tc.get("/api/settings").status_code == 401


class TestApiGuard401Message:
    """401 响应体 message（issue #221）：会话过期与未登录给出明确区分文案。"""

    def test_unauth_401_message(self, sso_client):
        """未登录（无会话 cookie）→ 401 响应体为「未登录」。"""
        tc, _ = sso_client
        resp = tc.get("/api/settings")
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("error") == "未登录（SSO 已启用）", body

    def test_expired_cookie_401_message(self, sso_client):
        """会话过期 → 401 响应体明确提示「登录已过期，请重新登录」。"""
        tc, tmp_path = sso_client
        secret_path = str(tmp_path / "session.key")
        secret = get_session_secret(secret_path)
        # now=0 → exp 落在 1970 年，必然已过期
        expired = create_session({"sub": "u", "username": "old"},
                                 days=7, secret=secret, now=0)
        tc.cookies.set("botler_session", expired)
        resp = tc.get("/api/settings")
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("error") == "登录已过期，请重新登录", body

    def test_tampered_cookie_401_message(self, sso_client):
        """篡改会话 cookie（签名无效）→ 401 响应体同样提示重新登录。"""
        tc, _ = sso_client
        _login(tc)
        jar = tc.cookies
        tampered = jar["botler_session"][:-4] + "dead"
        jar.set("botler_session", tampered)
        resp = tc.get("/api/settings")
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("error") == "登录已过期，请重新登录", body
