"""DeepSeek 账户余额查询测试（issue #138）。

覆盖凭据解析链（dsh 段 > AI 供应商 deepseek 项 > 环境变量）、余额接口
地址归一化（预设 /v1 前缀）、客户端请求构造/响应解析/错误处理（非 2xx /
超时 / 网络异常 / 缺 Key），以及 API 端点（GET /api/settings/deepseek-balance）
的 configured 判定与容错。用 httpx.MockTransport / monkeypatch 模拟，
不做真实外呼。
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.deepseek_balance import (
    BALANCE_TIMEOUT,
    DEFAULT_BALANCE_URL,
    DeepSeekBalanceClient,
    DeepSeekBalanceError,
    balance_url,
    resolve_deepseek_credentials,
)

CONFIG_TEXT = """\
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


@pytest.fixture
def client(tmp_path):
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path


def _settings(**overrides) -> SimpleNamespace:
    """构造最小 Settings 风格对象（resolve 只读 dsh / ai_providers）。"""
    base = {
        "dsh_api_key": "",
        "dsh_base_url": "",
        "ai_providers": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- 凭据解析链 ----

class TestResolveCredentials:
    def test_nothing_configured_returns_empty(self, monkeypatch):
        """未配置任何 Key（含环境变量）返回空串。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        api_key, base_url = resolve_deepseek_credentials(_settings())
        assert api_key == ""
        assert base_url == ""

    def test_dsh_segment_wins(self, monkeypatch):
        """dsh 段显式配置优先级最高（覆盖 AI 供应商与环境变量）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        s = _settings(
            dsh_api_key="dsh-key", dsh_base_url="https://dsh.example.com",
            ai_providers=[{
                "name": "DeepSeek 生产", "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "provider-key", "enabled": True,
            }],
        )
        api_key, base_url = resolve_deepseek_credentials(s)
        assert api_key == "dsh-key"
        assert base_url == "https://dsh.example.com"

    def test_ai_provider_deepseek_fallback(self, monkeypatch):
        """dsh 段未配时回退 AI 供应商 provider=deepseek 且启用的项。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        s = _settings(ai_providers=[
            {"name": "OpenAI", "provider": "openai", "base_url": "",
             "api_key": "sk-openai", "enabled": True},
            {"name": "DeepSeek 生产", "provider": "deepseek",
             "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-deepseek", "enabled": True},
        ])
        api_key, base_url = resolve_deepseek_credentials(s)
        assert api_key == "sk-deepseek"
        assert base_url == "https://api.deepseek.com/v1"

    def test_ai_provider_disabled_or_empty_key_ignored(self, monkeypatch):
        """AI 供应商 deepseek 项停用或 api_key 为空时跳过。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        s = _settings(ai_providers=[
            {"name": "停用", "provider": "deepseek", "base_url": "",
             "api_key": "sk-a", "enabled": False},
            {"name": "无 key", "provider": "deepseek", "base_url": "",
             "api_key": "  ", "enabled": True},
        ])
        api_key, _ = resolve_deepseek_credentials(s)
        assert api_key == ""

    def test_env_var_last_resort(self, monkeypatch):
        """dsh / AI 供应商均未配时回退环境变量。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example.com")
        api_key, base_url = resolve_deepseek_credentials(_settings())
        assert api_key == "env-key"
        assert base_url == "https://env.example.com"


# ---- 余额接口地址归一化 ----

class TestBalanceUrl:
    def test_default_url_when_no_base(self):
        """未配置 base_url 使用官方默认地址（issue #138 示例）。"""
        assert balance_url("") == DEFAULT_BALANCE_URL
        assert balance_url("   ") == DEFAULT_BALANCE_URL

    def test_preset_v1_prefix_stripped(self):
        """预设 base_url 尾部 /v1 归一化去掉后拼 /user/balance。"""
        assert balance_url("https://api.deepseek.com/v1") == DEFAULT_BALANCE_URL
        assert balance_url("https://api.deepseek.com/v1/") == DEFAULT_BALANCE_URL

    def test_plain_base_kept(self):
        """自定义 base_url 不带 /v1 时原样拼接。"""
        assert balance_url("https://api.deepseek.com") == DEFAULT_BALANCE_URL
        assert balance_url("https://gateway.example.com") == (
            "https://gateway.example.com/user/balance")


# ---- 客户端 ----

class TestDeepSeekBalanceClient:
    def _client(self, handler, api_key="sk-test", base_url=""):
        transport = httpx.MockTransport(handler)
        client = DeepSeekBalanceClient(
            api_key=api_key, base_url=base_url, timeout=5)
        client._http = httpx.Client(transport=transport)  # 注入 mock 传输
        return client

    def test_missing_api_key_rejected(self):
        """未配置 API Key 构造时报错（不发请求）。"""
        with pytest.raises(DeepSeekBalanceError, match="未配置 DeepSeek API Key"):
            DeepSeekBalanceClient(api_key="", base_url="")

    def test_fetch_request_and_parse(self):
        """GET /user/balance：Bearer 认证 + Accept 头，解析余额信息。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["auth"] = request.headers.get("Authorization")
            captured["accept"] = request.headers.get("Accept")
            return httpx.Response(200, json={
                "is_available": True,
                "balance_infos": [{
                    "currency": "CNY",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }],
            })

        client = self._client(handler, api_key="sk-test",
                              base_url="https://api.deepseek.com/v1")
        data = client.fetch()
        assert captured["method"] == "GET"
        # 预设 /v1 前缀归一化后打官方余额接口
        assert captured["url"] == DEFAULT_BALANCE_URL
        assert captured["auth"] == "Bearer sk-test"
        assert captured["accept"] == "application/json"
        assert data["is_available"] is True
        info = data["balance_infos"][0]
        assert info["currency"] == "CNY"
        assert info["total_balance"] == "110.00"
        assert info["granted_balance"] == "10.00"
        assert info["topped_up_balance"] == "100.00"

    def test_fetch_http_error_status(self):
        """非 2xx（如 401 无效 Key）抛 DeepSeekBalanceError。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={
                "error": {"message": "Authentication Fails",
                          "type": "authentication_error"},
            })

        client = self._client(handler)
        with pytest.raises(DeepSeekBalanceError, match="HTTP 401"):
            client.fetch()

    def test_fetch_timeout(self):
        """请求超时转可读错误。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        client = self._client(handler)
        with pytest.raises(DeepSeekBalanceError, match="超时"):
            client.fetch()

    def test_fetch_network_error(self):
        """网络异常转可读错误。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = self._client(handler)
        with pytest.raises(DeepSeekBalanceError, match="网络请求失败"):
            client.fetch()

    def test_default_timeout_positive(self):
        """默认超时为正数（10s）。"""
        assert BALANCE_TIMEOUT > 0


