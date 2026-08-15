"""概览页开放 issue 聚合 API（issue #64，issue #71 扩展美化字段，
issue #85 扩展右边栏详情字段，issue #92 扩展添加 issue 表单与创建）。

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

issue #71（参考 GitLab issue 页面美化）：透传 labels（带 GitLab 标签色）、
milestone、assignees、user_notes_count；每仓库额外查一次项目标签
（labels API）建 name→color 映射——查询失败或颜色非法时标签降级为
无色胶囊（中性样式），不中断整体、不进 errors（标签色只是视觉增强）。

issue #92（概览页「添加 Issue」按钮）：
- GET /api/issues/form-meta/{repo_id}：弹窗表单元数据——项目成员
  （members/all，分配人下拉）与项目标签（标签多选）。成员与标签均为
  必填字段的数据来源，任一查询失败返回 502（不可降级为空）。
- POST /api/issues：在指定仓库创建 issue（标题必填、描述选填、
  分配人必填、标签必填）；创建成功后清空 overview 缓存，前端刷新
  列表即可立即看到新 issue。
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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

# issue 对象透传给前端的字段（issue #85：description/author/state/
# created_at 供概览页右边栏展示详情，labels/milestone/assignees/
# user_notes_count 由 _trim_issue 二次加工）
_ISSUE_KEYS = ("iid", "title", "state", "updated_at", "created_at",
               "web_url", "description")

# GitLab 标签颜色必须是 6 位 hex（labels API 约定），非法值不透传，
# 防止拼进前端内联样式注入（issue #71）
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")


def clear_issue_cache() -> None:
    """清空模块级结果缓存（测试隔离用）。"""
    with _CACHE_LOCK:
        _CACHE["expires_at"] = 0.0
        _CACHE["data"] = None


def _trim_issue(issue: dict, label_colors: dict) -> dict:
    """精简 issue 对象：只保留概览页展示需要的字段。

    updated_at（GitLab ISO 8601 带时区）转 UTC 无后缀（前端 fmtAgo
    解析约定，与流水线 commit_time 一致）；缺失时静默为 None。

    issue #71 美化字段：
    - labels：名称数组 + label_colors 映射的 color/text_color（映射
      缺失或颜色非法 → None，前端按中性胶囊降级）；
    - milestone：对象只留 title；assignees：每条只留 name/username/
      avatar_url（头像展示）；user_notes_count：原样透传。

    issue #85 右边栏详情字段：
    - created_at：与 updated_at 同规则转 UTC 无后缀（前端 fmtTime
      解析约定）；
    - author：对象只留 name/username（与 assignees 精简风格一致）；
    - description：原样透传（Markdown 正文，前端 Markdown 组件渲染）；
    - state：原样透传（右边栏状态徽章）。
    """
    trimmed = {k: issue.get(k) for k in _ISSUE_KEYS}
    trimmed["updated_at"] = _commit_time_utc(issue.get("updated_at"))
    trimmed["created_at"] = _commit_time_utc(issue.get("created_at"))
    author = issue.get("author")
    trimmed["author"] = (
        {"name": author.get("name"), "username": author.get("username")}
        if isinstance(author, dict) else None)
    trimmed["labels"] = [
        _label_entry(name, label_colors)
        for name in (issue.get("labels") or [])
        if isinstance(name, str)
    ]
    milestone = issue.get("milestone")
    trimmed["milestone"] = (
        milestone.get("title") if isinstance(milestone, dict) else None)
    trimmed["assignees"] = [
        {"name": a.get("name"), "username": a.get("username"),
         "avatar_url": a.get("avatar_url")}
        for a in (issue.get("assignees") or [])
        if isinstance(a, dict)
    ]
    trimmed["user_notes_count"] = issue.get("user_notes_count")
    return trimmed


def _label_entry(name: str, label_colors: dict) -> dict:
    """标签名 → {name, color, text_color}：颜色从项目标签映射取，
    缺失或非 6 位 hex → None（前端中性胶囊兜底）；color 为 None 时
    text_color 一并置 None（无背景色时文字色无意义）。"""
    meta = label_colors.get(name) if isinstance(label_colors, dict) else None
    color = meta.get("color") if isinstance(meta, dict) else None
    text_color = meta.get("text_color") if isinstance(meta, dict) else None
    if not (isinstance(color, str) and _HEX_COLOR.match(color)):
        color = None
    if not (isinstance(text_color, str) and _HEX_COLOR.match(text_color)):
        text_color = None
    if color is None:
        text_color = None
    return {"name": name, "color": color, "text_color": text_color}


def _fetch_label_colors(client, project_id: int) -> dict:
    """查项目标签建 name→color 映射（issue #71）。

    labels API 失败（GitLabError/网络错误）静默降级为空映射——标签色
    只是视觉增强，不构成仓库数据不可用，不进 errors。
    """
    try:
        labels = client.list_project_labels(project_id)
    except (GitLabError, httpx.HTTPError):
        return {}
    mapping: dict = {}
    for label in labels or []:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            mapping[label["name"]] = {
                "color": label.get("color"),
                "text_color": label.get("text_color"),
            }
    return mapping


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
            # issue #71：项目标签色映射（labels API 失败时降级无色）
            label_colors = _fetch_label_colors(client, row["gitlab_project_id"])
            entry["issues"] = [_trim_issue(i, label_colors) for i in ordered]
            # issue #94：注入 project_id（GitLab 项目数字 ID）供前端
            # 右边栏「关闭 issue」按钮定位仓库（关闭接口按 project_id 匹配）
            for it in entry["issues"]:
                it["project_id"] = row["gitlab_project_id"]
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


@router.post("/{project_id}/{iid}/close")
def close_issue(request: Request, project_id: int, iid: int):
    """关闭指定 issue（issue #94：概览页右边栏「关闭 issue」按钮）。

    定位仓库：按 GitLab project_id 匹配「已启用」仓库（不存在/未启用
    → 404，与概览聚合只聚合启用仓库一致）；客户端选择与聚合一致
    （per-repo token 优先，回退全局 bot token）。成功后清空概览缓存，
    下一轮轮询立即反映关闭状态（issue 从开放列表消失）。

    错误映射：GitLab 404（issue 不存在）→ 404；GitLab 其他错误与
    网络错误 → 502（上游故障如实上报，不假装成功）。GitLab 对已关闭
    issue 再次 close 幂等（返回 200），重复点击/多标签页并发安全。
    """
    c = request.app.state.ctx
    row = None
    for r in c.db.list_repos():
        if r["gitlab_project_id"] == project_id and r["enabled"]:
            row = r
            break
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    client = _repo_client(c, row) or c.gitlab
    try:
        client.close_issue(project_id, iid)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    return {"ok": True, "state": "closed"}


# ---- issue #92：概览页添加 issue 表单与创建 ----


def _trim_member(member: dict) -> dict | None:
    """精简成员对象：只留 id/username/name。

    id 取 user_id（创建 issue 的 assignee_ids 需要 GitLab 用户 id，
    而 members/all 顶层 id 是成员关系 id）；user_id 缺失但有 username
    时 id 暂为 None，由调用方按 username 查 /users 补齐（issue #93：
    GitLab 19 实测 members/all 返回项不含 user_id 字段，仅顶层 id 与
    username/name）。既无 user_id 也无 username 的异常元素返回 None
    （调用方过滤）。
    """
    if not isinstance(member, dict):
        return None
    user_id = member.get("user_id")
    username = (member.get("username")
                if isinstance(member.get("username"), str) else None)
    if user_id is None and username is None:
        return None
    return {"id": user_id, "username": username,
            "name": member.get("name")}


def _form_meta_labels(labels: list[dict]) -> list[dict]:
    """项目标签 → 前端多选展示条目：复用 _label_entry 的 name/color/
    text_color 精简与颜色校验逻辑（issue #71 同款安全兜底）。"""
    color_map = {l["name"]: l for l in labels
                 if isinstance(l, dict) and isinstance(l.get("name"), str)}
    return [_label_entry(l["name"], color_map) for l in labels
            if isinstance(l, dict) and isinstance(l.get("name"), str)]


