"""概览页 CI/CD 流水线状态 API（issue #39）。

GET /api/pipelines/overview：遍历所有配置仓库（含未启用，issue #39 第二轮；
设置 ui.show_disabled_repos=false 时只返回已启用仓库，issue #142），返回
各仓库最新一次流水线的整体状态与按 jobs 聚合的 stage 进度（stage 顺序
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

issue #483：每条结果另带「最近一次全部成功的流水线」数据
（last_success_pipeline / last_success_stages / last_success_commit_time，
无成功记录或查询失败时为 null/[]）——流水线详情右边栏除当前流水线外
还可切换查看上一次运行全部成功的流水线详情。最新流水线本身已全部成功
时直接复用当前数据（零额外 GitLab 请求）；否则回查流水线历史找最近
一条 status == "success" 的记录并拉取其 jobs 聚合。
"""

from __future__ import annotations

import io
import logging
import threading
import time
import zipfile
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..events import global_bus
from ..gitlab_client import GitLabClient, GitLabError
from ..report_parsers import parse_report
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

# job 产物中不属于「用户意义上的产物」的噪音类型（issue #329）：
# trace = job 运行日志（job.log），metadata = GitLab 内部元数据。
# 这两类每个成功 job 必有、对用户无下载价值，聚合展示时过滤。
_ARTIFACT_NOISE_TYPES = {"trace", "metadata"}

# e2e 截图查看（issue #453）：CI/CD 详情右边栏直接预览 e2e:screenshots
# job（issue #445）产物归档内的截图，替代「只能下载整个 zip 再解压」。
# 截图列表接口需要下载完整 zip 归档解析 png 清单（归档约 20MB），
# 浏览会话内重复点击不应反复下载，做 60 秒 TTL 缓存（key=repo_id+job_id，
# 只缓存解析后的轻量列表，不缓存归档字节）。
_SCREENSHOT_LIST_TTL_SECONDS = 60.0
_SCREENSHOT_LISTS_LOCK = threading.Lock()
_SCREENSHOT_LISTS: dict[tuple[int, int], tuple[float, list[dict]]] = {}

# screenshot-file 接口允许的图片扩展名与对应 content-type
# （e2e:screenshots 产物为 png，兼容其他常见图片产物）
_SCREENSHOT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_SCREENSHOT_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

# 截图预览缩略图（issue #456）：缩略图网格不再直接加载整张原图（e2e
# 整页 Playwright 截图可达数 MB），后端用 Pillow 缩放出小尺寸 JPEG
# 预览图，前端先加载预览图，点击放大进入大图预览时才拉取原图。
# 预览图最长边像素；JPEG 压缩质量；TTL 内存缓存（key=repo_id+job_id+
# path，只缓存小体积预览字节，避免浏览会话内反复下载原图 + 重复缩放）。
_SCREENSHOT_PREVIEW_MAX_EDGE = 480
_SCREENSHOT_PREVIEW_JPEG_QUALITY = 72
_SCREENSHOT_PREVIEW_TTL_SECONDS = 300.0
_SCREENSHOT_PREVIEWS_LOCK = threading.Lock()
_SCREENSHOT_PREVIEWS: dict[tuple[int, int, str], tuple[float, tuple[bytes, str]]] = {}


def _trim_artifacts(job: dict) -> list[dict]:
    """精简 job 产物列表（issue #329）：保留 file_type / filename / size。

    GitLab jobs API 的 artifacts 数组含 trace（job.log）与 metadata
    （元数据），过滤不展示；archive（上传产物 zip）与各报告类型
    （cobertura / sast / dependency_scanning / license_scanning 等）
    原样透传。缺字段 / 非列表 / 元素非对象逐项兜底，不崩溃。
    """
    artifacts = job.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    trimmed: list[dict] = []
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if a.get("file_type") in _ARTIFACT_NOISE_TYPES:
            continue
        trimmed.append({
            "file_type": a.get("file_type"),
            "filename": a.get("filename"),
            "size": a.get("size"),
            "file_format": a.get("file_format"),
        })
    return trimmed

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
    with _SCREENSHOT_LISTS_LOCK:
        _SCREENSHOT_LISTS.clear()
    with _SCREENSHOT_PREVIEWS_LOCK:
        _SCREENSHOT_PREVIEWS.clear()


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
            "id": job.get("id"),
            "name": job.get("name"),
            "status": job.get("status"),
            "allow_failure": bool(job.get("allow_failure")),
            "web_url": job.get("web_url"),
            # issue #329：job 产物明细（前端流水线详情右边栏展示 + 下载）
            "artifacts": _trim_artifacts(job),
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


