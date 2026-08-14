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


class TestSsoSettings:
    """sso 段：Synology SSO 登录配置（issue #27）。"""

    def test_get_settings_includes_sso_defaults(self, client):
        """未配置时 sso 段返回默认值，client_secret 只返回掩码。"""
        tc, tmp_path = client
        data = tc.get("/api/settings").json()["sso"]
        assert data["enabled"] is False
        assert data["well_known_url"] == ""
        assert data["client_id"] == ""
        assert data["client_secret_masked"] == ""
        assert data["session_days"] == 30  # 默认 30 天（issue #27 第三轮用户确认）

    def test_update_sso_persists(self, client):
        """PUT sso 段写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"sso": {
            "enabled": True,
            "well_known_url": "https://nas.example.com/.well-known/openid-configuration",
            "client_id": "app-123",
            "client_secret": "secret-abc",
            "session_days": 14,
        }})
        assert resp.status_code == 200
        sso = resp.json()["sso"]
        assert sso["enabled"] is True
        assert sso["client_secret_masked"] != "secret-abc"
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "client_secret: secret-abc" in config_text

    def test_update_sso_masked_secret_not_overwritten(self, client):
        """前端回传掩码值（含 *）不覆盖真实 secret。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"sso": {"client_secret": "secret-abc"}})
        resp = tc.put("/api/settings", json={"sso": {"client_secret": "secr****c"}})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "client_secret: secret-abc" in config_text

    def test_update_sso_rejects_invalid_url(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"sso": {"well_known_url": "not-a-url"}})
        assert resp.status_code == 400

    def test_update_sso_rejects_bad_session_days(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"sso": {"session_days": 0}})
        assert resp.status_code == 400

    def test_update_sso_enable_requires_key_fields(self, client):
        """启用 SSO 时 well_known_url / client_id / client_secret 必填。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"sso": {"enabled": True}})
        assert resp.status_code == 400
        assert "client_id" in resp.json()["detail"]


class TestAiProvidersSettings:
    """ai_providers 段：AI API 供应商配置（issue #46，设置页增删改查供应商）。

    与 sso.client_secret 同模式：api_key 落盘 config.yaml、API 只返回掩码、
    编辑时留空或回传掩码值 = 保持现有。列表整体替换（与 repos/custom_labels 一致）。
    """

    PROVIDER = {
        "name": "DeepSeek",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-deepseek-123456",
        "model": "deepseek-chat",
        "enabled": True,
    }

    def test_get_settings_includes_ai_providers_empty(self, client):
        """未配置时 ai_providers 返回空列表。"""
        tc, tmp_path = client
        data = tc.get("/api/settings").json()["ai_providers"]
        assert data == []

    def test_put_ai_providers_persists(self, client):
        """PUT ai_providers 写回 config.yaml 并可读回（api_key 只返回掩码）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ai_providers": [self.PROVIDER]})
        assert resp.status_code == 200
        providers = resp.json()["ai_providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "DeepSeek"
        assert providers[0]["provider"] == "deepseek"
        assert providers[0]["base_url"] == "https://api.deepseek.com/v1"
        assert providers[0]["model"] == "deepseek-chat"
        assert providers[0]["enabled"] is True
        masked = providers[0]["api_key_masked"]
        assert "sk-deepseek-123456" not in masked  # 明文不回传
        assert "*" in masked  # 有掩码占位
        # config.yaml 是唯一事实来源，明文落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: sk-deepseek-123456" in config_text

    def test_put_masked_api_key_not_overwritten(self, client):
        """前端回传掩码值（含 *）不覆盖真实 key。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ai_providers": [self.PROVIDER]})
        masked = tc.get("/api/settings").json()["ai_providers"][0]["api_key_masked"]
        resp = tc.put("/api/settings", json={"ai_providers": [
            {**self.PROVIDER, "api_key": masked},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: sk-deepseek-123456" in config_text

    def test_put_blank_api_key_keeps_existing(self, client):
        """api_key 留空 = 保持现有（新增条目则存空串）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ai_providers": [self.PROVIDER]})
        resp = tc.put("/api/settings", json={"ai_providers": [
            {**self.PROVIDER, "api_key": ""},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: sk-deepseek-123456" in config_text

    def test_put_replaces_whole_list(self, client):
        """整体替换语义：新列表覆盖旧列表。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ai_providers": [self.PROVIDER]})
        resp = tc.put("/api/settings", json={"ai_providers": [
            {"name": "OpenAI", "provider": "openai",
             "base_url": "https://api.openai.com/v1",
             "api_key": "sk-openai-789", "model": "gpt-4o", "enabled": False},
        ]})
        assert resp.status_code == 200
        providers = resp.json()["ai_providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "OpenAI"
        assert providers[0]["enabled"] is False

    def test_put_empty_list_clears(self, client):
        """空列表清空配置。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ai_providers": [self.PROVIDER]})
        resp = tc.put("/api/settings", json={"ai_providers": []})
        assert resp.status_code == 200
        assert resp.json()["ai_providers"] == []
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "ai_providers" in config_text

    def test_put_rejects_blank_name(self, client):
        """name 必填非空。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ai_providers": [
            {**self.PROVIDER, "name": "   "},
        ]})
        assert resp.status_code == 400
        assert "name" in resp.json()["detail"]

    def test_put_rejects_duplicate_name(self, client):
        """name 唯一（掩码回传按 name 匹配旧值，重复会歧义）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ai_providers": [
            self.PROVIDER,
            {**self.PROVIDER, "model": "deepseek-reasoner"},
        ]})
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_put_rejects_invalid_base_url(self, client):
        """base_url 必须以 http(s):// 开头。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ai_providers": [
            {**self.PROVIDER, "base_url": "not-a-url"},
        ]})
        assert resp.status_code == 400

    def test_put_rejects_non_list(self, client):
        """ai_providers 必须是数组。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ai_providers": {"name": "x"}})
        assert resp.status_code == 400

    def test_env_ref_api_key_expanded_on_read(self, client):
        """config.yaml 中 api_key 支持 ${ENV} 引用（凭据不落明文，config.py 已有能力）。"""
        tc, tmp_path = client
        config_path = tmp_path / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text + "ai_providers:\n"
            "  - name: DeepSeek\n"
            "    provider: deepseek\n"
            "    base_url: https://api.deepseek.com/v1\n"
            "    api_key: ${BOTLER_TEST_DEEPSEEK_KEY}\n"
            "    model: deepseek-chat\n"
            "    enabled: true\n",
            encoding="utf-8")
        import os
        os.environ["BOTLER_TEST_DEEPSEEK_KEY"] = "sk-from-env"
        try:
            data = tc.get("/api/settings").json()["ai_providers"]
            assert data[0]["api_key_masked"].endswith("-env")
        finally:
            os.environ.pop("BOTLER_TEST_DEEPSEEK_KEY", None)


