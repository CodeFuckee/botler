"""Botler 插件体系核心（issue #140）。

插件 = 分类（PluginKind）+ 标识（name）+ 描述 + 能力实现，统一登记在全局
插件注册表 PluginRegistry 中。内置插件在 ``botler.plugins`` 包导入时注册；
外部插件通过 ``worker.plugin_paths`` 配置的 Python 模块路径加载（importlib）。

三类插件分类（对应需求的三块能力）：
- ``executor``：任务执行引擎（内置 claude / hermes / dsh）
- ``model_provider``：大模型 API 供应商（内置 gemini_nano_banana / openai_gpt_image）
- ``notifier``：任务消息发送通道（内置 webhook 外部推送 / in_app 网页通知）

调用方只面向注册表编程：按 ``kind + name`` 取出插件后调用其能力方法；
新增能力只需实现对应插件子类并注册，核心模块零改动。
"""

from __future__ import annotations

import importlib.util
import logging
from enum import Enum
from typing import Any

logger = logging.getLogger("botler.plugins")


class PluginKind(str, Enum):
    """插件分类：executor 任务执行引擎 / model_provider 生图 API 供应商 /
    vision_model_provider 识图 API 供应商 / notifier 任务消息发送通道。"""

    EXECUTOR = "executor"
    MODEL_PROVIDER = "model_provider"
    VISION_MODEL_PROVIDER = "vision_model_provider"
    NOTIFIER = "notifier"


class Plugin:
    """插件基类：分类 + 标识 + 描述 + 版本 + 能力方法。

    子类通过类属性声明 ``kind`` 与 ``name``（注册表键，配置项引用），
    实现对应分类的能力方法（run / generate / send_*）。
    """

    kind: PluginKind
    name: str = ""
    description: str = ""
    version: str = "1.0"

    def __init__(self, kind: PluginKind | None = None,
                 name: str | None = None,
                 description: str | None = None,
                 version: str | None = None) -> None:
        # kind/name 优先取构造参数，缺省回退类属性（子类声明式风格）
        if kind is not None:
            self.kind = kind
        if name is not None:
            self.name = name
        if description is not None:
            self.description = description
        if version is not None:
            self.version = version

    def __repr__(self) -> str:
        return (f"<{type(self).__name__} {self.kind.value}/{self.name} "
                f"v{self.version}>")


class ExecutorPlugin(Plugin):
    """任务执行引擎插件：实现 :meth:`run` 执行一次任务引擎。

    ``executor`` 参数为运行时的 ClaudeExecutor 实例（提供工作区 / 日志 /
    事件总线 / 停止请求等基础设施）；内置插件委托 executor 的既有引擎实现，
    自定义插件可实现完全独立的引擎逻辑。返回 ``(exit_code, output)``。
    """

    kind = PluginKind.EXECUTOR

    def run(self, executor: Any, task_id: int, repo: dict, issue: dict,
            resume_session: str | None = None,
            resume_history: list | None = None) -> tuple[int, str]:
        """执行一次任务引擎，返回 (exit_code, output)。"""
        raise NotImplementedError(f"执行引擎插件 {self.name} 未实现 run()")


class ImageProviderPlugin(Plugin):
    """大模型（生图）API 供应商插件：实现 :meth:`generate` 调用供应商接口。

    ``client`` 参数为 ImageModelClient 实例（提供 base_url / api_key /
    model / http 客户端等）；``default_base_url`` 与 ``default_model`` 作为
    设置页内置预设的默认值（均可被用户配置覆盖）。
    """

    kind = PluginKind.MODEL_PROVIDER
    display_name: str = ""       # 设置页展示名（预设名称）
    default_base_url: str = ""   # 内置预设默认接口地址
    default_model: str = ""      # 内置预设默认模型

    def generate(self, client: Any, prompt: str,
                 image: bytes | None = None, *,
                 mime_type: str = "image/png",
                 size: str = "1024x1024",
                 n: int = 1) -> list[Any]:
        """按 prompt（+ 可选参考图片）生成图片，返回 ImageResult 列表。"""
        raise NotImplementedError(f"生图供应商插件 {self.name} 未实现 generate()")

    def resolve_request_url(self, base_url: str, api_path: str) -> str:
        """解析生图请求地址（issue #150）。

        用户配置的 base_url 分两种情况：
        - 自定义完整端点（非空且不等于预设默认，如代理网关直接给出
          https://grsai.dakka.com.cn/v1/draw/completions）→ 直接原样
          作为请求地址使用，不再拼接操作路径；
        - 未配置 / 等于预设默认（含尾斜杠归一）→ 按官方接口在默认
          base_url 后拼接操作路径（如 /images/generations、
          /models/{model}:generateContent）。
        """
        if base_url and base_url != self.default_base_url:
            return base_url
        return f"{self.default_base_url}{api_path}"