def _last_success(client, project_id: int, latest: dict | None,
                latest_stages: list[dict], latest_commit_time: str | None
                ) -> tuple[dict | None, list[dict], str | None]:
    """最近一次全部成功的流水线（issue #483）。

    返回 (pipeline, stages, commit_time)：pipeline 为精简后的流水线对象
    （无则 None），stages 为其按 stage 聚合的任务明细（默认 []），
    commit_time 为其对应提交时间（默认 None）。「全部成功」按 GitLab
    流水线 status == "success" 判定（含 allow_failure job 失败的
    passed-with-warnings，与 GitLab 整体成功语义一致）。

    最新流水线本身已全部成功时直接复用当前数据（零额外 GitLab 请求）；
    否则回查流水线历史（id 倒序）找最近一条成功记录并拉取其 jobs 聚合。
    查询失败 / 无成功记录一律静默降级为 (None, [], None)——「上一次
    成功」是增强信息，失败不影响当前流水线展示（与 commit_time issue
    #43 同降级策略，不进 errors 列表）。
    """
    if latest is not None and latest.get("status") == "success":
        return latest, latest_stages, latest_commit_time
    try:
        pipelines = client.list_pipelines(project_id)
    except (GitLabError, httpx.HTTPError):
        return None, [], None
    target = next((pl for pl in pipelines
                   if pl.get("status") == "success"), None)
    if target is None:
        return None, [], None
    try:
        jobs = client.list_pipeline_jobs(project_id, target["id"])
    except (GitLabError, httpx.HTTPError):
        return None, [], None
    return (_trim_pipeline(target), aggregate_stages(jobs),
            _lookup_commit_time(client, project_id, target))


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
    """遍历所有配置仓库（含未启用，issue #39 第二轮），聚合各仓库最新流水线状态。

    issue #142：设置 ui.show_disabled_repos=false 时跳过未启用仓库（不再发起
    GitLab 查询），灵感 / CI/CD 页面均只展示已启用项目。
    """
    pipelines: list[dict] = []
    errors: list[str] = []
    show_disabled = c.config.get().ui_show_disabled_repos
    for row in c.db.list_repos():
        if not show_disabled and not row["enabled"]:
            continue
        entry = {"repo_id": row["id"], "repo_name": row["name"],
                 "enabled": bool(row["enabled"]),
                 "pipeline": None, "stages": [], "commit_time": None,
                 # issue #483：最近一次全部成功的流水线（详情右边栏切换查看）
                 "last_success_pipeline": None, "last_success_stages": [],
                 "last_success_commit_time": None}
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
            # issue #483：最近一次全部成功的流水线——当前流水线非成功时
            # 回查历史，失败静默降级（不中断当前流水线展示）
            entry["last_success_pipeline"], entry["last_success_stages"], \
                entry["last_success_commit_time"] = _last_success(
                    client, row["gitlab_project_id"], entry["pipeline"],
                    entry["stages"], entry["commit_time"])
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
    # issue #478：流水线概览实际重拉（非 TTL 缓存命中）→ 广播 pipeline
    # 事件，多标签页同步刷新（数据确实变化才通知，TTL 内重复请求不刷）
    global_bus.publish({"type": "pipeline"})
    return result


# 支持的报告产物 file_type（issue #337）：sast / dependency_scanning /
# junit 对应「查看报告」按钮与解析器分派；扩展名推导兜底（前端未传
# file_type 时按 .sarif/.json/.xml 判断）
_REPORT_TYPES = {"sast", "dependency_scanning", "junit"}
_REPORT_EXT_TYPES = {".sarif": "sast", ".json": "dependency_scanning",
                     ".xml": "junit"}

