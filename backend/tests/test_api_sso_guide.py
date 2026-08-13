"""SSO 配置指南 API 测试（issue #27 第六轮）：设置页直接展示配置文档。

背景：平台使用者看不到代码仓库里的本地文档 docs/Synology-SSO-配置指南.md，
用户要求把文档内容直接显示在设置页面上。实现：GET /api/settings/sso-guide
读取项目 docs/ 下的指南 Markdown 原文返回给前端渲染（单一文档来源，
docs/ 改动即页面生效）。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.api import settings as settings_api
from botler.auth import SsoAuth, SsoGuardMiddleware
from botler.config import ConfigManager
from botler.database import Database

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


@pytest.fixture
def open_client(tmp_path):
    """最小测试 app：SSO 未启用（开放访问），ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app)


@pytest.fixture
def sso_client(tmp_path):
    """最小测试 app：SSO 启用 + SsoGuardMiddleware（未登录访问 /api/* → 401）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_SSO, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    sso = SsoAuth(config, secret_path=str(tmp_path / "session.key"))
    ctx = SimpleNamespace(config=config, db=db, sso=sso)
    app = FastAPI()
    app.state.ctx = ctx
    app.add_middleware(SsoGuardMiddleware)
    app.include_router(api_router)
    return TestClient(app)


class TestSsoGuideApi:
    def test_guide_returns_markdown_content(self, open_client):
        """文档存在：返回 200 与指南 Markdown 原文（前端负责渲染）。"""
        tc = open_client
        resp = tc.get("/api/settings/sso-guide")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        # 关键内容断言：群晖侧配置步骤 + Botler 侧配置方式 + 常见问题
        assert "群晖 SSO Server" in data["content"]
        assert "Well-known URL" in data["content"]
        assert "常见问题" in data["content"]

    def test_guide_missing_file_returns_404(self, open_client, tmp_path, monkeypatch):
        """边界：指南文档不存在（误删/路径变更）→ 404 + 明确错误，前端提示降级。"""
        monkeypatch.setattr(
            settings_api, "SSO_GUIDE_PATH", tmp_path / "不存在的指南.md"
        )
        resp = open_client.get("/api/settings/sso-guide")
        assert resp.status_code == 404
        assert "detail" in resp.json()

    def test_guide_protected_when_sso_enabled(self, sso_client):
        """SSO 启用未登录：指南接口与其他 /api/* 一样受保护 → 401。"""
        assert sso_client.get("/api/settings/sso-guide").status_code == 401


class TestSsoSessionDaysDefault:
    def test_default_session_days_is_30(self, tmp_path):
        """回归（issue #27 第三轮）：用户确认登录有效期默认 30 天，
        未配置 sso 段时默认值应为 30（历史实现误为 7）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        assert config.get().sso_session_days == 30
