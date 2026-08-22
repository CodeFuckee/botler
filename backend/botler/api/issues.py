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

issue #100（「添加 issue」界面同步 GitLab 标签颜色）：GitLab labels
API 实际返回的颜色带 # 前缀（实测 "#6699cc"），_normalize_hex 统一
归一化为无 # 的 6 位 hex 透传（overview 列表 / form-meta 弹窗 /
右边栏共用 _label_entry，三处标签胶囊因此全部正确着色）；非法值仍
置 None 中性降级，防样式注入校验不因兼容 # 前缀而放宽。

issue #108（概览页右边栏标记编辑）：
- GET /api/issues/{project_id}/labels：项目标记池（编辑数据源，
  复用 _form_meta_labels 精简 + 颜色归一化）；
- PUT /api/issues/{project_id}/{iid}/labels：add/remove 一次提交
  加删标记（复用 GitLabClient.add_labels），成功后清空概览缓存并
  返回更新后的标记列表。
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ..database import (_parse_db_ts, normalize_issue_created_at,
                            normalize_issue_updated_at)
from ..gitlab_client import GitLabClient, GitLabError
from .pipelines import _commit_time_utc, _repo_client
from .tasks import _task_to_dict  # issue #167：任务执行详情右边栏复用任务序列化

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

# GitLab 标签颜色为 6 位 hex；labels API 实际返回带 # 前缀（实测
# {color: "#6699cc", text_color: "#FFFFFF"}，issue #100）。前端内联
# 样式自行拼 #（`#${color}`），故后端归一化为不带 # 的 6 位 hex 透传；
# 非法值不透传，防止拼进前端内联样式注入（issue #71）
_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _normalize_hex(value) -> str | None:
    """归一化 GitLab 标签颜色（issue #100）：可选 # 前缀 + 6 位 hex →
    无 # 前缀的 6 位 hex；非法值（含畸形 # 前缀、注入尝试）返回 None
    （调用方降级为无色胶囊）。"""
    if not isinstance(value, str):
        return None
    m = _HEX_COLOR.match(value.strip())
    return m.group(1) if m else None


def clear_issue_cache() -> None:
    """清空模块级结果缓存（测试隔离用）。"""
    with _CACHE_LOCK:
        _CACHE["expires_at"] = 0.0
        _CACHE["data"] = None
    # owner client 缓存（issue #130）：测试隔离 + 配置变更后重建
    global _OWNER_CLIENT, _OWNER_CLIENT_TOKEN
    _OWNER_CLIENT = None
    _OWNER_CLIENT_TOKEN = "" 


def _trim_assignees(issue: dict) -> list[dict]:
    """issue 对象 → 精简负责人列表（name/username/avatar_url，issue
    #303：负责人更新响应与 _trim_issue 的 assignees 字段共用）。"""
    return [
        {"name": a.get("name"), "username": a.get("username"),
         "avatar_url": a.get("avatar_url")}
        for a in (issue.get("assignees") or [])
        if isinstance(a, dict)
    ]


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
    trimmed["assignees"] = _trim_assignees(issue)
    trimmed["user_notes_count"] = issue.get("user_notes_count")
    return trimmed


def _label_entry(name: str, label_colors: dict) -> dict:
    """标签名 → {name, color, text_color}：颜色从项目标签映射取，
    经 _normalize_hex 归一化（issue #100：GitLab 返回带 # 前缀，
    统一转无 # 的 6 位 hex，前端拼 # 后即正确着色）；缺失或非法 →
    None（前端中性胶囊兜底）；color 为 None 时 text_color 一并置
    None（无背景色时文字色无意义）。"""
    meta = label_colors.get(name) if isinstance(label_colors, dict) else None
    color = _normalize_hex(meta.get("color")) if isinstance(meta, dict) else None
    text_color = _normalize_hex(meta.get("text_color")) \
        if isinstance(meta, dict) else None
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
                 "priority": row["priority"], "issues": [],
                 # issue #287：透传 GitLab project_id（手动调度顺序接口
                 # 定位仓库用）与该仓库手动调度顺序（iid 按 position 升序，
                 # 从未拖动过为空列表，前端据此渲染拖动后的顺序）
                 "project_id": row["gitlab_project_id"],
                 "manual_order": c.db.list_manual_orders(row["id"])}
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


