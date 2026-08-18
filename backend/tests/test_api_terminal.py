"""终端 API 测试（issue #183）：token 签发 + 反向代理到独立终端服务进程。

覆盖：
- POST /api/terminal/token：SSO 未启用签发本地用户 token（可被终端服务校验）；
  SSO 启用且未登录 401；
- GET /api/terminal/health：反向代理终端服务健康检查；
- WebSocket /api/terminal/ws/<name>：反向代理真实终端服务，stdin 回显验证
  PTY 会话（终端服务在测试线程内起真实 Tornado 进程，随机端口）。
"""

import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from terminado import NamedTermManager
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets

from botler import auth as auth_mod
from botler.api import router as api_router
from botler.auth import SsoAuth, SsoGuardMiddleware, verify_terminal_token
from botler.config import ConfigManager
from botler.database import Database
from terminal_service import make_terminal_app

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


def _build_app(tmp_path, config_text, secret_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    sso = SsoAuth(config, secret_path=secret_path)
    ctx = SimpleNamespace(config=config, db=db, sso=sso)
    app = FastAPI()
    app.state.ctx = ctx
    app.add_middleware(SsoGuardMiddleware)
    app.include_router(api_router)
    return TestClient(app)


@pytest.fixture
def open_client(tmp_path, monkeypatch):
    secret_path = str(tmp_path / "session.key")
    # 让签发 token 的默认密钥与会话密钥一致（同文件），终端服务用同一路径校验
    monkeypatch.setattr(auth_mod, "SESSION_SECRET_PATH", secret_path)
    return _build_app(tmp_path, CONFIG_NO_SSO, secret_path)


@pytest.fixture
def sso_client(tmp_path, monkeypatch):
    secret_path = str(tmp_path / "session.key")
    monkeypatch.setattr(auth_mod, "SESSION_SECRET_PATH", secret_path)
    return _build_app(tmp_path, CONFIG_SSO, secret_path)


@pytest.fixture
def term_upstream(tmp_path, monkeypatch):
    """在后台线程起真实终端服务进程（随机端口），并指向上游。"""
    secret_path = str(tmp_path / "session.key")
    manager = NamedTermManager(shell_command=["cat"], max_terminals=8)
    app = make_terminal_app(term_manager=manager, secret_path=secret_path)
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    async def _setup():
        sockets = bind_sockets(0, "127.0.0.1")
        server = HTTPServer(app)
        server.add_sockets(sockets)
        return server, sockets[0].getsockname()[1]

    server, port = asyncio.run_coroutine_threadsafe(_setup(), loop).result(timeout=5)
    monkeypatch.setenv("BOTLER_TERM_UPSTREAM", f"http://127.0.0.1:{port}")
    yield secret_path
    loop.call_soon_threadsafe(server.stop)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


class TestTokenEndpoint:
    def test_issue_token_when_sso_disabled(self, open_client):
        resp = open_client.post("/api/terminal/token")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"]
        assert body["expires_in"] > 0
        payload = verify_terminal_token(body["token"])
        assert payload is not None
        assert payload["sub"] == "local"

    def test_issue_token_blocked_when_sso_enabled_anonymous(self, sso_client):
        resp = sso_client.post("/api/terminal/token")
        assert resp.status_code == 401

    def test_issue_token_after_login(self, sso_client):
        # 手动签发会话 cookie（完整 SSO 流程在 test_auth.py 已覆盖）
        from botler.auth import create_session, get_session_secret
        secret = get_session_secret(str(sso_client.app.state.ctx.sso.secret_path))
        cookie = create_session(
            {"sub": "uid-1", "username": "zhangsan", "name": "张三", "email": "zs@example.com"},
            days=7, secret=secret,
        )
        sso_client.cookies.set("botler_session", cookie)
        resp = sso_client.post("/api/terminal/token")
        sso_client.cookies.clear()
        assert resp.status_code == 200
        payload = verify_terminal_token(resp.json()["token"])
        assert payload is not None
        assert payload["username"] == "zhangsan"


class TestTerminalProxy:
    def test_health_proxy(self, open_client, term_upstream):
        resp = open_client.get("/api/terminal/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "botler-terminal"

    def test_health_proxy_unreachable(self, open_client, monkeypatch):
        monkeypatch.setenv("BOTLER_TERM_UPSTREAM", "http://127.0.0.1:1")
        resp = open_client.get("/api/terminal/health")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False

    def test_ws_proxy_runs_shell(self, open_client, term_upstream):
        from botler.auth import create_terminal_token, get_session_secret
        secret = get_session_secret(term_upstream)
        token = create_terminal_token(
            {"sub": "uid-1", "username": "zhangsan", "name": "张三", "email": ""},
            secret=secret,
        )
        with open_client.websocket_connect(
            f"/api/terminal/ws/tab-1?token={token}"
        ) as ws:
            first = json.loads(ws.receive_text())
            assert first[0] == "setup"
            ws.send_text(json.dumps(["stdin", "proxy-echo\n"]))
            got = ""
            for _ in range(8):
                arr = json.loads(ws.receive_text())
                if arr[0] == "stdout":
                    got += arr[1]
                    if "proxy-echo" in got:
                        break
            assert "proxy-echo" in got, f"stdout 未包含回显: {got!r}"

    def test_ws_proxy_forged_token_rejected(self, open_client, term_upstream):
        from starlette.websockets import WebSocketDisconnect
        with open_client.websocket_connect(
            "/api/terminal/ws/tab-2?token=forged.token"
        ) as ws:
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_text()


class TestHealthExemption:
    def test_health_exempt_when_sso_enabled(self, sso_client, term_upstream):
        # SSO 启用时健康检查端点应放行（部署监控探活，与 /api/health 同语义）
        resp = sso_client.get("/api/terminal/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
