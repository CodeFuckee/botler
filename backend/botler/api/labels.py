"""标记库 API（issue #29）：默认清单（内置不可删除）+ 用户自定义标签增删。

GET    /api/labels             列出全部标签（default 内置清单 + custom 自定义）
POST   /api/labels             添加自定义标签 {name, color?, description?}
DELETE /api/labels/{name}      删除自定义标签（默认标签拒绝删除）
POST   /api/labels/{name}/sync 默认标签一键同步到全部已添加仓库（issue #307）
POST   /api/labels/sync-all    一键同步全部默认标签到全部已添加仓库（issue #358）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..gitlab_client import GitLabError
from ..labels import DEFAULT_COLOR, DEFAULT_LABELS, validate_label
from . import ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/labels", tags=["labels"])


@router.get("")
def list_labels(request: Request) -> dict:
    c = ctx(request)
    return {"default": DEFAULT_LABELS, "custom": c.config.get().custom_labels}


@router.post("")
def add_label(request: Request, body: dict) -> dict:
    """添加自定义标签。名称与默认清单/已有自定义标签重复、格式非法 → 400。"""
    c = ctx(request)
    name = (body.get("name") or "").strip()
    color = (body.get("color") or "").strip() or DEFAULT_COLOR
    error = validate_label(name, color)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if any(l["name"] == name for l in DEFAULT_LABELS):
        raise HTTPException(status_code=400, detail=f"「{name}」是默认标签，无需添加")
    existing = c.config.get().custom_labels
    if any(l["name"] == name for l in existing):
        raise HTTPException(status_code=400, detail=f"自定义标签「{name}」已存在")
    description = (body.get("description") or "").strip()
    labels = [*existing, {"name": name, "color": color, "description": description}]
    c.config.update_section("labels", {"custom": labels})
    return {"default": DEFAULT_LABELS, "custom": labels}


@router.delete("/{name}")
def delete_label(request: Request, name: str) -> dict:
    """删除自定义标签。默认标签不可删除（400），不存在 → 404。"""
    c = ctx(request)
    if any(l["name"] == name for l in DEFAULT_LABELS):
        raise HTTPException(status_code=400, detail=f"「{name}」是默认标签，不可删除")
    custom = c.config.get().custom_labels
    remaining = [l for l in custom if l["name"] != name]
    if len(remaining) == len(custom):
        raise HTTPException(status_code=404, detail=f"自定义标签「{name}」不存在")
    c.config.update_section("labels", {"custom": remaining})
    return {"default": DEFAULT_LABELS, "custom": remaining}


def _sync_label_to_repos(c, settings, spec, repos):
    """把单个默认标签同步到仓库列表（issue #307 / #358 共用核心逻辑）。

    同步语义与「添加仓库时补齐标记库默认标签」（issue #157）一致：
    目标项目上缺失才创建，已存在的不覆盖（保留用户已有的颜色/描述）；
    单个仓库失败为尽力而为，不中断其余仓库同步，失败明细随返回值返回。

    身份：per-repo client（仓库 remote URL 内嵌 token）优先，无 token
    回退全局 bot token（与 issue 创建 / logo 同步链路一致，issue #297）。

    返回 (created, already_exists, failed)，分别是被同步/已存在/失败的
    仓库名列表（失败项为 {"repo", "error"}）。
    """
    from ..git_remote import build_repo_client

    created: list[str] = []
    already_exists: list[str] = []
    failed: list[dict] = []
    for row in repos:
        repo = dict(row)  # sqlite3.Row → dict，统一按 dict 访问（issue #187）
        repo_label = repo.get("name") or str(repo["gitlab_project_id"])
        # per-repo client 优先（保证实例与凭据正确），无 token 回退全局
        client = build_repo_client(repo, settings.verify_ssl) or c.gitlab
        try:
            existing = {l["name"] for l in client.list_project_labels(
                repo["gitlab_project_id"])}
            if spec["name"] in existing:
                already_exists.append(repo_label)
                continue
            client.create_project_label(
                repo["gitlab_project_id"], spec["name"], spec["color"],
                spec.get("description"))
            created.append(repo_label)
        except GitLabError as e:
            failed.append({"repo": repo_label, "error": str(e)})
            logger.warning("同步默认标签「%s」到仓库 %s 失败（跳过）: %s",
                           spec["name"], repo_label, e)
    return created, already_exists, failed


@router.post("/sync-all")
def sync_all_default_labels(request: Request) -> dict:
    """一键同步全部默认标签到所有已添加仓库（issue #358）。

    标记库页「一键同步全部」按钮：一次调用把内置默认清单里的全部默认
    标签同步到平台已添加的全部仓库（DB list_repos 返回启用与未启用的
    全部未删除仓库），省去逐标签点击「同步到所有仓库」（issue #307）。

    同步语义与单标签同步（issue #307）完全一致：目标项目缺失才创建、
    已存在不覆盖；单仓库/单标签失败为尽力而为，不中断其余同步，失败
    明细按标签分组随响应返回。自定义标签不参与（与 issue #307 一致，
    自定义标签为个性化配置，不全局同步）。

    返回 {total_repos, labels, total_created, total_already_exists,
    total_failed}；labels 每项为 {label, created, already_exists, failed}。
    """
    c = ctx(request)
    settings = c.config.get()
    repos = c.db.list_repos()
    labels_result: list[dict] = []
    total_created = 0
    total_already_exists = 0
    total_failed = 0
    for spec in DEFAULT_LABELS:
        created, already_exists, failed = _sync_label_to_repos(
            c, settings, spec, repos)
        labels_result.append({
            "label": spec["name"],
            "created": created,
            "already_exists": already_exists,
            "failed": failed,
        })
        total_created += len(created)
        total_already_exists += len(already_exists)
        total_failed += len(failed)
        if created or failed:
            logger.info("默认标签「%s」同步完成：新建 %s / 已存在 %s / 失败 %s",
                        spec["name"], len(created), len(already_exists),
                        len(failed))
    logger.info("全部默认标签一键同步完成：涉及 %s 个仓库，新建 %s / "
                "已存在 %s / 失败 %s",
                len(repos), total_created, total_already_exists, total_failed)
    return {
        "total_repos": len(repos),
        "labels": labels_result,
        "total_created": total_created,
        "total_already_exists": total_already_exists,
        "total_failed": total_failed,
    }


@router.post("/{name}/sync")
def sync_default_label(request: Request, name: str) -> dict:
    """默认标签一键同步到全部已添加仓库（issue #307）。

    标记库页每个默认标签的「同步到所有仓库」按钮：点击后自动把该标签
    同步到平台已添加的全部仓库（DB list_repos 返回启用与未启用的全部
    未删除仓库），让新增/调整的默认标签在存量仓库上一键补齐，无需逐个
    仓库手动添加。

    同步语义与「添加仓库时补齐标记库默认标签」（issue #157）一致：
    目标项目上缺失才创建，已存在的不覆盖（保留用户已有的颜色/描述）；
    单个仓库失败为尽力而为，不中断其余仓库同步，失败明细随响应返回。

    身份：per-repo client（仓库 remote URL 内嵌 token）优先，无 token
    回退全局 bot token（与 issue 创建 / logo 同步链路一致，issue #297）。

    仅支持默认标签（内置清单）——自定义标签不提供一键同步（400）。
    返回 {label, total_repos, created, already_exists, failed}。
    """
    c = ctx(request)
    spec = next((l for l in DEFAULT_LABELS if l["name"] == name), None)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"「{name}」不是默认标签，仅默认标签支持一键同步到所有仓库")

    settings = c.config.get()
    repos = c.db.list_repos()
    created, already_exists, failed = _sync_label_to_repos(
        c, settings, spec, repos)
    if created or failed:
        logger.info("默认标签「%s」同步完成：新建 %s / 已存在 %s / 失败 %s",
                    spec["name"], len(created), len(already_exists), len(failed))
    return {
        "label": spec,
        "total_repos": len(created) + len(already_exists) + len(failed),
        "created": created,
        "already_exists": already_exists,
        "failed": failed,
    }
