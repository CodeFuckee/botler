"""Provider 工厂与注册表（issue #484）。

新增平台 = 实现 ``Provider`` 子类 + ``register()`` 一行注册，核心业务
逻辑无需任何改动（这是「后续新增平台不影响核心逻辑」的关键）。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Provider, ProviderError
from .domain import PLATFORM_GITLAB, PLATFORM_GITHUB, PLATFORM_GITEA, PLATFORM_LOCAL_DEMO
from .gitea_provider import GiteaProvider
from .github_provider import GitHubProvider
from .gitlab_provider import GitLabProvider
from .local_demo_provider import LocalDemoProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """平台 → Provider 类的注册表；create() 按平台名实例化。"""

    def __init__(self) -> None:
        self._classes: dict[str, type[Provider]] = {}

    def register(self, platform: str, provider_cls: type[Provider]) -> None:
        """注册平台实现（同名覆盖，便于测试替换）。"""
        key = platform.strip().lower()
        if not key:
            raise ValueError("平台名不能为空")
        self._classes[key] = provider_cls
        logger.info("注册平台 Provider: %s → %s", key, provider_cls.__name__)

    def unregister(self, platform: str) -> None:
        """注销平台实现（测试用）。"""
        self._classes.pop(platform.strip().lower(), None)

    def create(self, platform: str, **kwargs: Any) -> Provider:
        """按平台名实例化 Provider；未知平台抛 ProviderError(400)。"""
        key = platform.strip().lower()
        provider_cls = self._classes.get(key)
        if provider_cls is None:
            supported = "、".join(self.supported())
            raise ProviderError(
                f"不支持的代码平台: {platform}（当前支持: {supported}）",
                400, platform)
        return provider_cls(**kwargs)

    def supported(self) -> list[str]:
        """已注册平台名列表。

        内置平台按平台常量顺序输出（保持稳定）；额外注册的平台按注册
        顺序追加（dict 保序），保证自定义平台同样可被列出与创建。
        """
        order = (PLATFORM_GITLAB, PLATFORM_GITHUB, PLATFORM_GITEA, PLATFORM_LOCAL_DEMO)
        result = [p for p in order if p in self._classes]
        for p in self._classes:
            if p not in result:
                result.append(p)
        return result

    def describe(self) -> list[dict[str, str]]:
        """平台元信息（文档 / 接口输出用）：[{platform, display_name}]。"""
        return [
            {"platform": p, "display_name": self._classes[p].display_name}
            for p in self.supported()
        ]


# 进程内共享注册表（main.py 与外部调用方统一使用）
registry = ProviderRegistry()
registry.register(PLATFORM_GITLAB, GitLabProvider)
registry.register(PLATFORM_GITHUB, GitHubProvider)
registry.register(PLATFORM_GITEA, GiteaProvider)
registry.register(PLATFORM_LOCAL_DEMO, LocalDemoProvider)


def create_provider(platform: str, url: str | None = None,
                    token: str | None = None, verify_ssl: bool = True,
                    **kwargs: Any) -> Provider:
    """便捷工厂：按平台名创建 Provider。

    用法示例::

        gitlab = create_provider("gitlab", url="https://gitlab.example.com",
                                 token="glpat-xxx")
        github = create_provider("github", url="https://api.github.com",
                                 token="ghp_xxx")
        demo = create_provider("local_demo")  # 零依赖演示，无需 url/token

    未知平台 / 缺 token 等参数错误由 ProviderError 表达（status_code 400）。
    """
    if url is None and platform.lower() != PLATFORM_LOCAL_DEMO:
        raise ProviderError(
            f"创建 {platform} Provider 必须提供 url", 400, platform)
    if url is not None:
        kwargs.setdefault("url", url)
    if token is not None:
        kwargs.setdefault("token", token)
    kwargs.setdefault("verify_ssl", verify_ssl)
    return registry.create(platform, **kwargs)


def supported_platforms() -> list[str]:
    """当前支持的平台名列表（给文档 / 前端展示用）。"""
    return registry.supported()


__all__ = [
    "ProviderRegistry",
    "registry",
    "create_provider",
    "supported_platforms",
]
