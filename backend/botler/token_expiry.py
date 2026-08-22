"""GitLab Personal Access Token 到期状态与预警（issue #279）。"""
from __future__ import annotations

from datetime import date

from .notifier import Notifier

# 阈值由远及近检查，首轮即只发送当前最严重的一档；后续跨档再提醒。
_THRESHOLDS = ((3, "urgent"), (7, "critical"), (30, "warning"))
_LEVEL_LABELS = {
    "healthy": "正常", "warning": "30 天内到期", "critical": "7 天内到期",
    "urgent": "3 天内到期", "expired": "已到期", "unknown": "未记录",
}


def evaluate_expiry(expires_at: str | None, *, today: date | None = None) -> dict:
    """计算 YYYY-MM-DD 到期日的展示状态；非法或未填日期不猜测。"""
    if not expires_at:
        return {"level": "unknown", "days_remaining": None, "expires_at": None}
    try:
        expiry = date.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return {"level": "unknown", "days_remaining": None, "expires_at": None}
    remaining = (expiry - (today or date.today())).days
    if remaining < 0:
        level = "expired"
    elif remaining <= 3:
        level = "urgent"
    elif remaining <= 7:
        level = "critical"
    elif remaining <= 30:
        level = "warning"
    else:
        level = "healthy"
    return {"level": level, "days_remaining": remaining, "expires_at": expiry.isoformat()}


def expiry_level_key(status: dict) -> str | None:
    """将状态映射为告警去重键；正常和未知状态无需推送。"""
    level = status.get("level")
    if level == "warning":
        return "30d"
    if level == "critical":
        return "7d"
    if level == "urgent":
        return "3d"
    if level == "expired":
        return "expired"
    return None


class TokenExpiryChecker:
    """在巡检循环复用通知通道检查 owner 与仓库 token 到期时间。"""

    def __init__(self, db, notifier: Notifier, config=None):
        self.db = db
        self.notifier = notifier
        self.config = config

    def check(self, *, today: date | None = None) -> list[str]:
        cfg = self.config.get() if self.config else None
        if cfg is not None and (not cfg.alerts_enabled or not cfg.alert_token_expiry):
            return []
        triggered: list[str] = []
        for repo in self.db.list_repos():
            status = evaluate_expiry(repo["token_expires_at"], today=today)
            event = self._notify("repo", repo["id"], repo["name"], status, cfg)
            if event:
                triggered.append(event)
        if cfg is not None:
            status = evaluate_expiry(cfg.gitlab_owner_token_expires_at, today=today)
            event = self._notify("owner", 0, "Owner GitLab Token", status, cfg)
            if event:
                triggered.append(event)
        return triggered

    def _notify(self, subject: str, ident: int, name: str, status: dict, cfg) -> str | None:
        key = expiry_level_key(status)
        if key is None:
            return None
        event_type = f"alert_token_expiry_{key}_{subject}_{ident}"
        days = status["days_remaining"]
        if days is not None and days < 0:
            timing = f"已于 {-days} 天前到期"
        elif days == 0:
            timing = "今日到期"
        else:
            timing = f"剩余 {days} 天"
        title = f"⚠️ {name} token {_LEVEL_LABELS[status['level']]}"
        body = (f"{name} 的 GitLab Personal Access Token 将于 {status['expires_at']} 到期（{timing}）。"
                "请前往 GitLab 创建新的 Personal Access Token，并在 Botler 设置中更新配置。")
        window = cfg.alert_throttle_seconds if cfg is not None else 365 * 24 * 3600
        event_id = self.notifier.record_alert(event_type, title, body, data={
            "subject": subject, "repo_id": ident if subject == "repo" else None,
            "expires_at": status["expires_at"], "days_remaining": days,
            "level": status["level"],
        }, window_seconds=window)
        return event_type if event_id is not None else None
