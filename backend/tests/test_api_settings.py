"""系统设置 API 测试：browse 段（目录选择对话框默认初始定位目录）。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError

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


class TestImageModelsSettings:
    """image_models 段：识图模型配置（issue #135，设置页「识图模型」卡片）。

    与 ai_providers（issue #46）同模式：api_key 落盘 config.yaml、API 只
    返回掩码、编辑时留空或回传掩码值 = 保持现有；列表整体替换。
    """

    MODEL = {
        "name": "Gemini Nano Banana Pro",
        "provider": "gemini_nano_banana",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "AIza-test-123456",
        "model": "gemini-3-pro-image",
        "enabled": True,
    }

    def test_get_settings_includes_image_models_empty(self, client):
        """未配置时 image_models 返回空列表。"""
        tc, tmp_path = client
        data = tc.get("/api/settings").json()["image_models"]
        assert data == []

    def test_put_image_models_persists(self, client):
        """PUT image_models 写回 config.yaml 并可读回（api_key 只返回掩码）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [self.MODEL]})
        assert resp.status_code == 200
        models = resp.json()["image_models"]
        assert len(models) == 1
        assert models[0]["name"] == "Gemini Nano Banana Pro"
        assert models[0]["provider"] == "gemini_nano_banana"
        assert models[0]["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
        assert models[0]["model"] == "gemini-3-pro-image"
        assert models[0]["enabled"] is True
        masked = models[0]["api_key_masked"]
        assert "AIza-test-123456" not in masked  # 明文不回传
        assert "*" in masked  # 有掩码占位
        # config.yaml 是唯一事实来源，明文落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-test-123456" in config_text

    def test_put_masked_api_key_not_overwritten(self, client):
        """前端回传掩码值（含 *）不覆盖真实 key。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"image_models": [self.MODEL]})
        masked = tc.get("/api/settings").json()["image_models"][0]["api_key_masked"]
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "api_key": masked},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-test-123456" in config_text

    def test_put_blank_api_key_keeps_existing(self, client):
        """api_key 留空 = 保持现有（新增条目则存空串）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"image_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "api_key": ""},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-test-123456" in config_text

    def test_put_replaces_whole_list(self, client):
        """整体替换语义：新列表覆盖旧列表。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"image_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"image_models": [
            {"name": "GPT Image 2", "provider": "openai_gpt_image",
             "base_url": "https://api.openai.com/v1",
             "api_key": "sk-gpt-image-789", "model": "gpt-image-2",
             "enabled": False},
        ]})
        assert resp.status_code == 200
        models = resp.json()["image_models"]
        assert len(models) == 1
        assert models[0]["name"] == "GPT Image 2"
        assert models[0]["enabled"] is False

    def test_put_empty_list_clears(self, client):
        """空列表清空配置。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"image_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"image_models": []})
        assert resp.status_code == 200
        assert resp.json()["image_models"] == []
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "image_models" in config_text

    def test_put_rejects_blank_name(self, client):
        """name 必填非空。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "name": "   "},
        ]})
        assert resp.status_code == 400
        assert "name" in resp.json()["detail"]

    def test_put_rejects_duplicate_name(self, client):
        """name 唯一（掩码回传按 name 匹配旧值，重复会歧义）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [
            self.MODEL,
            {**self.MODEL, "model": "gemini-2.5-flash-image"},
        ]})
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_put_rejects_invalid_base_url(self, client):
        """base_url 必须以 http(s):// 开头。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "base_url": "not-a-url"},
        ]})
        assert resp.status_code == 400

    def test_put_rejects_non_list(self, client):
        """image_models 必须是数组。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": {"name": "x"}})
        assert resp.status_code == 400

    def test_put_provider_defaults_to_custom(self, client):
        """provider 缺省归一为 custom。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "provider": "  "},
        ]})
        assert resp.status_code == 200
        assert resp.json()["image_models"][0]["provider"] == "custom"

    def test_put_rejects_non_bool_enabled(self, client):
        """enabled 必须是布尔值。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"image_models": [
            {**self.MODEL, "enabled": "yes"},
        ]})
        assert resp.status_code == 400

    def test_env_ref_api_key_expanded_on_read(self, client):
        """config.yaml 中 api_key 支持 ${ENV} 引用（凭据不落明文）。"""
        tc, tmp_path = client
        config_path = tmp_path / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text + "image_models:\n"
            "  - name: GPT Image 2\n"
            "    provider: openai_gpt_image\n"
            "    base_url: https://api.openai.com/v1\n"
            "    api_key: ${BOTLER_TEST_GPT_IMAGE_KEY}\n"
            "    model: gpt-image-2\n"
            "    enabled: true\n",
            encoding="utf-8")
        import os
        os.environ["BOTLER_TEST_GPT_IMAGE_KEY"] = "sk-from-env"
        try:
            data = tc.get("/api/settings").json()["image_models"]
            assert data[0]["api_key_masked"].endswith("-env")
        finally:
            os.environ.pop("BOTLER_TEST_GPT_IMAGE_KEY", None)


