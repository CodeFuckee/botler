"""系统设置 API 测试：browse 段（目录选择对话框默认初始定位目录）。"""

from types import SimpleNamespace

import pytest
import base64
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError
from botler.image_models import ImageModelError
from botler.vision_models import VisionModelError

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




class TestShowDisabledReposSettings:
    """ui.show_disabled_repos 段：灵感 / CI/CD 页面是否显示未启用项目（issue #142）。"""

    def test_get_settings_show_disabled_repos_default_true(self, client):
        """未配置时默认 true（保持现状：显示未启用项目）。"""
        tc, tmp_path = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["ui"]["show_disabled_repos"] is True

    def test_update_show_disabled_repos_false_persists(self, client):
        """PUT ui.show_disabled_repos=false 写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ui": {"show_disabled_repos": False}})
        assert resp.status_code == 200
        assert resp.json()["ui"]["show_disabled_repos"] is False
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "show_disabled_repos: false" in config_text

    def test_update_show_disabled_repos_back_to_true(self, client):
        """关闭后重新开启：true 正常写回。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ui": {"show_disabled_repos": False}})
        resp = tc.put("/api/settings", json={"ui": {"show_disabled_repos": True}})
        assert resp.status_code == 200
        assert resp.json()["ui"]["show_disabled_repos"] is True

    def test_update_show_disabled_repos_rejects_non_bool(self, client):
        """非布尔值拒绝保存（400）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"ui": {"show_disabled_repos": "yes"}})
        assert resp.status_code == 400
        assert "show_disabled_repos" in resp.json()["detail"]

    def test_partial_update_preserves_timezone(self, client):
        """部分更新：只改 show_disabled_repos 不影响 timezone。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"ui": {"timezone": "Asia/Shanghai"}})
        resp = tc.put("/api/settings", json={"ui": {"show_disabled_repos": False}})
        assert resp.status_code == 200
        ui = resp.json()["ui"]
        assert ui["show_disabled_repos"] is False
        assert ui["timezone"] == "Asia/Shanghai"


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
    """image_models 段：生图模型配置（issue #135，设置页「生图模型」卡片）。

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


class TestImageModelTestEndpoint:
    """POST /api/settings/image-model-test：生图模型测试按钮（issue #137）。

    用提交的表单值（生图模式 provider / base_url / api_key / model）真实
    调用一次生图接口验证配置可用；api_key 掩码/留空、url/model 留空按
    name 回退已保存配置。生图成功 ok=true，失败 ok=false + 原因（不抛
    500，与 webhook-test 同容错策略）。
    """

    MODEL = TestImageModelsSettings.MODEL

    def _save(self, tc):
        tc.put("/api/settings", json={"image_models": [self.MODEL]})

    def _patch_client(self, monkeypatch, fake):
        """monkeypatch botler.image_models.ImageModelClient（端点函数内
        from ..image_models import 会在调用时取模块属性，patch 生效）。"""
        from botler import image_models as im_mod
        monkeypatch.setattr(im_mod, "ImageModelClient", fake)

    def test_test_missing_provider(self, client):
        """未选择生图模式（provider）直接 ok=false，不发请求。"""
        tc, _ = client
        resp = tc.post("/api/settings/image-model-test", json={"name": "x"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "生图模式" in data["error"]

    def test_test_ok_with_full_config(self, client, monkeypatch):
        """提交完整配置：ok=true + 生成张数/mime，客户端收到正确入参。"""
        tc, _ = client
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                generate=lambda prompt: [SimpleNamespace(
                    mime_type="image/png", data=b"\x89PNG-test")])

        self._patch_client(monkeypatch, fake_client)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "Gemini 生产", "provider": "gemini_nano_banana",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "AIza-test", "model": "gemini-3-pro-image",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["images"] == 1
        assert data["mime_type"] == "image/png"
        assert data["image_base64"] == "iVBORy10ZXN0"  # b"\x89PNG-test" 的 base64
        assert captured["provider"] == "gemini_nano_banana"
        assert captured["api_key"] == "AIza-test"
        assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
        assert captured["model"] == "gemini-3-pro-image"

    def test_test_ok_returns_image_base64(self, client, monkeypatch):
        """生图成功：返回首张图片 base64 + mime，前端可展示生成图片。"""
        tc, _ = client

        def fake_client(**kwargs):
            return SimpleNamespace(
                generate=lambda prompt: [SimpleNamespace(
                    mime_type="image/png", data=b"\x89PNG-test")])

        self._patch_client(monkeypatch, fake_client)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "Gemini 生产", "provider": "gemini_nano_banana",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "AIza-test", "model": "gemini-3-pro-image",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["image_base64"] == "iVBORy10ZXN0"
        assert data["mime_type"] == "image/png"

    def test_test_masked_key_falls_back_to_saved(self, client, monkeypatch):
        """掩码/留空的 api_key、url、model 按 name 回退已保存配置。"""
        tc, _ = client
        self._save(tc)
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                generate=lambda prompt: [SimpleNamespace(
                    mime_type="image/png", data=b"x")])

        self._patch_client(monkeypatch, fake_client)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "Gemini Nano Banana Pro",
            "provider": "gemini_nano_banana",
            "api_key": "AIza-****", "base_url": "", "model": "",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 回退到已保存的明文 key / url / model
        assert captured["api_key"] == "AIza-test-123456"
        assert captured["base_url"] == self.MODEL["base_url"]
        assert captured["model"] == "gemini-3-pro-image"

    def test_test_row_button_uses_saved_only(self, client, monkeypatch):
        """列表行「测试」只提交 name+provider：完全按已保存配置测试。"""
        tc, _ = client
        self._save(tc)
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                generate=lambda prompt: [SimpleNamespace(
                    mime_type="image/png", data=b"x")])

        self._patch_client(monkeypatch, fake_client)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "Gemini Nano Banana Pro",
            "provider": "gemini_nano_banana",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert captured["api_key"] == "AIza-test-123456"
        assert captured["base_url"] == self.MODEL["base_url"]
        assert captured["model"] == "gemini-3-pro-image"

    def test_test_generate_error_ok_false(self, client, monkeypatch):
        """生图接口报错（如 401）：ok=false + 错误信息，不抛 500。"""
        tc, _ = client

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def generate(self, prompt):
                raise ImageModelError("Gemini 请求失败: HTTP 401 invalid key")

        self._patch_client(monkeypatch, FakeClient)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "gemini_nano_banana",
            "api_key": "k", "base_url": "https://example.com/v1", "model": "m",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "401" in data["error"]

    def test_test_unknown_provider_ok_false(self, client, monkeypatch):
        """未知 provider 构造客户端失败：ok=false + 原因。"""
        tc, _ = client

        class FakeClient:
            def __init__(self, **kwargs):
                raise ImageModelError("不支持的生成模型类型: nope")

        self._patch_client(monkeypatch, FakeClient)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "nope", "api_key": "k",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "nope" in data["error"]

    def test_test_verify_ssl_follows_settings(self, client, monkeypatch):
        """verify_ssl 跟随全局设置（测试配置 verify_ssl: false）。"""
        tc, _ = client
        captured = {}

        def fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(generate=lambda prompt: [])

        self._patch_client(monkeypatch, fake_client)
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "gemini_nano_banana", "api_key": "k",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False  # 空结果 → 未返回图片数据
        assert captured["verify_ssl"] is False


    def test_test_json_decode_error_reports_response_detail(
            self, client, monkeypatch):
        """200 + 空 body（网关/代理常见）：错误信息带状态码/响应片段/
        请求地址，不再裸抛「Expecting value」（issue #151 复现路径）。

        修复前 resp.json() 抛 json.JSONDecodeError，被兜底 except 捕获
        显示成「生图测试失败: Expecting value: line 1 column 1 (char 0)」，
        用户无法定位；修复后应转为可诊断的 ImageModelError 文案。
        """
        from botler import image_models as im_mod

        class FakeClient(im_mod.ImageModelClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(200)  # 空响应体

                self._http = httpx.Client(
                    transport=httpx.MockTransport(handler))

        monkeypatch.setattr(im_mod, "ImageModelClient", FakeClient)
        tc, _ = client
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "gemini_nano_banana",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "k", "model": "gemini-3-pro-image",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "不是有效 JSON" in data["error"]
        assert "空响应体" in data["error"]
        assert "Expecting value" not in data["error"]

    def test_test_json_decode_error_openai_shows_raw_content(
            self, client, monkeypatch):
        """OpenAI 接口返回 200 + 非 JSON 内容（issue #151 后续反馈）：
        POST /api/settings/image-model-test 的错误信息直接完整展示接口
        原始返回内容，用户可直接看到接口返回了什么（不截断、不包裹
        冗长诊断提示）。"""
        from botler import image_models as im_mod

        raw = "gateway error: " + "y" * 300  # 超过旧实现 200 字符截断上限

        class FakeClient(im_mod.ImageModelClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200, text=raw,
                        headers={"Content-Type": "text/plain"})

                self._http = httpx.Client(
                    transport=httpx.MockTransport(handler))

        monkeypatch.setattr(im_mod, "ImageModelClient", FakeClient)
        tc, _ = client
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "openai_gpt_image",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test", "model": "gpt-image-2",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "不是有效 JSON" in data["error"]
        assert raw in data["error"]  # 完整原始内容直接展示
        assert "Expecting value" not in data["error"]

    def test_test_sse_openai_returns_image_base64(self, client, monkeypatch):
        """OpenAI 接口返回 SSE（text/event-stream）流式响应（issue #151
        用户反馈）：POST /api/settings/image-model-test 应解析 data 事件、
        下载 results 中的图片 URL，ok=true 并回传图片 base64（不再把
        SSE 流当普通 JSON 解析失败展示原始内容）。"""
        from botler import image_models as im_mod

        img_url = "https://file7.aitohumanize.com/file/0c593022c7fe4ec2a43515a91cade7a6.png"
        sse_text = (
            'data: {"id":"t1","progress":1,"status":"running","results":null,'
            '"error":"","failure_reason":""}\n'
            'data: {"id":"t1","progress":100,"status":"succeeded","results":'
            '[{"url":"' + img_url + '","width":0,"height":0}],'
            '"error":"","failure_reason":""}\n'
        )

        class FakeClient(im_mod.ImageModelClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.method == "POST":
                        return httpx.Response(
                            200, text=sse_text,
                            headers={"Content-Type": "text/event-stream"})
                    # GET：下载生成图片
                    assert str(request.url) == img_url
                    return httpx.Response(
                        200, content=b"\x89PNG-sse-api",
                        headers={"Content-Type": "image/png"})

                self._http = httpx.Client(
                    transport=httpx.MockTransport(handler))

        monkeypatch.setattr(im_mod, "ImageModelClient", FakeClient)
        tc, _ = client
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "openai_gpt_image",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test", "model": "gpt-image-2",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["images"] == 1
        assert data["mime_type"] == "image/png"
        assert data["image_base64"] == base64.b64encode(
            b"\x89PNG-sse-api").decode("ascii")

    def test_test_sse_openai_failed_reports_reason(self, client, monkeypatch):
        """OpenAI SSE 流最终 status=failed：POST /api/settings/
        image-model-test 错误信息包含失败原因（failure_reason / error），
        而非整段原始流内容。"""
        from botler import image_models as im_mod

        sse_text = (
            'data: {"id":"t1","progress":10,"status":"running","results":null,'
            '"error":"","failure_reason":""}\n'
            'data: {"id":"t1","progress":100,"status":"failed",'
            '"failure_reason":"违规内容拦截","error":"blocked",'
            '"results":null}\n'
        )

        class FakeClient(im_mod.ImageModelClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200, text=sse_text,
                        headers={"Content-Type": "text/event-stream"})

                self._http = httpx.Client(
                    transport=httpx.MockTransport(handler))

        monkeypatch.setattr(im_mod, "ImageModelClient", FakeClient)
        tc, _ = client
        resp = tc.post("/api/settings/image-model-test", json={
            "name": "x", "provider": "openai_gpt_image",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test", "model": "gpt-image-2",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "生图任务失败" in data["error"]
        assert "违规内容拦截" in data["error"]
        assert "Expecting value" not in data["error"]



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


class TestPluginPathsSettings:
    """worker.plugin_paths（issue #140）：外部插件模块路径列表配置。

    插件体系扩展点：新增执行引擎 / 大模型供应商 / 消息发送通道可经
    config.yaml 的 worker.plugin_paths 声明模块路径，启动时加载注册。
    """

    def test_get_settings_includes_plugin_paths(self, client):
        """GET 返回 plugin_paths（默认空列表）。"""
        tc, _ = client
        data = tc.get("/api/settings").json()
        assert data["worker"]["plugin_paths"] == []

    def test_update_plugin_paths_persists(self, client):
        """PUT plugin_paths 写回 config.yaml 并重读生效。"""
        tc, tmp_path = client
        paths = ["/opt/botler-plugins/my_engine.py",
                 "/opt/botler-plugins/feishu_channel.py"]
        resp = tc.put("/api/settings", json={"worker": {"plugin_paths": paths}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["plugin_paths"] == paths
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "plugin_paths" in config_text
        assert "/opt/botler-plugins/my_engine.py" in config_text
        s = ConfigManager(str(tmp_path / "config.yaml")).load()
        assert s.plugin_paths == paths

    def test_update_plugin_paths_strips_blank(self, client):
        """空白项剔除、保留非空路径（配置容错）。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"worker": {
            "plugin_paths": ["  /a.py  ", "   ", ""]}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["plugin_paths"] == ["/a.py"]

    def test_update_plugin_paths_rejects_non_list(self, client):
        """非字符串数组（字符串/数字）400 拒绝，不落盘。"""
        tc, tmp_path = client
        for bad in ("/a.py", 123, None, ["/a.py", 5]):
            resp = tc.put("/api/settings", json={"worker": {"plugin_paths": bad}})
            assert resp.status_code == 400, f"plugin_paths 值 {bad!r} 应拒绝"
        assert "plugin_paths" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_update_plugin_paths_keeps_other_worker_fields(self, client):
        """只提交 plugin_paths 时 worker 其他字段不受影响（部分更新）。"""
        tc, _ = client
        assert tc.put("/api/settings", json={"worker": {
            "max_concurrent_repos": 7}}).status_code == 200
        resp = tc.put("/api/settings", json={"worker": {
            "plugin_paths": ["/x.py"]}})
        assert resp.status_code == 200
        worker = resp.json()["worker"]
        assert worker["plugin_paths"] == ["/x.py"]
        assert worker["max_concurrent_repos"] == 7


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


