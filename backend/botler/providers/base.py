"""统一代码平台适配层抽象接口（issue #484）。

核心业务逻辑只依赖本模块的 ``Provider`` 抽象接口与 ``domain`` 通用模型，
不感知具体平台（GitLab / GitHub / Gitea / 本地演示）。新增平台 = 新增
``Provider`` 子类并注册到工厂，核心逻辑零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from .domain import (
    Issue,
    IssueComment,
    Pipeline,
    PipelineJob,
    Project,
    PullRequest,
    PullRequestState,
    Webhook,
)


class ProviderError(Exception):
    """平台适配层统一异常。

    与平台专有异常（如 GitLabError）解耦：核心业务逻辑捕获 ProviderError
    即可优雅降级，不依赖任何平台细节。``status_code`` 尽量保留平台 HTTP
    状态码（404 资源不存在 / 400 参数非法 / 401 凭据失效等）。
    """

    def __init__(self, message: str, status_code: int | None = None,
                 platform: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.platform = platform


def project_ref(project: Project | str) -> str:
    """把 Provider 接口的 project 参数（Project 或平台内 ref）归一为 ref 字符串。

    GitLab: 数字项目 id 或 path；GitHub / Gitea: ``owner/repo``；
    LocalDemo: 项目 key。
    """
    return project.id if isinstance(project, Project) else str(project)


class Provider(ABC):
    """代码平台适配层抽象基类。

    所有方法均以通用领域模型（domain.py）作为输入输出，方法命名与
    领域概念一致（Issue / PullRequest / Pipeline / Webhook），平台
    专有名词（如 GitLab MergeRequest）只允许出现在子类实现内部。
    """

    #: 平台标识（与 factory 注册表一致，如 "gitlab" / "github"）
    platform: ClassVar[str] = ""
    #: 平台展示名（文档 / 接口输出用）
    display_name: ClassVar[str] = ""

    def __init__(self, url: str = "", token: str | None = None,
                 verify_ssl: bool = True):
        self.url = (url or "").rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl

    # ---- 认证与项目 ----

    @abstractmethod
    def test_connection(self) -> bool:
        """校验 token 有效性；失败抛 ProviderError（401/403 等）。"""

    @abstractmethod
    def resolve_project(self, ref: str) -> Project:
        """把仓库引用（数字 id / owner/repo / URL / scp-like）解析为 Project。"""

    # ---- Issue ----

    @abstractmethod
    def get_issue(self, project: Project | str, iid: int) -> Issue:
        """按平台内编号取 Issue；不存在抛 ProviderError(404)。"""

    @abstractmethod
    def list_open_issues(self, project: Project | str,
                         limit: int | None = None) -> list[Issue]:
        """列出打开状态的 Issue（GitHub/Gitea 需过滤掉 PR 条目）。"""

    @abstractmethod
    def create_issue(self, project: Project | str, title: str,
                     description: str | None = None,
                     labels: list[str] | None = None) -> Issue:
        """创建 Issue；title 为空/超限抛 ProviderError(400)。"""

    @abstractmethod
    def update_issue(self, project: Project | str, iid: int, *,
                     title: str | None = None,
                     description: str | None = None,
                     state: str | None = None,
                     assignee: str | None = None) -> Issue:
        """更新 Issue 字段；state 取值 open/closed，assignee 为用户名。"""

    @abstractmethod
    def add_comment(self, project: Project | str, iid: int,
                    body: str) -> IssueComment:
        """在 Issue 下添加评论。"""

    @abstractmethod
    def list_issue_notes(self, project: Project | str, iid: int,
                         limit: int | None = None) -> list[IssueComment]:
        """Issue 评论列表（升序；limit 非空时最多取最近 limit 条）。"""

    @abstractmethod
    def add_labels(self, project: Project | str, iid: int,
                   labels: list[str]) -> None:
        """给 Issue 追加标签（不存在的标签按平台语义创建或忽略）。"""

    # ---- Pull Request / ChangeRequest ----

    @abstractmethod
    def get_pull_request(self, project: Project | str,
                         number: int) -> PullRequest:
        """按编号取 Pull Request；不存在抛 ProviderError(404)。"""

    @abstractmethod
    def list_pull_requests(self, project: Project | str,
                           state: PullRequestState | None = None,
                           limit: int | None = None) -> list[PullRequest]:
        """列出 Pull Request；state 为 None 时列出全部状态。"""

    @abstractmethod
    def create_pull_request(self, project: Project | str, *,
                            source_branch: str, target_branch: str,
                            title: str,
                            description: str | None = None) -> PullRequest:
        """创建 Pull Request（source → target）。"""

    @abstractmethod
    def merge_pull_request(self, project: Project | str,
                           number: int) -> PullRequest:
        """合并 Pull Request；返回合并后的最新状态。"""

    # ---- 流水线 ----

    @abstractmethod
    def get_latest_pipeline(self, project: Project | str,
                            ref: str | None = None) -> Pipeline | None:
        """最近一次流水线；无流水线返回 None（不抛错）。"""

    @abstractmethod
    def list_pipelines(self, project: Project | str,
                       limit: int = 20) -> list[Pipeline]:
        """流水线列表（新→旧）。"""

    @abstractmethod
    def list_pipeline_jobs(self, project: Project | str,
                           pipeline_id: Any) -> list[PipelineJob]:
        """流水线内任务列表。"""

    # ---- Webhook ----

    @abstractmethod
    def list_webhooks(self, project: Project | str) -> list[Webhook]:
        """项目 Webhook 列表。"""

    @abstractmethod
    def register_webhook(self, project: Project | str, url: str,
                         secret: str | None = None,
                         events: list[str] | None = None) -> Webhook:
        """注册 Webhook；events 为空时使用平台默认事件集。"""

    @abstractmethod
    def unregister_webhook(self, project: Project | str,
                           hook_id: Any) -> None:
        """注销 Webhook；不存在抛 ProviderError(404)。"""