def get_issues_overview(c) -> dict:
    """所有已启用仓库的开放 issue 聚合（10 秒 TTL 缓存）。

    供 /api/issues/overview 与全局搜索（issue #216 /api/search 的
    issues 模块）复用——搜索不再单独打 GitLab API，与概览页共享
    缓存与 per-repo client。
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE["data"] is not None and now < _CACHE["expires_at"]:
            return _CACHE["data"]
    result = _collect(c)
    with _CACHE_LOCK:
        _CACHE["expires_at"] = time.monotonic() + CACHE_TTL_SECONDS
        _CACHE["data"] = result
    return result


@router.get("/overview")
def issues_overview(request: Request):
    """所有已启用仓库的开放 issue 聚合（10 秒 TTL 缓存）。"""
    c = request.app.state.ctx
    return get_issues_overview(c)


# ---- issue #180：概览页「Issue 完成耗时」统计 ----
# 统计本地 tasks 表中成功终态（succeeded）任务，不依赖 GitLab API：
# 任务成功时系统会给 issue 打 bot-done 标签（executor issue #49），
# 完成耗时 = finished_at - created_at（系统接收时间 → bot-done 打标时间，
# 与任务详情「处理用时」语义一致，见 frontend fmtDuration issue #49）。
# 本地 SQLite 数据量小，直接实时计算，不做缓存。
@router.get("/completion-stats")
def issues_completion_stats(request: Request):
    """概览页「Issue 完成耗时」板块数据（issue #180 + #288）。

    返回已完成 issue 的平均完成耗时与按完成日（UTC）分组的逐日平均
    走势：
    - completed_count：已完成（succeeded）issue 数量；
    - avg_seconds：全部已完成 issue 的平均完成耗时（秒，保留 3 位小数）；
    - trend：按完成日分组 [{date, count, avg_seconds}]，按日期升序，
      供概览页最下方走势图绘制；无数据时 trend 为空数组；
    - repos：每个**已启用**仓库的拆分统计
      [{repo_id, repo_name, completed_count, avg_seconds, trend}]，仓库按
      配置优先级升序（与 overview 一致）；无已完成任务仓库
      completed_count=0 / avg_seconds=None / trend=[]（issue #288）。
    完成耗时为负/字段缺失/解析失败的任务行不计入统计（数据层过滤）；
    已禁用仓库不出现在 repos 列表（其历史任务仍计入全局统计）。
    """
    c = request.app.state.ctx
    rows = c.db.succeeded_durations()  # (repo_id, repo_name, 完成日, 秒)
    total = len(rows)
    if total == 0:
        return {"completed_count": 0, "avg_seconds": None, "trend": [],
                "repos": []}
    avg_all = sum(sec for _, _, _, sec in rows) / total
    by_day: dict[str, list[float]] = {}
    for _, _, day, sec in rows:
        by_day.setdefault(day, []).append(sec)
    trend = [
        {"date": day, "count": len(secs),
         "avg_seconds": round(sum(secs) / len(secs), 3)}
        for day, secs in sorted(by_day.items())
    ]
    # 按仓库拆分（issue #288）：只列已启用仓库，顺序与 overview 一致
    # （仓库按优先级升序）；该仓库无已完成任务时 avg_seconds=None、trend=[]
    by_repo: dict[int, list[tuple[str, float]]] = {}
    for repo_id, _, day, sec in rows:
        by_repo.setdefault(repo_id, []).append((day, sec))
    repos_out: list[dict] = []
    for row in c.db.list_repos():
        if not row["enabled"]:
            continue  # 需求：只展示已开启仓库（未启用/已删除不出现）
        durs = by_repo.get(row["id"], [])
        entry = {"repo_id": row["id"], "repo_name": row["name"],
                 "completed_count": len(durs)}
        if durs:
            entry["avg_seconds"] = round(
                sum(sec for _, sec in durs) / len(durs), 3)
            r_by_day: dict[str, list[float]] = {}
            for day, sec in durs:
                r_by_day.setdefault(day, []).append(sec)
            entry["trend"] = [
                {"date": day, "count": len(secs),
                 "avg_seconds": round(sum(secs) / len(secs), 3)}
                for day, secs in sorted(r_by_day.items())
            ]
        else:
            entry["avg_seconds"] = None
            entry["trend"] = []
        repos_out.append(entry)
    return {
        "completed_count": total,
        "avg_seconds": round(avg_all, 3),
        "trend": trend,
        "repos": repos_out,
    }



# ---- issue #130：概览页 issue 编辑操作优先 owner token ----
# owner gitlab token 只允许在概览页面上编辑 issue、添加 issue、关闭 issue、
# 在 issue 添加评论以及回复 issue 评论的时候使用，其他场景都不得使用；
# agent 无论如何都不能使用 owner gitlab token（executor 会话环境不注入），
# 只能使用自己仓库的认证 token 进行 issue 编辑。

# owner client 缓存（按 token 值判断重建，与 executor._owner_gitlab_client
# 同模式）：配置变化（config.yaml 重载）后下次调用自动换新 client。
_OWNER_CLIENT: GitLabClient | None = None
_OWNER_CLIENT_TOKEN: str = ""


def _owner_client(c) -> GitLabClient | None:
    """owner token 客户端（issue #130）：概览页 issue 编辑操作优先使用。

    未配置 owner token 返回 None（调用方沿用原链路 per-repo → 全局）。
    """
    cfg = c.config.get()
    token = (cfg.gitlab_owner_token or "").strip()
    if not token:
        return None
    global _OWNER_CLIENT, _OWNER_CLIENT_TOKEN
    if _OWNER_CLIENT is None or _OWNER_CLIENT_TOKEN != token:
        _OWNER_CLIENT = GitLabClient(
            cfg.gitlab_url, token, verify_ssl=cfg.verify_ssl)
        _OWNER_CLIENT_TOKEN = token
    return _OWNER_CLIENT


def _issue_edit_call(c, row, call):
    """概览页 issue 编辑操作执行（issue #130 + #132）：必须使用 owner
    token 客户端，绝不静默回退 bot token。

    owner token 只允许在概览页面上编辑 issue、添加 issue、关闭 issue、
    在 issue 添加评论以及回复 issue 评论的时候使用（agent 无论如何都
    不能使用 owner token，见 executor）；非编辑调用（查询、推送、流水
    线等）绝不使用 owner token。

    issue #132 修正：未配置 owner token 或 owner 401/403（token 失效/
    权限不足）时**不再**回退 per-repo/全局 bot token——否则用户经概览页
    发布的评论/回复会以 code01（bot）身份发出（实测复现）。改为返回
    明确错误，引导先在设置页配置/更新 owner token。
    """
    owner = _owner_client(c)
    if owner is None:
        raise HTTPException(
            400,
            "概览页 issue 编辑必须使用 owner token：gitlab.owner_token 未配置，"
            "请先在设置页配置 Owner GitLab Token 后重试")
    try:
        return call(owner)
    except GitLabError as e:
        if e.status_code in (401, 403):
            # issue #133：403 且 GitLab 明确返回 insufficient_scope（token
            # 缺 api scope，实测响应体 {"error":"insufficient_scope",...}）
            # 时直接指明根因，避免用户反复重新保存同一只读 scope 的 token
            # 仍持续 403；其余 401/403 保留原有通用提示（更新 token）
            if e.status_code == 403 and "insufficient_scope" in str(e):
                raise HTTPException(
                    502,
                    "概览页 issue 编辑 owner token 权限不足（403）：token 缺少 "
                    "api scope（只读 scope 无法写评论/编辑 issue），请在设置页 "
                    "重新保存勾选了 api scope 的 Owner GitLab Token") from e
            raise HTTPException(
                502,
                f"概览页 issue 编辑 owner token 失效（{e.status_code}）："
                "请在设置页更新 Owner GitLab Token 后重试") from e
        raise


def _enabled_repo_by_project_id(c, project_id: int) -> dict | None:
    """按 GitLab project_id 查找「已启用」仓库（issue #94 关闭接口与
    issue #97 详情接口共用）；无匹配返回 None。"""
    for r in c.db.list_repos():
        if r["gitlab_project_id"] == project_id and r["enabled"]:
            return r
    return None


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
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    try:
        _issue_edit_call(c, row, lambda cl: cl.close_issue(project_id, iid))
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    return {"ok": True, "state": "closed"}