class TestWebhookSettings:
    """webhook 段（issue #136）：任务完成时 webhook 推送配置。"""

    def test_get_settings_includes_webhook_defaults(self, client):
        """未配置时返回默认值：关闭、空地址、默认 content_type、默认模板。"""
        tc, tmp_path = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        wh = resp.json()["webhook"]
        assert wh["enabled"] is False
        assert wh["url"] == ""
        assert wh["content_type"] == "application/json"
        assert wh["authorization_masked"] == ""
        assert wh["body_template"]  # 默认模板非空

    def test_update_webhook_persists(self, client):
        """PUT webhook 段写回 config.yaml 并可读回。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"webhook": {
            "enabled": True,
            "url": "https://hooks.example.com/botler",
            "content_type": "application/json",
            "authorization": "Bearer tok123",
            "body_template": '{"repo":"{repo_name}"}',
        }})
        assert resp.status_code == 200
        wh = resp.json()["webhook"]
        assert wh["enabled"] is True
        assert wh["url"] == "https://hooks.example.com/botler"
        assert wh["content_type"] == "application/json"
        assert wh["body_template"] == '{"repo":"{repo_name}"}'
        assert "*" in wh["authorization_masked"]  # 掩码返回
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "webhook:" in config_text and "https://hooks.example.com/botler" in config_text

    def test_update_webhook_authorization_masked_keeps_existing(self, client):
        """authorization 回传掩码值/空串 = 保持现有凭据。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"webhook": {
            "enabled": True, "url": "https://h.example.com/x",
            "authorization": "Bearer real-secret",
        }})
        # 掩码值（含 *）不覆盖：文件仍保留真实凭据
        resp = tc.put("/api/settings", json={"webhook": {
            "authorization": "Bearer ********", "enabled": True,
            "url": "https://h.example.com/x",
        }})
        assert resp.status_code == 200
        assert "Bearer real-secret" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
        # 空串同样不覆盖
        tc.put("/api/settings", json={"webhook": {"authorization": "  "}})
        assert "Bearer real-secret" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_update_webhook_rejects_bad_url(self, client):
        """url 必须以 http(s):// 开头。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"webhook": {"url": "not-a-url"}})
        assert resp.status_code == 400
        assert "http(s)://" in resp.json()["detail"]

    def test_update_webhook_rejects_bad_types(self, client):
        """enabled 必须布尔、字符串字段必须是字符串。"""
        tc, tmp_path = client
        assert tc.put("/api/settings", json={"webhook": {"enabled": "yes"}}).status_code == 400
        assert tc.put("/api/settings", json={"webhook": {"url": 123}}).status_code == 400
        assert tc.put("/api/settings", json={"webhook": {"content_type": []}}).status_code == 400

    def test_update_webhook_blank_content_type_normalized(self, client):
        """content_type 空白归一为 application/json。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"webhook": {"content_type": "   "}})
        assert resp.status_code == 200
        assert resp.json()["webhook"]["content_type"] == "application/json"

    def test_webhook_test_without_url(self, client):
        """未配置地址时测试推送返回 ok=false + 错误信息。"""
        tc, tmp_path = client
        resp = tc.post("/api/settings/webhook-test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["error"]

    def test_webhook_test_ok(self, client, monkeypatch):
        """配置地址后测试推送返回 ok=true（monkeypatch 发送链路）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"webhook": {
            "enabled": True, "url": "https://h.example.com/x",
        }})
        from botler.webhook_push import WebhookPusher
        monkeypatch.setattr(WebhookPusher, "send_test",
                            lambda self: {"status_code": 200, "text": "ok"})
        resp = tc.post("/api/settings/webhook-test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status_code"] == 200

    def test_webhook_test_http_error(self, client, monkeypatch):
        """目标返回非 2xx 时测试推送返回 ok=false + 状态码。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"webhook": {
            "enabled": True, "url": "https://h.example.com/x",
        }})
        from botler.webhook_push import WebhookPusher
        monkeypatch.setattr(WebhookPusher, "send_test",
                            lambda self: {"status_code": 500, "text": "boom"})
        resp = tc.post("/api/settings/webhook-test")
        data = resp.json()
        assert data["ok"] is False
        assert "500" in data["error"]

    def test_webhook_test_exception_ok_false(self, client, monkeypatch):
        """发送异常时测试推送返回 ok=false，不抛 500。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"webhook": {
            "enabled": True, "url": "https://h.example.com/x",
        }})
        from botler.webhook_push import WebhookPushError
        from botler.webhook_push import WebhookPusher

        def boom(self):
            raise WebhookPushError("webhook 请求失败: connect error")

        monkeypatch.setattr(WebhookPusher, "send_test", boom)
        resp = tc.post("/api/settings/webhook-test")
        data = resp.json()
        assert data["ok"] is False
        assert "connect error" in data["error"]



class TestVisionModelsSettings:
    """vision_models 段：识图模型配置（issue #152，设置页「识图模型」卡片）。

    与 image_models（issue #135）同模式：api_key 落盘 config.yaml、API 只
    返回掩码、编辑时留空或回传掩码值 = 保持现有；列表整体替换。
    """

    MODEL = {
        "name": "Gemini 视觉",
        "provider": "gemini_vision",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "AIza-vision-test-123",
        "model": "gemini-2.5-flash",
        "enabled": True,
    }

    def test_get_settings_includes_vision_models_empty(self, client):
        """未配置时 vision_models 返回空列表。"""
        tc, tmp_path = client
        data = tc.get("/api/settings").json()["vision_models"]
        assert data == []

    def test_put_vision_models_persists(self, client):
        """PUT vision_models 写回 config.yaml 并可读回（api_key 只返回掩码）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [self.MODEL]})
        assert resp.status_code == 200
        models = resp.json()["vision_models"]
        assert len(models) == 1
        assert models[0]["name"] == "Gemini 视觉"
        assert models[0]["provider"] == "gemini_vision"
        assert models[0]["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
        assert models[0]["model"] == "gemini-2.5-flash"
        assert models[0]["enabled"] is True
        masked = models[0]["api_key_masked"]
        assert "AIza-vision-test-123" not in masked  # 明文不回传
        assert "*" in masked  # 有掩码占位
        # config.yaml 是唯一事实来源，明文落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-vision-test-123" in config_text

    def test_put_masked_api_key_not_overwritten(self, client):
        """前端回传掩码值（含 *）不覆盖真实 key。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"vision_models": [self.MODEL]})
        masked = tc.get("/api/settings").json()["vision_models"][0]["api_key_masked"]
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "api_key": masked},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-vision-test-123" in config_text

    def test_put_blank_api_key_keeps_existing(self, client):
        """api_key 留空 = 保持现有（新增条目则存空串）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"vision_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "api_key": ""},
        ]})
        assert resp.status_code == 200
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "api_key: AIza-vision-test-123" in config_text

    def test_put_replaces_whole_list(self, client):
        """整体替换语义：新列表覆盖旧列表。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"vision_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"vision_models": [
            {"name": "OpenAI 视觉", "provider": "openai_vision",
             "base_url": "https://api.openai.com/v1",
             "api_key": "sk-vision-789", "model": "gpt-4o",
             "enabled": False},
        ]})
        assert resp.status_code == 200
        models = resp.json()["vision_models"]
        assert len(models) == 1
        assert models[0]["name"] == "OpenAI 视觉"
        assert models[0]["enabled"] is False

    def test_put_empty_list_clears(self, client):
        """空列表清空配置。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"vision_models": [self.MODEL]})
        resp = tc.put("/api/settings", json={"vision_models": []})
        assert resp.status_code == 200
        assert resp.json()["vision_models"] == []
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "vision_models" in config_text

    def test_put_rejects_blank_name(self, client):
        """name 必填非空。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "name": "   "},
        ]})
        assert resp.status_code == 400
        assert "name" in resp.json()["detail"]

    def test_put_rejects_duplicate_name(self, client):
        """name 唯一（掩码回传按 name 匹配旧值，重复会歧义）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [
            self.MODEL,
            {**self.MODEL, "model": "gemini-2.5-pro"},
        ]})
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_put_rejects_invalid_base_url(self, client):
        """base_url 必须以 http(s):// 开头。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "base_url": "not-a-url"},
        ]})
        assert resp.status_code == 400

    def test_put_rejects_non_list(self, client):
        """vision_models 必须是数组。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": {"name": "x"}})
        assert resp.status_code == 400

    def test_put_provider_defaults_to_custom(self, client):
        """provider 缺省归一为 custom。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "provider": "  "},
        ]})
        assert resp.status_code == 200
        assert resp.json()["vision_models"][0]["provider"] == "custom"

    def test_put_rejects_non_bool_enabled(self, client):
        """enabled 必须是布尔值。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"vision_models": [
            {**self.MODEL, "enabled": "yes"},
        ]})
        assert resp.status_code == 400

    def test_env_ref_api_key_expanded_on_read(self, client):
        """config.yaml 中 api_key 支持 ${ENV} 引用（凭据不落明文）。"""
        tc, tmp_path = client
        config_path = tmp_path / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            config_text + "vision_models:\n"
            "  - name: Gemini 视觉\n"
            "    provider: gemini_vision\n"
            "    base_url: https://generativelanguage.googleapis.com/v1beta\n"
            "    api_key: ${BOTLER_TEST_VISION_KEY}\n"
            "    model: gemini-2.5-flash\n"
            "    enabled: true\n",
            encoding="utf-8")
        import os
        os.environ["BOTLER_TEST_VISION_KEY"] = "sk-vision-from-env"
        try:
            data = tc.get("/api/settings").json()["vision_models"]
            assert data[0]["api_key_masked"].endswith("-env")
        finally:
            os.environ.pop("BOTLER_TEST_VISION_KEY", None)


