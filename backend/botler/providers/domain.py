"""统一代码平台适配层的通用领域模型（issue #484）。

设计目标：核心业务逻辑只依赖本模块的通用模型，不感知具体平台
（GitLab / GitHub / Gitea / 本地演示）。平台专有概念（如 GitLab 的
MergeRequest）只允许出现在对应 Provider 适配器内部，领域层统一使用
``PullRequest``（别名 ``ChangeRequest``）。

每个模型保留 ``raw`` 字段存放平台原始响应，方便调试与后续扩展，
不影响领域层的通用语义。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, TypeAlias

# ---------------------------------------------------------------------------
# 平台标识（Provider.platform 与工厂注册表统一使用）
# ---------------------------------------------------------------------------
PLATFORM_GITLAB = "gitlab"
PLATFORM_GITHUB = "github"
PLATFORM_GITEA = "gitea"
PLATFORM_LOCAL_DEMO = "local_demo"

# 当前支持的平台全集（新增平台 = 新增 Provider 子类 + 注册，无需改核心逻辑）
SUPPORTED_PLATFORMS: tuple[str, ...] = (
    PLATFORM_GITLAB,
    PLATFORM_GITHUB,
    PLATFORM_GITEA,
    PLATFORM_LOCAL_DEMO,
)


# ---------------------------------------------------------------------------
# 通用状态枚举：各 Provider 负责把平台专有状态字符串映射为这些通用值
# ---------------------------------------------------------------------------
class IssueState(str, enum.Enum):
    """Issue 通用状态（GitLab opened/closed、GitHub open/closed 等）。"""

    OPEN = "open"
    CLOSED = "closed"


class PullRequestState(str, enum.Enum):
    """Pull Request / ChangeRequest 通用状态。

    GitLab: opened/closed/merged；GitHub: open/closed（merged 由 merged 标记
    区分）；Gitea: open/closed/merged；LocalDemo 同 Gitea。
    """

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class PipelineStatus(str, enum.Enum):
    """流水线通用状态；无法识别的平台状态一律映射为 UNKNOWN（不抛错）。"""

    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"
    PENDING = "pending"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 各平台状态字符串 → 通用枚举的映射表（Provider 内部共用，避免重复散落）
# ---------------------------------------------------------------------------
_GITLAB_ISSUE_STATE = {
    "opened": IssueState.OPEN,
    "closed": IssueState.CLOSED,
}
_GITHUB_ISSUE_STATE = {
    "open": IssueState.OPEN,
    "closed": IssueState.CLOSED,
}
_GITLAB_PR_STATE = {
    "opened": PullRequestState.OPEN,
    "closed": PullRequestState.CLOSED,
    "merged": PullRequestState.MERGED,
}
_GITHUB_PR_STATE = {
    "open": PullRequestState.OPEN,
    "closed": PullRequestState.CLOSED,
}
_GITEA_PR_STATE = {
    "open": PullRequestState.OPEN,
    "closed": PullRequestState.CLOSED,
    "merged": PullRequestState.MERGED,
}
_GITLAB_PIPELINE_STATUS = {
    "success": PipelineStatus.SUCCESS,
    "failed": PipelineStatus.FAILED,
    "running": PipelineStatus.RUNNING,
    "pending": PipelineStatus.PENDING,
    "created": PipelineStatus.PENDING,
    "waiting_for_resource": PipelineStatus.PENDING,
    "preparing": PipelineStatus.PENDING,
    "canceled": PipelineStatus.CANCELED,
    "cancelled": PipelineStatus.CANCELED,
    "skipped": PipelineStatus.SKIPPED,
    "manual": PipelineStatus.PENDING,
}
_GITHUB_PIPELINE_STATUS = {
    "success": PipelineStatus.SUCCESS,
    "failure": PipelineStatus.FAILED,
    "failed": PipelineStatus.FAILED,
    "cancelled": PipelineStatus.CANCELED,
    "canceled": PipelineStatus.CANCELED,
    "skipped": PipelineStatus.SKIPPED,
    "in_progress": PipelineStatus.RUNNING,
    "queued": PipelineStatus.PENDING,
    "pending": PipelineStatus.PENDING,
    "requested": PipelineStatus.PENDING,
    "completed": PipelineStatus.SUCCESS,  # completed 单独出现时视为成功（结论在 conclusion）
}
_GITEA_STATUS_STATE = {
    "success": PipelineStatus.SUCCESS,
    "failure": PipelineStatus.FAILED,
    "failed": PipelineStatus.FAILED,
    "error": PipelineStatus.FAILED,
    "pending": PipelineStatus.PENDING,
    "warning": PipelineStatus.UNKNOWN,
}
# GitHub Actions job 状态（conclusion 优先，status 兜底）
_GITHUB_JOB_STATUS = {
    "success": PipelineStatus.SUCCESS,
    "failure": PipelineStatus.FAILED,
    "cancelled": PipelineStatus.CANCELED,
    "skipped": PipelineStatus.SKIPPED,
    "in_progress": PipelineStatus.RUNNING,
    "queued": PipelineStatus.PENDING,
    "pending": PipelineStatus.PENDING,
}


def _safe(value: Any, default: str = "") -> str:
    """dict.get 的字符串安全取值：None / 非 str 一律归 default。"""
    return value if isinstance(value, str) else default


def _safe_int(value: Any, default: int = 0) -> int:
    """dict.get 的整数安全取值：非 int / 无法转换一律归 default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _safe_labels(value: Any) -> list[str]:
    """标签安全取值：GitLab 返回逗号分隔字符串，GitHub/Gitea 返回对象数组。

    issue #484：跨平台归一为纯标签名列表（平台颜色等专有属性保留在 raw）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        return names
    return []


def _user_from_payload(payload: Any) -> User | None:
    """从平台用户对象（可能为 None / 非 dict）安全构造 User。"""
    if not isinstance(payload, dict):
        return None
    return User(
        id=payload.get("id"),
        username=_safe(payload.get("username") or payload.get("login")),
        name=_safe(payload.get("name")),
        web_url=_safe(payload.get("web_url") or payload.get("html_url")),
        raw=payload,
    )


# ---------------------------------------------------------------------------
# 通用领域模型
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class User:
    """平台用户（作者 / 负责人等通用表示）。"""

    id: Any
    username: str = ""
    name: str = ""
    web_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class Project:
    """代码仓库的通用表示。

    ``id`` 是平台内稳定标识：GitLab 为数字项目 id（字符串化），
    GitHub / Gitea 为 ``owner/repo``，LocalDemo 为演示项目 key。
    """

    id: str
    name: str = ""
    path: str = ""  # path_with_namespace / full_name
    web_url: str = ""
    default_branch: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_gitlab(cls, payload: dict) -> Project:
        return cls(
            id=str(payload.get("id", "")),
            name=_safe(payload.get("name")),
            path=_safe(payload.get("path_with_namespace")),
            web_url=_safe(payload.get("web_url")),
            default_branch=_safe(payload.get("default_branch")),
            raw=payload,
        )

    @classmethod
    def from_github_like(cls, payload: dict, path: str) -> Project:
        """GitHub / Gitea 仓库对象结构一致（full_name / default_branch）。"""
        return cls(
            id=path,
            name=_safe(payload.get("name")),
            path=_safe(payload.get("full_name")) or path,
            web_url=_safe(payload.get("html_url")),
            default_branch=_safe(payload.get("default_branch")),
            raw=payload,
        )


@dataclass(slots=True)
class Issue:
    """Issue 通用模型（跨平台字段归一，平台细节进 raw）。"""

    id: Any
    iid: int
    title: str
    description: str = ""
    state: IssueState = IssueState.OPEN
    labels: list[str] = field(default_factory=list)
    author: User | None = None
    assignees: list[User] = field(default_factory=list)
    web_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_gitlab(cls, payload: dict) -> Issue:
        return cls(
            id=payload.get("id"),
            iid=_safe_int(payload.get("iid")),
            title=_safe(payload.get("title")),
            description=_safe(payload.get("description")),
            state=_GITLAB_ISSUE_STATE.get(
                _safe(payload.get("state")), IssueState.OPEN),
            labels=_safe_labels(payload.get("labels")),
            author=_user_from_payload(payload.get("author")),
            assignees=[
                u for u in (_user_from_payload(a) for a in payload.get("assignees") or [])
                if u is not None
            ],
            web_url=_safe(payload.get("web_url")),
            created_at=_safe(payload.get("created_at")),
            updated_at=_safe(payload.get("updated_at")),
            raw=payload,
        )

    @classmethod
    def from_github(cls, payload: dict) -> Issue:
        """GitHub issue 对象（含 pull_request 键的是 PR，由调用方过滤）。"""
        return cls(
            id=payload.get("id"),
            iid=_safe_int(payload.get("number")),
            title=_safe(payload.get("title")),
            description=_safe(payload.get("body")),
            state=_GITHUB_ISSUE_STATE.get(
                _safe(payload.get("state")), IssueState.OPEN),
            labels=_safe_labels(payload.get("labels")),
            author=_user_from_payload(payload.get("user")),
            assignees=[
                u for u in (_user_from_payload(a) for a in payload.get("assignees") or [])
                if u is not None
            ],
            web_url=_safe(payload.get("html_url")),
            created_at=_safe(payload.get("created_at")),
            updated_at=_safe(payload.get("updated_at")),
            raw=payload,
        )

    # Gitea issue 结构与 GitHub 一致（state 取值 open/closed 相同）
    from_gitea = from_github


@dataclass(slots=True)
class PullRequest:
    """Pull Request / ChangeRequest 通用模型。

    统一使用 ``PullRequest`` 命名，GitLab 的 MergeRequest 概念只在
    GitLabProvider 内部出现，不进入领域层（issue #484）。
    """

    id: Any
    number: int
    title: str
    state: PullRequestState = PullRequestState.OPEN
    source_branch: str = ""
    target_branch: str = ""
    description: str = ""
    author: User | None = None
    web_url: str = ""
    merged: bool = False
    created_at: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_gitlab(cls, payload: dict) -> PullRequest:
        state = _GITLAB_PR_STATE.get(_safe(payload.get("state")), PullRequestState.OPEN)
        return cls(
            id=payload.get("id"),
            number=_safe_int(payload.get("iid")),
            title=_safe(payload.get("title")),
            state=state,
            source_branch=_safe(payload.get("source_branch")),
            target_branch=_safe(payload.get("target_branch")),
            description=_safe(payload.get("description")),
            author=_user_from_payload(payload.get("author")),
            web_url=_safe(payload.get("web_url")),
            merged=bool(payload.get("merged_at")) or state is PullRequestState.MERGED,
            created_at=_safe(payload.get("created_at")),
            raw=payload,
        )

    @classmethod
    def from_github(cls, payload: dict) -> PullRequest:
        state = _GITHUB_PR_STATE.get(_safe(payload.get("state")), PullRequestState.OPEN)
        merged = bool(payload.get("merged_at")) or bool(payload.get("merged"))
        if state is PullRequestState.CLOSED and merged:
            state = PullRequestState.MERGED
        return cls(
            id=payload.get("id"),
            number=_safe_int(payload.get("number")),
            title=_safe(payload.get("title")),
            state=state,
            source_branch=_safe((payload.get("head") or {}).get("ref")),
            target_branch=_safe((payload.get("base") or {}).get("ref")),
            description=_safe(payload.get("body")),
            author=_user_from_payload(payload.get("user")),
            web_url=_safe(payload.get("html_url")),
            merged=merged,
            created_at=_safe(payload.get("created_at")),
            raw=payload,
        )

    @classmethod
    def from_gitea(cls, payload: dict) -> PullRequest:
        """Gitea PR 与 GitHub 结构基本一致，但 state 可直接为 merged。"""
        state = _GITEA_PR_STATE.get(_safe(payload.get("state")), PullRequestState.OPEN)
        merged = bool(payload.get("merged_at")) or state is PullRequestState.MERGED
        if state is PullRequestState.CLOSED and merged:
            state = PullRequestState.MERGED
        return cls(
            id=payload.get("id"),
            number=_safe_int(payload.get("number")),
            title=_safe(payload.get("title")),
            state=state,
            source_branch=_safe((payload.get("head") or {}).get("ref")),
            target_branch=_safe((payload.get("base") or {}).get("ref")),
            description=_safe(payload.get("body")),
            author=_user_from_payload(payload.get("user")),
            web_url=_safe(payload.get("html_url")),
            merged=merged,
            created_at=_safe(payload.get("created_at")),
            raw=payload,
        )


# ChangeRequest 与 PullRequest 是同一通用概念（issue #484 明示两者皆可）
ChangeRequest: TypeAlias = PullRequest


@dataclass(slots=True)
class IssueComment:
    """Issue 评论通用模型（GitLab note / GitHub issue comment）。"""

    id: Any
    body: str
    author: User | None = None
    created_at: str = ""
    system: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_gitlab(cls, payload: dict) -> IssueComment:
        return cls(
            id=payload.get("id"),
            body=_safe(payload.get("body")),
            author=_user_from_payload(payload.get("author")),
            created_at=_safe(payload.get("created_at")),
            system=bool(payload.get("system")),
            raw=payload,
        )

    @classmethod
    def from_github(cls, payload: dict) -> IssueComment:
        return cls(
            id=payload.get("id"),
            body=_safe(payload.get("body")),
            author=_user_from_payload(payload.get("user")),
            created_at=_safe(payload.get("created_at")),
            system=False,
            raw=payload,
        )

    from_gitea = from_github


Comment: TypeAlias = IssueComment


@dataclass(slots=True)
class Pipeline:
    """流水线通用模型。

    GitLab pipeline / GitHub Actions workflow run / Gitea commit status 聚合
    （LocalDemo 为模拟数据）。``id`` 为平台内流水线标识（字符串化）。
    """

    id: Any
    status: PipelineStatus = PipelineStatus.UNKNOWN
    ref: str = ""
    sha: str = ""
    web_url: str = ""
    created_at: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class PipelineJob:
    """流水线内任务（job）通用模型。"""

    id: Any
    name: str
    stage: str = ""
    status: PipelineStatus = PipelineStatus.UNKNOWN
    web_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class Webhook:
    """Webhook 通用模型。``events`` 为平台事件名列表（平台差异保留在 raw）。"""

    id: Any
    url: str = ""
    events: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 状态映射公共函数（Provider 内部使用，测试可单独覆盖）
# ---------------------------------------------------------------------------
def map_gitlab_pipeline_status(value: Any) -> PipelineStatus:
    return _GITLAB_PIPELINE_STATUS.get(_safe(value).lower(), PipelineStatus.UNKNOWN)


def map_github_pipeline_status(value: Any) -> PipelineStatus:
    return _GITHUB_PIPELINE_STATUS.get(_safe(value).lower(), PipelineStatus.UNKNOWN)


def map_github_job_status(value: Any) -> PipelineStatus:
    return _GITHUB_JOB_STATUS.get(_safe(value).lower(), PipelineStatus.UNKNOWN)


def map_gitea_status_state(value: Any) -> PipelineStatus:
    return _GITEA_STATUS_STATE.get(_safe(value).lower(), PipelineStatus.UNKNOWN)