@router.post("/{project_id}/{iid}/run")
def run_issue(request: Request, project_id: int, iid: int):
    """手动执行一个开放 issue（issue #431）。

    概览页右边栏「执行」按钮的专用入口：不复用失败任务的会话或记录，
    每次有效请求都以 ``manual`` 来源新建任务并交由现有调度器排队。这样
    「执行」与「重试」的断点续跑语义明确分离。

    已存在 queued/running/retrying 任务时返回 409，避免重复入队；GitLab
    已关闭的 issue 返回 400，避免绕过前端隐藏按钮。任务创建使用数据库
    的条件插入，覆盖并发请求在活跃检查与创建之间的竞态。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    active = c.db.find_active_task(project_id, iid)
    if active is not None:
        raise HTTPException(409, "该 issue 已有任务在执行中，无法重复执行")
    client = _repo_client(c, row) or c.gitlab
    try:
        issue = client.get_issue(project_id, iid)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    if issue.get("state") != "opened":
        raise HTTPException(400, "issue 已关闭，无法执行")
    task_id = c.db.create_task(
        row["id"], project_id, iid,
        issue.get("title") or f"issue #{iid}",
        triggered_by="manual",
        issue_labels=issue.get("labels") or [],
        issue_updated_at=normalize_issue_updated_at(issue.get("updated_at")),
        issue_created_at=normalize_issue_created_at(issue.get("created_at")))
    if task_id is None:
        raise HTTPException(409, "该 issue 已有任务在执行中，无法重复执行")
    c.scheduler.enqueue(task_id)
    clear_issue_cache()
    return {"task_id": task_id, "status": "queued", "mode": "created"}


@router.post("/{project_id}/{iid}/retry")
def retry_issue(request: Request, project_id: int, iid: int):
    """重新执行 issue 对应的任务（issue #117：概览页右边栏「重试」按钮）。

    定位仓库：按 GitLab project_id 匹配「已启用」仓库（不存在/未启用
    → 404，与关闭/详情接口一致）；客户端选择与聚合一致（per-repo
    token 优先，回退全局 bot token）。动作语义：
    - 该 issue 已有活跃任务（queued/running/retrying）→ 409（防重复
      执行，与任务页手动重试的冲突判定一致）；
    - 最近任务为 failed/interrupted/canceled_by_user → 复用该任务记录
      重试（重置 queued 重新入队，保留 claude 会话断点续跑；issue #242
      移出队列的任务可重新入队恢复）；
    - 无任务记录或最近任务已终态成功（如 bot-failed 标签残留但任务
      实际已完成）→ 新建任务入队（triggered_by=manual，记录 issue
      标签/更新时间供调度器按优先级排序）。
    成功后清空概览缓存，前端刷新列表即可看到 issue 进入「运行中」组。
    错误映射：GitLab 404（issue 不存在）→ 404；GitLab 其他错误与
    网络错误 → 502。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    # 并发防重：已有活跃任务（排队/执行/重试中）不允许再次重试
    active = c.db.find_active_task(project_id, iid)
    if active is not None:
        raise HTTPException(409, "该 issue 已有任务在执行中，无法重试")
    latest = c.db.find_latest_task(project_id, iid)
    # 最近任务为 failed/interrupted/canceled_by_user → 复用任务记录重试
    # （与任务页手动重试一致：清空失败字段、保留断点续跑会话；
    #  canceled_by_user 为 issue #242 移出队列的终态，可重新入队）
    if latest is not None and latest["status"] in (
            "failed", "interrupted", "canceled_by_user"):
        result = c.db.retry_task(latest["id"])
        if result == "conflict":
            raise HTTPException(409, "该 issue 已有任务在执行中，无法重试")
        if result != "ok":
            raise HTTPException(400, f"任务重试失败（{result}）")
        # issue #69：清除历史停止请求残留，避免 worker 领取时被打回 interrupted
        c.executor.clear_stop_request(latest["id"])
        c.scheduler.enqueue(latest["id"])
        clear_issue_cache()
        return {"task_id": latest["id"], "status": "queued", "mode": "retried"}
    # 无失败任务可重试（无记录 / 最近任务已终态成功）→ 新建任务入队，
    # 拉取 issue 标题与标签（与对账补入队的入队字段一致）
    client = _repo_client(c, row) or c.gitlab
    try:
        issue = client.get_issue(project_id, iid)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    task_id = c.db.create_task(
        row["id"], project_id, iid,
        issue.get("title") or f"issue #{iid}",
        triggered_by="manual",
        issue_labels=issue.get("labels") or [],
        issue_updated_at=normalize_issue_updated_at(issue.get("updated_at")),
        issue_created_at=normalize_issue_created_at(issue.get("created_at")))
    if task_id is None:
        # 竞态：创建期间出现活跃任务（极端并发），按冲突处理
        raise HTTPException(409, "该 issue 已有任务在执行中，无法重试")
    c.scheduler.enqueue(task_id)
    clear_issue_cache()
    return {"task_id": task_id, "status": "queued", "mode": "created"}