# jobs API 的报告条目 filename 是 GitLab 摄入后的内部文件名（例如
# gl-sast-report.json / junit.xml.gz），并不一定是 artifacts:paths 归档内
# 的 CI 原始路径。单文件下载内部名会 404，因此需要从 job 的 ZIP 归档
# 中按报告类型寻找原始文件。限制单文件大小，避免异常归档占用过多内存。
_REPORT_ARCHIVE_MAX_FILE_SIZE = 50 * 1024 * 1024
_REPORT_ARCHIVE_SUFFIXES = {
    "sast": (".sarif", ".json"),
    "dependency_scanning": (".json",),
    "junit": (".xml",),
}
_REPORT_ARCHIVE_KEYWORDS = {
    "sast": ("sast", "sarif", "bandit", "semgrep", "gitleaks"),
    "dependency_scanning": ("depend", "deps", "audit", "vulnerab"),
    "junit": ("junit", "test", "report"),
}


def _response_content(resp: httpx.Response) -> bytes:
    """读取响应字节并确保连接归还连接池。"""
    try:
        return resp.content
    finally:
        resp.close()


def _archive_report_candidates(zf: zipfile.ZipFile, requested: str,
                               file_type: str) -> list[zipfile.ZipInfo]:
    """按报告类型筛选并排序 ZIP 内候选文件。"""
    suffixes = _REPORT_ARCHIVE_SUFFIXES[file_type]
    keywords = _REPORT_ARCHIVE_KEYWORDS[file_type]
    requested_name = requested.rsplit("/", 1)[-1].lower()
    if requested_name.endswith(".gz"):
        requested_name = requested_name[:-3]

    ranked: list[tuple[int, str, zipfile.ZipInfo]] = []
    for info in zf.infolist():
        if info.is_dir() or info.file_size > _REPORT_ARCHIVE_MAX_FILE_SIZE:
            continue
        name = info.filename.replace("\\", "/")
        basename = name.rsplit("/", 1)[-1].lower()
        if not basename.endswith(suffixes):
            continue
        # JSON/XML 归档常同时包含 package.json、coverage.xml 等无关文件；
        # 除精确同名外，仅保留名称带报告语义的候选，避免误解析为空报告。
        exact = basename == requested_name
        keyword_match = any(word in basename for word in keywords)
        if not exact and not keyword_match and not basename.endswith(".sarif"):
            continue
        rank = 0 if exact else (1 if basename.endswith(".sarif") else 2)
        ranked.append((rank, name, info))
    return [item[2] for item in sorted(ranked, key=lambda item: (item[0], item[1]))]