class TestDshSettings:
    """dsh 段（issue #84）：GET 掩码返回 + PUT 更新与校验。"""

    def test_get_settings_includes_dsh_defaults(self, client):
        tc, _ = client
        data = tc.get("/api/settings").json()
        dsh = data["dsh"]
        assert dsh["provider"] == "deepseek-official"
        assert dsh["model"] == "deepseek-v4-flash"
        assert dsh["max_tokens"] is None
        assert dsh["reasoning_effort"] == ""
        assert dsh["session_root"] == ""
        assert dsh["api_key_masked"] == ""

    def test_update_dsh_persists(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"dsh": {
            "provider": "deepseek-official",
            "model": "deepseek-chat",
            "max_tokens": 8192,
            "reasoning_effort": "max",
            "session_root": "/var/dsh-sessions",
        }})
        assert resp.status_code == 200
        dsh = resp.json()["dsh"]
        assert dsh["model"] == "deepseek-chat"
        assert dsh["max_tokens"] == 8192
        assert dsh["reasoning_effort"] == "max"
        assert dsh["session_root"] == "/var/dsh-sessions"
        # 写回 config.yaml 生效（重读磁盘）
        config = ConfigManager(str(tmp_path / "config.yaml"))
        s = config.load()
        assert s.dsh_model == "deepseek-chat"
        assert s.dsh_max_tokens == 8192
        assert s.dsh_reasoning_effort == "max"

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

    def test_update_dsh_rejects_bad_reasoning_effort(self, client):
        """推理等级（issue #123）：白名单 off/high/max + 空串，其余拒绝。"""
        tc, _ = client
        for bad in ("low", "medium", "MAX", 123, None, ["max"]):
            resp = tc.put("/api/settings",
                          json={"dsh": {"reasoning_effort": bad}})
            assert resp.status_code == 400, f"reasoning_effort={bad!r} 应拒绝"
        # 白名单值 + 空串合法（空白串 strip 归一，避免空白字符串落盘）
        for ok in ("off", "high", "max", "", " high "):
            resp = tc.put("/api/settings",
                          json={"dsh": {"reasoning_effort": ok}})
            assert resp.status_code == 200, f"reasoning_effort={ok!r} 应接受"

    def test_update_dsh_reasoning_effort_blank_normalized(self, client):
        """空白串归一为空（不设置），不落盘空白字符（issue #123）。"""
        tc, _ = client
        resp = tc.put("/api/settings",
                      json={"dsh": {"reasoning_effort": "   "}})
        assert resp.status_code == 200
        assert resp.json()["dsh"]["reasoning_effort"] == ""

    def test_update_dsh_accepts_null_max_tokens(self, client):
        """max_tokens: null = 恢复 provider 默认。"""
        tc, _ = client
        tc.put("/api/settings", json={"dsh": {"max_tokens": 8192}})
        resp = tc.put("/api/settings", json={"dsh": {"max_tokens": None}})
        assert resp.status_code == 200
        assert resp.json()["dsh"]["max_tokens"] is None


