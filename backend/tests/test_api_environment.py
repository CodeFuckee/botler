"""本地环境检测 API 测试（issue #22）：GET /api/environment。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler import environment
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


class TestUpgradeApi:
    """工具升级 API（issue #465）：POST /api/environment/upgrade。"""

    def test_upgrade_success_schedules_restart(self, client, monkeypatch):
        """升级成功 → 200 + 调度重启标记 + 返回新版本。"""
        tc, tmp_path = client
        monkeypatch.setattr(
            "botler.environment.upgrade_tool",
            lambda key: {"key": key, "name": "Claude Code",
                         "upgraded": True, "version": "2.0.0"})
        monkeypatch.setattr(
            "botler.environment.schedule_restart",
            lambda delay=2.0: True)
        resp = tc.post("/api/environment/upgrade", json={"key": "claude"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["key"] == "claude"
        assert data["version"] == "2.0.0"
        assert data["restarting"] is True

    def test_upgrade_unknown_tool_400(self, client, monkeypatch):
        """未知工具 → 400 携带可读错误信息。"""
        tc, tmp_path = client
        def _raise(key):
            raise environment.UpgradeError(f"未知工具: {key}")
        monkeypatch.setattr("botler.environment.upgrade_tool", _raise)
        resp = tc.post("/api/environment/upgrade", json={"key": "nope"})
        assert resp.status_code == 400
        assert "未知工具" in resp.json()["detail"]

    def test_upgrade_blank_key_400(self, client, monkeypatch):
        """key 为空/纯空白 → 400。"""
        tc, tmp_path = client
        monkeypatch.setattr(
            "botler.environment.upgrade_tool",
            lambda key: pytest.fail("不应执行升级"))
        resp = tc.post("/api/environment/upgrade", json={"key": "   "})
        assert resp.status_code == 400

    def test_upgrade_missing_key_422(self, client):
        """请求体缺 key → 422（pydantic 校验）。"""
        tc, tmp_path = client
        resp = tc.post("/api/environment/upgrade", json={})
        assert resp.status_code == 422

    def test_upgrade_failure_no_restart(self, client, monkeypatch):
        """升级失败（UpgradeError）→ 400 且不调度重启。"""
        tc, tmp_path = client
        monkeypatch.setattr(
            "botler.environment.upgrade_tool",
            lambda key: (_ for _ in ()).throw(
                environment.UpgradeError("下载 gh 升级包失败（HTTP 404）")))
        monkeypatch.setattr(
            "botler.environment.schedule_restart",
            lambda delay=2.0: pytest.fail("失败时不应调度重启"))
        resp = tc.post("/api/environment/upgrade", json={"key": "gh"})
        assert resp.status_code == 400
        assert "HTTP 404" in resp.json()["detail"]
