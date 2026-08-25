"""通知事件 API（issue #21 + issue #215 已读/未读）。

- GET /events：前端轮询增量拉取（游标 after），返回 read 字段与全局
  unread_count（导航栏未读徽标数据源，复用同一轮询零额外请求）；
- GET /：通知中心全量列表（最新优先，issue #215）；
- POST /{id}/read、POST /read-all：标记单条/全部已读。

前端维护已见事件 id 游标，按游标拉取新事件；设置页开关决定哪些类型
要弹通知（过滤在前端做，改设置立即生效无需重启）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/notifications", tags=["notifications"])

MAX_LIMIT = 200


def _to_event(r) -> dict:
    """行 → API 事件字典（read = read_at 非空，issue #215）。"""
    return {
        "id": r["id"],
        "type": r["type"],
        "title": r["title"],
        "body": r["body"],
        "repo_name": r["repo_name"],
        "task_id": r["task_id"],
        "data": r["data"],
        "created_at": r["created_at"],
        "read": r["read_at"] is not None,
    }


@router.get("")
def list_center(
    request: Request,
    limit: int = Query(100, ge=1, le=MAX_LIMIT, description="最多返回条数"),
):
    """通知中心全量列表（issue #215）：最新优先，含未读计数。"""
    c = request.app.state.ctx
    rows = c.db.list_recent_notifications(limit=limit)
    return {
        "notifications": [_to_event(r) for r in rows],
        "unread_count": c.db.count_unread_notifications(),
    }


@router.get("/events")
def list_events(
    request: Request,
    after: int = Query(0, ge=0, description="只返回 id 大于该值的事件（游标）"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT, description="最多返回条数"),
):
    """增量拉取通知事件。返回 events 列表、最新事件 id（供下次游标）
    与全局未读计数（导航栏徽标，issue #215）。"""
    c = request.app.state.ctx
    rows = c.db.list_notifications(after_id=after, limit=limit)
    events = [_to_event(r) for r in rows]
    latest = events[-1]["id"] if events else after
    return {
        "events": events,
        "latest_id": latest,
        "unread_count": c.db.count_unread_notifications(),
    }


@router.post("/read-all")
def read_all(request: Request):
    """全部通知标记已读（issue #215）。返回本次更新的行数（幂等）。"""
    c = request.app.state.ctx
    updated = c.db.mark_all_notifications_read()
    return {"updated": updated}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, request: Request):
    """标记单条通知已读（issue #215）；通知不存在返回 404。"""
    c = request.app.state.ctx
    if not c.db.mark_notification_read(notification_id):
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"id": notification_id, "read": True}
