"""聚合告警检测（issue #229）。

背景：平台有通知通道（网页通知 in_app 事件 + webhook 推送，issue
#21/#136）与 bot-failed 标签，但异常（任务连续失败 / 队列堆积 / GitLab
token 失效 / 磁盘空间不足）没有主动告警——无人值守场景只能用户打开页面
发现。本模块把「异常信号 → 聚合检测 → 现有 notifier 通知」收敛为对账循环
内的一次 ``check()`` 调用（issue #229「定时检测（可并入对账循环）」）：

- **失败率**（``alert_failure_rate``）：近 ``failure_rate_window`` 秒
  （默认 1 小时）终态任务失败率 > ``failure_rate_threshold``（默认 50%）
  → ``alert_failure_rate``；
- **队列堆积**（``alert_queue_backlog``）：活跃任务（queued/running/
  retrying）超过 ``queue_backlog_threshold`` 条且 ``queue_stall_minutes``
  内无任何任务收尾（无进度）→ ``alert_queue_backlog``；
- **token 失效**（``alert_token_invalid``）：对账时探测 GitLab token
  （GET /user 返回 401/403）→ ``alert_token_invalid``；传输层故障
  （连接超时等）不算 token 失效，不告警；
- **磁盘空间**（``alert_disk_low``）：数据目录剩余空间 <
  ``disk_min_free_mb``（默认 512 MiB，与 health.probe_disk 一致）
  → ``alert_disk_low``。

所有阈值在设置页「聚合告警」卡片可配置（config.yaml ``alerts`` 段）。
通知复用现有通道：in_app 经 ``Notifier.record_alert`` 落库网页通知事件
（前端轮询弹系统通知），webhook 经 ``WebhookPusher.send_alert`` 推送
结构化 payload；同类型告警按 ``throttle_seconds``（默认 1 小时）节流，
避免对账周期反复提醒。任一通道失败仅记日志，不阻塞对账循环（与任务收尾
通知同容错策略）。
"""

from __future__ import annotations

import logging
import time

from .database import Database
from .gitlab_client import GitLabError
from .health import default_data_dir, probe_disk
from .notifier import (
    ALERT_DISK_LOW,
    ALERT_FAILURE_RATE,
    ALERT_QUEUE_BACKLOG,
    ALERT_TOKEN_INVALID,
    Notifier,
)
from .webhook_push import WebhookPusher, WebhookPushError

logger = logging.getLogger(__name__)

# 各告警事件的默认节流窗口（秒）：与 config alerts.throttle_seconds 同源，
# 这里仅在 Notifier.record_alert 未显式传窗口时兜底（AlertChecker 始终显式传）
DEFAULT_ALERT_THROTTLE_SECONDS = 3600


def _utc_ts(seconds_ago: int, now: float | None = None) -> str:
    """now - seconds_ago 的 UTC 时间字符串（YYYY-MM-DD HH:MM:SS，与库内
    finished_at / created_at 格式一致）。now 缺省取当前时间；测试可注入。"""
    base = time.time() if now is None else now
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base - seconds_ago))


