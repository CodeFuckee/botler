"""Prometheus 运行指标（issue #208）。

背景：平台只有 ``/api/health``（基础状态）与日志，无结构化运行指标——
任务队列长度、执行时长分布、失败率、webhook 处理延迟、GitLab API 调用
次数/错误率、磁盘空间等均无法持续观测。本模块为 ``GET /metrics``
（Prometheus 文本格式）提供指标定义与采集逻辑：

- **计数器**（运行期自增，进程重启归零）：
  - ``botler_webhook_received_total{result}``：webhook 请求接收计数，
    按结果分类 accepted / rejected / error（webhook.py 埋点）；
  - ``botler_gitlab_api_requests_total{method}``：GitLab API HTTP 请求
    总数（每次实际请求尝试计一次，gitlab_client.py 埋点）；
  - ``botler_gitlab_api_errors_total{method}``：GitLab API 调用失败总数
    （HTTP >= 400 / 传输层异常，gitlab_client.py 埋点）；
- **gauge**（每次抓取时由 :func:`render_metrics` 刷新）：
  - ``botler_task_state{status}``：任务状态计数（tasks 表实时聚合）；
  - ``botler_queue_depth`` / ``botler_running_tasks``：调度器内存队列深度
    与运行中任务数；
  - ``botler_disk_free_bytes`` / ``botler_disk_total_bytes``：数据目录
    磁盘剩余/总空间（复用 health.py 的探测逻辑）；
  - ``botler_db_size_bytes``：SQLite 数据库文件大小；
- **histogram**（抓取时由 tasks 表实时聚合，重启不丢历史）：
  - ``botler_task_duration_seconds``：任务执行时长分布
    （started_at → finished_at，秒）。

端点注册在 main.py（根路径 ``/metrics``，不在 ``/api/`` 前缀下——SSO
中间件只保护 ``/api/*``，天然放行，符合 issue 验收「无 SSO 保护该端点，
或纳管」的个人自用语义）。
"""

from __future__ import annotations

import os
from typing import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from prometheus_client.metrics_core import HistogramMetricFamily

from .health import default_data_dir, probe_disk

# 任务执行时长直方图桶（秒）：1min / 5min / 15min / 30min / 1h / 2h / 6h
DURATION_BUCKETS = (60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 21600.0)

# 任务状态全集（gauge 固定发射全部状态，缺失补 0——避免残留标签值，
# 与 database.task_stats 的「只在库状态」语义互补）
TASK_STATUSES = (
    "queued", "running", "retrying",
    "succeeded", "failed", "interrupted", "canceled_by_user",
)

_registry: CollectorRegistry = CollectorRegistry(auto_describe=True)
webhook_received: Counter
gitlab_api_requests: Counter
gitlab_api_errors: Counter
task_state: Gauge
queue_depth: Gauge
running_tasks: Gauge
disk_free_bytes: Gauge
disk_total_bytes: Gauge
db_size_bytes: Gauge
_duration_collector: "TaskDurationCollector"


class TaskDurationCollector:
    """任务执行时长直方图（自定义 collector，DB 聚合，抓取时实时计算）。

    不做进程内累计：时长数据在 tasks 表（started_at / finished_at）已
    完整持久化，重启不丢历史；每次抓取时按桶一次 SQL 聚合，避免跨重启
    漂移。collect() 需要访问 DB，通过 :func:`render_metrics` 每次抓取前
    注入的 db_provider 获取（自定义 collector 的标准用法）。
    """

    def __init__(self, registry: CollectorRegistry, buckets=DURATION_BUCKETS):
        self._buckets = tuple(float(b) for b in buckets)
        self._db_provider: Callable[[], object] | None = None
        registry.register(self)

    def set_db_provider(self, provider: Callable[[], object]) -> None:
        """注入 DB 获取函数（抓取时同步调用，供 collect 查询聚合）。"""
        self._db_provider = provider

    def collect(self):
        db = self._db_provider() if self._db_provider is not None else None
        if db is None or not hasattr(db, "task_duration_histogram"):
            return
        count, total, counts = db.task_duration_histogram(self._buckets)
        family = HistogramMetricFamily(
            "botler_task_duration_seconds",
            "任务执行时长分布（秒，started_at → finished_at）",
            buckets=[(str(b), float(counts[i])) for i, b in enumerate(self._buckets)]
                    + [("+Inf", float(count))],
            sum_value=float(total),
        )
        yield family


