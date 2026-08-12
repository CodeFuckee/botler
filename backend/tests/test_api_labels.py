"""标记库 API 测试（issue #29）：默认清单不可删除、自定义标签增删与校验。

覆盖：GET 列表、POST 添加（含格式/重名/默认名冲突校验）、DELETE 删除
（默认标签拒绝、不存在 404）、持久化到 config.yaml。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.labels import DEFAULT_LABELS

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
    return TestClient(app), tmp_path, config


class TestListLabels:
    def test_get_returns_default_and_custom(self, client):
        api, _, _ = client
        r = api.get("/api/labels")
        assert r.status_code == 200
        data = r.json()
        assert len(data["default"]) == 13
        assert data["default"][0]["name"] == "bug"
        # 默认清单含流程标签
        names = {l["name"] for l in data["default"]}
        assert {"bug", "feature", "bot-done", "in-progress"} <= names
        assert data["custom"] == []


class TestAddLabel:
    def test_add_custom_label_persists(self, client):
        api, _, config = client
        r = api.post("/api/labels", json={
            "name": "security", "color": "#111111", "description": "安全相关"})
        assert r.status_code == 200
        data = r.json()
        assert data["custom"] == [{
            "name": "security", "color": "#111111", "description": "安全相关"}]
        # 持久化到 config.yaml，且默认清单不落盘（内置）
        assert config.get().custom_labels == [{
            "name": "security", "color": "#111111", "description": "安全相关"}]

    def test_add_duplicate_custom_rejected(self, client):
        api, _, _ = client
        assert api.post("/api/labels", json={"name": "security"}).status_code == 200
        r = api.post("/api/labels", json={"name": "security"})
        assert r.status_code == 400
        assert "已存在" in r.json()["detail"]

    def test_add_default_name_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "bug"})
        assert r.status_code == 400
        assert "默认标签" in r.json()["detail"]

    def test_add_invalid_name_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "../evil"})
        assert r.status_code == 400
        assert "标签名须以字母或数字开头" in r.json()["detail"]
        # 空名
        r = api.post("/api/labels", json={"name": "  "})
        assert r.status_code == 400

    def test_add_invalid_color_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "security", "color": "red"})
        assert r.status_code == 400
        assert "#RRGGBB" in r.json()["detail"]

    def test_add_without_color_uses_default(self, client):
        api, _, config = client
        r = api.post("/api/labels", json={"name": "hotfix"})
        assert r.status_code == 200
        assert r.json()["custom"][0]["color"] == "#6699cc"

    def test_add_with_space_and_dash_name(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "urgent-1"})
        assert r.status_code == 200


class TestDeleteLabel:
    def test_delete_custom_label(self, client):
        api, _, config = client
        api.post("/api/labels", json={"name": "security"})
        r = api.delete("/api/labels/security")
        assert r.status_code == 200
        assert r.json()["custom"] == []
        assert config.get().custom_labels == []

    def test_delete_default_rejected(self, client):
        api, _, _ = client
        r = api.delete("/api/labels/bug")
        assert r.status_code == 400
        assert "默认标签" in r.json()["detail"]
        r = api.delete("/api/labels/bot-done")
        assert r.status_code == 400

    def test_delete_missing_404(self, client):
        api, _, _ = client
        r = api.delete("/api/labels/nonexistent")
        assert r.status_code == 404


def test_default_labels_match_sync_script():
    """内置默认清单与 docs/labels.md / scripts/sync_labels.py 保持一致。"""
    import importlib.util
    from pathlib import Path
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "sync_labels.py"
    spec = importlib.util.spec_from_file_location("sync_labels", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    script_names = {l["name"] for l in mod.LABELS}
    default_names = {l["name"] for l in DEFAULT_LABELS}
    assert default_names == script_names