class AlertChecker:
    """聚合告警检测器：对账循环内执行，触发后经现有 notifier 分发。

    :param config:   ConfigManager（alerts 段阈值）
    :param db:       Database（失败率 / 队列进度 / 节流查询）
    :param notifier: Notifier（in_app 网页通知事件记录，含节流）
    :param gitlab:   GitLabClient（token 探测；None = 不探测，仅用注入状态）
    :param data_dir: 数据目录（磁盘探测；None = 默认数据目录）
    """

    def __init__(self, config, db: Database, notifier: Notifier,
                 gitlab=None, data_dir=None):
        self.config = config
        self.db = db
        self.notifier = notifier
        self.gitlab = gitlab
        self.data_dir = data_dir

    # ---- 入口 ----

    def check(self, *, token_status: str | None = None) -> list[str]:
        """执行一轮告警检测，返回触发的告警类型列表（已通知，未节流）。

        token_status: "ok" / "invalid" / "error" / None——调用方（对账）
        已知全局 token 状态时直接传入避免重复探测；None 且启用了 token
        告警时本方法自行探测。单项检测失败只记日志，不阻塞其他检测。
        """
        cfg = self.config.get()
        if not cfg.alerts_enabled:
            return []
        triggered: list[str] = []
        for name, fn in (
            ("failure_rate", self._check_failure_rate),
            ("queue_backlog", self._check_queue_backlog),
            ("token", self._check_token),
            ("disk", self._check_disk),
        ):
            try:
                alert_type = fn(cfg, token_status=token_status)
            except Exception:  # noqa: BLE001 单项检测失败不影响其他告警
                logger.exception("聚合告警检测 %s 失败", name)
                continue
            if alert_type:
                triggered.append(alert_type)
        return triggered

    # ---- 四类检测 ----

    def _check_failure_rate(self, cfg, *, token_status=None) -> str | None:
        """近 1 小时终态任务失败率 > 阈值 → alert_failure_rate。"""
        if not cfg.alert_failure_rate:
            return None
        since_ts = _utc_ts(cfg.alert_failure_rate_window)
        stats = self.db.task_failure_stats(since_ts)
        rate_pct = stats["rate"] * 100
        if stats["total"] == 0 or rate_pct <= cfg.alert_failure_rate_threshold:
            return None
        title = "⚠️ 任务失败率过高"
        body = (f"近 {cfg.alert_failure_rate_window // 60} 分钟任务失败率 "
                f"{rate_pct:.0f}%（{stats['failed']}/{stats['total']}）"
                f"超过阈值 {cfg.alert_failure_rate_threshold:.0f}%")
        if not self._notify(ALERT_FAILURE_RATE, title, body, {
            "total": stats["total"], "failed": stats["failed"],
            "rate": round(stats["rate"], 4),
            "threshold": cfg.alert_failure_rate_threshold,
            "window_seconds": cfg.alert_failure_rate_window,
        }, cfg):
            return None  # 节流窗口内已通知过，不重复分发
        return ALERT_FAILURE_RATE

    def _check_queue_backlog(self, cfg, *, token_status=None) -> str | None:
        """活跃任务数 > 阈值且窗口内无任务收尾（无进度）→ alert_queue_backlog。"""
        if not cfg.alert_queue_backlog:
            return None
        from .database import ACTIVE_STATUSES
        depth = self.db.count_tasks(status=list(ACTIVE_STATUSES))
        if depth < cfg.alert_queue_backlog_threshold:
            return None
        since_ts = _utc_ts(cfg.alert_queue_stall_minutes * 60)
        if self.db.count_terminal_since(since_ts) > 0:
            return None  # 窗口内有任务收尾 = 有进度
        title = "⚠️ 任务队列堆积"
        body = (f"队列活跃任务 {depth} 条超过阈值 "
                f"{cfg.alert_queue_backlog_threshold} 条，且近 "
                f"{cfg.alert_queue_stall_minutes} 分钟无任务收尾（疑似卡死）")
        if not self._notify(ALERT_QUEUE_BACKLOG, title, body, {
            "depth": depth, "threshold": cfg.alert_queue_backlog_threshold,
            "stall_minutes": cfg.alert_queue_stall_minutes,
        }, cfg):
            return None  # 节流窗口内已通知过，不重复分发
        return ALERT_QUEUE_BACKLOG

    def _check_token(self, cfg, *, token_status=None) -> str | None:
        """GitLab token 失效（401/403）→ alert_token_invalid。

        传输层故障（无 status_code）不算 token 失效，不告警——网络抖动
        有对账重试兜底，误报反而骚扰用户。
        """
        if not cfg.alert_token_invalid:
            return None
        if token_status is None:
            if self.gitlab is None:
                return None
            try:
                self.gitlab.test_connection()
                token_status = "ok"
            except GitLabError as e:
                token_status = "invalid" if e.status_code in (401, 403) else "error"
            except Exception:  # noqa: BLE001 非 GitLabError 异常按网络故障处理
                return None
        if token_status != "invalid":
            return None
        title = "🚨 GitLab token 失效"
        body = "GitLab 访问返回 401/403，token 可能已过期或被吊销，请到设置页更新后重试"
        if not self._notify(ALERT_TOKEN_INVALID, title, body, {}, cfg):
            return None  # 节流窗口内已通知过，不重复分发
        return ALERT_TOKEN_INVALID

    def _check_disk(self, cfg, *, token_status=None) -> str | None:
        """数据目录磁盘剩余 < 阈值 → alert_disk_low。"""
        if not cfg.alert_disk_low:
            return None
        data_dir = self.data_dir or default_data_dir()
        probe = probe_disk(data_dir, min_free=cfg.alert_disk_min_free_mb * 1024 * 1024)
        if probe.get("status") == "ok":
            return None
        free_mb = probe.get("free_mb", 0)
        title = "⚠️ 磁盘空间不足"
        body = (f"数据目录 {data_dir} 剩余空间 {free_mb} MiB，"
                f"低于阈值 {cfg.alert_disk_min_free_mb} MiB")
        if not self._notify(ALERT_DISK_LOW, title, body, {
            "free_mb": free_mb, "threshold_mb": cfg.alert_disk_min_free_mb,
            "detail": probe.get("detail", ""),
        }, cfg):
            return None  # 节流窗口内已通知过，不重复分发
        return ALERT_DISK_LOW

    # ---- 通知分发 ----

    def _notify(self, alert_type: str, title: str, body: str,
                data: dict, cfg) -> bool:
        """in_app 落库（节流）+ webhook 推送，任一失败仅记日志。

        返回是否真正通知（False = 节流窗口内已通知过，不再重复分发）。
        """
        event_id = self.notifier.record_alert(
            alert_type, title, body, data=data,
            window_seconds=cfg.alert_throttle_seconds)
        if event_id is None:
            # 节流窗口内已通知过：in_app 不重复落库，webhook 也不重复推送
            return False
        try:
            WebhookPusher(self.config).send_alert(
                alert_type, title, body, detail=data.get("detail", ""))
        except WebhookPushError as e:
            logger.warning("聚合告警 %s webhook 推送失败: %s", alert_type, e)
        except Exception:  # noqa: BLE001 推送异常不影响对账循环
            logger.exception("聚合告警 %s webhook 推送异常", alert_type)
        return True


__all__ = [
    "AlertChecker",
    "DEFAULT_ALERT_THROTTLE_SECONDS",
    "_utc_ts",
]