def _init_metrics() -> None:
    """（重新）创建全部指标对象并注册到当前注册表（供 reset_for_tests 复用）。"""
    global webhook_received, gitlab_api_requests, gitlab_api_errors
    global task_state, queue_depth, running_tasks
    global disk_free_bytes, disk_total_bytes, db_size_bytes, _duration_collector
    webhook_received = Counter(
        "botler_webhook_received_total",
        "GitLab webhook 请求接收总数（按结果分类 accepted/rejected/error）",
        ("result",), registry=_registry)
    gitlab_api_requests = Counter(
        "botler_gitlab_api_requests_total",
        "GitLab API HTTP 请求总数（按方法）",
        ("method",), registry=_registry)
    gitlab_api_errors = Counter(
        "botler_gitlab_api_errors_total",
        "GitLab API 调用失败总数（HTTP>=400 / 传输层异常，按方法）",
        ("method",), registry=_registry)
    task_state = Gauge(
        "botler_task_state", "任务状态计数（tasks 表实时聚合）",
        ("status",), registry=_registry)
    queue_depth = Gauge(
        "botler_queue_depth", "调度器排队任务深度（内存队列）", registry=_registry)
    running_tasks = Gauge(
        "botler_running_tasks", "正在执行的任务数", registry=_registry)
    disk_free_bytes = Gauge(
        "botler_disk_free_bytes", "数据目录剩余空间（字节）", registry=_registry)
    disk_total_bytes = Gauge(
        "botler_disk_total_bytes", "数据目录总空间（字节）", registry=_registry)
    db_size_bytes = Gauge(
        "botler_db_size_bytes", "SQLite 数据库文件大小（字节）", registry=_registry)
    _duration_collector = TaskDurationCollector(_registry)


_init_metrics()


# ---- 埋点函数（供 webhook.py / gitlab_client.py 调用，不直接依赖指标对象）----

def inc_webhook_received(result: str) -> None:
    """webhook 接收计数 +1（result: accepted / rejected / error）。"""
    webhook_received.labels(result=result).inc()


def inc_gitlab_api_request(method: str) -> None:
    """GitLab API 请求计数 +1（每次实际 HTTP 尝试计一次）。"""
    gitlab_api_requests.labels(method=method).inc()


def inc_gitlab_api_error(method: str) -> None:
    """GitLab API 调用失败计数 +1（HTTP>=400 / 传输层异常）。"""
    gitlab_api_errors.labels(method=method).inc()


# ---- 抓取渲染 ----

def _db_file_size(path: str) -> int:
    """SQLite 库文件大小（字节）；文件缺失/不可读返回 0。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def render_metrics(ctx) -> str:
    """组装 /metrics 文本（Prometheus 文本格式 v0.0.4）。

    每次抓取时刷新 gauge 值（任务状态计数 / 队列深度 / 磁盘 / DB 大小），
    直方图由 :class:`TaskDurationCollector` 在 generate_latest 时从 DB
    实时聚合。ctx 需暴露 ``db``（Database：task_stats / task_duration_histogram /
    path）与 ``scheduler``（stats() 返回 {"running": int, "queued": int}）；
    任一数据源异常只跳过对应指标，不阻塞整体输出（个人自用监控尽力而为）。
    """
    db = getattr(ctx, "db", None)
    scheduler = getattr(ctx, "scheduler", None)

    # 任务状态计数（固定发射全部状态，缺失补 0）
    stats = db.task_stats() if db is not None else {}
    for status in TASK_STATUSES:
        task_state.labels(status=status).set(int(stats.get(status, 0) or 0))

    # 调度器队列深度 / 运行中任务数
    if scheduler is not None:
        try:
            s = scheduler.stats()
            queue_depth.set(int(s.get("queued", 0) or 0))
            running_tasks.set(int(s.get("running", 0) or 0))
        except Exception:  # noqa: BLE001 调度器异常不阻塞指标输出
            pass

    # 磁盘 / DB 大小
    try:
        disk = probe_disk(default_data_dir())
        disk_free_bytes.set(float(disk.get("free_bytes", 0) or 0))
        disk_total_bytes.set(float(disk.get("total_bytes", 0) or 0))
    except Exception:  # noqa: BLE001
        pass
    if db is not None:
        db_size_bytes.set(float(_db_file_size(getattr(db, "path", "") or "") or 0))

    # 直方图 DB provider 注入（collect 时同步查询）
    _duration_collector.set_db_provider(lambda: db)

    return generate_latest(_registry).decode("utf-8")


def reset_for_tests() -> None:
    """清空全部指标与注册表（测试隔离：每个用例独立计数起点）。

    仅测试用：webhook/gitlab 埋点走 inc_* 函数，读当前模块级指标对象，
    重置后埋点自动落到新注册表，不会写进旧对象。
    """
    global _registry
    _registry = CollectorRegistry(auto_describe=True)
    _init_metrics()


__all__ = [
    "CONTENT_TYPE_LATEST",
    "DURATION_BUCKETS",
    "TASK_STATUSES",
    "inc_webhook_received",
    "inc_gitlab_api_request",
    "inc_gitlab_api_error",
    "render_metrics",
    "reset_for_tests",
]
