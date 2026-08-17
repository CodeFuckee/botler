"""插件管理 API 测试（issue #145）：插件页面的安装 / 卸载 / 设置后端接口。

覆盖：
- GET /api/plugins：按分类分组返回全部已注册插件（内置/外部来源标记、
  模型供应商默认预设、worker.engine / plugin_paths 上下文）；
- POST /api/plugins/install：安装外部插件模块（校验文件存在、模块可
  加载且至少注册一个插件、与已装插件冲突检测；成功 = 写入
  worker.plugin_paths + 全局注册表热加载；失败不落盘）；
- POST /api/plugins/uninstall：卸载外部插件（从配置与注册表同时移除，
  内置插件不在 plugin_paths 中不可卸载）；
- POST /api/plugins/reload：按当前 plugin_paths 清空并重载外部插件；
- PUT /api/plugins/settings：设置默认执行引擎（executor 插件设置，
  复用 worker.engine，非法值 400 拒绝）。
"""

import textwrap
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.plugins import PluginKind, get_registry, get_plugin

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
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。

    与 test_api_settings.py 同款 fixture；插件全局注册表为进程内单例，
    teardown 时清理外部插件，保证用例之间互不残留（内置插件不受影响）。
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    yield TestClient(app), tmp_path
    # 全局插件注册表跨测试共享：清理外部插件，避免用例间残留
    get_registry().clear_external()


def write_plugin_module(tmp_path, name="feishu_channel", code=None):
    """在 tmp_path 下写一个外部插件模块，返回路径字符串。

    默认模块注册一个 notifier 插件（feishu_channel），与内置插件
    （webhook / in_app）不同名，可用于安装/卸载/重载全链路测试。
    """
    if code is None:
        code = textwrap.dedent(f"""\
            from botler.plugins import NotifierPlugin, register_plugin

            class FeishuChannelPlugin(NotifierPlugin):
                name = "{name}"
                description = "飞书消息通道（测试外部插件）"

                def send_test(self, context, repo_name="测试仓库"):
                    return {{"ok": True}}

            register_plugin(FeishuChannelPlugin())
        """)
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return str(path)


class TestListPlugins:
    """GET /api/plugins：插件列表视图。"""

    def test_list_includes_three_kinds_with_builtins(self, client):
        """默认返回四类分组（含识图模型供应商，issue #152），内置插件
        齐全且 builtin=true。"""
        tc, _ = client
        resp = tc.get("/api/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["plugins"].keys()) == {
            PluginKind.EXECUTOR.value,
            PluginKind.MODEL_PROVIDER.value,
            PluginKind.VISION_MODEL_PROVIDER.value,
            PluginKind.NOTIFIER.value,
        }
        executors = data["plugins"]["executor"]
        assert [p["name"] for p in executors] == ["claude", "hermes", "dsh"]
        assert all(p["builtin"] is True and p["path"] is None for p in executors)
        assert data["plugins"]["model_provider"][0]["name"] == "gemini_nano_banana"
        # 识图模型供应商（issue #152）：内置 gemini_vision / openai_vision / custom
        assert [p["name"] for p in data["plugins"]["vision_model_provider"]] == [
            "gemini_vision", "openai_vision", "custom"]
        assert data["plugins"]["notifier"][0]["name"] == "webhook"
        # 上下文：默认引擎与外部插件路径列表
        assert data["engine"] == "claude"
        assert data["plugin_paths"] == []

    def test_list_includes_model_provider_presets(self, client):
        """模型供应商插件返回默认预设（设置页生图模型预设同源）。"""
        tc, _ = client
        data = tc.get("/api/plugins").json()
        gemini = next(
            p for p in data["plugins"]["model_provider"]
            if p["name"] == "gemini_nano_banana")
        assert gemini["display_name"] == "Gemini Nano Banana Pro"
        assert gemini["default_base_url"].startswith("https://")
        assert gemini["default_model"]

    def test_list_marks_external_plugin_with_path(self, client):
        """外部插件 builtin=false 且 path 指向模块路径（安装后）。"""
        tc, tmp_path = client
        path = write_plugin_module(tmp_path)
        assert tc.post("/api/plugins/install", json={"path": path}).status_code == 200
        data = tc.get("/api/plugins").json()
        feishu = next(
            p for p in data["plugins"]["notifier"]
            if p["name"] == "feishu_channel")
        assert feishu["builtin"] is False
        assert feishu["path"] == path
        assert data["plugin_paths"] == [path]


