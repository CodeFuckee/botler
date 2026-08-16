"""插件体系核心测试（issue #140）。

覆盖：
- 插件注册表核心：注册 / 查询 / 重复冲突 / 缺失 / 列表 / 排序
- 三类插件基类能力方法（executor.run / model_provider.generate /
  notifier.send_* 默认行为与未实现报错）
- 外部插件模块加载（worker.plugin_paths 模式）
- 全局注册表内置插件完整性（导入 botler.plugins 自动注册）
"""

import sys
import textwrap

import pytest

from botler.plugins import (
    ExecutorPlugin,
    ImageProviderPlugin,
    NotifierPlugin,
    Plugin,
    PluginConflictError,
    PluginKind,
    PluginNotFoundError,
    PluginRegistry,
    get_plugin,
    get_registry,
    has_plugin,
    list_plugins,
    plugin_names,
    register_plugin,
)


# ---- 独立注册表核心 ----

class TestRegistryCore:
    def test_register_and_get(self):
        """注册后可按 kind + name 取回同一实例。"""
        registry = PluginRegistry()
        plugin = Plugin(kind=PluginKind.EXECUTOR, name="demo",
                        description="演示插件")
        registry.register(plugin)
        assert registry.get(PluginKind.EXECUTOR, "demo") is plugin

    def test_duplicate_register_conflicts(self):
        """同名同分类重复注册抛 PluginConflictError。"""
        registry = PluginRegistry()
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="demo"))
        with pytest.raises(PluginConflictError, match="重复注册"):
            registry.register(Plugin(kind=PluginKind.EXECUTOR, name="demo"))

    def test_same_name_different_kind_ok(self):
        """不同分类下同名插件互不冲突。"""
        registry = PluginRegistry()
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="demo"))
        registry.register(Plugin(kind=PluginKind.NOTIFIER, name="demo"))
        assert registry.has(PluginKind.EXECUTOR, "demo")
        assert registry.has(PluginKind.NOTIFIER, "demo")

    def test_missing_plugin_raises(self):
        """未注册插件抛 PluginNotFoundError，且报错含可选列表。"""
        registry = PluginRegistry()
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="a"))
        with pytest.raises(PluginNotFoundError, match="a"):
            registry.get(PluginKind.EXECUTOR, "missing")

    def test_names_and_list_keep_order(self):
        """names / list 保持注册顺序。"""
        registry = PluginRegistry()
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="a"))
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="b"))
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="c"))
        assert registry.names(PluginKind.EXECUTOR) == ["a", "b", "c"]
        assert [p.name for p in registry.list(PluginKind.EXECUTOR)] == ["a", "b", "c"]

    def test_registry_isolated_between_kinds(self):
        """不同分类的注册互不可见。"""
        registry = PluginRegistry()
        registry.register(Plugin(kind=PluginKind.EXECUTOR, name="x"))
        assert not registry.has(PluginKind.MODEL_PROVIDER, "x")
        assert registry.names(PluginKind.MODEL_PROVIDER) == []

    def test_register_rejects_non_plugin(self):
        """非 Plugin 实例注册抛 TypeError。"""
        registry = PluginRegistry()
        with pytest.raises(TypeError, match="Plugin"):
            registry.register("not-a-plugin")  # type: ignore[arg-type]


# ---- 插件基类能力方法 ----

class _DemoExecutor(ExecutorPlugin):
    name = "demo_executor"
    description = "测试执行引擎"

    def run(self, executor, task_id, repo, issue,
            resume_session=None, resume_history=None):
        return (0, f"run:{task_id}:{repo['name']}")


