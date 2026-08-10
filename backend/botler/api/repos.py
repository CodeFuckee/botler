"""仓库管理 API：列表 / 添加（自动识别 project_id + 注册 webhook）/ 更新 / 删除 / 连通性测试 / 模版。"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import RepoConfig
from ..gitlab_client import GitLabError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repos", tags=["repos"])


class RepoCreate(BaseModel):
    url: str = Field(description="GitLab 项目 URL（如 https://host/group/project.git）或数字 project_id")
    name: str | None = None
    prompt_template: str | None = None
    enabled: bool = True
    webhook_url: str | None = Field(
        default=None, description="webhook 回调地址覆盖（默认用当前请求的 base_url）")


class RepoUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    prompt_template: str | None = None
    enabled: bool | None = None


def _repo_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "gitlab_project_id": row["gitlab_project_id"],
        "name": row["name"],
        "url": row["url"],
        "local_path": row["local_path"],
        "prompt_template": row["prompt_template"],
        "enabled": bool(row["enabled"]),
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
    ))
    config.update_repos([config.repo_to_config_dict(r) for r in kept])


@router.get("")
def list_repos(request: Request):
    c = ctx_of(request)
    rows = c.db.list_repos()
    return {"repos": [_repo_row_to_dict(r) for r in rows]}


@router.post("", status_code=201)
def add_repo(request: Request, body: RepoCreate):
    c = ctx_of(request)
    config = c.config.get()

    try:
        project = c.gitlab.resolve_project(body.url)
    except GitLabError as e:
        raise HTTPException(400, f"无法识别项目: {e}")

    project_id = int(project["id"])
    name = body.name or project.get("path") or project.get("name") or str(project_id)
    url = project.get("http_url_to_repo") or project.get("web_url") or body.url

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
        prompt_template=body.prompt_template, enabled=body.enabled)
    _sync_repo_to_config(request.app, _repo_row_to_dict(c.db.get_repo(repo_id)))

    logger.info("添加仓库 %s (project=%s) 并注册 webhook", name, project_id)
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