class TestDshSettings:
    """dsh 段（issue #84）：GET 掩码返回 + PUT 更新与校验。"""

    def test_get_settings_includes_dsh_defaults(self, client):
        tc, _ = client
        data = tc.get("/api/settings").json()
        dsh = data["dsh"]
        assert dsh["provider"] == "deepseek-official"
        assert dsh["model"] == "deepseek-v4-flash"
        assert dsh["max_tokens"] is None
        assert dsh["session_root"] == ""
        assert dsh["api_key_masked"] == ""

    def test_update_dsh_persists(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"dsh": {
            "provider": "deepseek-official",
            "model": "deepseek-chat",
            "max_tokens": 8192,
            "session_root": "/var/dsh-sessions",
        }})
        assert resp.status_code == 200
        dsh = resp.json()["dsh"]
        assert dsh["model"] == "deepseek-chat"
        assert dsh["max_tokens"] == 8192
        assert dsh["session_root"] == "/var/dsh-sessions"
        # 写回 config.yaml 生效（重读磁盘）
        config = ConfigManager(str(tmp_path / "config.yaml"))
        s = config.load()
        assert s.dsh_model == "deepseek-chat"
        assert s.dsh_max_tokens == 8192

    def test_update_dsh_api_key_masked(self, client):
        """api_key 明文写入；GET 只返回掩码。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"dsh": {"api_key": "sk-secret"}})
        assert resp.status_code == 200
        dsh = resp.json()["dsh"]
        assert "*" in dsh["api_key_masked"]  # 有掩码占位
        assert "sk-secret" not in str(resp.json())  # 明文不回流

    def test_update_dsh_masked_key_not_overwritten(self, client):
        """回传掩码值（含 *）视为未修改，保留现有凭据（同 sso 模式）。"""
        tc, _ = client
        tc.put("/api/settings", json={"dsh": {"api_key": "sk-real"}})
        before = tc.get("/api/settings").json()["dsh"]["api_key_masked"]
        resp = tc.put("/api/settings", json={"dsh": {"api_key": "sk-****"}})
        assert resp.status_code == 200
        after = resp.json()["dsh"]["api_key_masked"]
        assert after == before  # 掩码回传不覆盖，凭据未变

    def test_update_dsh_blank_api_key_keeps_existing(self, client):
        tc, _ = client
        tc.put("/api/settings", json={"dsh": {"api_key": "sk-real"}})
        before = tc.get("/api/settings").json()["dsh"]["api_key_masked"]
        resp = tc.put("/api/settings", json={"dsh": {"api_key": ""}})
        assert resp.json()["dsh"]["api_key_masked"] == before

    def test_update_dsh_rejects_non_string(self, client):
        tc, _ = client
        resp = tc.put("/api/settings", json={"dsh": {"model": 123}})
        assert resp.status_code == 400
        assert "dsh.model 必须是字符串" in resp.json()["detail"]

    def test_update_dsh_rejects_bad_max_tokens(self, client):
        tc, _ = client
        for bad in (0, -1, "8192", 1.5):
            resp = tc.put("/api/settings", json={"dsh": {"max_tokens": bad}})
            assert resp.status_code == 400, f"max_tokens={bad} 应拒绝"

    def test_update_dsh_accepts_null_max_tokens(self, client):
        """max_tokens: null = 恢复 provider 默认。"""
        tc, _ = client
        tc.put("/api/settings", json={"dsh": {"max_tokens": 8192}})
        resp = tc.put("/api/settings", json={"dsh": {"max_tokens": None}})
        assert resp.status_code == 200
        assert resp.json()["dsh"]["max_tokens"] is None
