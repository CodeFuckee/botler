"""仓库管理 API：列表 / 添加（自动识别 project_id + 注册 webhook）/ 更新 / 删除 / 连通性测试 / 模版。"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import RepoConfig
from ..database import DEFAULT_PRIORITY
from ..gitlab_client import GitLabError
from ..labels import DEFAULT_LABELS
from ..git_remote import build_client_from_url, mask_url_token

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
        "remote_username": row["remote_username"],
        "prompt_template": row["prompt_template"],
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        # issue #188：仓库 logo 元信息（「生成图标」生成后写入；前端按
        # logo_path 是否非空决定是否展示 logo，logo_updated_at 作 img
        # src 缓存击穿参数）
        "logo_path": row["logo_path"],
        "logo_updated_at": row["logo_updated_at"],
        "logo_mime": row["logo_mime"],
        "created_at": row["created_at"],
    }


def _masked_repo_row(row) -> dict:
    """API 展示用仓库行：url 脱敏（issue #60），token 不出现在响应上。

    DB 与 config.yaml 仍保存真实 url（clone 需要）；仅 API 出口脱敏。
    """
    d = _repo_row_to_dict(row)
    d["url"] = mask_url_token(d["url"])
    return d


def _sync_default_labels(client, project_id: int) -> list[str]:
    """添加仓库时在目标 GitLab 项目上补齐标记库内置默认标签（issue #157）。

    目标项目上缺失的默认标签（labels.DEFAULT_LABELS，与 docs/labels.md /
    scripts/sync_labels.py 保持一致）逐个创建；已存在的标签保持不变——
    只补缺失，不覆盖用户已有的颜色/描述。尽力而为：读取或创建失败只记
    日志，不阻塞仓库添加——仓库主体（项目识别 + webhook 注册）已就绪，
    标签缺失不影响平台正常工作，用户可在 GitLab 上手动补建。

    返回本次实际创建的标签名列表（供日志使用）。
    """
    try:
        existing = {l["name"] for l in client.list_project_labels(project_id)}
    except GitLabError as e:
        logger.warning("添加仓库 %s：读取远端标签失败，跳过默认标签补齐: %s",
                       project_id, e)
        return []
    created: list[str] = []
    for spec in DEFAULT_LABELS:
        if spec["name"] in existing:
            continue
        try:
            client.create_project_label(
                project_id, spec["name"], spec["color"], spec.get("description"))
            created.append(spec["name"])
        except GitLabError as e:
            logger.warning("添加仓库 %s：创建默认标签「%s」失败（忽略）: %s",
                           project_id, spec["name"], e)
    if created:
        logger.info("添加仓库 %s：补齐标记库默认标签 %s", project_id, created)
    return created


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
        remote_username=repo_dict.get("remote_username"),
        priority=repo_dict["priority"],
    ))
    config.update_section("repos", [config.repo_to_config_dict(r) for r in kept])


@router.get("")
def list_repos(request: Request):
    c = ctx_of(request)
    rows = c.db.list_repos()
    return {"repos": [_masked_repo_row(r) for r in rows]}


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
    # issue #60：remote url 可能内嵌 token，返回前脱敏
    for r in remotes:
        r["url"] = mask_url_token(r["url"])
    return {"remotes": remotes, "local_path": body.local_path}


@router.post("", status_code=201)
def add_repo(request: Request, body: RepoCreate):
    c = ctx_of(request)
    config = c.config.get()

    url, local_path, remote_name = _resolve_repo_source(body, c)
    # 原始 remote URL（可能内嵌 token）：识别成功后 url 会被替换为 API
    # 返回的干净 url，兜底解析 token 必须用这个原始值（issue #77）
    remote_url = url
    # webhook 回调地址（默认当前请求 base_url，允许覆盖）；提前计算，
    # 兜底 client 构建时一并传入（issue #77）
    webhook_base = body.webhook_url or str(request.base_url).rstrip("/")
    # issue #77：识别项目默认走全局 token；全局 token 失效（401/403）而
    # remote URL 内嵌 token 时，用内嵌 token 的临时 client 兜底重试
    # （与 executor / reconciler 的 per-repo 兜底模式一致）。用户场景：
    # 本地仓库 remote url 带用户名和 token、git pull/push 正常，但平台
    # 全局 token 失效 → 添加仓库报「无法识别项目: token 无效或已过期」。
    fallback_client = None
    try:
        project = c.gitlab.resolve_project(url)
    except GitLabError as e:
        if e.status_code not in (401, 403):
            raise
        fallback = build_client_from_url(remote_url, config.verify_ssl,
                                         webhook_base_url=webhook_base)
        if fallback is None:
            # remote 无内嵌 token，兜底不可用，保持原有 400 错误
            raise HTTPException(400, f"无法识别项目: {e}")
        logger.info("添加仓库：全局 token 失效（%s），改用 remote url 内嵌 token 识别项目", e)
        try:
            project = fallback.resolve_project(remote_url)
        except GitLabError as e2:
            raise HTTPException(400, f"无法识别项目: {e2}")
        fallback_client = fallback

    project_id = int(project["id"])
    name = body.name or project.get("path") or project.get("name") or str(project_id)
    url = project.get("http_url_to_repo") or project.get("web_url") or url

    existing = c.db.get_repo_by_project_id(project_id)
    if existing:
        raise HTTPException(409, f"仓库已存在（id={existing['id']}，name={existing['name']}）")

    # 注册 webhook：识别已用 remote token 兜底时复用同一 client，
    # 全局 client 注册 401/403 时同样用 remote token 兜底重试
    from ..gitlab_client import GitLabClient
    temp_client = GitLabClient(config.gitlab_url, config.gitlab_token,
                               verify_ssl=config.verify_ssl,
                               webhook_base_url=webhook_base)
    try:
        if fallback_client is not None:
            fallback_client.register_webhook(project_id, config.webhook_secret)
        else:
            temp_client.register_webhook(project_id, config.webhook_secret)
    except GitLabError as e:
        if fallback_client is None and e.status_code in (401, 403):
            retry_client = build_client_from_url(remote_url, config.verify_ssl,
                                                 webhook_base_url=webhook_base)
            if retry_client is not None:
                logger.info("添加仓库：注册 webhook 全局 token 失效（%s），"
                            "改用 remote url 内嵌 token 重试", e)
                try:
                    retry_client.register_webhook(project_id, config.webhook_secret)
                except GitLabError as e2:
                    raise HTTPException(502, f"注册 webhook 失败: {e2}")
            else:
                raise HTTPException(502, f"注册 webhook 失败: {e}")
        else:
            raise HTTPException(502, f"注册 webhook 失败: {e}")

    # issue #157：目标 GitLab 项目补齐标记库内置默认标签（缺失的创建、
    # 已存在的不动；尽力而为，失败只记日志不阻塞添加）。与 webhook 注册
    # 共用同一 client（全局 token 失效时用 remote token 兜底 client）。
    _sync_default_labels(fallback_client or temp_client, project_id)

    # issue #153：仓库用户——remote url userinfo 的用户名（如 agent），
    # 读取 remote url 获取后随仓库落库，设置页展示、灵感提交 issue 默认分配人。
    # remote_url 是识别前的原始 URL（可能内嵌 user:token@），用其解析用户名。
    from ..git_remote import parse_remote_url
    remote_username = parse_remote_url(remote_url)["username"]

    repo_id = c.db.upsert_repo(
        project_id=project_id, name=name, url=url,
        prompt_template=body.prompt_template, enabled=body.enabled,
        local_path=local_path, remote_name=remote_name,
        remote_username=remote_username,
        priority=body.priority if body.priority is not None else DEFAULT_PRIORITY)
    _sync_repo_to_config(request.app, _repo_row_to_dict(c.db.get_repo(repo_id)))

    source = f"local_path={local_path}" if local_path else f"url={url}"
    logger.info("添加仓库 %s (project=%s, %s) 并注册 webhook", name, project_id, source)
    return _masked_repo_row(c.db.get_repo(repo_id))


@router.put("/{repo_id}")
def update_repo(request: Request, repo_id: int, body: RepoUpdate):
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")

    fields = body.model_dump(exclude_unset=True)
    # issue #60 脱敏防护：前端回传掩码 url（含 *）视为「未修改」，
    # 不覆盖 DB 中的真实凭据（与 sso client_secret 掩码模式一致）
    if fields.get("url") and "*" in fields["url"]:
        fields.pop("url")
    # issue #153：URL 变更（未脱敏）时按新 URL 重新推导仓库用户，与
    # 添加仓库行为一致；未变更 URL 时保留既有 remote_username。
    if fields.get("url") and "remote_username" not in fields:
        from ..git_remote import parse_remote_url
        fields["remote_username"] = parse_remote_url(fields["url"])["username"]
    if fields:
        c.db.update_repo(repo_id, **fields)
    updated = _repo_row_to_dict(c.db.get_repo(repo_id))
    # 仅当仓库仍存在于 config 时同步（避免把已删除的仓库写回去）
    if any(r.project_id == updated["gitlab_project_id"] for r in c.config.get().repos):
        _sync_repo_to_config(request.app, updated)
    return _masked_repo_row(c.db.get_repo(repo_id))


@router.delete("/{repo_id}")
def delete_repo(request: Request, repo_id: int):
    """删除仓库：注销 webhook + 从 config 移除 + db 软删除（deleted_at 标记，issue #62）。

    软删除行保留供任务历史解析仓库名，但不再出现在仓库列表
    （list_repos 默认过滤 deleted_at 非空的行）；与「停用」（enabled=False、
    行仍可见可重新启用）区分。
    """
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")

    # 注销 webhook（尽力而为，失败不阻塞删除）
    try:
        c.gitlab.unregister_webhook(row["gitlab_project_id"])
    except GitLabError as e:
        logger.warning("注销 webhook 失败（忽略）: %s", e)

    c.config.remove_repo(row["gitlab_project_id"])
    c.db.soft_delete_repo(repo_id)
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


@router.post("/{repo_id}/remote-user")
def read_repo_remote_user(request: Request, repo_id: int):
    """读取仓库 remote url 获取仓库用户（issue #153）。

    仓库用户 = remote URL userinfo 里的用户名（https://user:token@host/...），
    是用户在 git 配置里填写的账号（如 agent）。读取顺序：local_path 的
    git remote -v → 回退 workspace/<name> 克隆目录 → 最后回退仓库存储的
    url 字符串 userinfo（读取逻辑见 git_remote.read_repo_remote_username）。

    读取结果写回仓库 remote_username（含 None：清除旧值）并同步
    config.yaml，作为灵感一键提交 issue 时的默认分配人。读取是尽力而为：
    目录不可读 / 不是 git 仓库 / 无该 remote / URL 无凭据 → 返回
    remote_username 为 null，不报错（用户可在设置页看到说明）。
    """
    c = ctx_of(request)
    row = c.db.get_repo(repo_id)
    if row is None:
        raise HTTPException(404, "仓库不存在")
    from ..git_remote import read_repo_remote_username

    username = read_repo_remote_username(row)
    c.db.update_repo(repo_id, remote_username=username)
    updated = _repo_row_to_dict(c.db.get_repo(repo_id))
    # 仅当仓库仍存在于 config 时同步（避免把已删除的仓库写回去）
    if any(r.project_id == updated["gitlab_project_id"] for r in c.config.get().repos):
        _sync_repo_to_config(request.app, updated)
    logger.info("仓库 %s 读取 remote url 仓库用户: %s", row["name"], username or "（无）")
    return {"remote_username": username}


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
