"""通知事件 API（issue #21）：前端轮询增量拉取后弹浏览器系统通知。

前端维护已见事件 id 游标，按游标拉取新事件；设置页开关决定哪些类型
要弹通知（过滤在前端做，改设置立即生效无需重启）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/notifications", tags=["notifications"])

MAX_LIMIT = 200


@router.get("/events")
def list_events(
    request: Request,
    after: int = Query(0, ge=0, description="只返回 id 大于该值的事件（游标）"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT, description="最多返回条数"),
):
    """增量拉取通知事件。返回 events 列表与最新事件 id（供下次游标）。"""
    c = request.app.state.ctx
    rows = c.db.list_notifications(after_id=after, limit=limit)
    events = [
        {
            "id": r["id"],
            "type": r["type"],
            "title": r["title"],
            "body": r["body"],
            "repo_name": r["repo_name"],
            "task_id": r["task_id"],
            "data": r["data"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    latest = events[-1]["id"] if events else after
    return {"events": events, "latest_id": latest}