def _issue_create_client(c, row):
    """创建 issue 用的 GitLab client：与 issue 查询一致的 per-repo
    client 优先（仓库自身 token），无 token 回退全局 bot token。"""
    return _repo_client(c, row) or c.gitlab


@router.get("/form-meta/{repo_id}")
def issue_form_meta(request: Request, repo_id: int):
    """添加 issue 弹窗表单元数据（issue #92）：项目成员 + 项目标签。

    成员与标签分别是分配人（必填）与标签（必填）的数据来源，任一查询
    失败返回 502（不可像 overview 那样降级为空）；repo_id 为平台仓库
    内部 id（非 GitLab project id）。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    if not row["enabled"]:
        raise HTTPException(400, "仓库未启用")
    client = _issue_create_client(c, row)
    try:
        members = client.list_project_members(row["gitlab_project_id"])
        labels = client.list_project_labels(row["gitlab_project_id"])
        # issue #93：members/all 返回项可能不含 user_id（GitLab 19 实测），
        # 按 username 查 /users 补齐真实用户 id；查不到（用户已删除等）
        # 的成员剔除——分配人下拉不能出现无法分配的条目
        member_entries = []
        for m in (_trim_member(x) for x in members or []):
            if m is None:
                continue
            if m["id"] is None:
                m["id"] = client.get_user_id_by_username(m["username"])
                if m["id"] is None:
                    continue
            member_entries.append(m)
    except GitLabError as e:
        raise HTTPException(502, f"获取仓库成员/标签失败: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"获取仓库成员/标签网络错误: {str(e)[:200]}") from e
    return {"members": member_entries, "labels": _form_meta_labels(labels or [])}


class IssueCreate(BaseModel):
    repo_id: int
    title: str
    description: str | None = None
    assignee_id: int | None = None
    labels: list[str]


@router.post("", status_code=201)
def create_issue(request: Request, body: IssueCreate):
    """在指定仓库创建 issue（issue #92）。

    校验：标题必填（GitLab 硬性要求）、分配人必填（用户确认方案）、
    标签必填（用户确认方案，至少一个非空白项）；描述选填。创建成功后
    清空 overview 缓存——前端创建成功后立即刷新列表，不能拿到 10 秒
    TTL 旧缓存（test_create_invalidates_overview_cache 覆盖）。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(body.repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    if not row["enabled"]:
        raise HTTPException(400, "仓库未启用")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    labels = [str(l).strip() for l in (body.labels or []) if str(l).strip()]
    if not labels:
        raise HTTPException(400, "请至少选择一个标签")
    if body.assignee_id is None:
        raise HTTPException(400, "请选择分配人")
    description = (body.description or "").strip() or None
    client = _issue_create_client(c, row)
    try:
        issue = client.create_issue(
            row["gitlab_project_id"], title, description=description,
            assignee_id=body.assignee_id, labels=labels)
    except GitLabError as e:
        raise HTTPException(502, f"创建 issue 失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"创建 issue 网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    # 返回精简后的新 issue（标签颜色省略——创建刚完成，前端随即刷新
    # 列表从 overview 获取完整数据）
    return _trim_issue(issue, {})