# ---- API 端点 ----

class TestDeepSeekBalanceApi:
    """GET /api/settings/deepseek-balance：概览页余额卡片数据源（issue #138）。"""

    def _save_provider(self, tc, provider):
        tc.put("/api/settings", json={"ai_providers": [provider]})

    def test_not_configured(self, client, monkeypatch):
        """未配置任何 DeepSeek Key：configured=false，不发请求。"""
        tc, _ = client
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        resp = tc.get("/api/settings/deepseek-balance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["balance"] is None
        assert data["error"] is None

    def test_configured_with_balance(self, client, monkeypatch):
        """配置 deepseek 项：后端代调余额接口，返回余额信息（无 Key 泄露）。"""
        tc, _ = client
        self._save_provider(tc, {
            "name": "DeepSeek 生产", "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-secret-123456", "model": "deepseek-chat",
            "enabled": True,
        })
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(fetch=lambda: {
                "is_available": True,
                "balance_infos": [{
                    "currency": "CNY", "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }],
            })

        from botler import deepseek_balance as ds_mod
        monkeypatch.setattr(ds_mod, "DeepSeekBalanceClient", fake_client)
        resp = tc.get("/api/settings/deepseek-balance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["error"] is None
        bal = data["balance"]
        assert bal["is_available"] is True
        assert bal["balance_infos"][0]["total_balance"] == "110.00"
        assert bal["fetched_at"]
        # 客户端收到明文 Key（服务端代调），但响应不回显
        assert captured["api_key"] == "sk-secret-123456"
        assert captured["base_url"] == "https://api.deepseek.com/v1"
        assert "sk-secret" not in resp.text

    def test_configured_but_api_error(self, client, monkeypatch):
        """余额接口报错：configured=true + error 字段，不抛 500。"""
        tc, _ = client
        self._save_provider(tc, {
            "name": "DeepSeek 生产", "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-secret-123456", "model": "deepseek-chat",
            "enabled": True,
        })

        def fake_client(**kwargs):
            return SimpleNamespace(fetch=lambda: (_ for _ in ()).throw(
                DeepSeekBalanceError("DeepSeek 余额查询失败: HTTP 401 "
                                     "Authentication Fails")))

        from botler import deepseek_balance as ds_mod
        monkeypatch.setattr(ds_mod, "DeepSeekBalanceClient", fake_client)
        resp = tc.get("/api/settings/deepseek-balance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["balance"] is None
        assert "HTTP 401" in data["error"]

    def test_env_key_configured(self, client, monkeypatch):
        """仅环境变量配置 Key 时也判定为已配置并代调查询。"""
        tc, _ = client
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-secret-123456")
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(fetch=lambda: {
                "is_available": True, "balance_infos": []})

        from botler import deepseek_balance as ds_mod
        monkeypatch.setattr(ds_mod, "DeepSeekBalanceClient", fake_client)
        resp = tc.get("/api/settings/deepseek-balance")
        assert resp.status_code == 200
        assert resp.json()["configured"] is True
        assert captured["api_key"] == "env-secret-123456"
        # 未配置 base_url：默认官方余额接口
        assert captured["base_url"] == ""