class VisionProviderPlugin(Plugin):
    """识图（视觉理解）API 供应商插件：实现 :meth:`describe` 调用供应商接口。

    ``client`` 参数为 VisionModelClient 实例（提供 base_url / api_key /
    model / http 客户端等）；``default_base_url`` 与 ``default_model`` 作为
    设置页内置预设的默认值（均可被用户配置覆盖）。

    ``describe()`` 入参：图片字节 + MIME 类型 + 描述指令 prompt，返回
    模型对图片内容的文本描述。自定义 Base URL 语义与生图插件一致
    （issue #150）：非空且不等于预设默认 → 作为完整请求地址直接使用，
    不再拼接操作路径；未配置 / 等于预设默认 → 按官方接口拼接。
    """

    kind = PluginKind.VISION_MODEL_PROVIDER
    display_name: str = ""       # 设置页展示名（预设名称）
    default_base_url: str = ""   # 内置预设默认接口地址
    default_model: str = ""      # 内置预设默认模型

    def describe(self, client: Any, image: bytes, *,
                 mime_type: str = "image/png",
                 prompt: str = "") -> str:
        """描述图片内容，返回文本描述。"""
        raise NotImplementedError(f"识图供应商插件 {self.name} 未实现 describe()")

    def resolve_request_url(self, base_url: str, api_path: str) -> str:
        """解析识图请求地址（issue #150 语义，与生图插件一致）。

        用户配置的 base_url 分两种情况：
        - 自定义完整端点（非空且不等于预设默认，如代理网关直接给出
          https://api.example.com/v1/chat/completions）→ 直接原样
          作为请求地址使用，不再拼接操作路径；
        - 未配置 / 等于预设默认（含尾斜杠归一）→ 按官方接口在默认
          base_url 后拼接操作路径（如 /chat/completions、
          /models/{model}:generateContent）。
        """
        if base_url and base_url != self.default_base_url:
            return base_url
        return f"{self.default_base_url}{api_path}"


class NotifierPlugin(Plugin):
    """任务消息发送通道插件：任务成功 / 失败时向通道发送消息。

    ``context`` 参数为运行时的 ClaudeExecutor 实例（提供 config / db /
    notifier 等）；各通道自行检查启用条件（如 webhook.enabled、地址配置），
    未启用返回 None（调用方跳过）；发送失败抛异常（调用方统一容错，
    仅记日志不阻塞任务收尾）。
    """

    kind = PluginKind.NOTIFIER

    def send_task_succeeded(self, context: Any, task: dict,
                            repo_name: str = "", repo_url: str = "",
                            issue: dict | None = None) -> dict | None:
        """任务成功消息；未启用返回 None。"""
        return None

    def send_task_failed(self, context: Any, task: dict,
                         reason: str, repo_name: str = "") -> dict | None:
        """任务失败（需人工介入）消息；未启用返回 None。"""
        return None

    def send_test(self, context: Any, repo_name: str = "测试仓库") -> dict:
        """设置页「测试发送」：验证通道配置可用性。"""
        raise NotImplementedError(f"消息通道插件 {self.name} 未实现 send_test()")


class PluginConflictError(RuntimeError):
    """插件重复注册（同名同分类已存在）。"""


class PluginNotFoundError(RuntimeError):
    """按 kind + name 查询插件不存在。"""