@router.post("/{project_id}/{iid}/prioritize")
def prioritize_issue(request: Request, project_id: int, iid: int):
    """排队任务优先处理（issue #242：概览页 issue 右边栏「优先处理」按钮）。

    定位仓库：按 GitLab project_id 匹配「已启用」仓库（不存在/未启用
    → 404，与关闭/详情接口一致）。动作语义：该 issue 存在排队中
    （queued）任务时，把该任务人工优先级置顶（top，manual_priority=0），
    调度器派发时优先于仓库/标签规则；无排队任务（无任务/running/终态）
    → 400（已 running 任务不受影响）。操作写入 task_logs，成功后清空
    概览缓存，前端刷新即可看到 issue 进入「运行中」组（若被优先派发）。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    active = c.db.find_active_task(project_id, iid)
    if active is None or active["status"] != "queued":
        raise HTTPException(400, "仅排队中（queued）的任务可优先处理")
    result, new_priority = c.db.reorder_manual_priority(active["id"], "top")
    if result != "ok":
        raise HTTPException(400, f"优先处理失败（{result}）")
    clear_issue_cache()
    return {"task_id": active["id"], "status": "queued",
            "manual_priority": new_priority}


# ---- issue #108：概览页右边栏标记编辑 ----


@router.get("/{project_id}/labels")
def project_labels(request: Request, project_id: int):
    """项目标记池（issue #108：概览页右边栏「编辑标记」数据源）。

    仓库定位与关闭接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；客户端选择与聚合一致（per-repo token 优先，回退
    全局 bot token）。标记池是编辑的数据来源，查询失败不可降级为空
    （降级会让用户误以为仓库无标记）→ 502（与 form-meta 的标签查询
    错误处理一致）。

    返回复用 _form_meta_labels 精简：{name, color, text_color}，颜色
    经 _normalize_hex 归一化（issue #100 的 # 前缀兼容）。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    client = _repo_client(c, row) or c.gitlab
    try:
        labels = client.list_project_labels(project_id)
    except GitLabError as e:
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    return {"labels": _form_meta_labels(labels or [])}


# ---- issue #303：概览页右边栏负责人编辑 ----


@router.get("/{project_id}/members")
def project_members(request: Request, project_id: int):
    """项目成员清单（issue #303：概览页右边栏负责人下拉数据源）。

    仓库定位与标签池接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；客户端选择与聚合一致（per-repo token 优先，回退
    全局 bot token，只读查询）。成员是负责人下拉的数据来源，查询失败
    不可降级为空（降级会让用户误以为仓库无成员）→ 502（与 form-meta
    的成员查询错误处理一致）。

    返回复用 _project_members 精简：{id, username, name}，id 为
    GitLab 用户 id（更新 issue 负责人的 assignee_ids 需要该值，与
    members/all 顶层成员关系 id 区分）。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    client = _repo_client(c, row) or c.gitlab
    try:
        members = _project_members(client, project_id)
    except GitLabError as e:
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    return {"members": members}


# ---- issue #287：概览页「其他」分组手动调度顺序 ----
# 用户在「调度器执行顺序」排序下拖动 issue 上下移动，把调整后的整组顺序
# 全量保存（PUT），调度器派发时优先按此顺序（见 scheduler._task_sort_key）。
# 仅存 Botler 本地数据库（issue_manual_orders 表），不改 GitLab 侧字段。

# 单仓库手动调度顺序条数上限：概览页单仓库最多聚合 100 条开放 issue，
# 此处放宽到 200（防止异常请求写爆本地库；正常拖动远小于该值）
MAX_MANUAL_ORDERS_PER_REPO = 200


class ManualOrderUpdate(BaseModel):
    """手动调度顺序更新体：整组 issue_iid 列表（按用户拖动后的顺序）。"""
    iids: list[int]


@router.get("/{project_id}/manual-orders")
def get_manual_orders(request: Request, project_id: int):
    """读取仓库手动调度顺序：iid 按 position 升序（用户拖动后的顺序）。

    仓库不存在/未启用 → 404。返回 {"project_id", "iids"}。"""
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    return {"project_id": project_id,
            "iids": c.db.list_manual_orders(row["id"])}


@router.put("/{project_id}/manual-orders")
def put_manual_orders(request: Request, project_id: int,
                      body: ManualOrderUpdate):
    """全量保存仓库手动调度顺序（issue #287：拖动 issue 后整组顺序）。

    body.iids 为拖动后的整组 issue_iid 列表：非正整数/重复项剔除（保序
    去重，与 _normalize_label_names 风格一致）、空列表清空手动顺序、
    超长截断到 MAX_MANUAL_ORDERS_PER_REPO。保存成功后清空 overview 缓存
    （下一次轮询即返回新顺序）。返回保存后的 {"project_id", "iids"}。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    seen: set[int] = set()
    out: list[int] = []
    for iid in body.iids:
        if not isinstance(iid, int) or iid <= 0 or iid in seen:
            continue
        seen.add(iid)
        out.append(iid)
        if len(out) >= MAX_MANUAL_ORDERS_PER_REPO:
            break
    c.db.replace_manual_orders(row["id"], out)
    clear_issue_cache()
    return {"project_id": project_id, "iids": out}


