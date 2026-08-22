"""运行数据保留管理（issue #204）。

定时清理仅删除任务明细、任务执行日志和过期通知；tasks 表的终态摘要始终保留。
同时按大小归档 PM2 输出日志，避免常驻进程的 stdout/stderr 无限增长。
"""

from __future__ import annotations

import gzip
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

SCHEDULE_TIMEZONE = "Asia/Shanghai"
SCHEDULED_HOUR = 4
SCHEDULED_MINUTE = 0


class RetentionManager:
    """根据 ``retention`` 配置清理运行数据，并提供手动触发入口。"""

    def __init__(self, db, config, log_dir: str | Path | None = None):
        self.db = db
        self.config = config
        self.log_dir = Path(log_dir) if log_dir is not None else self._default_log_dir()
        self._aps = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)

    @staticmethod
    def _default_log_dir() -> Path:
        """与执行器日志目录保持一致，支持部署时 BOTLER_DATA_DIR 覆盖。"""
        data_dir = os.environ.get("BOTLER_DATA_DIR")
        if data_dir:
            return Path(data_dir) / "logs"
        return Path(__file__).resolve().parents[2] / "logs"

    def cleanup(self, now: datetime | None = None) -> dict[str, int]:
        """执行一次清理，返回各类别删除/轮转数量。"""
        settings = self.config.get()
        result = {"task_logs": 0, "notification_events": 0, "log_files": 0}
        if not getattr(settings, "retention_enabled", True):
            logger.info("数据保留清理已关闭（retention.enabled=false），本次跳过")
            return result

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        task_cutoff = self._cutoff(current, settings.retention_task_logs_days)
        notification_cutoff = self._cutoff(current, settings.retention_notification_events_days)

        paths = self.db.expired_task_log_paths(
            self._cutoff(current, settings.retention_log_files_days))
        result["task_logs"] = self.db.prune_task_logs(task_cutoff)
        result["notification_events"] = self.db.prune_notification_events(notification_cutoff)
        result["log_files"] = self._remove_task_log_files(paths)
        result["log_files"] += self._rotate_pm2_logs(settings.retention_pm2_max_log_size_mb)
        logger.info("数据保留清理完成：任务日志 %s 条，通知 %s 条，日志文件 %s 个",
                    result["task_logs"], result["notification_events"], result["log_files"])
        return result

    @staticmethod
    def _cutoff(now: datetime, days: int) -> str:
        return (now - timedelta(days=days)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _remove_task_log_files(self, paths: list[str]) -> int:
        """仅删除日志根目录内的普通文件，拒绝 DB 异常路径与符号链接。"""
        removed = 0
        root = self.log_dir.resolve()
        for raw_path in paths:
            try:
                path = Path(raw_path)
                resolved = path.resolve(strict=False)
                if root not in resolved.parents or path.is_symlink() or not path.is_file():
                    continue
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("删除过期任务日志失败 %s: %s", raw_path, exc)
        return removed

    def _rotate_pm2_logs(self, max_size_mb: int) -> int:
        """压缩归档超限 PM2 输出并清空活动文件，避免 PM2 持有旧 inode。"""
        limit = max_size_mb * 1024 * 1024
        if limit <= 0 or not self.log_dir.is_dir():
            return 0
        rotated = 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for path in self.log_dir.glob("pm2-*.log"):
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size <= limit:
                    continue
                archive = path.with_name(f"{path.name}.{stamp}.gz")
                with path.open("rb") as source, gzip.open(archive, "wb") as target:
                    target.writelines(source)
                # truncate 原文件而不是 rename，PM2 已打开的文件描述符继续写入新文件。
                with path.open("r+b") as active:
                    active.truncate(0)
                rotated += 1
            except OSError as exc:
                logger.warning("轮转 PM2 日志失败 %s: %s", path, exc)
        return rotated

    def start_scheduler(self) -> None:
        if self._aps.running:
            return
        self._aps.add_job(self._scheduled_cleanup,
                          CronTrigger(hour=SCHEDULED_HOUR, minute=SCHEDULED_MINUTE),
                          id="botler-retention", name="数据保留清理",
                          coalesce=True, max_instances=1, replace_existing=True)
        self._aps.start()
        logger.info("数据保留清理已启动（每天 %02d:%02d %s）",
                    SCHEDULED_HOUR, SCHEDULED_MINUTE, SCHEDULE_TIMEZONE)

    def stop_scheduler(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    def _scheduled_cleanup(self) -> None:
        try:
            self.cleanup()
        except Exception as exc:  # noqa: BLE001 - 定时任务失败不能中断服务
            logger.exception("定时数据保留清理失败: %s", exc)
