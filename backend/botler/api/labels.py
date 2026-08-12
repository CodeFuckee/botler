"""标记库 API（issue #29）：默认清单（内置不可删除）+ 用户自定义标签增删。

GET    /api/labels        列出全部标签（default 内置清单 + custom 自定义）
POST   /api/labels        添加自定义标签 {name, color?, description?}
DELETE /api/labels/{name} 删除自定义标签（默认标签拒绝删除）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..labels import DEFAULT_COLOR, DEFAULT_LABELS, validate_label
from . import ctx

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
    c.config.update_custom_labels(labels)
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
    c.config.update_custom_labels(remaining)
    return {"default": DEFAULT_LABELS, "custom": remaining}