class TestIssuePrioritySettings:
    """worker.issue_priority 段（issue #76）：issue 标签处理优先级顺序配置。"""

    def test_get_settings_includes_issue_priority_default(self, client):
        """未配置时返回默认顺序 bug > test > feature。"""
        tc, _ = client
        data = tc.get("/api/settings").json()
        assert data["worker"]["issue_priority"] == ["bug", "test", "feature"]

    def test_update_issue_priority_persists(self, client):
        """更新标签顺序并写回 config.yaml。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"worker": {
            "issue_priority": ["bug", "feature", "test"]}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["issue_priority"] == ["bug", "feature", "test"]
        # 写回 config.yaml 生效（重读磁盘）
        config = ConfigManager(str(tmp_path / "config.yaml"))
        s = config.load()
        assert s.issue_priority_labels == ["bug", "feature", "test"]

    def test_update_issue_priority_keeps_other_worker_fields(self, client):
        """部分更新：只提交 issue_priority 不影响 worker 其他字段。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"worker": {"max_concurrent_repos": 5}})
        resp = tc.put("/api/settings", json={"worker": {
            "issue_priority": ["feature", "bug"]}})
        assert resp.status_code == 200
        config = ConfigManager(str(tmp_path / "config.yaml"))
        s = config.load()
        assert s.max_concurrent_repos == 5
        assert s.issue_priority_labels == ["feature", "bug"]

    def test_update_issue_priority_rejects_non_list(self, client):
        tc, _ = client
        for bad in ("bug,test", 123, {"bug": 1}, None):
            resp = tc.put("/api/settings", json={"worker": {"issue_priority": bad}})
            assert resp.status_code == 400, f"issue_priority={bad} 应拒绝"

    def test_update_issue_priority_rejects_empty_list(self, client):
        """空列表拒绝：至少保留一个标签（全未命中 = 全部排最后，无意义）。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {"issue_priority": []}})
        assert resp.status_code == 400

    def test_update_issue_priority_rejects_non_string_item(self, client):
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {
            "issue_priority": ["bug", 123]}})
        assert resp.status_code == 400

    def test_update_issue_priority_rejects_invalid_label_name(self, client):
        """标签名须符合 GitLab 标签规则（字母/数字开头）。"""
        tc, _ = client
        for bad in ("", " ", "-bug", "bug label 中文", "a" * 61):
            resp = tc.put("/api/settings", json={"worker": {
                "issue_priority": [bad, "feature"]}})
            assert resp.status_code == 400, f"标签名 {bad!r} 应拒绝"

    def test_update_issue_priority_rejects_duplicate(self, client):
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {
            "issue_priority": ["bug", "test", "bug"]}})
        assert resp.status_code == 400


class TestEngineSettings:
    """worker.engine（issue #113）：设置页切换后端编写代码的 agent。

    引擎白名单 claude / hermes / dsh 与 executor._engine 对齐；
    非法值在 API 层直接 400 拒绝（executor 层回退仅防御手工改坏 config.yaml）。
    """

    def test_get_settings_includes_engine_default(self, client):
        """未配置时 GET 返回默认引擎 claude。"""
        tc, _ = client
        data = tc.get("/api/settings").json()
        assert data["worker"]["engine"] == "claude"

    def test_update_engine_dsh_persists(self, client):
        """PUT worker.engine=dsh 写回 config.yaml 并重读生效。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"worker": {"engine": "dsh"}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["engine"] == "dsh"
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "engine: dsh" in config_text
        s = ConfigManager(str(tmp_path / "config.yaml")).load()
        assert s.engine == "dsh"

    def test_update_engine_hermes_persists(self, client):
        """PUT worker.engine=hermes 成功。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {"engine": "hermes"}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["engine"] == "hermes"

    def test_update_engine_back_to_claude(self, client):
        """从其他引擎切回 claude 成功（重复切换场景）。"""
        tc, _ = client
        assert tc.put("/api/settings", json={"worker": {"engine": "dsh"}}).status_code == 200
        resp = tc.put("/api/settings", json={"worker": {"engine": "claude"}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["engine"] == "claude"

    def test_update_engine_normalizes_case_and_whitespace(self, client):
        """引擎名 strip + 小写归一（与 executor._engine 读取行为一致）。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {"engine": "  DSH "}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["engine"] == "dsh"

    def test_update_engine_rejects_unknown_value(self, client):
        """白名单外的引擎名 400 拒绝，不落盘。"""
        tc, tmp_path = client
        for bad in ("gpt", "openai", "cursor", "deepseek"):
            resp = tc.put("/api/settings", json={"worker": {"engine": bad}})
            assert resp.status_code == 400, f"引擎名 {bad!r} 应拒绝"
        assert "engine:" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_update_engine_rejects_non_string(self, client):
        """非字符串（数字/空值）400 拒绝。"""
        tc, _ = client
        for bad in (123, None, True, ["dsh"]):
            resp = tc.put("/api/settings", json={"worker": {"engine": bad}})
            assert resp.status_code == 400, f"引擎值 {bad!r} 应拒绝"

    def test_update_engine_rejects_empty_string(self, client):
        """空串/纯空白 400 拒绝（不允许清空引擎配置）。"""
        tc, _ = client
        for bad in ("", "   "):
            resp = tc.put("/api/settings", json={"worker": {"engine": bad}})
            assert resp.status_code == 400, f"引擎值 {bad!r} 应拒绝"

    def test_update_engine_partial_update_keeps_other_worker_fields(self, client):
        """只提交 engine 时 worker 其他字段不受影响（部分更新）。"""
        tc, tmp_path = client
        assert tc.put("/api/settings", json={"worker": {
            "max_concurrent_repos": 5, "engine": "dsh"}}).status_code == 200
        # 后续只提交 engine，不携带其他字段
        resp = tc.put("/api/settings", json={"worker": {"engine": "hermes"}})
        assert resp.status_code == 200
        worker = resp.json()["worker"]
        assert worker["engine"] == "hermes"
        assert worker["max_concurrent_repos"] == 5


