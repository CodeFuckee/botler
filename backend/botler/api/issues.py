"""概览页开放 issue 聚合 API（issue #64）。

GET /api/issues/overview：遍历所有「已启用」仓库，聚合各仓库的开放
（opened）issue，供概览页展示：
- 外层按仓库优先级升序（复用 list_repos 的 ORDER BY priority, id，
  数字小在前，同优先级按仓库 id）；
- 内层按 issue 最后更新时间（updated_at）降序，最新更新在前；
- 只查开放 issue，未启用仓库与已软删除仓库不出现在结果中；
- 每仓库最多 100 条（limit），防大仓库翻页打爆 GitLab API。

与 pipelines/overview（issue #39）一致：per-repo client 优先（直接复用
pipelines 模块的 _repo_client 及其缓存），单仓库失败不中断整体（HTTP
200），失败明细进 errors 列表；结果带 10 秒 TTL 内存缓存。issue 的
updated_at 统一转 UTC 无后缀（复用 pipelines._commit_time_utc），与
前端 fmtAgo/fmtTime 解析约定对齐。
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
from fastapi import APIRouter, Request

from ..gitlab_client import GitLabError
from .pipelines import _commit_time_utc, _repo_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/issues", tags=["issues"])

# 概览页开放 issue 轮询缓存：10 秒 TTL（前端 15s 轮询 + 多标签页并发兜底）
CACHE_TTL_SECONDS = 10.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"expires_at": 0.0, "data": None}

# 每仓库最多聚合的开放 issue 条数（服务端 order_by=updated_at 排序后
# 取前 N 条，即最新更新的 N 条）
MAX_ISSUES_PER_REPO = 100

# issue 对象透传给前端的字段（丢弃 description/author 等无关字段）
_ISSUE_KEYS = ("iid", "title", "updated_at", "web_url")


def clear_issue_cache() -> None:
    """清空模块级结果缓存（测试隔离用）。"""
    with _CACHE_LOCK:
        _CACHE["expires_at"] = 0.0
        _CACHE["data"] = None


def _trim_issue(issue: dict) -> dict:
    """精简 issue 对象：只保留概览页展示需要的字段。

    updated_at（GitLab ISO 8601 带时区）转 UTC 无后缀（前端 fmtAgo
    解析约定，与流水线 commit_time 一致）；缺失时静默为 None。
    """
    trimmed = {k: issue.get(k) for k in _ISSUE_KEYS}
    trimmed["updated_at"] = _commit_time_utc(issue.get("updated_at"))
    return trimmed


def _collect(c) -> dict:
    """遍历所有已启用仓库，聚合各仓库开放 issue（仓库按优先级升序）。"""
    repos: list[dict] = []
    errors: list[str] = []
    total = 0
    for row in c.db.list_repos():
        if not row["enabled"]:
            continue  # 需求：只读已启用的仓库（未启用/已删除不出现）
        entry = {"repo_id": row["id"], "repo_name": row["name"],
                 "priority": row["priority"], "issues": []}
        # 与流水线概览一致（issue #60）：优先用仓库自己 remote url 内嵌的
        # token 查询（复用 pipelines 模块的 per-repo client 缓存），
        # 无 token 回退全局 bot token（兼容旧仓库）
        client = _repo_client(c, row) or c.gitlab
        try:
            issues = client.list_open_issues(
                row["gitlab_project_id"],
                order_by="updated_at", sort="desc", limit=MAX_ISSUES_PER_REPO)
            # 兜底本地排序：GitLab 对 order_by=updated_at 的响应通常已有序，
            # 此处保证字段缺失/API 不遵守排序时输出仍稳定（最新在前）
            ordered = sorted(
                issues, key=lambda i: i.get("updated_at") or "", reverse=True)
            entry["issues"] = [_trim_issue(i) for i in ordered]
            total += len(entry["issues"])
        except GitLabError as e:
            errors.append(f"仓库 {row['name']}: {e}")
        except httpx.HTTPError as e:
            # per-repo client 可能指向不可达 host（remote url 解析出的地址）
            errors.append(f"仓库 {row['name']}: 网络错误: {str(e)[:200]}")
        repos.append(entry)
    return {"repos": repos, "errors": errors, "total": total}


@router.get("/overview")
def issues_overview(request: Request):
    """所有已启用仓库的开放 issue 聚合（10 秒 TTL 缓存）。"""
    c = request.app.state.ctx
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE["data"] is not None and now < _CACHE["expires_at"]:
            return _CACHE["data"]
    result = _collect(c)
    with _CACHE_LOCK:
        _CACHE["expires_at"] = time.monotonic() + CACHE_TTL_SECONDS
        _CACHE["data"] = result
    return result
