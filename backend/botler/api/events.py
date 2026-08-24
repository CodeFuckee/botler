"""全局事件流端点：GET /api/events 以 SSE 推送数据变更通知（issue #478）。

概览页/统计页由「固定间隔轮询」改为「事件驱动刷新」：后端在数据真实
变化点（任务创建/状态变化、issue 增删改、灵感增删改、设置保存、流水线
概览缓存重拉）向全局事件总线发布轻量通知事件，前端单一 SSE 连接订阅，
收到事件后只刷新对应数据模块；断线重连后前端全量兜底刷新（事件不携带
数据，重连不丢数据、无历史回放需求）。

事件 data 为 JSON {"type": "...", "ts": "..."}，type 取值：
- task        任务创建 / 状态变化（活跃任务列表、导航水位、统计聚合）
- issue       开放 issue 聚合数据变化（GitLab issue 增删改 / 对账 / 转 issue）
- inspiration 灵感增删改（本地数据库）
- settings    设置保存（维护模式等前端立即生效项）
- pipeline    流水线概览数据重拉（多标签页同步）

心跳：每 15 秒发 ": ping" 注释行（EventSource 忽略），保持代理连接
不过期；连接断开即取消订阅（生成器 finally 退订，避免订阅泄漏）。
"""

from __future__ import annotations

import asyncio
import json
import queue
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from botler.events import AppEventBus, global_bus

router = APIRouter(prefix="/events")

# 心跳间隔（秒）：长连接经反向代理时若无数据可能被空闲断开
PING_INTERVAL_SECONDS = 15.0
# 订阅队列非空轮询间隔（秒）：越短事件延迟越低，越短 CPU 占用越高
_POLL_INTERVAL_SECONDS = 0.5


def _sse(event: dict) -> str:
    """事件 → SSE data 行。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _event_stream(bus: AppEventBus, request: Request):
    """订阅全局总线并逐事件推送；空闲发心跳；断开退订。"""
    q = bus.subscribe()
    try:
        last_ping = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                event = q.get_nowait()
            except queue.Empty:  # noqa: PERF203 空队列正常轮询等待
                pass
            else:
                yield _sse(event)
                continue
            if time.monotonic() - last_ping >= PING_INTERVAL_SECONDS:
                last_ping = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        bus.unsubscribe(q)


@router.get("")
def global_events(request: Request):
    """全局事件流（SSE）：订阅全局事件总线，推送数据变更通知。

    返回 StreamingResponse：连接保持长开，事件到达即推送，空闲发心跳
    保活；客户端断开（EventSource 关闭/断网）时生成器 finally 退订。
    """
    return StreamingResponse(
        _event_stream(global_bus, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
