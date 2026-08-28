"""AI 配置辅助 API 测试（issue #499）：设置页「获取模型」按钮的后端代理。

覆盖 POST /api/ai/list-models —— 通过 OpenAI 兼容 ``GET {base_url}/models``
获取供应商全部模型 id 列表（去重保序）。用 httpx.MockTransport 模拟
上游，不做真实外呼；断言请求构造、响应解析、错误映射与掩码 Key 匹配
已保存配置等行为。
"""

from types import SimpleNamespace

import httpx
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
ai_providers:
  - name: 生产 DeepSeek
    provider: deepseek
    base_url: https://api.deepseek.com/v1
    api_key: sk-saved-secret
    model: deepseek-chat
    enabled: true
    priority: 100
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    tc = TestClient(app)

    # 注入 MockTransport：默认返回 OpenAI 兼容 models 列表成功响应
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "object": "list",
            "data": [
                {"id": "deepseek-chat", "object": "model"},
                {"id": "deepseek-reasoner", "object": "model"},
                {"id": "deepseek-chat", "object": "model"},  # 重复项
            ],
        })

    from botler.api import ai as ai_module
    monkeypatch.setattr(
        ai_module, "_make_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    def set_handler(new_handler):
        monkeypatch.setattr(
            ai_module, "_make_http_client",
            lambda: httpx.Client(transport=httpx.MockTransport(new_handler)))

    tc._set_handler = set_handler
    tc._seen = seen
    return tc, tmp_path


def _models_handler(payload=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return handler


class TestFetchModelIdsUnit:
    """fetch_model_ids 纯函数：请求构造 / 响应解析 / 错误映射。"""

    def _make_client(self, handler):
        from botler.api.ai import fetch_model_ids
        http = httpx.Client(transport=httpx.MockTransport(handler))
        return fetch_model_ids, http

    def test_request_construction(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

        fn, http = self._make_client(handler)
        models = fn(base_url="https://api.example.com/v1", api_key="sk-abc", http=http)
        assert models == ["m1", "m2"]
        assert seen["url"] == "https://api.example.com/v1/models"
        assert seen["auth"] == "Bearer sk-abc"

    def test_base_url_already_ends_with_models(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"data": [{"id": "m1"}]})

        fn, http = self._make_client(handler)
        models = fn(base_url="https://api.example.com/v1/models", api_key="", http=http)
        assert models == ["m1"]
        assert seen["url"] == "https://api.example.com/v1/models"

    def test_no_api_key_no_auth_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"data": [{"id": "m1"}]})

        fn, http = self._make_client(handler)
        fn(base_url="http://localhost:11434/v1", api_key="", http=http)
        assert seen["auth"] is None

    def test_empty_list(self):
        fn, http = self._make_client(_models_handler({"data": []}))
        assert fn(base_url="https://api.example.com/v1", api_key="k", http=http) == []

    def test_deduplicate_preserve_order(self):
        fn, http = self._make_client(_models_handler({
            "data": [{"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": "c"}]}))
        assert fn(base_url="https://api.example.com/v1", api_key="k", http=http) == ["b", "a", "c"]

    def test_items_without_id_skipped(self):
        fn, http = self._make_client(_models_handler({
            "data": [{"id": "m1"}, {"foo": "bar"}, {"id": ""}, 42, None]}))
        assert fn(base_url="https://api.example.com/v1", api_key="k", http=http) == ["m1"]

    def test_non_2xx_raises(self):
        fn, http = self._make_client(_models_handler(
            {"error": {"message": "invalid key"}}, status=401))
        from botler.api.ai import ModelListError
        with pytest.raises(ModelListError, match="HTTP 401"):
            fn(base_url="https://api.example.com/v1", api_key="bad", http=http)

    def test_network_error_raises(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("boom")

        fn, http = self._make_client(handler)
        from botler.api.ai import ModelListError
        with pytest.raises(ModelListError) as excinfo:
            fn(base_url="https://api.example.com/v1", api_key="k", http=http)
        assert excinfo.value.is_network is True
        assert "网络" in str(excinfo.value)

    def test_invalid_json_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        fn, http = self._make_client(handler)
        from botler.api.ai import ModelListError
        with pytest.raises(ModelListError, match="合法 JSON"):
            fn(base_url="https://api.example.com/v1", api_key="k", http=http)

    def test_missing_data_field_raises(self):
        fn, http = self._make_client(_models_handler({"object": "list"}))
        from botler.api.ai import ModelListError
        with pytest.raises(ModelListError, match="data 模型列表"):
            fn(base_url="https://api.example.com/v1", api_key="k", http=http)

    def test_data_not_list_raises(self):
        fn, http = self._make_client(_models_handler({"data": "oops"}))
        from botler.api.ai import ModelListError
        with pytest.raises(ModelListError, match="data 模型列表"):
            fn(base_url="https://api.example.com/v1", api_key="k", http=http)


class TestListModelsApi:
    """POST /api/ai/list-models 端点：参数校验 / 掩码 Key 匹配 / 响应结构。"""

    def test_success_returns_models(self, client):
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-live",
            "name": "生产 DeepSeek",
        })
        assert resp.status_code == 200
        assert resp.json() == {"models": ["deepseek-chat", "deepseek-reasoner"]}
        assert tc._seen["url"] == "https://api.deepseek.com/v1/models"
        assert tc._seen["auth"] == "Bearer sk-live"

    def test_masked_key_matches_saved_config(self, client):
        """api_key 为掩码（含 *）时按 name 匹配已保存明文 key。"""
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-sa********cret",
            "name": "生产 DeepSeek",
        })
        assert resp.status_code == 200
        assert tc._seen["auth"] == "Bearer sk-saved-secret", \
            "掩码 Key 应回退为已保存配置的明文 Key"

    def test_empty_key_matches_saved_config(self, client):
        """api_key 留空时同样按 name 匹配已保存配置（编辑留空 = 保持现有）。"""
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "name": "生产 DeepSeek",
        })
        assert resp.status_code == 200
        assert tc._seen["auth"] == "Bearer sk-saved-secret"

    def test_empty_key_no_match_sends_no_auth(self, client):
        """新增供应商（未保存过）且 Key 留空 → 请求不带认证头（本地服务场景）。"""
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "name": "本地 Ollama",
        })
        assert resp.status_code == 200
        assert tc._seen["auth"] is None

    def test_missing_base_url_rejected(self, client):
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={"base_url": "", "api_key": "k"})
        assert resp.status_code == 400
        assert "http(s)://" in resp.json()["detail"]

    def test_invalid_base_url_rejected(self, client):
        tc, _ = client
        resp = tc.post("/api/ai/list-models", json={"base_url": "api.example.com/v1", "api_key": "k"})
        assert resp.status_code == 400

    def test_upstream_error_maps_to_400(self, client):
        tc, _ = client
        tc._set_handler(_models_handler(
            {"error": {"message": "invalid key"}}, status=401))
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1", "api_key": "bad"})
        assert resp.status_code == 400
        assert "HTTP 401" in resp.json()["detail"]

    def test_network_error_maps_to_502(self, client):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("boom")

        tc, _ = client
        tc._set_handler(handler)
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1", "api_key": "k"})
        assert resp.status_code == 502
        assert "网络" in resp.json()["detail"]

    def test_empty_models_list_ok(self, client):
        tc, _ = client
        tc._set_handler(_models_handler({"data": []}))
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1", "api_key": "k"})
        assert resp.status_code == 200
        assert resp.json() == {"models": []}

    def test_invalid_json_upstream_maps_to_400(self, client):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>oops</html>")

        tc, _ = client
        tc._set_handler(handler)
        resp = tc.post("/api/ai/list-models", json={
            "base_url": "https://api.deepseek.com/v1", "api_key": "k"})
        assert resp.status_code == 400
        assert "JSON" in resp.json()["detail"]