class TestVisionModelTestEndpoint:
    """POST /api/settings/vision-model-test：识图模型测试按钮（issue #152）。

    用户上传一张图片（multipart）后调用配置的识图模型描述图片内容。
    用提交的表单值（provider / base_url / api_key / model / prompt）真实
    调用一次识图接口验证配置可用；api_key 掩码/留空、url/model 留空按
    name 回退已保存配置。成功 ok=true + 描述文本，失败 ok=false + 原因
    （不抛 500，与 image-model-test 同容错策略）。
    """

    MODEL = TestVisionModelsSettings.MODEL
    PNG = b"\x89PNG-test-image"

    def _save(self, tc):
        tc.put("/api/settings", json={"vision_models": [self.MODEL]})

    def _patch_client(self, monkeypatch, fake):
        """monkeypatch botler.vision_models.VisionModelClient（端点函数内
        from ..vision_models import 会在调用时取模块属性，patch 生效）。"""
        from botler import vision_models as vm_mod
        monkeypatch.setattr(vm_mod, "VisionModelClient", fake)

    def _post(self, tc, **overrides):
        """multipart 上传：图片文件 + 表单字段。"""
        data = {
            "name": "Gemini 视觉",
            "provider": "gemini_vision",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "AIza-test",
            "model": "gemini-2.5-flash",
            "prompt": "请描述这张图片的内容",
        }
        data.update(overrides)
        return tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data=data)

    def test_slow_model_call_does_not_block_event_loop(self, client, monkeypatch):
        """慢速识图模型调用不冻结事件循环（issue #164 回归防护）。

        旧实现：async 端点内直接同步调用模型（最长 60s），会冻结 uvicorn
        事件循环——期间所有其它请求（含浏览器后续 fetch）无法被处理，
        连接级失败表现为「✗ Failed to fetch」。修复后模型调用在线程池
        执行，并发请求不受影响。
        """
        import threading
        import time
        tc, _ = client

        class SlowClient:
            def __init__(self, **kwargs):
                pass

            def describe(self, image, *, mime_type, prompt):
                time.sleep(3.0)  # 模拟慢速/挂起的模型调用（同步阻塞）
                return "ok"

        self._patch_client(monkeypatch, SlowClient)
        results = {}

        def slow_call():
            results["slow"] = self._post(tc).status_code

        def fast_call():
            t0 = time.monotonic()
            r = tc.get("/api/settings")
            results["fast"] = (r.status_code, time.monotonic() - t0)

        # 进入上下文管理器：所有请求共享同一事件循环（等价 uvicorn 单
        # 进程），慢请求阻塞时并发请求应仍能及时返回
        with tc:
            t1 = threading.Thread(target=slow_call)
            t2 = threading.Thread(target=fast_call)
            t1.start()
            time.sleep(0.5)  # 确保慢请求先进入模型调用
            t2.start()
            t2.join(timeout=6)
            t1.join(timeout=8)
        assert results["slow"] == 200
        status, elapsed = results["fast"]
        assert status == 200
        # 并发 GET 不应被 3s 慢调用拖住（旧实现会阻塞约 3s）
        assert elapsed < 1.5, (
            f"识图测试的模型调用阻塞了事件循环，并发请求耗时 {elapsed:.2f}s")

    def test_test_missing_image(self, client):
        """未上传图片直接 ok=false，不发请求。"""
        tc, _ = client
        resp = tc.post("/api/settings/vision-model-test", data={
            "name": "x", "provider": "gemini_vision",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "图片" in data["error"]

    def test_test_missing_provider(self, client):
        """未选择识图模型（provider）直接 ok=false，不发请求。"""
        tc, _ = client
        resp = tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data={"name": "x"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "识图模型" in data["error"]

    def test_test_ok_with_full_config(self, client, monkeypatch):
        """提交完整配置：ok=true + 描述文本，客户端收到正确入参与图片。"""
        tc, _ = client
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                captured["image"] = image
                captured["mime_type"] = mime_type
                captured["prompt"] = prompt
                return "图片里有一只橘色的猫"

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["description"] == "图片里有一只橘色的猫"
        assert captured["provider"] == "gemini_vision"
        assert captured["api_key"] == "AIza-test"
        assert captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
        assert captured["model"] == "gemini-2.5-flash"
        assert captured["image"] == self.PNG
        assert captured["mime_type"] == "image/png"
        assert captured["prompt"] == "请描述这张图片的内容"

    def test_test_masked_key_falls_back_to_saved(self, client, monkeypatch):
        """掩码/留空的 api_key、url、model 按 name 回退已保存配置。"""
        tc, _ = client
        self._save(tc)
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc, api_key="AIza-****", base_url="", model="")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 回退到已保存的明文 key / url / model
        assert captured["api_key"] == "AIza-vision-test-123"
        assert captured["base_url"] == self.MODEL["base_url"]
        assert captured["model"] == "gemini-2.5-flash"

    def test_test_row_button_uses_saved_only(self, client, monkeypatch):
        """列表行「测试」只提交 name+provider：完全按已保存配置测试。"""
        tc, _ = client
        self._save(tc)
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data={"name": "Gemini 视觉", "provider": "gemini_vision"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert captured["api_key"] == "AIza-vision-test-123"
        assert captured["base_url"] == self.MODEL["base_url"]
        assert captured["model"] == "gemini-2.5-flash"

    def test_test_describe_error_ok_false(self, client, monkeypatch):
        """识图接口报错（如 401）：ok=false + 错误信息，不抛 500。"""
        tc, _ = client

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def describe(self, image, *, mime_type, prompt):
                raise VisionModelError("Gemini 请求失败: HTTP 401 invalid key")

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "401" in data["error"]

    def test_test_unknown_provider_ok_false(self, client, monkeypatch):
        """未知 provider 构造客户端失败：ok=false + 原因。"""
        tc, _ = client

        class FakeClient:
            def __init__(self, **kwargs):
                raise VisionModelError("不支持的识图模型类型: nope")

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc, provider="nope")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "nope" in data["error"]

    def test_test_verify_ssl_follows_settings(self, client, monkeypatch):
        """verify_ssl 跟随全局设置（测试配置 verify_ssl: false）。"""
        tc, _ = client
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        assert captured["verify_ssl"] is False

    def test_test_row_button_undefined_placeholders_fallback(self, client, monkeypatch):
        """列表行「测试」缺失字段被 FormData 转成字符串 'undefined'（issue #154）：
        应视为空值回退已保存配置，而不是把 'undefined' 当作真实 base_url 发起
        请求（否则 httpx 报 Request URL is missing an 'http://' or 'https://'
        protocol.）。
        """
        tc, _ = client
        self._save(tc)
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data={"name": "Gemini 视觉", "provider": "gemini_vision",
                  "base_url": "undefined", "api_key": "undefined",
                  "model": "undefined"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 回退到已保存的明文 key / url / model（"undefined" 不是合法配置值）
        assert captured["api_key"] == "AIza-vision-test-123"
        assert captured["base_url"] == self.MODEL["base_url"]
        assert captured["model"] == "gemini-2.5-flash"

    def test_test_invalid_scheme_returns_clear_error(self, client, monkeypatch):
        """Base URL 不带 http(s):// 协议（issue #154）：返回明确中文提示，
        而不是构造客户端后让 httpx 报 "Request URL is missing an 'http://'
        or 'https://' protocol." 的晦涩错误。
        """
        tc, _ = client
        called = []

        class BoomClient:
            def __init__(self, **kwargs):
                called.append(kwargs)  # 不应被构造：scheme 校验先于客户端创建

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, BoomClient)
        resp = tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data={"name": "qwen3-vl-flash", "provider": "custom",
                  "base_url": "localhost:8080/v1/chat/completions",
                  "api_key": "sk-test", "model": "qwen3-vl-flash"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "必须以 http:// 或 https:// 开头" in data["error"]
        assert called == []  # 未进入网络调用

    def test_test_custom_row_button_undefined_falls_back_to_saved(self, client, monkeypatch):
        """用户反馈场景（issue #154）：自定义识图模型（阿里云 compatible-mode
        网关，模型 qwen3-vl-flash）列表行「测试」缺失字段被 FormData 转成
        "undefined" 时，回退已保存配置（含 https:// 的 base_url / api_key /
        model）发起请求，不再报协议错误。
        """
        tc, _ = client
        saved = {
            "name": "qwen3-vl-flash",
            "provider": "custom",
            "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1/chat/completions",
            "api_key": "sk-aliyun-test-123",
            "model": "qwen3-vl-flash",
            "enabled": True,
        }
        tc.put("/api/settings", json={"vision_models": [saved]})
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "图中有高楼与蓝天"

        self._patch_client(monkeypatch, FakeClient)
        resp = tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data={"name": "qwen3-vl-flash", "provider": "custom",
                  "base_url": "undefined", "api_key": "undefined",
                  "model": "undefined"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["description"] == "图中有高楼与蓝天"
        # 回退到已保存的明文 key / url / model
        assert captured["api_key"] == "sk-aliyun-test-123"
        assert captured["base_url"] == (
            "https://token-plan.cn-beijing.maas.aliyuncs.com/"
            "compatible-mode/v1/chat/completions")
        assert captured["model"] == "qwen3-vl-flash"
        assert captured["provider"] == "custom"

    def test_test_network_error_passes_through_request_url(self, client, monkeypatch):
        """网络层失败（超时/连接失败，issue #156）：describe 抛出的错误
        信息带「请求地址 + 请求头 + 请求体」，端点应完整透传给前端展示
        （ok=false + 错误原文，不截断、不吞掉诊断线索）。"""
        tc, _ = client

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def describe(self, image, *, mime_type, prompt):
                raise VisionModelError(
                    "识图模型「Gemini 视觉」请求超时（>60s）（请求地址: "
                    "https://api.example.com/v1beta/models/gemini-2.5-flash"
                    ":generateContent，请求头: {\"X-goog-api-key\": "
                    "\"***（已掩码）\"}，请求体: {\"contents\": "
                    "[{\"parts\": [{\"inline_data\": {\"data\": "
                    "\"base64…（已截断）\"}}]}]}）")

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "请求地址" in data["error"]
        assert "api.example.com" in data["error"]
        assert "generateContent" in data["error"]
        # issue #156：POST 请求头（密钥掩码）与请求体原文一并透传前端
        assert "请求头" in data["error"]
        assert "请求体" in data["error"]
        assert "已掩码" in data["error"]
        assert "已截断" in data["error"]


class TestMinioSettings:
    """minio 段：识图图片上传 MinIO 配置（issue #163）。"""

    def test_get_settings_minio_defaults(self, client):
        """未配置 minio 段时返回默认值（关闭、凭据掩码为空串）。"""
        tc, _ = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()["minio"]
        assert data["enabled"] is False
        assert data["endpoint"] == ""
        assert data["access_key_masked"] == ""
        assert data["secret_key_masked"] == ""
        assert data["bucket"] == "public"
        assert data["public_base_url"] == ""

    def test_put_minio_persists(self, client):
        """PUT minio 段写回 config.yaml 并可读回（凭据只返回掩码）。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"minio": {
            "enabled": True,
            "endpoint": "127.0.0.1:9000",
            "secure": False,
            "access_key": "minioadmin",
            "secret_key": "minioadmin",
            "bucket": "public",
            "public_base_url": "http://img.example.com:9000",
            "verify_ssl": False,
        }})
        assert resp.status_code == 200
        data = resp.json()["minio"]
        assert data["enabled"] is True
        assert data["endpoint"] == "127.0.0.1:9000"
        assert data["bucket"] == "public"
        assert data["public_base_url"] == "http://img.example.com:9000"
        assert "*" in data["access_key_masked"]  # 掩码而非明文
        assert data["access_key_masked"] != "minioadmin"
        assert "*" in data["secret_key_masked"]
        assert data["secret_key_masked"] != "minioadmin"  # 明文不流转
        # config.yaml 是唯一事实来源，应已落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "minio:" in config_text and "minioadmin" in config_text

    def test_put_minio_masked_key_keeps_existing(self, client):
        """掩码/空串凭据 = 保持现有值，不覆盖真实凭据。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"minio": {
            "enabled": True, "access_key": "real-key-123",
            "secret_key": "real-secret-456", "endpoint": "h:9000",
            "public_base_url": "http://img.example.com"}})
        resp = tc.put("/api/settings", json={"minio": {
            "access_key": "real-****", "secret_key": ""}})
        assert resp.status_code == 200
        data = resp.json()["minio"]
        assert "123" in data["access_key_masked"]  # 真实值仍在（掩码展示）
        assert "456" in data["secret_key_masked"]
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "real-key-123" in config_text
        assert "real-secret-456" in config_text

    def test_put_minio_rejects_non_bool(self, client):
        """enabled / secure / verify_ssl 必须是布尔值。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"minio": {"enabled": "yes"}})
        assert resp.status_code == 400
        assert "布尔值" in resp.json()["detail"]

    def test_put_minio_rejects_non_string(self, client):
        """endpoint / 凭据 / 桶 / 公网前缀必须是字符串。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"minio": {"bucket": 123}})
        assert resp.status_code == 400
        assert "必须是字符串" in resp.json()["detail"]

    def test_put_minio_rejects_bad_public_base_url(self, client):
        """public_base_url 非空时必须以 http(s):// 开头。"""
        tc, _ = client
        resp = tc.put("/api/settings", json={"minio": {
            "public_base_url": "img.example.com:9000"}})
        assert resp.status_code == 400
        assert "http:// 或 https://" in resp.json()["detail"]


