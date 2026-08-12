"""本地环境检测 API 测试（issue #22）：GET /api/environment。"""

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

FAKE_RESULT = {
    "hostname": "test-host",
    "platform": "linux x86_64",
    "detected_at": "2026-08-12T15:00:00+08:00",
    "tools": [
        {"key": "claude", "name": "Claude Code", "installed": True,
         "version": "1.2.3", "latest": "1.4.0", "up_to_date": False},
        {"key": "docker", "name": "Docker", "installed": False,
         "version": None, "latest": None, "up_to_date": None},
    ],
}


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


class TestEnvironmentApi:
    def test_get_environment_returns_detection_result(self, client, monkeypatch):
        """GET /api/environment 返回环境检测结果（mock 检测逻辑避免真实执行）。"""
        tc, tmp_path = client
        monkeypatch.setattr(
            "botler.environment.detect_local_environment",
            lambda **kw: FAKE_RESULT,
        )
        resp = tc.get("/api/environment")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hostname"] == "test-host"
        assert data["platform"] == "linux x86_64"
        assert data["detected_at"]
        assert len(data["tools"]) == 2
        claude = data["tools"][0]
        assert claude["key"] == "claude"
        assert claude["installed"] is True
        assert claude["version"] == "1.2.3"
        assert claude["latest"] == "1.4.0"
        assert claude["up_to_date"] is False
        docker = data["tools"][1]
        assert docker["installed"] is False
        assert docker["up_to_date"] is None