def _report_from_archive(content: bytes, requested: str,
                         file_type: str) -> tuple[str, dict] | None:
    """从 job ZIP 归档定位并解析报告；无候选返回 None。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            candidates = _archive_report_candidates(zf, requested, file_type)
            if not candidates:
                return None
            parse_errors: list[str] = []
            for info in candidates:
                try:
                    text = zf.read(info).decode("utf-8", errors="replace")
                    return info.filename, parse_report(file_type, text)
                except (ValueError, RuntimeError, zipfile.BadZipFile) as e:
                    parse_errors.append(str(e))
    except zipfile.BadZipFile as e:
        raise ValueError("任务产物归档不是有效 ZIP 文件") from e
    detail = parse_errors[0] if parse_errors else "未知格式"
    raise ValueError(f"任务产物归档内的报告无法解析: {detail}")


def _report_file_type(file: str, file_type: str) -> str | None:
    """确定报告类型：显式 file_type 优先，其次按扩展名推导；都不支持返回 None。"""
    if file_type:
        return file_type if file_type in _REPORT_TYPES else None
    ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
    return _REPORT_EXT_TYPES.get("." + ext)


@router.get("/{repo_id}/report")
def pipeline_report(repo_id: int, job_id: int, request: Request,
                    file: str = "", file_type: str = ""):
    """查看指定 job 的报告（解析后 JSON，issue #337）。

    概览页流水线详情抽屉「查看报告」经此代理 GitLab 单文件产物并解析：
    - file_type=sast → SARIF 问题列表（bandit/semgrep/gitleaks）
    - file_type=dependency_scanning → 依赖漏洞列表（deps-python/deps-frontend）
    - file_type=junit → 测试用例明细（backend:test / frontend:build）

    前端浏览器不持有 GitLab token，报告读取与解析统一在后端完成；
    文件路径必须是产物归档内相对路径（拒绝绝对路径与路径穿越）。
    """
    # 路径校验：非空、非绝对路径、任一路径段不得为 ..
    if not file or file.startswith("/") or ".." in file.split("/"):
        raise HTTPException(422, "报告文件路径不合法")
    ftype = _report_file_type(file, file_type)
    if ftype is None:
        raise HTTPException(422, "不支持的报告类型")
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    client = _repo_client(c, row) or c.gitlab
    actual_file = file
    try:
        resp = client.download_job_artifact_file(
            row["gitlab_project_id"], job_id, file)
        content = _response_content(resp)
    except GitLabError as e:
        if e.status_code != 404:
            raise HTTPException(502, f"GitLab 报告下载失败: {e}") from e
        # GitLab jobs API 返回的报告 filename 是摄入后的内部名，不一定
        # 存在于可下载归档；内部名 404 时回退下载 ZIP 并定位 CI 原始路径。
        try:
            archive_resp = client.download_job_artifacts(
                row["gitlab_project_id"], job_id)
            archive_content = _response_content(archive_resp)
        except GitLabError as archive_error:
            if archive_error.status_code == 404:
                raise HTTPException(404, "报告文件不存在或任务无该产物") \
                    from archive_error
            raise HTTPException(
                502, f"GitLab 报告归档下载失败: {archive_error}") from archive_error
        try:
            archived = _report_from_archive(archive_content, file, ftype)
        except ValueError as archive_error:
            raise HTTPException(502, f"报告归档解析失败: {archive_error}") \
                from archive_error
        if archived is None:
            raise HTTPException(404, "报告文件不存在或任务无该产物") from e
        actual_file, report = archived
        return {"job_id": job_id, "filename": actual_file,
                "file_type": ftype, "report": report}
    try:
        text = content.decode("utf-8", errors="replace")
        report = parse_report(ftype, text)
    except ValueError as e:
        raise HTTPException(502, f"报告解析失败: {e}") from e
    return {"job_id": job_id, "filename": actual_file, "file_type": ftype,
            "report": report}


@router.get("/{repo_id}/artifacts")
def pipeline_artifacts_download(repo_id: int, job_id: int, request: Request):
    """下载指定 job 的流水线产物（zip 归档，issue #329）。

    概览页流水线详情右边栏「下载产物」经此代理 GitLab
    GET /projects/{gitlab_project_id}/jobs/{job_id}/artifacts：前端
    浏览器不持有 GitLab token，产物下载统一走后端（per-repo token
    优先，回退全局 bot token，与 /overview 同链路）。GitLab 侧返回
    的字节流式透传，并带 Content-Disposition attachment 供浏览器
    保存为本地文件。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    client = _repo_client(c, row) or c.gitlab
    try:
        resp = client.download_job_artifacts(row["gitlab_project_id"], job_id)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "该任务无产物或不存在")
        raise HTTPException(502, f"GitLab 产物下载失败: {e}") from e
    filename = f"job-{job_id}-artifacts.zip"
    return StreamingResponse(
        resp.iter_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- e2e 截图查看（issue #453）----

# 产物归档内路径校验（列表/单张截图共用）：非空、非绝对路径、任一路径
# 段不得为 ..；与报告文件路径校验同规则，杜绝路径穿越。
def _validate_artifact_path(path: str) -> None:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(422, "产物文件路径不合法")


def _list_png_screenshots(zf: zipfile.ZipFile) -> list[dict]:
    """从 job 产物归档中列出 png 截图（issue #453）。

    e2e:screenshots job 的归档路径形如
    frontend/screenshots/<页面>/<视口>.png（另有 index.html 索引页，
    非 png 不列出）；从路径倒数两级解析 page / viewport 供前端按页面
    分组展示。任何路径结构都能兜底：解析不出页面时用文件名本身。
    按 (page, viewport) 稳定排序保证展示顺序确定。
    """
    shots: list[dict] = []
    for info in zf.infolist():
        if info.is_dir() or info.file_size <= 0:
            continue
        name = info.filename.replace("\\", "/")
        if not name.lower().endswith(".png"):
            continue
        parts = [seg for seg in name.split("/") if seg]
        if len(parts) >= 2:
            page = parts[-2]
            viewport = parts[-1]
        else:
            page = "—"
            viewport = parts[-1] if parts else name
        # viewport 展示名去掉扩展名（desktop-1440x900.png → desktop-1440x900）
        if viewport.lower().endswith(".png"):
            viewport = viewport[:-4]
        shots.append({"path": name, "page": page,
                      "viewport": viewport, "size": info.file_size})
    shots.sort(key=lambda s: (s["page"], s["viewport"]))
    return shots


@router.get("/{repo_id}/screenshots")
def pipeline_screenshots(repo_id: int, job_id: int, request: Request):
    """列出指定 job 产物归档内的 png 截图（issue #453）。

    CI/CD 详情右边栏「查看截图」经此代理下载 job 的 zip 归档并列出
    其中全部 png（路径/页面/视口/大小），前端按页面分组渲染缩略图。
    前端浏览器不持有 GitLab token，产物读取统一在后端完成；结果带
    60 秒 TTL 缓存（归档约 20MB，避免浏览会话内重复下载）。
    归档内无 png（普通 build 产物）返回空列表，不视为错误。
    """
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    key = (repo_id, job_id)
    now = time.monotonic()
    with _SCREENSHOT_LISTS_LOCK:
        hit = _SCREENSHOT_LISTS.get(key)
        if hit is not None and now - hit[0] < _SCREENSHOT_LIST_TTL_SECONDS:
            return {"job_id": job_id, "screenshots": hit[1]}
    client = _repo_client(c, row) or c.gitlab
    try:
        resp = client.download_job_artifacts(row["gitlab_project_id"], job_id)
        content = _response_content(resp)
    except GitLabError as e:
        if e.status_code == 404:
            raise HTTPException(404, "该任务无产物或不存在") from e
        raise HTTPException(502, f"GitLab 产物下载失败: {e}") from e
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            shots = _list_png_screenshots(zf)
    except zipfile.BadZipFile as e:
        raise HTTPException(502, "任务产物归档不是有效 ZIP 文件") from e
    with _SCREENSHOT_LISTS_LOCK:
        _SCREENSHOT_LISTS[key] = (now, shots)
    return {"job_id": job_id, "screenshots": shots}


def _load_screenshot_bytes(client, gitlab_project_id: int, job_id: int,
                             path: str) -> bytes:
    """下载 job 产物归档内单张截图图片的原始字节（issue #456 抽出共用）。

    优先走 GitLab 单文件下载接口（GET /jobs/{job_id}/artifacts/{path}，
    已在真实实例验证对归档内路径可用）；404（个别路径单文件不可达）
    回退下载整个 zip 归档定位提取（与报告查看 issue #337 同模式）。
    screenshot-file（原图）与 screenshot-preview（预览图）共用。
    """
    try:
        resp = client.download_job_artifact_file(
            gitlab_project_id, job_id, path)
        return _response_content(resp)
    except GitLabError as e:
        if e.status_code != 404:
            raise HTTPException(502, f"GitLab 截图下载失败: {e}") from e
        # 单文件接口 404 时回退 zip 归档定位（与报告查看同模式）
        try:
            archive_resp = client.download_job_artifacts(
                gitlab_project_id, job_id)
            archive_content = _response_content(archive_resp)
        except GitLabError as archive_error:
            if archive_error.status_code == 404:
                raise HTTPException(404, "截图不存在或任务无该产物") from archive_error
            raise HTTPException(
                502, f"GitLab 截图归档下载失败: {archive_error}") from archive_error
        try:
            with zipfile.ZipFile(io.BytesIO(archive_content)) as zf:
                try:
                    return zf.read(path)
                except KeyError as missing:
                    raise HTTPException(
                        404, "截图不存在或任务无该产物") from missing
        except zipfile.BadZipFile as archive_error:
            raise HTTPException(502, "任务产物归档不是有效 ZIP 文件") from archive_error


def _make_screenshot_preview(content: bytes, ext: str,
                             path: str = "") -> tuple[bytes, str]:
    """用 Pillow 把原图字节缩放出小尺寸 JPEG 预览图（issue #456）。

    最长边缩放到 _SCREENSHOT_PREVIEW_MAX_EDGE（等比），JPEG 压缩输出；
    RGBA/调色板等模式先转 RGB。Pillow 缺失或图片损坏无法解码时
    返回 (原图字节, 原 content-type)，预览能力降级但不影响查看原图。
    """
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as im:
            im.thumbnail((_SCREENSHOT_PREVIEW_MAX_EDGE,
                          _SCREENSHOT_PREVIEW_MAX_EDGE))
            if im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=_SCREENSHOT_PREVIEW_JPEG_QUALITY)
            return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 预览是增强能力，任何失败都降级回原图
        logger.warning("截图预览图生成失败，回退返回原图（path=%s）", path)
        return content, _SCREENSHOT_CONTENT_TYPES[ext]


