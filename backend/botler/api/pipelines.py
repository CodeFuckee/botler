"""概览页 CI/CD 流水线状态 API（issue #39）。

GET /api/pipelines/overview：遍历所有配置仓库（含未启用，issue #39 第二轮），
返回各仓库最新一次流水线的整体状态与按 jobs 聚合的 stage 进度（stage 顺序
= jobs 首次出现顺序，即 .gitlab-ci.yml 定义顺序），供概览页以 GitLab CI/CD
风格展示：
- 运行完成没有（pipeline.status 是否终态）
- 运行成功还是失败
- 运行到哪个阶段（stage 状态：success/failed/running/pending/canceled）
- 还有哪些阶段（stage 列表）
- 最近流水线对应提交的提交时间（commit_time，issue #43；UTC 无后缀，
  查询失败静默为 None，不进 errors）

多仓库场景下单仓库失败不中断整体（HTTP 200），失败明细进 errors 列表
（与 /tasks/reconcile-all 的 issue #38 模式一致）；无流水线仓库 pipeline
为 null；每条结果带 enabled 字段供前端标注未启用仓库。为避免前端轮询
打爆 GitLab API，结果带 10 秒 TTL 内存缓存。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request

from ..gitlab_client import GitLabClient, GitLabError
from ..git_remote import build_repo_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

# 概览页流水线轮询缓存：10 秒 TTL（前端 15s 轮询 + 多标签页并发兜底）
CACHE_TTL_SECONDS = 10.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"expires_at": 0.0, "data": None}

# per-repo GitLab 客户端缓存（issue #60）：key = repo_id，TTL 60 秒。
# 概览页对每个仓库用其本地目录 git remote url 内嵌的 token 建客户端
# （每个仓库有自己的 token），缓存避免每轮轮询重复跑 git 子进程与
# 重建 httpx client；解析失败（回退全局）不缓存，token 轮换后 60 秒
# 内自动生效。
_REPO_CLIENT_TTL_SECONDS = 60.0
_REPO_CLIENTS_LOCK = threading.Lock()
_REPO_CLIENTS: dict[int, tuple[float, GitLabClient]] = {}

# 待运行类 job 状态（stage 聚合时统一视为 pending）
_PENDING_STATUSES = {"pending", "created", "waiting_for_resource",
                     "preparing", "scheduled"}

# pipeline 对象透传给前端的字段（丢弃 user/tag 等无关字段）
_PIPELINE_KEYS = ("id", "iid", "status", "ref", "sha", "web_url",
                  "created_at", "updated_at", "finished_at", "duration")


def clear_pipeline_cache() -> None:
    """清空模块级缓存（测试隔离用）。"""
    with _CACHE_LOCK:
        _CACHE["expires_at"] = 0.0
        _CACHE["data"] = None
    with _REPO_CLIENTS_LOCK:
        _REPO_CLIENTS.clear()


def _stage_status(jobs: list[dict]) -> str:
    """聚合单个 stage 的 job 状态为 stage 状态（参考 GitLab CI/CD 语义）。

    优先级：failed（allow_failure 的 job 失败不算失败，GitLab 显示
    passed with warnings）> running > pending 系列 > canceled > success；
    manual / skipped 不影响聚合结果。空列表视为 success（无 job 可失败）。
    """
    if any(j.get("status") == "failed" and not j.get("allow_failure") for j in jobs):
        return "failed"
    statuses = [j.get("status") for j in jobs]
    if "running" in statuses:
        return "running"
    if any(s in _PENDING_STATUSES for s in statuses):
        return "pending"
    if "canceled" in statuses:
        return "canceled"
    return "success"


def aggregate_stages(jobs: list[dict]) -> list[dict]:
    """按 stage 分组聚合 jobs，返回 [{name, status, jobs:[精简 job]}]。

    stage 顺序 = job id 升序（issue #44 修复）：GitLab jobs API 默认按
    job id 倒序返回，且不响应 sort 参数；job id 为全局自增序列，同一
    pipeline 内 id 升序即 job 创建顺序，与 .gitlab-ci.yml 的 stage 定义
    顺序一致。无 stage 字段的 job 跳过。
    """
    ordered = sorted(jobs, key=lambda j: j.get("id") or 0)
    stages: list[dict] = []
    by_name: dict[str, dict] = {}
    for job in ordered:
        name = job.get("stage")
        if not name:
            continue
        entry = by_name.get(name)
        if entry is None:
            entry = {"name": name, "jobs": []}
            by_name[name] = entry
            stages.append(entry)
        entry["jobs"].append({
            "name": job.get("name"),
            "status": job.get("status"),
            "allow_failure": bool(job.get("allow_failure")),
            "web_url": job.get("web_url"),
        })
    for entry in stages:
        entry["status"] = _stage_status(entry["jobs"])
    return stages


def _trim_pipeline(pipeline: dict) -> dict:
    """精简 pipeline 对象：只保留概览页展示需要的字段。"""
    return {k: pipeline.get(k) for k in _PIPELINE_KEYS}


def _commit_time_utc(value: str | None) -> str | None:
    """GitLab commit API 的 committed_date（ISO 8601 带时区）→ UTC 无后缀
    'YYYY-MM-DD HH:MM:SS'（与 executor 落库时间格式一致，issue #42 约定，
    前端 fmtTime 按此格式解析）。空值 / 解析失败返回 None。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 无时区输入按 UTC 处理
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _lookup_commit_time(client, project_id: int, pipeline: dict) -> str | None:
    """查最近流水线对应提交的提交时间（issue #43）。

    提交时间仅为展示增强信息：commit 查询失败 / 不存在 / 缺字段时静默
    降级为 None（不进 errors 列表），不影响卡片其余部分展示。
    """
    sha = pipeline.get("sha")
    if not sha:
        return None
    try:
        commit = client.get_commit(project_id, sha)
    except GitLabError:
        return None
    if not isinstance(commit, dict):
        return None
    return _commit_time_utc(commit.get("committed_date"))


