"""任务 token 用量统计 API（issue #235 验收标准 3）。

按仓库/引擎/时间段聚合的统计接口：数据来自本地 task_usage 表（执行侧
采集落库），不依赖 GitLab API；时间段过滤按 usage 记录日期（UTC
'YYYY-MM-DD'，含端点）。返回 summary（全局合计）+ by_repo / by_engine /
by_date（分组明细），供前端统计板块聚合展示。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/usage", tags=["usage"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/stats")
def usage_stats(
    request: Request,
    repo_id: int | None = Query(None),
    engine: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
):
    """按仓库/引擎/时间段聚合 token 用量统计。

    - repo_id：按仓库过滤（可选）；engine：按引擎过滤（claude/hermes/dsh）；
    - since/until：UTC 日期 'YYYY-MM-DD'（含端点），过滤 usage 记录日期；
    - 返回 summary（合计）+ by_repo / by_engine / by_date 分组明细，
      空数据时分组为空数组、summary 各 token 合计为 0（前端空态渲染）。
    """
    c = request.app.state.ctx
    for key, value in (("since", since), ("until", until)):
        if value is not None and not _DATE_RE.match(value):
            raise HTTPException(
                400, f"{key} 必须是 YYYY-MM-DD 格式的 UTC 日期（如 2026-08-18）")
    return c.db.usage_stats(repo_id=repo_id, engine=engine,
                            since=since, until=until)


def ctx_of(request: Request):
    return request.app.state.ctx