def _screenshot_ext(path: str) -> str:
    """校验截图路径并返回小写扩展名（不含点）。非法路径抛 422。"""
    _validate_artifact_path(path)
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in _SCREENSHOT_IMAGE_EXTS:
        raise HTTPException(422, "仅支持查看图片类产物（png/jpg/gif/webp）")
    return ext


@router.get("/{repo_id}/screenshot-file")
def pipeline_screenshot_file(repo_id: int, job_id: int, request: Request,
                             path: str = ""):
    """返回 job 产物归档内单张截图图片字节流（issue #453）。

    优先走 GitLab 单文件下载接口（GET /jobs/{job_id}/artifacts/{path}，
    已在真实实例验证对归档内路径可用）；404（个别路径单文件不可达）
    回退下载整个 zip 归档定位提取（与报告查看 issue #337 同模式）。
    仅允许图片扩展名，杜绝路径穿越。图片字节直接返回，前端 <img>
    即可渲染，无需浏览器持有 GitLab token。
    """
    ext = _screenshot_ext(path)
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    client = _repo_client(c, row) or c.gitlab
    content = _load_screenshot_bytes(
        client, row["gitlab_project_id"], job_id, path)
    return Response(content=content, media_type=_SCREENSHOT_CONTENT_TYPES[ext])


