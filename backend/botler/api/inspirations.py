"""灵感 API（issue #131）：概览页「灵感」板块。

需求：概览页在「开放 Issue」下方、「CI/CD 流水线」上方增加灵感板块，
用户可按仓库随手记录关于该仓库新功能的灵感；灵感只保存在 Botler
本地 SQLite 数据库，不提交到 GitLab issue。

接口设计（本地数据，无需 GitLab API，无缓存）：
- GET    /api/inspirations/overview：聚合所有未软删除仓库（按优先级
  升序、同优先级按仓库 id，与 list_repos 一致），每个仓库带灵感列表
  （按 updated_at 降序）。无灵感的仓库也返回（前端展示空状态 + 添加
  表单），与开放 issue 板块同构。设置 ui.show_disabled_repos=false 时
  只返回已启用仓库（issue #142）。
- POST   /api/inspirations：创建灵感（repo_id + content 必填）。
- PUT    /api/inspirations/{id}：更新灵感内容（刷新 updated_at）。
- DELETE /api/inspirations/{id}：删除灵感。

校验：repo_id 必须指向存在且未软删除的仓库（400）；content 去除首尾
空白后非空（400）、长度不超过 5000 字（400，随手笔记的合理上限）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inspirations", tags=["inspirations"])

# 灵感内容长度上限（去除首尾空白后）：随手笔记场景的合理上限
MAX_CONTENT_LEN = 5000


class InspirationCreate(BaseModel):
    repo_id: int = Field(description="仓库本地 id（repos.id）")
    content: str = Field(description="灵感内容")


class InspirationUpdate(BaseModel):
    content: str = Field(description="灵感内容")


def _validate_content(content: str) -> str:
    """校验并规范化灵感内容：去除首尾空白，非空且不超长。"""
    text = content.strip()
    if not text:
        raise HTTPException(400, "灵感内容不能为空")
    if len(text) > MAX_CONTENT_LEN:
        raise HTTPException(400, f"灵感内容不能超过 {MAX_CONTENT_LEN} 字")
    return text


def _require_repo(c, repo_id: int):
    """校验仓库存在且未软删除，返回仓库行；否则抛 400。

    get_repo 不区分软删除（软删除仅写 deleted_at 标记），这里显式
    排除 deleted_at 非空的行——已删除仓库不允许再记录灵感（issue #131）。
    """
    repo = c.db.get_repo(repo_id)
    if repo is None or repo["deleted_at"] is not None:
        raise HTTPException(400, f"仓库不存在或已删除（id={repo_id}）")
    return repo


def _row_to_dict(row) -> dict:
    """灵感行 → API 响应对象（含仓库名快照，展示无需再查仓库表）。"""
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "repo_name": row["repo_name"],
        "content": row["content"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/overview")
def inspiration_overview(request: Request):
    """概览页灵感板块聚合数据：所有未软删除仓库 + 各自灵感列表。

    仓库顺序与仓库列表一致（priority 升序、同优先级按 id）；灵感按
    updated_at 降序（最新改动在前）。无灵感的仓库返回空列表。
    """
    c = request.app.state.ctx
    repos = c.db.list_repos()
    # issue #142：设置关闭时隐藏未启用项目（灵感 / CI/CD 页面一致）
    if not c.config.get().ui_show_disabled_repos:
        repos = [r for r in repos if r["enabled"]]
    insp_rows = c.db.list_inspirations()
    by_repo: dict[int, list[dict]] = {}
    for row in insp_rows:
        by_repo.setdefault(row["repo_id"], []).append(_row_to_dict(row))
    return {
        "repos": [
            {
                "repo_id": r["id"],
                "repo_name": r["name"],
                "enabled": bool(r["enabled"]),
                "priority": r["priority"],
                "inspirations": by_repo.get(r["id"], []),
            }
            for r in repos
        ],
    }


@router.post("", status_code=201)
def create_inspiration(request: Request, body: InspirationCreate):
    c = request.app.state.ctx
    _require_repo(c, body.repo_id)
    content = _validate_content(body.content)
    insp_id = c.db.create_inspiration(body.repo_id, content)
    row = c.db.get_inspiration(insp_id)
    assert row is not None  # 刚插入的记录必然可查
    return _row_to_dict(row)


@router.put("/{inspiration_id}")
def update_inspiration(request: Request, inspiration_id: int,
                       body: InspirationUpdate):
    c = request.app.state.ctx
    content = _validate_content(body.content)
    if not c.db.update_inspiration(inspiration_id, content):
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    row = c.db.get_inspiration(inspiration_id)
    assert row is not None
    return _row_to_dict(row)


@router.delete("/{inspiration_id}", status_code=204)
def delete_inspiration(request: Request, inspiration_id: int):
    c = request.app.state.ctx
    if not c.db.delete_inspiration(inspiration_id):
        raise HTTPException(404, f"灵感不存在（id={inspiration_id}）")
    return None
