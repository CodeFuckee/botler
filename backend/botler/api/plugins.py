"""插件管理 API（issue #145）：插件页面的安装 / 卸载 / 设置接口。

插件体系（issue #140）把执行引擎 / 大模型供应商 / 消息发送通道统一为
插件注册表；本模块提供面向 Web UI 的管理入口：

- ``GET /api/plugins``：按分类分组列出全部已注册插件（内置/外部来源、
  模型供应商默认预设、worker.engine / plugin_paths 上下文）；
- ``POST /api/plugins/install``：安装外部插件模块（试加载校验 → 写入
  worker.plugin_paths → 全局注册表热加载，失败不落盘）；
- ``POST /api/plugins/uninstall``：卸载外部插件（配置与注册表同时移除，
  内置插件不可卸载）；
- ``POST /api/plugins/reload``：按当前 plugin_paths 清空并重载外部插件；
- ``PUT /api/plugins/settings``：默认执行引擎设置（executor 插件设置，
  复用 worker.engine，与设置页「任务调度」卡片同源）。

安全策略：路径只接受部署机本地 Python 模块文件（worker.plugin_paths
既有扩展点），安装前先隔离注册表试加载验证「可导入且至少注册一个插件、
与已安装插件无同名同分类冲突」，任何校验失败都不落盘。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..plugins import (
    PluginKind,
    PluginRegistry,
    get_registry,
    list_plugins,
)

router = APIRouter(prefix="/plugins", tags=["plugins"])

logger = logging.getLogger("botler.api.plugins")

def _plugin_view(plugin, registry: PluginRegistry) -> dict[str, Any]:
    """单插件视图：元信息 + 来源（内置 = 无外部路径）+ 供应商预设。"""
    path = registry.path_of(plugin.kind, plugin.name)
    return {
        "kind": plugin.kind.value,
        "name": plugin.name,
        "description": plugin.description,
        "version": plugin.version,
        # 外部插件来源路径；内置插件为 None（列表页展示「内置」徽章）
        "builtin": path is None,
        "path": path,
        # 生图供应商预设（设置页「生图模型」卡片同源，供插件页展示）
        "display_name": getattr(plugin, "display_name", "") or "",
        "default_base_url": getattr(plugin, "default_base_url", "") or "",
        "default_model": getattr(plugin, "default_model", "") or "",
    }


def _view(c) -> dict[str, Any]:
    """插件管理页视图：三类分组 + worker 上下文。"""
    s = c.config.get()
    registry = get_registry()
    return {
        "engine": s.engine,
        "plugin_paths": list(s.plugin_paths or []),
        "plugins": {
            kind.value: [_plugin_view(p, registry) for p in registry.list(kind)]
            for kind in PluginKind
        },
    }


def _normalize_paths(settings) -> list[str]:
    """worker.plugin_paths 归一（strip + 去空白项），与 settings API 一致。"""
    return [str(p).strip() for p in (settings.plugin_paths or [])
            if str(p).strip()]


@router.get("")
def list_plugins_api(request: Request):
    """插件列表：按分类分组返回全部已注册插件与 worker 上下文。"""
    return _view(request.app.state.ctx)


@router.post("/install")
def install_plugin(request: Request, body: dict):
    """安装外部插件模块（worker.plugin_paths 扩展点）。

    流程：路径非空/文件存在 → 隔离注册表试加载（可导入且至少注册一个
    插件、与已安装插件无同名同分类冲突）→ 写入配置 → 全局热加载。
    任何校验失败 400 拒绝且不落盘；热加载防御性失败回滚配置。
    """
    c = request.app.state.ctx
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path 不能为空")
    paths = _normalize_paths(c.config.get())
    if path in paths:
        raise HTTPException(400, f"插件已安装: {path}")
    if not os.path.isfile(path):
        raise HTTPException(400, f"插件模块文件不存在: {path}")

    # 隔离注册表试加载：校验模块可导入且注册了插件，不污染全局注册表
    probe = PluginRegistry()
    errors: list[str] = []
    probe.load_external([path], errors=errors)
    if errors:
        raise HTTPException(400, f"插件加载失败: {errors[0]}")
    registered = probe.registered_external(path)
    if not registered:
        raise HTTPException(400, f"模块未注册任何插件: {path}")

    # 冲突检测：与全局注册表（内置 + 已装外部插件）同名同分类拒绝
    global_registry = get_registry()
    for kind, name in registered:
        if global_registry.has(kind, name):
            raise HTTPException(
                400, f"插件 {kind.value}/{name} 与已安装插件冲突，安装已拒绝")

    # 校验通过：写入配置（唯一事实来源）后热加载到全局注册表
    c.config.update_worker({"plugin_paths": paths + [path]})
    loaded = global_registry.load_external([path], errors=errors)
    if not loaded:
        # 防御性回滚：热加载失败不残留配置（理论不达，试加载已通过）
        c.config.update_worker({"plugin_paths": paths})
        raise HTTPException(
            500, f"插件热加载失败: {errors[0] if errors else '未知错误'}")
    logger.info("插件安装成功: %s（%s 个插件）", path, len(registered))
    return _view(c)


@router.post("/uninstall")
def uninstall_plugin(request: Request, body: dict):
    """卸载外部插件：从 worker.plugin_paths 与全局注册表同时移除。

    内置插件不在 plugin_paths 中，卸载会命中「未安装」校验被拒绝；
    卸载后已注册插件立即从注册表消失（新任务不再使用），重启后也不会
    再加载。若被卸载的是当前默认执行引擎，任务引擎回退 claude（与
    executor 未知引擎回退策略一致）。
    """
    c = request.app.state.ctx
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path 不能为空")
    paths = _normalize_paths(c.config.get())
    if path not in paths:
        raise HTTPException(400, f"插件未安装: {path}（仅外部插件可卸载）")
    new_paths = [p for p in paths if p != path]
    c.config.update_worker({"plugin_paths": new_paths})
    removed = get_registry().remove_external(path)
    logger.info("插件卸载成功: %s（移除 %s 个插件）", path, len(removed))
    return _view(c)


@router.post("/reload")
def reload_plugins(request: Request):
    """重新加载外部插件：清空全局注册表中的外部插件，按当前
    worker.plugin_paths 重新加载（新增/修改的外部插件模块即时生效）。

    与启动时同容错策略：加载失败仅记日志告警，不阻塞其余插件。
    """
    c = request.app.state.ctx
    paths = _normalize_paths(c.config.get())
    registry = get_registry()
    registry.clear_external()
    errors: list[str] = []
    loaded = registry.load_external(paths, errors=errors)
    if errors:
        logger.warning("插件重载部分失败: %s", errors)
    logger.info("插件重载完成: 成功 %s 个路径，失败 %s 个",
                len(loaded), len(errors))
    return _view(c)


@router.put("/settings")
def update_plugin_settings(request: Request, body: dict):
    """插件设置：默认执行引擎（executor 插件设置）。

    复用 worker.engine（与设置页「任务调度」卡片同源，两处修改互相
    可见）；引擎白名单由插件注册表派生（外部引擎插件自动纳入）。
    """
    c = request.app.state.ctx
    engine = body.get("engine")
    choices = tuple(p.name for p in list_plugins(PluginKind.EXECUTOR))
    choices_text = " / ".join(choices)
    if not isinstance(engine, str) or not engine.strip():
        raise HTTPException(400, f"engine 必须是字符串（可选 {choices_text}）")
    engine = engine.strip().lower()
    if engine not in choices:
        raise HTTPException(
            400, f"engine 取值非法: {engine}（可选 {choices_text}）")
    c.config.update_worker({"engine": engine})
    logger.info("默认执行引擎已更新: %s", engine)
    return _view(c)