def _normalize_label_names(names: list[str] | None) -> list[str]:
    """标记名归一化（issue #108）：去空白、去空串、保序去重，与创建
    issue 的标签校验风格一致。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in names or []:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


class IssueLabelsUpdate(BaseModel):
    add: list[str] | None = None
    remove: list[str] | None = None


@router.put("/{project_id}/{iid}/labels")
def update_issue_labels(request: Request, project_id: int, iid: int,
                        body: IssueLabelsUpdate):
    """更新 issue 标记（issue #108：概览页右边栏标记编辑）。

    语义：add 添加标记、remove 移除标记，同一次请求同时生效（复用
    GitLabClient.add_labels 的 add_labels/remove_labels 同请求语义）。
    标记名归一化（去空白、去重）；add/remove 归一化后全空 → 400
    （空提交无意义）。注意 GitLab 的 remove_labels 对不存在的标记
    返回 404——前端按「当前标记集合 diff」提交 remove，保证移除的
    都是实际存在的标记。

    仓库定位与客户端选择与关闭接口一致；成功后清空概览缓存（下一
    轮轮询立即反映新标记）并返回更新后的标记列表（从 GitLab 返回的
    更新后 issue 提取 labels + 项目标记色映射，前端 label-pill 直接
    使用；色映射查询失败降级无色，与 overview 行为一致）。

    错误映射：GitLab 404（issue 不存在）→ 404；GitLab 其他错误与
    网络错误 → 502。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    add = _normalize_label_names(body.add)
    remove = _normalize_label_names(body.remove)
    if not add and not remove:
        raise HTTPException(400, "没有需要变更的标记")
    try:
        issue = _issue_edit_call(
            c, row, lambda cl: cl.add_labels(project_id, iid, add, remove=remove or None))
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    label_colors = _fetch_label_colors(_repo_client(c, row) or c.gitlab, project_id)
    return {"labels": _trim_issue(issue, label_colors)["labels"]}


class IssueAssigneeUpdate(BaseModel):
    """负责人更新体（issue #303）：assignee_id 为 GitLab 用户 id
    （项目成员接口返回的 id）；None 表示清除负责人。"""
    assignee_id: int | None = None