class TestPluginCapabilities:
    def test_executor_run_unimplemented_raises(self):
        """ExecutorPlugin 未实现 run 时调用抛 NotImplementedError。"""
        class _Raw(ExecutorPlugin):
            name = "raw"
        with pytest.raises(NotImplementedError, match="raw"):
            _Raw().run(None, 1, {}, {})

    def test_executor_run_delegates(self):
        """ExecutorPlugin 子类 run 正常执行并返回 (exit_code, output)。"""
        plugin = _DemoExecutor()
        code, output = plugin.run(None, 7, {"name": "demo"}, {})
        assert code == 0
        assert output == "run:7:demo"

    def test_model_provider_generate_unimplemented_raises(self):
        """ImageProviderPlugin 未实现 generate 时调用抛 NotImplementedError。"""
        class _Raw(ImageProviderPlugin):
            name = "raw"
        with pytest.raises(NotImplementedError, match="raw"):
            _Raw().generate(None, "画一只猫")

    def test_notifier_send_defaults_none(self):
        """NotifierPlugin 默认 send_* 返回 None（未启用跳过语义）。"""
        class _Raw(NotifierPlugin):
            name = "raw"
        plugin = _Raw()
        assert plugin.send_task_succeeded(None, {}) is None
        assert plugin.send_task_failed(None, {}, "原因") is None
        with pytest.raises(NotImplementedError, match="raw"):
            plugin.send_test(None)

    def test_plugin_meta_defaults(self):
        """插件默认版本 1.0，可覆盖 description / version。"""
        plugin = Plugin(kind=PluginKind.NOTIFIER, name="n")
        assert plugin.version == "1.0"
        assert plugin.description == ""
        assert plugin.kind == PluginKind.NOTIFIER
        assert plugin.name == "n"
        assert "n" in repr(plugin)

    def test_plugin_override_meta(self):
        plugin = Plugin(kind=PluginKind.EXECUTOR, name="e",
                        description="描述", version="2.0")
        assert plugin.description == "描述"
        assert plugin.version == "2.0"


# ---- 外部插件加载 ----

class TestExternalLoad:
    def test_load_external_registers(self, tmp_path):
        """load_external 加载模块并完成注册（返回成功路径列表）。"""
        mod = tmp_path / "my_plugin.py"
        mod.write_text(textwrap.dedent("""\
            from botler.plugins import ExecutorPlugin, register_plugin

            class MyEngine(ExecutorPlugin):
                name = "my_engine"
                description = "外部引擎插件"

            register_plugin(MyEngine())
        """), encoding="utf-8")
        registry = PluginRegistry()
        loaded = registry.load_external([str(mod)])
        assert loaded == [str(mod)]
        assert registry.has(PluginKind.EXECUTOR, "my_engine")
        plugin = registry.get(PluginKind.EXECUTOR, "my_engine")
        assert plugin.description == "外部引擎插件"

    def test_load_external_skips_missing_file(self, tmp_path, caplog):
        """缺失文件加载失败仅记日志，不抛异常（容错策略）。"""
        registry = PluginRegistry()
        missing = str(tmp_path / "not_exist.py")
        loaded = registry.load_external([missing])
        assert loaded == []
        assert registry.names(PluginKind.EXECUTOR) == []

    def test_load_external_empty_and_blank(self):
        """空列表 / 空白路径不加载任何插件。"""
        registry = PluginRegistry()
        assert registry.load_external(None) == []
        assert registry.load_external(["", "  "]) == []

    def test_load_external_import_error_ignored(self, tmp_path, caplog):
        """模块内异常不影响加载器（仅记日志，其他模块继续加载）。"""
        bad = tmp_path / "bad.py"
        bad.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        good = tmp_path / "good.py"
        good.write_text(textwrap.dedent("""\
            from botler.plugins import NotifierPlugin, register_plugin
            register_plugin(type("G", (NotifierPlugin,), {"name": "good_channel"})())
        """), encoding="utf-8")
        registry = PluginRegistry()
        loaded = registry.load_external([str(bad), str(good)])
        assert loaded == [str(good)]
        assert registry.has(PluginKind.NOTIFIER, "good_channel")


# ---- 全局注册表与内置插件 ----

class TestGlobalRegistry:
    def test_get_registry_singleton(self):
        """get_registry 返回同一单例。"""
        assert get_registry() is get_registry()

    def test_builtin_executor_plugins(self):
        """内置执行引擎插件齐全（claude / hermes / dsh）。"""
        names = plugin_names(PluginKind.EXECUTOR)
        assert "claude" in names
        assert "hermes" in names
        assert "dsh" in names

    def test_builtin_model_provider_plugins(self):
        """内置大模型供应商插件齐全（gemini_nano_banana / openai_gpt_image）。"""
        names = plugin_names(PluginKind.MODEL_PROVIDER)
        assert "gemini_nano_banana" in names
        assert "openai_gpt_image" in names

    def test_builtin_notifier_plugins(self):
        """内置任务消息通道插件齐全（webhook / in_app）。"""
        names = plugin_names(PluginKind.NOTIFIER)
        assert "webhook" in names
        assert "in_app" in names

    def test_get_plugin_returns_builtin(self):
        """get_plugin 能取回内置插件（含描述信息）。"""
        claude = get_plugin(PluginKind.EXECUTOR, "claude")
        assert claude.description != ""
        assert has_plugin(PluginKind.EXECUTOR, "claude")
        assert not has_plugin(PluginKind.EXECUTOR, "not-exist")
