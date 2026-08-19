"""MCP 工具管理 API 测试（issue #172）：工具页面的后端接口。

覆盖：
- GET /api/tools：工具列表 + 内置市场清单 + 已保存的市场索引地址；
- POST /api/tools：创建自定义工具（成功 / 校验失败 400 / 重名 400）；
- PUT /api/tools/{id}：更新（部分字段 / 不存在 404 / 校验失败 400）；
- DELETE /api/tools/{id}：删除（成功 / 不存在 404）；
- POST /api/tools/install：内置市场安装（成功 / 未知 400 / 重复 400）；
- POST /api/tools/import：URL 导入（JSON 文件成功 / 非 http 400）；
- POST /api/tools/market-index：远端市场索引拉取（成功保存地址 /
  非法格式 400）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    yield TestClient(app), tmp_path


def create_payload(**overrides):
    payload = {
        "name": "custom-tool",
        "description": "自定义工具",
        "kind": "stdio",
        "command": "python3",
        "args": ["-m", "demo"],
        "env": {"TOKEN": "abc"},
    }
    payload.update(overrides)
    return payload


class TestListTools:
    def test_empty(self, client):
        tc, _ = client
        resp = tc.get("/api/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tools"] == []
        assert data["market_index_url"] == ""
        assert len(data["market"]) >= 3

    def test_with_tools_and_index_url(self, client):
        tc, tmp = client
        tc.post("/api/tools", json=create_payload())
        tc.post("/api/tools/market-index",
                json={"url": "https://index.example/tools.json"}) \
            if False else None
        # 直接写 meta 验证返回
        tc.app.state.ctx.db.set_tool_meta("market_index_url",
                                          "https://idx.example/list.json")
        resp = tc.get("/api/tools")
        data = resp.json()
        assert [t["name"] for t in data["tools"]] == ["custom-tool"]
        assert data["market_index_url"] == "https://idx.example/list.json"


class TestCreateTool:
    def test_create_ok(self, client):
        tc, _ = client
        resp = tc.post("/api/tools", json=create_payload())
        assert resp.status_code == 200
        tool = resp.json()
        assert tool["name"] == "custom-tool"
        assert tool["source"] == "custom"
        assert tool["enabled"] is True

    def test_create_invalid(self, client):
        tc, _ = client
        resp = tc.post("/api/tools", json=create_payload(kind="webrpc"))
        assert resp.status_code == 400
        assert "类型" in resp.json()["detail"]

    def test_create_empty_name(self, client):
        tc, _ = client
        resp = tc.post("/api/tools", json=create_payload(name=""))
        assert resp.status_code == 400
        assert "不能为空" in resp.json()["detail"]

    def test_create_duplicate(self, client):
        tc, _ = client
        tc.post("/api/tools", json=create_payload())
        resp = tc.post("/api/tools", json=create_payload())
        assert resp.status_code == 400
        assert "已存在" in resp.json()["detail"]


class TestUpdateTool:
    def test_update_ok(self, client):
        tc, _ = client
        tool = tc.post("/api/tools", json=create_payload()).json()
        resp = tc.put(f"/api/tools/{tool['id']}",
                      json={"description": "更新后", "enabled": False})
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["description"] == "更新后"
        assert updated["enabled"] is False
        assert updated["command"] == "python3"

    def test_update_missing(self, client):
        tc, _ = client
        resp = tc.put("/api/tools/999", json={"description": "x"})
        assert resp.status_code == 404

    def test_update_invalid(self, client):
        tc, _ = client
        tool = tc.post("/api/tools", json=create_payload()).json()
        resp = tc.put(f"/api/tools/{tool['id']}", json={"command": ""})
        assert resp.status_code == 400
        assert "command" in resp.json()["detail"]

    def test_update_empty_patch(self, client):
        tc, _ = client
        tool = tc.post("/api/tools", json=create_payload()).json()
        resp = tc.put(f"/api/tools/{tool['id']}", json={})
        assert resp.status_code == 400


class TestDeleteTool:
    def test_delete_ok(self, client):
        tc, _ = client
        tool = tc.post("/api/tools", json=create_payload()).json()
        resp = tc.delete(f"/api/tools/{tool['id']}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert tc.get("/api/tools").json()["tools"] == []

    def test_delete_missing(self, client):
        tc, _ = client
        assert tc.delete("/api/tools/999").status_code == 404


class TestInstallBuiltin:
    def test_install_ok(self, client):
        tc, _ = client
        resp = tc.post("/api/tools/install", json={"name": "web-fetch"})
        assert resp.status_code == 200
        tool = resp.json()
        assert tool["name"] == "web-fetch"
        assert tool["source"] == "builtin"
        assert tool["command"] == "npx"

    def test_install_unknown(self, client):
        tc, _ = client
        resp = tc.post("/api/tools/install", json={"name": "nope"})
        assert resp.status_code == 400

    def test_install_duplicate(self, client):
        tc, _ = client
        tc.post("/api/tools/install", json={"name": "web-fetch"})
        resp = tc.post("/api/tools/install", json={"name": "web-fetch"})
        assert resp.status_code == 400
        assert "已安装" in resp.json()["detail"]


# ---- URL 导入 / 市场索引（本地 HTTP server）----

class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        body = route.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class TestImport:
    def test_import_json_file(self, client, http_server):
        _Handler.routes["/tool.json"] = json.dumps({
            "mcpServers": {"imp-a": {"command": "python3"}},
        })
        resp = tc_post(client, "/api/tools/import",
                       {"url": f"{http_server}/tool.json"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["imported"][0]["name"] == "imp-a"
        assert data["imported"][0]["source"] == "url"

    def test_import_bad_url(self, client):
        resp = tc_post(client, "/api/tools/import", {"url": "ftp://x/y.json"})
        assert resp.status_code == 400
        assert "http" in resp.json()["detail"]

    def test_import_404(self, client, http_server):
        resp = tc_post(client, "/api/tools/import",
                       {"url": f"{http_server}/missing.json"})
        assert resp.status_code == 400


class TestMarketIndexApi:
    def test_market_index_ok(self, client, http_server):
        _Handler.routes["/idx.json"] = json.dumps({
            "tools": [{"name": "mkt-a", "command": "python3"}],
        })
        resp = tc_post(client, "/api/tools/market-index",
                       {"url": f"{http_server}/idx.json"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["candidates"][0]["name"] == "mkt-a"
        # 索引地址已保存
        assert tc_get(client, "/api/tools").json()["market_index_url"] \
            == f"{http_server}/idx.json"

    def test_market_index_bad_format(self, client, http_server):
        _Handler.routes["/bad.json"] = json.dumps({"foo": 1})
        resp = tc_post(client, "/api/tools/market-index",
                       {"url": f"{http_server}/bad.json"})
        assert resp.status_code == 400


def tc_post(client, path, payload):
    tc, _ = client
    return tc.post(path, json=payload)


def tc_get(client, path):
    tc, _ = client
    return tc.get(path)