@router.put("/{project_id}/{iid}/assignee")
def update_issue_assignee(request: Request, project_id: int, iid: int,
                          body: IssueAssigneeUpdate):
    """更新 issue 负责人（issue #303：概览页右边栏负责人下拉修改）。

    assignee_id 为 GitLab 用户 id（GET /api/issues/{project_id}/members
    返回的 id）；传 None 清除负责人（GitLabClient.update_issue_assignee
    将 assignee_ids 置空数组，GitLab 侧同步生效）。编辑操作走 owner
    token（_issue_edit_call，issue #130）；成功后清空概览缓存（下一轮
    轮询即反映新负责人）并返回更新后 issue 的精简负责人列表（前端本地
    即时展示，无需等待轮询）。

    错误映射：GitLab 404（issue 不存在）→ 404；GitLab 其他错误与
    网络错误 → 502。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    assignee_ids = [body.assignee_id] if body.assignee_id is not None else []
    try:
        issue = _issue_edit_call(
            c, row,
            lambda cl: cl.update_issue_assignee(project_id, iid, assignee_ids))
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    return {"assignees": _trim_assignees(issue)}


# ---- issue #97：概览页右边栏评论与活动 ----

# 每 issue 最多拉取的 notes 条数（升序后截尾，即时间线上最近的 N 条）
MAX_NOTES_PER_ISSUE = 100


def _trim_note(note: dict) -> dict:
    """精简 note 对象（issue #97：评论/活动展示字段）。

    - system：GitLab 系统事件标志（true=活动，false=用户评论，前端
      按此分区渲染），缺失按 False（评论）兜底；
    - author：对象只留 name/username/avatar_url（头像展示）；
    - created_at：与 issue 时间同规则转 UTC 无后缀（前端 fmtTime
      解析约定），缺失静默为 None；
    - body：原样透传（评论正文前端 Markdown 渲染；系统活动为纯文本）。
    """
    author = note.get("author")
    return {
        "id": note.get("id"),
        "body": note.get("body"),
        "system": bool(note.get("system")),
        "author": (
            {"name": author.get("name"), "username": author.get("username"),
             "avatar_url": author.get("avatar_url")}
            if isinstance(author, dict) else None),
        "created_at": _commit_time_utc(note.get("created_at")),
    }


# issue #349：每 issue 最多拉取的标记活动事件条数（与 notes 同模式，
# 防大 issue 翻页打爆 API；事件按 id 升序即时间正序，截尾保留最近 N 条）
MAX_LABEL_EVENTS_PER_ISSUE = 100


def _trim_label_event(event: dict) -> dict:
    """精简 resource_label_events 对象（issue #349：标记活动展示字段）。

    - action：add/remove（谁添加/移除了标记，未知值原样透传前端兜底）；
    - label：标记对象只留 name（标签胶囊/文案展示）；
    - user：操作人对象只留 name/username/avatar_url（头像展示，与
      note 同规则），缺失按 None（前端显示「—」）；
    - created_at：与 note 同规则转 UTC 无后缀（前端 fmtTime 解析约定），
      缺失静默为 None；
    - id：事件 id（前端列表 key）。
    """
    user = event.get("user")
    label = event.get("label")
    return {
        "id": event.get("id"),
        "action": event.get("action"),
        "label": label.get("name") if isinstance(label, dict) else None,
        "user": (
            {"name": user.get("name"), "username": user.get("username"),
             "avatar_url": user.get("avatar_url")}
            if isinstance(user, dict) else None),
        "created_at": _commit_time_utc(event.get("created_at")),
    }


def _task_duration_seconds(latest) -> float | None:
    """任务记录 → 完成耗时秒数（issue #300）。

    仅当该 issue 最近任务成功终态（succeeded——任务完成时系统会给 issue
    打 bot-done 标签，executor issue #49）且 created_at / finished_at
    均存在、解析成功、用时非负时返回（finished_at - created_at，与
    issue #180 完成耗时统计语义一致：系统接收时间 → bot-done 打标时间）；
    未完成（无任务记录/运行中/失败/中断）或时间数据异常（缺字段、格式
    非法、时钟回拨产生负值）返回 None，前端「完成耗时」行显示「—」。
    """
    if latest is None:
        return None
    if latest["status"] != "succeeded":
        return None
    start = _parse_db_ts(latest["created_at"])
    end = _parse_db_ts(latest["finished_at"])
    if start is None or end is None:
        return None
    sec = (end - start).total_seconds()
    if sec < 0:
        return None
    return round(sec, 3)


def _task_engine_name(latest) -> str | None:
    """任务记录 → 执行引擎（issue #120 回退链第 1/2 级）。

    1. 最近一次任务落库的 engine（executor.run_task 按任务写入）；
    2. 旧任务未落库 engine 时按断点续跑会话字段推断（dsh_session_id
       → dsh，hermes_history → hermes，claude_session_id → claude）；
    无任务记录返回 None（调用方回退全局 worker.engine）。
    """
    if latest is None:
        return None
    engine = latest["engine"]
    if engine:
        return engine
    if latest["dsh_session_id"]:
        return "dsh"
    if latest["hermes_history"]:
        return "hermes"
    if latest["claude_session_id"]:
        return "claude"
    return None


@router.get("/{project_id}/{iid}/detail")
def issue_detail(request: Request, project_id: int, iid: int):
    """issue 评论与活动详情（issue #97：概览页右边栏展示）。

    仓库定位与关闭接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；客户端选择与聚合一致（per-repo token 优先，回退
    全局 bot token）。notes 升序拉取、每 issue 最多 100 条。不缓存
    ——抽屉打开时按需拉取、关闭即弃，评论更新后重新打开立即生效。

    响应字段：
    - notes：评论与活动（system 分区）；
    - label_events（issue #349）：标记活动事件（谁添加/移除了哪个标记，
      id/action/label/user/created_at 精简字段，与 notes 同时间规则）；
    - engine（issue #120）：该 issue 最近任务实际使用的执行引擎
      （无任务记录回退全局 worker.engine）；
    - task_id（issue #290）：该 issue 最近一条任务记录 id——已执行过
      （有任务记录）才返回，从未执行/尚未派发为 null，概览页右边栏
      「任务」行据此展示对应任务 id；
    - task_duration_seconds（issue #300）：该 issue 最近任务的完成耗时
      秒数——仅当任务成功终态（succeeded）且有合法 created_at /
      finished_at 时返回（finished_at - created_at，与 issue #180
      完成耗时语义一致）；未完成/时间数据异常为 null，概览页右边栏
      「完成耗时」行据此展示（未完成显示「—」）。

    错误映射：GitLab 404（issue 不存在）→ 404；GitLab 其他错误与
    网络错误 → 502（上游故障如实上报，前端展示重试按钮）。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    client = _repo_client(c, row) or c.gitlab
    try:
        notes = client.list_issue_notes(project_id, iid,
                                        limit=MAX_NOTES_PER_ISSUE)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "issue 不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    # 标记活动（issue #349）：resource_label_events 独立于 notes 拉取
    # （实测 notes 不含标记加/删事件），随 detail 一并返回供右边栏
    # 「标记活动」区块展示；拉取失败（旧版 GitLab 无此端点/网络故障）
    # 静默降级为空列表——标记活动是补充信息，不因上游故障拖垮整个
    # 抽屉（notes 等主内容仍正常展示）
    try:
        label_events = client.list_issue_label_events(
            project_id, iid, limit=MAX_LABEL_EVENTS_PER_ISSUE)
    except (GitLabError, httpx.HTTPError):
        label_events = []
    # 异常元素（非 dict）防御性过滤，不因单条坏数据拖垮整个抽屉；
    # engine（issue #120）：该 issue 最近任务实际执行的引擎（无任务
    # 记录回退全局 worker.engine），前端右边栏「执行引擎」行据此展示；
    # task_id（issue #290）：该 issue 最近一条任务记录 id——已执行过
    # （有任务记录）才有值，从未执行/尚未派发为 null，前端右边栏
    # 「任务」行据此展示对应任务 id（无任务显示「—」）；
    # task_duration_seconds（issue #300）：该 issue 最近任务完成耗时
    # 秒数——仅成功终态（succeeded）且时间字段合法时返回，其余为
    # null，前端右边栏「完成耗时」行据此展示（未完成显示「—」）
    latest = c.db.find_latest_task(project_id, iid)
    return {"notes": [_trim_note(n) for n in notes or [] if isinstance(n, dict)],
            "label_events": [_trim_label_event(e) for e in label_events or []
                             if isinstance(e, dict)],
            "engine": (_task_engine_name(latest)
                       or str(getattr(c.config.get(), "engine", "")
                              or "claude").strip().lower()),
            "task_id": latest["id"] if latest is not None else None,
            # issue #242：该 issue 最近任务的状态——概览页右边栏据此对
            # 排队中（queued）任务展示「优先处理」按钮（置顶人工优先级）
            "task_status": latest["status"] if latest is not None else None,
            "task_duration_seconds": _task_duration_seconds(latest)}