class TestVisionModelTestEndpointMinio:
    """vision-model-test 端点 MinIO 接线（issue #163）。

    启用并配置完整 MinIO 时，端点把 MinIO 图片存储注入 VisionModelClient
    （describe 内先哈希上传，识图请求传 http URL）；未配置时为 None
    （保持 base64 内联输入，原行为）。
    """

    MODEL = TestVisionModelsSettings.MODEL
    PNG = b"\x89PNG-test-image"

    def _patch_client(self, monkeypatch, fake):
        from botler import vision_models as vm_mod
        monkeypatch.setattr(vm_mod, "VisionModelClient", fake)

    def _post(self, tc, **overrides):
        data = {
            "name": "Gemini 视觉",
            "provider": "gemini_vision",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "AIza-test",
            "model": "gemini-2.5-flash",
            "prompt": "请描述这张图片的内容",
        }
        data.update(overrides)
        return tc.post(
            "/api/settings/vision-model-test",
            files={"image": ("test.png", self.PNG, "image/png")},
            data=data)

    def _enable_minio(self, client):
        """在临时 config.yaml 写入 minio 段（ConfigManager 按 mtime 自动
        重载），返回配置路径。"""
        tc, tmp_path = client
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + "\n"
            "minio:\n"
            "  enabled: true\n"
            "  endpoint: 127.0.0.1:9000\n"
            "  access_key: minioadmin\n"
            "  secret_key: minioadmin\n"
            "  bucket: public\n"
            "  public_base_url: http://img.example.com:9000\n",
            encoding="utf-8")
        return config_path

    def test_minio_disabled_passes_no_store(self, client, monkeypatch):
        """未配置 minio：image_store 为 None（原行为，base64 内联）。"""
        tc, _ = client
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        assert captured["image_store"] is None

    def test_minio_enabled_passes_image_store(self, client, monkeypatch):
        """启用并配置完整 minio：image_store 注入客户端（describe 内图片
        哈希上传 MinIO 后传 http URL）。"""
        tc, _ = client
        self._enable_minio(client)
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def describe(self, image, *, mime_type, prompt):
                return "ok"

        self._patch_client(monkeypatch, FakeClient)
        resp = self._post(tc)
        assert resp.status_code == 200
        store = captured["image_store"]
        assert store is not None
        assert store.cfg.bucket == "public"
        assert store.cfg.public_base_url == "http://img.example.com:9000"
