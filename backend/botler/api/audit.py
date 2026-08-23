"""操作审计日志 API（issue #260）。

- GET    /api/audit-logs         分页查询审计日志（按操作类型/执行者/
                                  目标类型过滤），响应含总条数 + 可选
                                  操作类型列表 + 当前用户是否管理员；
- GET    /api/audit-logs/actions 审计日志出现过的全部操作类型（过滤下拉）；
- DELETE /api/audit-logs/{id}    删除单条审计日志（仅管理员，普通用户 403）。

访问控制：SSO 未启用（本机单用户）恒可访问；SSO 启用且配置了管理员名单
（audit_logs.admin_usernames）时，仅名单内用户可查看/删除（名单为空 =
 所有登录用户均可访问）。删除接口独立校验，普通用户一律 403。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..audit import admin_required, is_admin
from . import ctx

router = APIRouter(prefix="/audit-logs", tags=["audit"])


def _row_to_dict(row) -> dict:
    """audit_logs 行 → API 响应字典（detail JSON 解析 + 时间原样透传）。"""
    import json
    detail = {}
    try:
        detail = json.loads(row["detail"] or "{}")
    except (TypeError, ValueError):
        detail = {}
    return {
        "id": row["id"],
        "actor": row["actor"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "detail": detail,
        "created_at": row["created_at"],
        "ip": row["ip"],
    }


@router.get("")
def list_audit_logs(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    action: str | None = None,
    actor: str | None = None,
    target_type: str | None = None,
):
    """分页查询审计日志（issue #260）。

    按 id 倒序（即时间倒序）；action / actor / target_type 为精确过滤
    （action 供设置页过滤下拉使用）。响应附带 actions（全部操作类型，
    供下拉渲染）与 admin（当前用户是否管理员，前端据此显隐删除按钮）。
    """
    c = ctx(request)
    admin_required(request, c.config)
    rows, total = c.db.list_audit_logs(
        offset=(page - 1) * per_page, limit=per_page,
        action=action, actor=actor, target_type=target_type)
    return {
        "items": [_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "actions": c.db.list_audit_actions(),
        "admin": is_admin(request, c.config),
    }


@router.get("/actions")
def list_audit_actions(request: Request):
    """审计日志出现过的全部操作类型（去重升序，过滤下拉数据源）。"""
    c = ctx(request)
    admin_required(request, c.config)
    return {"actions": c.db.list_audit_actions()}


@router.delete("/{audit_id}")
def delete_audit_log(request: Request, audit_id: int):
    """删除单条审计日志（仅管理员，issue #260 验收标准 3）。

    普通用户（SSO 启用且配置了管理员名单时的非名单用户）一律 403；
    行不存在返回 404。删除不可恢复——前端需二次确认。
    """
    c = ctx(request)
    admin_required(request, c.config)
    if not c.db.delete_audit_log(audit_id):
        raise HTTPException(404, "审计日志不存在")
    return {"ok": True}