@router.get("/{project_id}/{iid}/tasks")
def issue_tasks(request: Request, project_id: int, iid: int):
    """该 issue 的任务执行记录（issue #167：概览页右边栏「查看执行的
    详情」数据源）。

    仓库定位与 detail/close 接口一致（project_id 匹配「已启用」仓库，
    不存在/未启用 → 404）；按 project_id + issue_iid 查任务表（id 倒序、
    最新在前，同 issue 多条任务记录——重新指派/对账补入队/手动重试——
    全部返回）。任务字典复用任务列表接口的序列化（含 status/engine/
    commit_url/时间等），供前端第二层右边栏展示与切换；任务详情
    （日志/实时执行）仍由既有 GET /api/tasks/{task_id} 与
    /api/tasks/{task_id}/execution、/api/tasks/{task_id}/events 提供。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    rows = c.db.list_tasks_by_issue(project_id, iid)
    # issue #237：带出仓库级覆盖字段，任务序列化按「仓库级 > 全局」解析生效参数
    repo = {"name": row["name"], "url": row["url"],
            "timeout_seconds": row["timeout_seconds"],
            "max_retries": row["max_retries"], "engine": row["engine"]}
    return {"tasks": [_task_to_dict(r, repo, settings=c.config.get())
                      for r in rows],
            "total": len(rows)}


# ---- issue #125：概览页右边栏添加评论与回复评论 ----


class IssueCommentCreate(BaseModel):
    """评论内容（添加评论 / 回复评论共用）。"""
    body: str


def _comment_text(raw) -> str:
    """评论内容归一化：去首尾空白，空串返回 ''（调用方按 400 处理）。"""
    return (raw or "").strip()


def _create_comment(request: Request, project_id: int, iid: int,
                    text: str, reply_to: int | None = None) -> dict:
    """添加评论 / 回复评论共用实现（issue #125）。

    定位仓库与客户端选择与 detail 接口一致；正文为空 → 400（GitLab
    对空正文同样拒绝，提前校验避免上游错误）；回复时先由
    GitLabClient.reply_to_note 解析目标评论所在 discussion（notes
    API 不含 discussion_id）。成功后清空概览缓存（user_notes_count
    与 updated_at 已变化，下一轮轮询立即反映）。
    """
    c = request.app.state.ctx
    row = _enabled_repo_by_project_id(c, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    if not text:
        raise HTTPException(400, "评论内容不能为空")
    try:
        def _do(cl):
            if reply_to is None:
                return cl.add_comment(project_id, iid, text)
            return cl.reply_to_note(project_id, iid, reply_to, text)
        note = _issue_edit_call(c, row, _do)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "评论不存在") from e
        raise HTTPException(502, f"GitLab API 错误: {e}") from e
    except httpx.HTTPError as e:
        # per-repo client 可能指向不可达 host（remote url 解析出的地址）
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    return {"note": _trim_note(note)}


# Issue 评论附件只接受 GitLab 能稳定预览的常见格式；限制 10 MiB，避免
# 浏览器误选大文件后占用应用内存和 GitLab 上传配额。
_COMMENT_IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
}
_COMMENT_IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _is_supported_comment_image(data: bytes, mime_type: str) -> bool:
    """校验声明 MIME 与文件签名，拒绝伪装成图片的任意文件。"""
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def _markdown_image(alt: str, url: str) -> str:
    """生成安全的 GitLab Markdown 图片引用，仅接受 GitLab 上传相对路径。"""
    if not isinstance(url, str) or not url.startswith("/uploads/"):
        raise HTTPException(502, "GitLab 图片上传未返回有效地址")
    # Markdown alt 文本不允许换行、反斜杠或方括号影响语法结构。
    safe_alt = re.sub(r"[\\\[\]\r\n]", "_", alt or "图片")
    return f"![{safe_alt}]({url})"


async def _upload_project_image(request: Request, row, image: UploadFile) -> dict:
    """上传图片到指定仓库的 GitLab 项目并返回可嵌入 Issue 的 Markdown。

    创建 Issue 与发表评论必须共用同一校验、owner token 和 GitLab 上传链路，
    确保图片不落本地、也不会出现 Botler 与 GitLab 内容不同步。
    """
    c = request.app.state.ctx
    mime_type = (image.content_type or "").lower().strip()
    if mime_type not in _COMMENT_IMAGE_MIME_TYPES:
        raise HTTPException(400, "仅支持 PNG、JPEG、GIF、WebP 图片")
    data = await image.read()
    if not data:
        raise HTTPException(400, "图片内容为空")
    if len(data) > _COMMENT_IMAGE_MAX_BYTES:
        raise HTTPException(400, "图片不能超过 10 MiB")
    if not _is_supported_comment_image(data, mime_type):
        raise HTTPException(400, "图片格式与文件内容不匹配")
    filename = (image.filename or "图片").strip() or "图片"
    project_id = row["gitlab_project_id"]
    try:
        uploaded = _issue_edit_call(
            c, row, lambda cl: cl.upload_issue_attachment(
                project_id, filename, data, mime_type))
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "仓库不存在") from e
        raise HTTPException(502, f"GitLab 图片上传失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"网络错误: {str(e)[:200]}") from e
    return {"markdown": _markdown_image(filename, (uploaded or {}).get("url"))}


@router.post("/{repo_id}/attachments", status_code=201)
async def upload_create_issue_attachment(request: Request, repo_id: int,
                                         image: UploadFile = File(...)):
    """上传添加 Issue 弹窗选择的图片（issue #437）。

    先上传至目标 GitLab 项目，前端拿到 Markdown 后再创建 Issue，以便图片
    与 Issue 正文在 GitLab 中同步展示；上传失败时不会创建半成品 Issue。
    """
    row = request.app.state.ctx.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    if not row["enabled"]:
        raise HTTPException(400, "仓库未启用")
    return await _upload_project_image(request, row, image)


@router.post("/{project_id}/{iid}/attachments", status_code=201)
async def upload_issue_attachment(request: Request, project_id: int, iid: int,
                                  image: UploadFile = File(...)):
    """上传一张待发布到 Issue 评论的图片（issue #432）。"""
    row = _enabled_repo_by_project_id(request.app.state.ctx, project_id)
    if row is None:
        raise HTTPException(404, "仓库不存在或未启用")
    return await _upload_project_image(request, row, image)


