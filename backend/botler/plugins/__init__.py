"""Botler 插件体系（issue #140）。

把执行引擎、大模型 API 供应商、webhook 发送任务消息做成插件形式：
- ``base``：插件模型（PluginKind / Plugin / PluginRegistry）+ 全局注册表
- ``executors``：任务执行引擎插件（claude / hermes / dsh）
- ``models``：生图 API 供应商插件（gemini_nano_banana / openai_gpt_image）
- ``vision_models``：识图 API 供应商插件（gemini_vision / openai_vision / custom，issue #152）
- ``notifiers``：任务消息发送通道插件（webhook / in_app）
- ``auto_issue``：任务失败自动创建 GitLab issue 上报插件（issue #347）

导入本包即注册全部内置插件；外部插件通过 ``worker.plugin_paths`` 配置的
Python 模块路径在应用启动时加载。调用方统一使用
:func:`get_plugin` / :func:`list_plugins` 等便捷函数访问注册表。
"""

from .base import (
    ImageProviderPlugin,
    Plugin,
    PluginConflictError,
    PluginKind,
    PluginNotFoundError,
    PluginRegistry,
    ExecutorPlugin,
    NotifierPlugin,
    VisionProviderPlugin,
    get_plugin,
    get_registry,
    has_plugin,
    list_plugins,
    plugin_names,
    register_plugin,
)

# 生图供应商插件公共类型 / 常量（image_models.py 依赖，从 models 模块再导出）
from .models import (
    DEFAULT_SIZE,
    DEFAULT_TIMEOUT,
    ImageModelError,
    ImageResult,
)

# 识图供应商插件公共类型 / 常量（vision_models.py 依赖，从 vision_models 模块再导出）
from .vision_models import (
    DEFAULT_TIMEOUT as VISION_DEFAULT_TIMEOUT,
    DEFAULT_VISION_PROMPT,
    VisionModelError,
    format_request_info,
)

# 导入内置插件子模块触发注册（导入顺序即注册顺序，也即列表展示顺序）
from . import executors as _executors  # noqa: F401
from . import models as _models  # noqa: F401
from . import vision_models as _vision_models  # noqa: F401
from . import notifiers as _notifiers  # noqa: F401
from . import auto_issue as _auto_issue  # noqa: F401

__all__ = [
    "DEFAULT_SIZE",
    "DEFAULT_TIMEOUT",
    "VISION_DEFAULT_TIMEOUT",
    "DEFAULT_VISION_PROMPT",
    "format_request_info",
    "ImageModelError",
    "VisionModelError",
    "ImageProviderPlugin",
    "VisionProviderPlugin",
    "ImageResult",
    "Plugin",
    "PluginConflictError",
    "PluginKind",
    "PluginNotFoundError",
    "PluginRegistry",
    "ExecutorPlugin",
    "NotifierPlugin",
    "get_plugin",
    "get_registry",
    "has_plugin",
    "list_plugins",
    "plugin_names",
    "register_plugin",
]