def _repo_client(c, row) -> GitLabClient | None:
    """per-repo 客户端（带缓存）；解析失败返回 None（调用方回退全局）。"""
    repo_id = row["id"]
    now = time.monotonic()
    with _REPO_CLIENTS_LOCK:
        hit = _REPO_CLIENTS.get(repo_id)
        if hit is not None and now - hit[0] < _REPO_CLIENT_TTL_SECONDS:
            return hit[1]
    client = build_repo_client(row, c.config.get().verify_ssl)
    if client is not None:
        with _REPO_CLIENTS_LOCK:
            _REPO_CLIENTS[repo_id] = (now, client)
    return client


def _collect(c) -> dict:
    """遍历所有配置仓库（含未启用，issue #39 第二轮），聚合各仓库最新流水线状态。"""
    pipelines: list[dict] = []
    errors: list[str] = []
    for row in c.db.list_repos():
        entry = {"repo_id": row["id"], "repo_name": row["name"],
                 "enabled": bool(row["enabled"]),
                 "pipeline": None, "stages": [], "commit_time": None}
        # issue #60：优先用仓库自己 remote url 内嵌的 token 查流水线，
        # 无 token 回退全局 bot token（兼容旧仓库）
        client = _repo_client(c, row) or c.gitlab
        try:
            pipeline = client.get_latest_pipeline(row["gitlab_project_id"])
            if pipeline is None:
                pipelines.append(entry)
                continue
            jobs = client.list_pipeline_jobs(row["gitlab_project_id"], pipeline["id"])
            entry["pipeline"] = _trim_pipeline(pipeline)
            entry["stages"] = aggregate_stages(jobs)
            entry["commit_time"] = _lookup_commit_time(
                client, row["gitlab_project_id"], pipeline)
        except GitLabError as e:
            errors.append(f"仓库 {row['name']}: {e}")
        except httpx.HTTPError as e:
            # per-repo client 可能指向不可达 host（remote url 解析出的地址）
            errors.append(f"仓库 {row['name']}: 网络错误: {str(e)[:200]}")
        pipelines.append(entry)
    return {"pipelines": pipelines, "errors": errors}


@router.get("/overview")
def pipelines_overview(request: Request):
    """所有配置仓库（含未启用）的最新流水线状态（10 秒 TTL 缓存）。"""
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