@router.post("/{project_id}/{iid}/comments", status_code=201)
def create_issue_comment(request: Request, project_id: int, iid: int,
                         body: IssueCommentCreate):
    """添加 issue 评论（issue #125：概览页右边栏「添加评论」）。

    正文必填（去空白后为空 → 400）。成功后清空概览缓存并返回新建
    评论的精简对象（前端本地即时追加展示，无需重新拉取详情）。
    错误映射：仓库不存在/未启用 → 404；GitLab 404（issue 不存在）
    → 404；GitLab 其他错误与网络错误 → 502。
    """
    return _create_comment(request, project_id, iid,
                           _comment_text(body.body))


@router.post("/{project_id}/{iid}/comments/{note_id}/reply", status_code=201)
def reply_issue_comment(request: Request, project_id: int, iid: int,
                        note_id: int, body: IssueCommentCreate):
    """回复 issue 某条评论（issue #125：概览页右边栏「回复评论」）。

    与添加评论同一正文校验；note_id 为被回复评论的 id（后端经
    discussions API 解析其所在线程后追加回复）。成功后清空概览缓存
    并返回新建回复的精简对象。错误映射：仓库不存在/未启用 → 404；
    GitLab 404（issue/评论不存在）→ 404；GitLab 其他错误与网络
    错误 → 502。
    """
    return _create_comment(request, project_id, iid,
                           _comment_text(body.body), reply_to=note_id)


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


def _project_members(client, project_id: int) -> list[dict]:
    """项目成员 → 精简条目（issue #303：负责人下拉 / issue #92：添加
    issue 分配人下拉共用）。

    复用 _trim_member 精简 + issue #93 的 user_id 补齐：members/all
    返回项可能不含 user_id（GitLab 19 实测），按 username 查 /users
    补齐真实用户 id；查不到（用户已删除等）的成员剔除——分配人/负责人
    下拉不能出现无法分配的条目。
    """
    members = client.list_project_members(project_id)
    out: list[dict] = []
    for m in (_trim_member(x) for x in members or []):
        if m is None:
            continue
        if m["id"] is None:
            m["id"] = client.get_user_id_by_username(m["username"])
            if m["id"] is None:
                continue
        out.append(m)
    return out


def _form_meta_labels(labels: list[dict]) -> list[dict]:
    """项目标签 → 前端多选展示条目：复用 _label_entry 的 name/color/
    text_color 精简与颜色归一化逻辑（issue #100：GitLab 返回的 # 前缀
    颜色在此归一化为无 # 的 6 位 hex，前端 label-pill 直接拼 # 着色）。"""
    color_map = {l["name"]: l for l in labels
                 if isinstance(l, dict) and isinstance(l.get("name"), str)}
    return [_label_entry(l["name"], color_map) for l in labels
            if isinstance(l, dict) and isinstance(l.get("name"), str)]


def _issue_create_client(c, row):
    """创建 issue 用的 GitLab client：与 issue 查询一致的 per-repo
    client 优先（仓库自身 token），无 token 回退全局 bot token。

    仅 form-meta（添加 issue 弹窗的成员/标签数据源，只读查询）使用；
    create_issue 写操作走 _issue_edit_call（issue #130：owner token
    优先）。"""
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
        member_entries = _project_members(client, row["gitlab_project_id"])
        labels = client.list_project_labels(row["gitlab_project_id"])
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
    标签必填（用户确认方案，至少一个非空白项）；描述选填。描述为空
    （None/空白）时透传 None，由 GitLabClient.create_issue 在发送
    GitLab API 请求前兜底填充标题（issue #103）。创建成功后清空
    overview 缓存——前端创建成功后立即刷新列表，不能拿到 10 秒
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
    try:
        issue = _issue_edit_call(
            c, row,
            lambda cl: cl.create_issue(
                row["gitlab_project_id"], title, description=description,
                assignee_id=body.assignee_id, labels=labels))
    except GitLabError as e:
        raise HTTPException(502, f"创建 issue 失败: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"创建 issue 网络错误: {str(e)[:200]}") from e
    clear_issue_cache()
    # 返回精简后的新 issue（标签颜色省略——创建刚完成，前端随即刷新
    # 列表从 overview 获取完整数据）
    return _trim_issue(issue, {})