@router.get("/{repo_id}/screenshot-preview")
def pipeline_screenshot_preview(repo_id: int, job_id: int, request: Request,
                                path: str = ""):
    """返回单张截图的缩略预览图（JPEG 小图，issue #456）。

    缩略图网格不再直接加载整张原图（e2e 整页截图可达数 MB），先加载
    后端用 Pillow 缩放出的小尺寸 JPEG 预览图；用户点击放大进入大图
    预览时才由前端请求 /screenshot-file 拉取原图。预览字节带 300 秒
    TTL 内存缓存，避免浏览会话内反复下载原图 + 重复缩放。Pillow 无法
    解码（损坏图片/缺失依赖）时兜底返回原图，预览能力降级不影响查看。
    """
    ext = _screenshot_ext(path)
    c = request.app.state.ctx
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    key = (repo_id, job_id, path)
    now = time.monotonic()
    with _SCREENSHOT_PREVIEWS_LOCK:
        hit = _SCREENSHOT_PREVIEWS.get(key)
        if hit is not None and now - hit[0] < _SCREENSHOT_PREVIEW_TTL_SECONDS:
            preview, media_type = hit[1]
            return Response(content=preview, media_type=media_type)
    client = _repo_client(c, row) or c.gitlab
    content = _load_screenshot_bytes(
        client, row["gitlab_project_id"], job_id, path)
    preview, media_type = _make_screenshot_preview(content, ext, path)
    with _SCREENSHOT_PREVIEWS_LOCK:
        _SCREENSHOT_PREVIEWS[key] = (now, (preview, media_type))
    return Response(content=preview, media_type=media_type)
