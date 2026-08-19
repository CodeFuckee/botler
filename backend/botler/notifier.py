"""网页通知事件记录器（issue #21）。

事件类型（与前端「通知时机」开关一一对应）：
- task_succeeded: 任务成功（issue 已由 Claude 关闭）→ 「issue 完成」通知
- task_failed: 任务失败（Claude 无法解决/重试耗尽，需人工介入）→ 「任务需要交互」通知
- queue_empty: 对账扫描后某仓库无任何待处理 open issue → 「issue 列表为空」通知
- queue_no_work: 对账扫描后有 open issue 但全部已有活跃任务 → 「无 issue 可处理」通知

任务类事件靠 notification_events.task_id 唯一索引幂等（同一任务收尾只记一次）；
队列类事件（无 task_id）靠 record_throttled 节流：同一仓库同类型在窗口期内
（默认 1 小时）只记一次，避免对账周期扫描反复提醒骚扰用户。
"""

from __future__ import annotations

import calendar
import json
import time

from .database import Database

# 队列类事件节流窗口（秒）：同一仓库同类型在此窗口内不重复记录
QUEUE_THROTTLE_SECONDS = 3600

# 事件正文里失败原因的最大长度（正文只展示摘要，详情在任务页）
_BODY_MAX = 200

# 聚合告警事件类型（issue #229）：平台级异常（无 repo_name），节流按类型
# 全局判定（record_alert），与队列类事件的「同仓库同类型」节流维度不同
ALERT_FAILURE_RATE = "alert_failure_rate"
ALERT_QUEUE_BACKLOG = "alert_queue_backlog"
ALERT_TOKEN_INVALID = "alert_token_invalid"
ALERT_DISK_LOW = "alert_disk_low"


class Notifier:
    def __init__(self, db: Database):
        self.db = db

    def record(self, type_: str, title: str, body: str = "",
               repo_name: str | None = None, task_id: int | None = None,
               data: dict | None = None) -> int | None:
        """记录一条事件，返回 id；task_id 重复时返回 None（幂等）。"""
        payload = json.dumps(data, ensure_ascii=False) if data else None
        return self.db.add_notification(
            type_, title, body[: _BODY_MAX], repo_name=repo_name,
            task_id=task_id, data=payload)

    def record_throttled(self, type_: str, title: str, body: str = "",
                         repo_name: str | None = None,
                         window_seconds: int = QUEUE_THROTTLE_SECONDS) -> int | None:
        """节流记录队列类事件：同仓库同类型窗口期内已记录则跳过，返回 id 或 None。

        窗口判定用事件 id 序号而非时间戳：同一秒内多次扫描也不会重复；
        SQLite created_at 精度到秒，id 序号单调递增更可靠。
        """
        if repo_name:
            last = self.db.last_notification(repo_name, type_)
            if last is not None:
                # SQLite datetime('now') 存 UTC，按 UTC 解析为 epoch 比较窗口
                created = last["created_at"]
                try:
                    prev_ts = calendar.timegm(time.strptime(created, "%Y-%m-%d %H:%M:%S"))
                except (TypeError, ValueError):
                    prev_ts = 0
                if time.time() - prev_ts < window_seconds:
                    return None
        return self.record(type_, title, body, repo_name=repo_name)

    def record_alert(self, type_: str, title: str, body: str = "",
                     data: dict | None = None,
                     window_seconds: int = QUEUE_THROTTLE_SECONDS) -> int | None:
        """节流记录全局聚合告警事件（issue #229）：同类型窗口期内跳过。

        与 record_throttled 的差异：告警无 repo_name（平台级），节流按
        类型全局判定（last_alert_notification 不带仓库过滤），窗口默认
        1 小时，避免对账周期反复提醒骚扰用户。返回事件 id 或 None（节流）。
        """
        last = self.db.last_alert_notification(type_)
        if last is not None:
            created = last["created_at"]
            try:
                prev_ts = calendar.timegm(time.strptime(created, "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                prev_ts = 0
            if time.time() - prev_ts < window_seconds:
                return None
        return self.record(type_, title, body, data=data)

    # ---- 任务类事件 ----

    def task_succeeded(self, task: dict, repo_name: str | None = None) -> int | None:
        """任务成功（issue 已关闭）→ 「issue 完成」通知。"""
        return self.record(
            "task_succeeded",
            "🤖 issue 已完成",
            f"{repo_name or ''} #{task['issue_iid']} {task['issue_title'] or ''}".strip(),
            repo_name=repo_name,
            task_id=task["id"],
            data={"issue_iid": task["issue_iid"], "issue_title": task["issue_title"]},
        )

    def task_failed(self, task: dict, reason: str, repo_name: str | None = None) -> int | None:
        """任务失败（需人工介入）→ 「任务需要交互」通知。"""
        return self.record(
            "task_failed",
            "⚠️ 任务需要人工介入",
            f"{repo_name or ''} #{task['issue_iid']} {task['issue_title'] or ''}：{reason}".strip(),
            repo_name=repo_name,
            task_id=task["id"],
            data={"issue_iid": task["issue_iid"], "issue_title": task["issue_title"],
                  "reason": reason},
        )

    # ---- 队列类事件（由对账扫描产生，带节流）----

    def queue_empty(self, repo_name: str) -> int | None:
        """对账发现该仓库无任何待处理 open issue → 「issue 列表为空」通知。"""
        return self.record_throttled(
            "queue_empty",
            "📭 队列已空",
            f"{repo_name}：暂无待处理 issue",
            repo_name=repo_name)

    def queue_no_work(self, repo_name: str, active_count: int) -> int | None:
        """对账发现有 open issue 但全部已有活跃任务 → 「无 issue 可处理」通知。"""
        return self.record_throttled(
            "queue_no_work",
            "🔄 无新任务",
            f"{repo_name}：有 {active_count} 个 issue 但均已在处理中",
            repo_name=repo_name)