class PluginRegistry:
    """插件注册表：按分类 + 名称登记插件，支持查询 / 列表 / 外部加载。

    内部按分类维护有序字典（注册顺序即列表顺序，保证内置插件展示顺序稳定）。
    """

    def __init__(self) -> None:
        self._plugins: dict[PluginKind, dict[str, Plugin]] = {
            kind: {} for kind in PluginKind
        }
        # 外部插件来源跟踪（issue #145 插件管理页）：路径 → 该模块注册的
        # (kind, name) 列表；反向表 (kind, name) → 来源路径，供插件页
        # 展示外部来源与卸载（移除配置 + 注册表）使用。内置插件不入表。
        self._external_by_path: dict[str, list[tuple[PluginKind, str]]] = {}
        self._external_path: dict[tuple[PluginKind, str], str] = {}
        # load_external 执行期间当前模块路径（register 据此标记外部来源）
        self._loading_external_path: str | None = None

    # ---- 注册与查询 ----

    def register(self, plugin: Plugin) -> None:
        """登记插件；同名同分类重复注册抛 PluginConflictError。"""
        if not isinstance(plugin, Plugin):
            raise TypeError(f"注册对象必须是 Plugin 实例: {plugin!r}")
        existing = self._plugins[plugin.kind].get(plugin.name)
        if existing is not None:
            raise PluginConflictError(
                f"插件重复注册: {plugin.kind.value}/{plugin.name}"
                f"（已存在: {existing.description or existing.name}）")
        self._plugins[plugin.kind][plugin.name] = plugin
        # 外部加载期间注册 → 记录来源（供插件管理页展示/卸载）
        if self._loading_external_path is not None:
            self._external_by_path.setdefault(
                self._loading_external_path, []).append((plugin.kind, plugin.name))
            self._external_path[(plugin.kind, plugin.name)] = self._loading_external_path
        logger.debug("插件已注册: %s", plugin)

    def get(self, kind: PluginKind, name: str) -> Plugin:
        """按分类 + 名称取插件；未注册抛 PluginNotFoundError（含可选列表）。"""
        try:
            return self._plugins[kind][name]
        except KeyError:
            names = ", ".join(self.names(kind)) or "无"
            raise PluginNotFoundError(
                f"插件未注册: {kind.value}/{name}（当前可选: {names}）") from None

    def has(self, kind: PluginKind, name: str) -> bool:
        """判断某分类下是否存在指定名称插件。"""
        return name in self._plugins[kind]

    def names(self, kind: PluginKind) -> list[str]:
        """列出某分类下全部插件名（注册顺序）。"""
        return list(self._plugins[kind].keys())

    def list(self, kind: PluginKind) -> list[Plugin]:
        """列出某分类下全部插件（注册顺序）。"""
        return list(self._plugins[kind].values())

    # ---- 外部插件加载 ----

    def load_external(self, module_paths: list[str] | None,
                     errors: list[str] | None = None) -> list[str]:
        """加载外部插件模块（worker.plugin_paths），返回成功加载的路径列表。

        模块内调用 :func:`register_plugin` 完成登记；加载 / 注册失败仅记
        日志告警，不阻塞应用启动（与 webhook 推送同容错策略）。

        ``errors`` 可选收集器：非 None 时把每个失败模块的原因追加进去
        （插件管理页安装校验需要精确错误信息，见 api/plugins.py）。
        """
        loaded: list[str] = []
        for path in module_paths or []:
            path = str(path).strip()
            if not path:
                continue
            try:
                # 模块名唯一化：同一路径重复加载用同一名称（幂等语义由
                # 注册表冲突检测兜底），不同路径互不干扰
                module_name = f"_botler_plugin_{abs(hash(path))}"
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    logger.warning("外部插件模块无效: %s", path)
                    continue
                module = importlib.util.module_from_spec(spec)
                # 执行模块期间 register_plugin 注册到当前注册表
                # （而非全局），保证「加载进哪个注册表就注册进哪个」；
                # 同时记录当前模块路径，register 据此标记外部来源
                global _active_registry
                previous = _active_registry
                _active_registry = self
                previous_path = self._loading_external_path
                self._loading_external_path = path
                try:
                    spec.loader.exec_module(module)
                finally:
                    _active_registry = previous
                    self._loading_external_path = previous_path
                loaded.append(path)
                logger.info("外部插件加载成功: %s", path)
            except Exception as e:  # noqa: BLE001 插件加载失败不阻塞应用启动
                logger.exception("外部插件加载失败: %s", path)
                if errors is not None:
                    errors.append(f"{path}: {e}")
        return loaded


    # ---- 外部插件来源查询与移除（issue #145 插件管理页） ----

    def registered_external(self, path: str) -> list[tuple[PluginKind, str]]:
        """返回指定路径模块注册的全部 (kind, name)（未注册返回空列表）。"""
        return list(self._external_by_path.get(path, []))

    def path_of(self, kind: PluginKind, name: str) -> str | None:
        """外部插件来源路径；内置插件返回 None。"""
        return self._external_path.get((kind, name))

    def remove_external(self, path: str) -> list[tuple[PluginKind, str]]:
        """按来源路径移除该模块注册的全部外部插件，返回移除的列表。

        内置插件（无外部来源）不受影响；路径未注册过插件时幂等返回空。
        """
        removed = list(self._external_by_path.get(path, []))
        for kind, name in removed:
            self._plugins[kind].pop(name, None)
            self._external_path.pop((kind, name), None)
        self._external_by_path.pop(path, None)
        if removed:
            logger.info("外部插件已卸载: %s（%s 个插件）", path, len(removed))
        return removed

    def clear_external(self) -> int:
        """清空全部外部插件（重载前调用），返回移除的插件数量。"""
        total = 0
        for path in list(self._external_by_path):
            total += len(self.remove_external(path))
        return total


# ---- 全局注册表单例与便捷函数 ----

_registry: PluginRegistry | None = None
# load_external 执行外部模块期间的注册目标（None = 全局注册表）
_active_registry: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    """全局插件注册表单例（进程内共享）。"""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


def register_plugin(plugin: Plugin) -> Plugin:
    """注册插件到当前注册表（外部插件加载期间 = 目标注册表；否则 = 全局），
    返回插件本身（供内置 / 外部插件模块调用）。"""
    target = _active_registry if _active_registry is not None else get_registry()
    target.register(plugin)
    return plugin


def get_plugin(kind: PluginKind, name: str) -> Plugin:
    """按分类 + 名称取插件（全局注册表）。"""
    return get_registry().get(kind, name)


def has_plugin(kind: PluginKind, name: str) -> bool:
    """判断全局注册表是否存在指定分类 + 名称的插件。"""
    return get_registry().has(kind, name)


def list_plugins(kind: PluginKind) -> list[Plugin]:
    """列出全局注册表某分类下全部插件（注册顺序）。"""
    return get_registry().list(kind)


def plugin_names(kind: PluginKind) -> list[str]:
    """列出全局注册表某分类下全部插件名（注册顺序）。"""
    return get_registry().names(kind)