class TestInstallPlugin:
    """POST /api/plugins/install：安装外部插件。"""

    def test_install_success_persists_and_registers(self, client):
        """安装成功：写入 worker.plugin_paths + 全局注册表热加载。"""
        tc, tmp_path = client
        path = write_plugin_module(tmp_path)
        resp = tc.post("/api/plugins/install", json={"path": path})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin_paths"] == [path]
        assert any(
            p["name"] == "feishu_channel" and p["builtin"] is False
            for p in data["plugins"]["notifier"])
        # config.yaml 已落盘（唯一事实来源）
        assert "feishu_channel" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
        # 全局注册表已热加载（无需重启即生效）
        assert get_registry().has(PluginKind.NOTIFIER, "feishu_channel")
        assert get_plugin(PluginKind.NOTIFIER, "feishu_channel").description \
            == "飞书消息通道（测试外部插件）"

    def test_install_strips_whitespace_and_dedup(self, client):
        """路径 strip 归一；已安装路径重复安装 400 拒绝。"""
        tc, tmp_path = client
        path = write_plugin_module(tmp_path)
        assert tc.post("/api/plugins/install",
                       json={"path": f"  {path}  "}).status_code == 200
        resp = tc.post("/api/plugins/install", json={"path": path})
        assert resp.status_code == 400
        assert "已安装" in resp.json()["detail"]

    def test_install_rejects_empty_and_missing(self, client):
        """空路径 / 文件不存在 400 拒绝，不落盘。"""
        tc, tmp_path = client
        for bad in ("", "   ", str(tmp_path / "no_such.py")):
            resp = tc.post("/api/plugins/install", json={"path": bad})
            assert resp.status_code == 400, f"路径 {bad!r} 应拒绝"
        assert "plugin_paths" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_install_rejects_module_without_plugins(self, client):
        """模块可导入但不注册任何插件 → 400（避免无效路径落盘）。"""
        tc, tmp_path = client
        empty = tmp_path / "empty.py"
        empty.write_text("x = 1\n", encoding="utf-8")
        resp = tc.post("/api/plugins/install", json={"path": str(empty)})
        assert resp.status_code == 400
        assert "未注册" in resp.json()["detail"]
        assert "plugin_paths" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_install_rejects_module_with_syntax_error(self, client):
        """模块加载失败（语法错误）→ 400，不落盘。"""
        tc, tmp_path = client
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:", encoding="utf-8")
        resp = tc.post("/api/plugins/install", json={"path": str(bad)})
        assert resp.status_code == 400
        assert "加载失败" in resp.json()["detail"]
        assert "plugin_paths" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_install_rejects_conflict_with_builtin(self, client):
        """模块注册的插件与内置同名同分类 → 400 拒绝且不落盘。"""
        tc, tmp_path = client
        path = write_plugin_module(
            tmp_path, name="conflict", code=textwrap.dedent("""\
                from botler.plugins import ExecutorPlugin, register_plugin

                class FakeClaude(ExecutorPlugin):
                    name = "claude"  # 与内置 claude 冲突
                    description = "冒名插件"

                register_plugin(FakeClaude())
            """))
        resp = tc.post("/api/plugins/install", json={"path": path})
        assert resp.status_code == 400
        assert "冲突" in resp.json()["detail"]
        assert "plugin_paths" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")


class TestUninstallPlugin:
    """POST /api/plugins/uninstall：卸载外部插件。"""

    def test_uninstall_removes_config_and_registry(self, client):
        """卸载成功：plugin_paths 移除 + 注册表移除。"""
        tc, tmp_path = client
        path = write_plugin_module(tmp_path)
        assert tc.post("/api/plugins/install", json={"path": path}).status_code == 200
        resp = tc.post("/api/plugins/uninstall", json={"path": path})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin_paths"] == []
        assert not any(p["name"] == "feishu_channel" for p in data["plugins"]["notifier"])
        assert not get_registry().has(PluginKind.NOTIFIER, "feishu_channel")
        # 内置插件不受影响
        assert get_registry().has(PluginKind.NOTIFIER, "webhook")

    def test_uninstall_rejects_not_installed(self, client):
        """未安装的路径卸载 → 400。"""
        tc, _ = client
        resp = tc.post("/api/plugins/uninstall", json={"path": "/nonexistent.py"})
        assert resp.status_code == 400
        assert "未安装" in resp.json()["detail"]

    def test_uninstall_rejects_empty(self, client):
        """空路径 → 400。"""
        tc, _ = client
        resp = tc.post("/api/plugins/uninstall", json={"path": "  "})
        assert resp.status_code == 400


class TestReloadPlugins:
    """POST /api/plugins/reload：按配置清空并重载外部插件。"""

    def test_reload_clears_and_reloads_external(self, client):
        """reload 后外部插件仍在（按 plugin_paths 重载），内置不受影响。"""
        tc, tmp_path = client
        path = write_plugin_module(tmp_path)
        assert tc.post("/api/plugins/install", json={"path": path}).status_code == 200
        resp = tc.post("/api/plugins/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin_paths"] == [path]
        assert any(
            p["name"] == "feishu_channel" and p["builtin"] is False
            for p in data["plugins"]["notifier"])
        assert get_registry().has(PluginKind.NOTIFIER, "feishu_channel")

    def test_reload_without_external_keeps_builtins(self, client):
        """无外部插件时 reload 幂等，内置插件完整。"""
        tc, _ = client
        resp = tc.post("/api/plugins/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin_paths"] == []
        assert [p["name"] for p in data["plugins"]["executor"]] == ["claude", "hermes", "dsh"]


class TestPluginSettings:
    """PUT /api/plugins/settings：默认执行引擎设置（executor 插件设置）。"""

    def test_set_engine_persists(self, client):
        """设置合法引擎写回 worker.engine（与设置页任务调度同源）。"""
        tc, tmp_path = client
        resp = tc.put("/api/plugins/settings", json={"engine": "dsh"})
        assert resp.status_code == 200
        assert resp.json()["engine"] == "dsh"
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "engine: dsh" in config_text

    def test_set_engine_rejects_invalid(self, client):
        """未知引擎 400 拒绝，不落盘。"""
        tc, tmp_path = client
        resp = tc.put("/api/plugins/settings", json={"engine": "not_a_real_engine"})
        assert resp.status_code == 400
        assert "engine" in resp.json()["detail"]
        assert "engine" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_set_engine_rejects_missing(self, client):
        """缺少 engine 字段 400 拒绝。"""
        tc, _ = client
        resp = tc.put("/api/plugins/settings", json={})
        assert resp.status_code == 400
