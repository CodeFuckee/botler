"""系统设置 API 测试：browse 段（目录选择对话框默认初始定位目录）。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database

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


class TestBrowseSettings:
    def test_get_settings_includes_browse(self, client):
        """未配置时 default_path 返回空串（前端留空 = 后端默认主目录）。"""
        tc, tmp_path = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["browse"]["default_path"] == ""

    def test_update_browse_default_path(self, client):
        """PUT browse.default_path 写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        target = tmp_path / "start"
        target.mkdir()
        resp = tc.put("/api/settings", json={"browse": {"default_path": str(target)}})
        assert resp.status_code == 200
        assert resp.json()["browse"]["default_path"] == str(target)
        # config.yaml 是唯一事实来源，应已落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "browse:" in config_text and str(target) in config_text

    def test_update_browse_blank_clears(self, client):
        """留空清空配置（回退服务器用户主目录）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"browse": {"default_path": "/tmp"}})
        resp = tc.put("/api/settings", json={"browse": {"default_path": "   "}})
        assert resp.status_code == 200
        assert resp.json()["browse"]["default_path"] == ""

    def test_update_browse_rejects_non_string(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"browse": {"default_path": 123}})
        assert resp.status_code == 400
        assert "必须是字符串" in resp.json()["detail"]


class TestTimezoneSettings:
    """ui.timezone 段：显示时区设置（issue #14，页面任务时间与本机时区不一致）。"""

    def test_get_settings_includes_timezone_empty(self, client):
        """未配置时 timezone 返回空串（前端默认跟随浏览器本机时区）。"""
        tc, tmp_path = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["ui"]["timezone"] == ""

    def test_update_timezone_persists(self, client):
        """PUT ui.timezone 写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ui": {"timezone": "Asia/Shanghai"}})
        assert resp.status_code == 200
        assert resp.json()["ui"]["timezone"] == "Asia/Shanghai"
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "timezone: Asia/Shanghai" in config_text

    def test_update_timezone_empty_clears(self, client):
        """清空 = 跟随浏览器本机时区。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ui": {"timezone": "Asia/Shanghai"}})
        resp = tc.put("/api/settings", json={"ui": {"timezone": ""}})
        assert resp.status_code == 200
        assert resp.json()["ui"]["timezone"] == ""

    def test_update_timezone_rejects_invalid(self, client):
        """非法 IANA 时区名拒绝保存（400）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ui": {"timezone": "Mars/Olympus"}})
        assert resp.status_code == 400
        assert "时区" in resp.json()["detail"]
