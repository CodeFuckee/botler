"""仓库管理 API：列表 / 添加（自动识别 project_id + 注册 webhook）/ 更新 / 删除 / 连通性测试 / 模版。"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import RepoConfig
from ..database import DEFAULT_PRIORITY
from ..gitlab_client import GitLabError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repos", tags=["repos"])


class RepoCreate(BaseModel):
    url: str | None = Field(
        default=None, description="GitLab 项目 URL（如 https://host/group/project.git）或数字 project_id，与 local_path 二选一")
    local_path: str | None = Field(
        default=None, description="本地 git 仓库文件夹路径（与 url 二选一），平台运行 git remote -v 读取 remote")
    remote_name: str | None = Field(
        default=None, description="local_path 方式下选中的 remote 名称（如 origin）")
    name: str | None = None
    prompt_template: str | None = None
    enabled: bool = True
    priority: int | None = Field(
        default=None, ge=1, le=999,
        description="调度优先级（issue #51）：1~999 整数，数字越小越优先，缺省 100")
    webhook_url: str | None = Field(
        default=None, description="webhook 回调地址覆盖（默认用当前请求的 base_url）")


class LocalPathBody(BaseModel):
    local_path: str = Field(description="本地 git 仓库文件夹路径")


def _resolve_repo_source(body: RepoCreate, c) -> tuple[str, str | None, str | None]:
    """确定仓库来源，返回 (url, local_path, remote_name)。

    url 方式直接透传；local_path 方式在服务端运行 git remote -v，
    从选中的 remote 读取仓库 URL（local_path 同时被记录为执行工作区）。
    """
    if body.local_path:
        from ..git_remote import NoGitRemoteError, list_local_remotes

        try:
            remotes = list_local_remotes(body.local_path)
        except NoGitRemoteError as e:
            raise HTTPException(400, f"读取本地仓库 remote 失败: {e}")
        if not body.remote_name:
            raise HTTPException(400, "local_path 方式需要指定 remote_name")
        match = next((r for r in remotes if r["name"] == body.remote_name), None)
        if match is None:
            available = ", ".join(r["name"] for r in remotes)
            raise HTTPException(400, f"本地仓库没有 remote「{body.remote_name}」（可用: {available}）")
        return match["url"], body.local_path, body.remote_name
    if not body.url or not body.url.strip():
        raise HTTPException(400, "需要提供 url 或 local_path 其中之一")
    return body.url.strip(), None, None


class RepoUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    prompt_template: str | None = None
    enabled: bool | None = None
    priority: int | None = Field(
        default=None, ge=1, le=999,
        description="调度优先级（issue #51）：1~999 整数，数字越小越优先")


def _repo_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "gitlab_project_id": row["gitlab_project_id"],
        "name": row["name"],
        "url": row["url"],
        "local_path": row["local_path"],
        "remote_name": row["remote_name"],
        "prompt_template": row["prompt_template"],
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "created_at": row["created_at"],
    }


def _sync_repo_to_config(app, repo_dict: dict) -> None:
    """将仓库写回 config.yaml（config 是唯一事实来源）。"""
    config = app.state.ctx.config
    settings = config.get()
    kept = [r for r in settings.repos if r.project_id != repo_dict["gitlab_project_id"]]
    kept.append(RepoConfig(
        project_id=repo_dict["gitlab_project_id"],
        name=repo_dict["name"],
        url=repo_dict["url"],
        enabled=repo_dict["enabled"],
        prompt_template=repo_dict["prompt_template"] or None,
        local_path=repo_dict.get("local_path"),
        remote_name=repo_dict.get("remote_name"),
        priority=repo_dict["priority"],
    ))
    config.update_repos([config.repo_to_config_dict(r) for r in kept])


@router.get("")
def list_repos(request: Request):
    c = ctx_of(request)
    rows = c.db.list_repos()
    return {"repos": [_repo_row_to_dict(r) for r in rows]}


@router.get("/browse")
def browse_directories(request: Request, path: str | None = None):
    """列出服务器上指定路径的子目录（前端目录选择对话框逐级浏览用）。

    返回当前路径、父路径与子目录列表；每个子目录标记是否为 git 仓库、
    是否可读。伪文件系统（/proc、/sys 等）不展示。

    path 缺省或为空时，定位到配置的默认初始目录（browse.default_path，
    未配置则回退服务器用户主目录 ~），供对话框打开时初始定位使用；
    显式传 path=/ 仍从根目录开始浏览。
    """
    from ..dir_browse import DirBrowseError, list_subdirectories, resolve_default_path

    target = path.strip() if path else ""
    if not target:
        target = resolve_default_path(request.app.state.ctx.config.get().browse_default_path)
    try:
        return list_subdirectories(target)
    except DirBrowseError as e:
        raise HTTPException(400, str(e))


@router.post("/discover")
def discover_remote(request: Request, body: LocalPathBody):
    """读取本地 git 仓库的 remote 列表（前端展示，供用户选择）。"""
    c = ctx_of(request)
    from ..git_remote import NoGitRemoteError, list_local_remotes

    try:
        remotes = list_local_remotes(body.local_path)
    except NoGitRemoteError as e:
        raise HTTPException(400, f"读取本地仓库 remote 失败: {e}")
    return {"remotes": remotes, "local_path": body.local_path}


@router.post("", status_code=201)
def add_repo(request: Request, body: RepoCreate):
    c = ctx_of(request)
    config = c.config.get()

    url, local_path, remote_name = _resolve_repo_source(body, c)
    try:
        project = c.gitlab.resolve_project(url)
    except GitLabError as e:
        raise HTTPException(400, f"无法识别项目: {e}")

    project_id = int(project["id"])
    name = body.name or project.get("path") or project.get("name") or str(project_id)
    url = project.get("http_url_to_repo") or project.get("web_url") or url

    existing = c.db.get_repo_by_project_id(project_id)
    if existing:
        raise HTTPException(409, f"仓库已存在（id={existing['id']}，name={existing['name']}）")

    # 注册 webhook（默认用当前请求的 base_url，允许覆盖）
    webhook_base = body.webhook_url or str(request.base_url).rstrip("/")
    from ..gitlab_client import GitLabClient
    temp_client = GitLabClient(config.gitlab_url, config.gitlab_token,
                               verify_ssl=config.verify_ssl,
                               webhook_base_url=webhook_base)
    try:
        temp_client.register_webhook(project_id, config.webhook_secret)
    except GitLabError as e:
        raise HTTPException(502, f"注册 webhook 失败: {e}")

    repo_id = c.db.upsert_repo(
        project_id=project_id, name=name, url=url,
        prompt_template=body.prompt_template, enabled=body.enabled,
        local_path=local_path, remote_name=remote_name,
        priority=body.priority if body.priority is not None else DEFAULT_PRIORITY)
    _sync_repo_to_config(request.app, _repo_row_to_dict(c.db.get_repo(repo_id)))

    source = f"local_path={local_path}" if local_path else f"url={url}"
    logger.info("添加仓库 %s (project=%s, %s) 并注册 webhook", name, project_id, source)
    return _repo_row_to_dict(c.db.get_repo(repo_id))


@router.put("/{repo_id}")
def update_repo(request: Request, repo_id: int, body: RepoUpdate):
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")

    fields = body.model_dump(exclude_unset=True)
    if fields:
        c.db.update_repo(repo_id, **fields)
    updated = _repo_row_to_dict(c.db.get_repo(repo_id))
    # 仅当仓库仍存在于 config 时同步（避免把已删除的仓库写回去）
    if any(r.project_id == updated["gitlab_project_id"] for r in c.config.get().repos):
        _sync_repo_to_config(request.app, updated)
    return updated


@router.delete("/{repo_id}")
def delete_repo(request: Request, repo_id: int):
    """删除仓库：注销 webhook + 从 config 移除 + db 软删除（保留任务历史）。"""
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")

    # 注销 webhook（尽力而为，失败不阻塞删除）
    try:
        c.gitlab.unregister_webhook(row["gitlab_project_id"])
    except GitLabError as e:
        logger.warning("注销 webhook 失败（忽略）: %s", e)

    config = c.config.get()
    config.update_repos([
        config.repo_to_config_dict(r) for r in config.repos
        if r.project_id != row["gitlab_project_id"]
    ])
    c.db.update_repo(repo_id, enabled=False)
    logger.info("删除仓库 %s (project=%s)", row["name"], row["gitlab_project_id"])
    return {"ok": True}


@router.post("/{repo_id}/test")
def test_repo(request: Request, repo_id: int):
    """测试连通性：token 有效性 + 项目可达性 + webhook 状态。"""
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    result: dict = {}
    try:
        user = c.gitlab.test_connection()
        result["token"] = {"ok": True, "user": user.get("username")}
    except GitLabError as e:
        result["token"] = {"ok": False, "error": str(e)}
    try:
        project = c.gitlab.get_project(row["gitlab_project_id"])
        result["project"] = {"ok": True, "path": project.get("path_with_namespace")}
    except GitLabError as e:
        result["project"] = {"ok": False, "error": str(e)}
    try:
        ok, msg = c.gitlab.test_webhook(row["gitlab_project_id"])
        result["webhook"] = {"ok": ok, "message": msg}
    except GitLabError as e:
        result["webhook"] = {"ok": False, "error": str(e)}
    return result


@router.post("/{repo_id}/reconcile")
def reconcile_repo(request: Request, repo_id: int):
    """对账：立即扫描该仓库，把「assignee 是 bot 但任务表无活跃记录」的 open
    issues 补入队（仓库页「对账」按钮，issue #17）。同步执行并直接返回结果，
    与设置页的全局异步对账（/settings/reconcile-now）互补。
    """
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    if not row["enabled"]:
        return {"ok": True, "scanned": 0, "enqueued": 0, "note": "仓库已停用，未扫描"}
    result = c.reconciler.reconcile_once(repo_id=repo_id)
    if result.get("errors"):
        raise HTTPException(502, f"对账失败: {result['errors'][0]}")
    return {"ok": True, "scanned": result["scanned"], "enqueued": result["enqueued"]}


@router.get("/{repo_id}/template")
def get_template(request: Request, repo_id: int):
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    from ..templates import PLACEHOLDERS
    return {
        "template": c.renderer.resolve_template(row),
        "is_override": bool(row["prompt_template"]),
        "placeholders": PLACEHOLDERS,
    }


@router.put("/{repo_id}/template")
def update_template(request: Request, repo_id: int, body: dict):
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    text = (body.get("template") or "").strip()
    if not text:
        # 空 = 清空覆盖，回退全局模版
        text = None
    c.db.update_repo(repo_id, prompt_template=text)
    updated = _repo_row_to_dict(c.db.get_repo(repo_id))
    if any(r.project_id == updated["gitlab_project_id"] for r in c.config.get().repos):
        _sync_repo_to_config(request.app, updated)
    return _repo_row_to_dict(c.db.get_repo(repo_id))


def ctx_of(request: Request):
    return request.app.state.ctx
