"""统计看板 API（issue #264）。

GET /api/stats/dashboard：本地任务表（tasks）聚合的统计看板数据——
总览卡片（任务总数/成功率/平均耗时/失败数）、按引擎 / 仓库 / 来源分组
对比与失败原因 Top 分布。数据来自本地 SQLite tasks 表（与任务列表同表
同口径，保证验收标准 1「统计页各维度数字与任务列表一致」），不依赖
GitLab API；时间段按任务创建时间（UTC）过滤（days=0 为全部）；复用
概览页 10 秒 TTL 缓存模式（issues.py 的 issue #180 同款缓存）。
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Query, Request

# 聚合逻辑在 Database.dashboard_stats → 模块级 aggregate_dashboard
# （database.py 导出，纯函数可单测），API 层只做参数校验与缓存。
router = APIRouter(prefix="/stats", tags=["stats"])

# 复用概览页 10 秒 TTL 缓存模式（issue #264：本地 SQLite 聚合，量小，
# 10s 缓存足够避免高频刷新重复计算）
CACHE_TTL_SECONDS = 10.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/dashboard")
def dashboard_stats(
    request: Request,
    days: int = Query(0, ge=0, le=365,
                      description="统计时间段：0=全部，N=最近 N 天（按任务创建时间 UTC）"),
):
    """统计看板聚合数据（issue #264）。

    - days：0=全部时间段；7/30=最近 7/30 天（前端时间段选择持久化项）；
    - 返回 overview（总览卡片）+ by_engine / by_repo / by_source（分组
      对比）+ failure_reasons（失败原因 Top 分布，与 #40 失败分类口径
      联动：failed/interrupted 任务的 error_message 归一化后 Top 10）；
    - 无任务数据时 overview 各计数为 0、success_rate 为 None、分组与
      失败原因为空数组（前端渲染空态不报错）。
    """
    c = request.app.state.ctx
    key = str(days)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now < hit[0]:
            return hit[1]
    result = c.db.dashboard_stats(days)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
    return result


def clear_cache() -> None:
    """清空统计缓存（测试与配置重载场景用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