class TestResumeTemplateSettings:
    """templates.resume 段（issue #116）：中断恢复提示词可编辑，与全局模版同机制。

    中断恢复引导语此前硬编码在 executor.py，用户无法修改；本组用例锁定
    settings API 的读写语义：未配置返回内置默认、PUT 落盘 config.yaml、
    清空恢复内置默认（中断恢复必须有引导语，不允许空模版）、非字符串 400。
    """

    def test_get_settings_includes_resume_default(self, client):
        """未配置时返回内置默认恢复提示词（前端展示可编辑基线）。"""
        tc, _ = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        resume = resp.json()["templates"]["resume"]
        assert "继续处理（中断恢复）" in resume
        assert "{issue_iid}" in resume and "{repo_name}" in resume

    def test_update_resume_persists(self, client):
        """PUT templates.resume 自定义文本写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        custom = "继续处理 {repo_name} 的 issue #{issue_iid}，从断点继续。"
        resp = tc.put("/api/settings", json={"templates": {"resume": custom}})
        assert resp.status_code == 200
        assert resp.json()["templates"]["resume"] == custom
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "resume:" in config_text and custom in config_text

    def test_update_resume_blank_restores_default(self, client):
        """清空/纯空白 = 移除自定义键，恢复内置默认（不允许空模版）。"""
        tc, tmp_path = client
        assert tc.put("/api/settings", json={
            "templates": {"resume": "自定义恢复提示"}}).status_code == 200
        resp = tc.put("/api/settings", json={"templates": {"resume": "   "}})
        assert resp.status_code == 200
        assert "继续处理（中断恢复）" in resp.json()["templates"]["resume"]
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "resume:" not in config_text

    def test_update_resume_rejects_non_string(self, client):
        """非字符串（数字/布尔/对象）400 拒绝。"""
        tc, _ = client
        for bad in (123, True, ["文本"], {"x": 1}):
            resp = tc.put("/api/settings", json={"templates": {"resume": bad}})
            assert resp.status_code == 400, f"取值 {bad!r} 应拒绝"
            assert "必须是字符串" in resp.json()["detail"]

    def test_update_resume_keeps_default_template(self, client):
        """单独更新 resume 不影响 templates.default（部分更新）。"""
        tc, _ = client
        before = tc.get("/api/settings").json()["templates"]["default"]
        assert tc.put("/api/settings", json={
            "templates": {"resume": "新恢复提示"}}).status_code == 200
        after = tc.get("/api/settings").json()["templates"]["default"]
        assert after == before


# ---- issue #133：Owner token 保存前校验（api scope） ----

class StubOwnerValidateClient:
    """Owner token 保存校验桩（issue #133）：按 token 值模拟
    /personal_access_tokens/self 响应，可注入无效/缺 scope/旧版 GitLab 等。"""

    def __init__(self, url, token, verify_ssl=True, webhook_base_url=None):
        self.url = url
        self.token = token

    def get_personal_access_token_self(self):
        if self.token == "invalid-token":
            raise GitLabError("token 无效或已过期（401）", 401)
        if self.token == "forbidden-self":
            raise GitLabError("权限不足（403）: 403 Forbidden", 403)
        if self.token == "no-self-endpoint":
            # 旧版 GitLab（< 15.7）无 self 端点 → 404，调用方降级校验
            raise GitLabError(
                "资源不存在（404）: /personal_access_tokens/self", 404)
        scopes = {
            "readonly-token": ["read_api", "read_user"],
            "no-scope-token": [],
        }.get(self.token, ["api", "read_api", "read_user"])
        return {"id": 1, "scopes": scopes}

    def test_connection(self):
        if self.token == "invalid-token":
            raise GitLabError("token 无效或已过期（401）", 401)
        return {"id": 1, "username": "owner"}


class TestOwnerTokenSaveValidation:
    """Owner token 保存前校验（issue #133）：token 必须有效且含 api scope。

    复现背景：用户配置 owner token 后概览页全部编辑报「owner token 失效
    （403）」。实测根因：token 只勾了 read_api 等只读 scope，缺 api
    scope——GitLab 对这类 token 提交写操作（评论/回复/添加 issue）返回
    403 insufficient_scope（响应体 {"error":"insufficient_scope",
    "error_description":"The request requires higher privileges..."}）。
    此前设置页保存时不做任何校验，不可用的 token 直接落盘，用户反复
    重新保存仍 403。修复：保存真实 token 时先调
    /personal_access_tokens/self 校验有效性 + api scope，不满足直接
    400 拒绝且不落盘，并给出明确指引。
    """

    def test_save_without_api_scope_rejected(self, client, monkeypatch):
        """token 只含 read_api（无 api scope）：400 拒绝并明确提示勾选
        api，且不得落盘（复现用例：issue #133 用户配置的正是这种 token）。"""
        from botler.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, "GitLabClient",
                            StubOwnerValidateClient)
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={
            "gitlab": {"owner_token": "readonly-token"}})
        assert resp.status_code == 400, resp.text
        assert "api scope" in resp.json()["detail"]
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "owner_token" not in config_text, "校验失败不得落盘"

    def test_save_valid_token_with_api_scope_ok(self, client, monkeypatch):
        """token 有效且含 api scope：正常保存落盘。"""
        from botler.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, "GitLabClient",
                            StubOwnerValidateClient)
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={
            "gitlab": {"owner_token": "valid-token"}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["gitlab"]["owner_token_masked"] != ""
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "owner_token: valid-token" in config_text

    def test_save_invalid_token_rejected(self, client, monkeypatch):
        """token 无效/已过期（401）：400 拒绝并提示重新生成，不落盘。"""
        from botler.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, "GitLabClient",
                            StubOwnerValidateClient)
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={
            "gitlab": {"owner_token": "invalid-token"}})
        assert resp.status_code == 400, resp.text
        assert "无效或已过期" in resp.json()["detail"]
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "owner_token" not in config_text

    def test_save_blank_or_masked_keeps_existing(self, client, monkeypatch):
        """空串/掩码值 = 保持现有凭据：跳过校验也不覆盖（与既有语义一致）。"""
        from botler.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, "GitLabClient",
                            StubOwnerValidateClient)
        tc, tmp_path = client
        # 先保存一个有效 token
        assert tc.put("/api/settings", json={
            "gitlab": {"owner_token": "valid-token"}}).status_code == 200
        before = tc.get("/api/settings").json()["gitlab"]["owner_token_masked"]
        # 掩码值保存 → 保持现有
        resp = tc.put("/api/settings", json={
            "gitlab": {"owner_token": "glpa****xxxx"}})
        assert resp.status_code == 200, resp.text
        # 空串保存 → 保持现有
        resp = tc.put("/api/settings", json={"gitlab": {"owner_token": "  "}})
        assert resp.status_code == 200, resp.text
        after = tc.get("/api/settings").json()["gitlab"]["owner_token_masked"]
        assert after == before
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "owner_token: valid-token" in config_text

    def test_save_old_gitlab_fallback_validity(self, client, monkeypatch):
        """旧版 GitLab 无 self 端点（404）：降级只校验 token 有效性，
        有效则放行（无法查 scope 时不过度拦截）。"""
        from botler.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, "GitLabClient",
                            StubOwnerValidateClient)
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={
            "gitlab": {"owner_token": "no-self-endpoint"}})
        assert resp.status_code == 200, resp.text
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "owner_token: no-self-endpoint" in config_text
