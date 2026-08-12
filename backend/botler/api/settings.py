"""系统设置 API：读取/更新 worker、claude、全局模版（写回 config.yaml）。

凭据（gitlab.bot_token / webhook_secret）不通过 API 回写，只读掩码状态，
避免凭据在界面层反复流转；改凭据请直接编辑 config.yaml / .env。
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import KNOWN_FIELDS

router = APIRouter(prefix="/settings", tags=["settings"])


class WorkerPatch(BaseModel):
    max_concurrent_repos: int | None = None
    task_timeout_seconds: int | None = None
    max_retries: int | None = None
    reconcile_interval_seconds: int | None = None


class ClaudePatch(BaseModel):
    command: str | None = None
    args: list[str] | None = None


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


@router.get("")
def get_settings(request: Request):
    c = ctx_of(request)
    s = c.config.get()
    return {
        "gitlab": {
            "url": s.gitlab_url,
            "bot_username": s.bot_username,
            "bot_token_masked": _mask(s.gitlab_token),
            "webhook_secret_masked": _mask(s.webhook_secret),
            "verify_ssl": s.verify_ssl,
        },
        "worker": {
            "max_concurrent_repos": s.max_concurrent_repos,
            "task_timeout_seconds": s.task_timeout_seconds,
            "max_retries": s.max_retries,
            "reconcile_interval_seconds": s.reconcile_interval_seconds,
        },
        "claude": {
            "command": s.claude_command,
            "args": s.claude_args,
        },
        "templates": {
            "default": s.default_template,
        },
        "browse": {
            "default_path": s.browse_default_path or "",
        },
        "backup": {
            "enabled": s.backup_enabled,
            "retention_days": s.backup_retention_days,
        },
        "ui": {
            # 页面时间显示时区（IANA 名，空 = 跟随浏览器本机时区）
            "timezone": s.ui_timezone,
        },
        "notifications": {
            # 网页通知（issue #21）：总开关 + 各通知时机开关
            "enabled": s.notifications_enabled,
            "task_needs_interaction": s.notify_task_needs_interaction,
            "issue_completed": s.notify_issue_completed,
            "queue_empty": s.notify_queue_empty,
            "queue_no_work": s.notify_queue_no_work,
        },
        "env": {
            # 只读信息：Claude Code 认证来源（服务器环境变量）
            "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
            "anthropic_model": os.environ.get("ANTHROPIC_MODEL", ""),
        },
    }


@router.put("")
def update_settings(request: Request, body: dict):
    """更新 worker / claude / templates.default。body 传哪些键就更新哪些。"""
    c = ctx_of(request)

    worker_patch = body.get("worker")
    if worker_patch is not None:
        _validate_worker(worker_patch)
        c.config.update_worker(worker_patch)

    claude_patch = body.get("claude")
    if claude_patch is not None:
        _validate_claude(claude_patch)
        c.config.update_claude(claude_patch)

    tpl = body.get("templates")
    if tpl is not None and "default" in tpl:
        c.config.update_default_template(tpl["default"])

    browse = body.get("browse")
    if browse is not None:
        _validate_browse(browse)
        c.config.update_browse(browse)

    backup = body.get("backup")
    if backup is not None:
        _validate_backup(backup)
        c.config.update_backup(backup)

    ui = body.get("ui")
    if ui is not None:
        _validate_ui(ui)
        c.config.update_ui(ui)

    notify = body.get("notifications")
    if notify is not None:
        _validate_notifications(notify)
        c.config.update_notifications(notify)

    return get_settings(request)


@router.post("/reconcile-now")
def reconcile_now(request: Request):
    """手动触发一次对账扫描（调试用）。"""
    c = ctx_of(request)
    import threading
    result: dict = {}

    def _run():
        try:
            result.update(c.reconciler.reconcile_once())
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "note": "对账已在后台触发，稍后查看任务列表"}


def _validate_worker(patch: dict) -> None:
    for key in KNOWN_FIELDS["worker"]:
        if key in patch:
            val = patch[key]
            if not isinstance(val, int) or val <= 0:
                raise HTTPException(400, f"{key} 必须是正整数")
            if key == "max_concurrent_repos" and val > 16:
                raise HTTPException(400, "max_concurrent_repos 过大（上限 16）")
            if key == "task_timeout_seconds" and val > 7200:
                raise HTTPException(400, "task_timeout_seconds 过大（上限 7200s）")


def _validate_claude(patch: dict) -> None:
    if "command" in patch and (not patch["command"] or not isinstance(patch["command"], str)):
        raise HTTPException(400, "claude.command 必须是字符串")
    if "args" in patch and (not isinstance(patch["args"], list) or not all(isinstance(a, str) for a in patch["args"])):
        raise HTTPException(400, "claude.args 必须是字符串数组")


def _validate_browse(patch: dict) -> None:
    if "default_path" in patch:
        val = patch["default_path"]
        if not isinstance(val, str):
            raise HTTPException(400, "browse.default_path 必须是字符串（留空 = 服务器用户主目录）")
        # 空串/空白 = 清空配置，回退默认主目录
        patch["default_path"] = val.strip() or None


def _validate_backup(patch: dict) -> None:
    """校验 backup 段：enabled 布尔、retention_days 1~365 正整数。"""
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise HTTPException(400, "backup.enabled 必须是布尔值")
    if "retention_days" in patch:
        val = patch["retention_days"]
        if not isinstance(val, int) or isinstance(val, bool) or not 1 <= val <= 365:
            raise HTTPException(400, "backup.retention_days 必须是 1~365 的整数（天）")


def _validate_ui(patch: dict) -> None:
    """校验 ui 段：timezone 为空串（跟随浏览器）或合法 IANA 时区名（issue #14）。"""
    if "timezone" in patch:
        val = patch["timezone"]
        if not isinstance(val, str):
            raise HTTPException(400, "ui.timezone 必须是字符串（IANA 时区名，空 = 跟随本机）")
        val = val.strip()
        patch["timezone"] = val
        if val:
            try:
                ZoneInfo(val)
            except ZoneInfoNotFoundError:
                raise HTTPException(400, f"ui.timezone 不是有效的 IANA 时区名: {val}") from None


def _validate_notifications(patch: dict) -> None:
    """校验 notifications 段：所有开关必须是布尔值（issue #21）。"""
    for key in ("enabled", "task_needs_interaction", "issue_completed",
                "queue_empty", "queue_no_work"):
        if key in patch and not isinstance(patch[key], bool):
            raise HTTPException(400, f"notifications.{key} 必须是布尔值")


def ctx_of(request: Request):
    return request.app.state.ctx
