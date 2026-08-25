"""统一代码平台适配层（issue #484）。

把 GitLab 的强绑定抽象为统一的 ``Provider`` 适配层，核心业务逻辑只依赖
本包的抽象接口与通用领域模型（``PullRequest`` / ``ChangeRequest`` /
``Issue`` 等），不感知具体平台。

当前平台：
- ``gitlab``      GitLabProvider（包装既有 GitLabClient，全功能）
- ``github``      GitHubProvider（GitHub REST API v3）
- ``gitea``       GiteaProvider（Gitea API v1，私有化部署）
- ``local_demo``  LocalDemoProvider（内存演示，零依赖）

新增平台：实现 ``Provider`` 子类并 ``registry.register(平台名, 类)``，
核心业务逻辑零改动。
"""

from .base import Provider, ProviderError, project_ref
from .domain import (
    ChangeRequest,
    Comment,
    Issue,
    IssueComment,
    IssueState,
    Pipeline,
    PipelineJob,
    PipelineStatus,
    PLATFORM_GITEA,
    PLATFORM_GITHUB,
    PLATFORM_GITLAB,
    PLATFORM_LOCAL_DEMO,
    Project,
    PullRequest,
    PullRequestState,
    SUPPORTED_PLATFORMS,
    User,
    Webhook,
)
from .factory import (
    ProviderRegistry,
    create_provider,
    registry,
    supported_platforms,
)
from .gitea_provider import GiteaProvider
from .github_provider import GitHubProvider
from .gitlab_provider import GitLabProvider
from .local_demo_provider import LocalDemoProvider

__all__ = [
    # 抽象接口
    "Provider",
    "ProviderError",
    "project_ref",
    # 领域模型
    "Project",
    "User",
    "Issue",
    "IssueComment",
    "Comment",
    "PullRequest",
    "ChangeRequest",
    "Pipeline",
    "PipelineJob",
    "Webhook",
    "IssueState",
    "PullRequestState",
    "PipelineStatus",
    # 平台标识
    "PLATFORM_GITLAB",
    "PLATFORM_GITHUB",
    "PLATFORM_GITEA",
    "PLATFORM_LOCAL_DEMO",
    "SUPPORTED_PLATFORMS",
    # 工厂
    "ProviderRegistry",
    "registry",
    "create_provider",
    "supported_platforms",
    # 实现
    "GitLabProvider",
    "GitHubProvider",
    "GiteaProvider",
    "LocalDemoProvider",
]
