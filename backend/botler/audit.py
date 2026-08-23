"""操作审计辅助（issue #260）：关键操作留痕的公共设施。

- current_actor(request)：从 SSO 会话取执行者用户名；SSO 未启用 / 未登录
  时返回 "local"（本机直连/非 SSO 场景）。
- client_ip(request)：取来源 IP（X-Forwarded-For 优先，回退直连地址），
  供审计记录追溯「谁在哪个 IP 操作」。
- record_audit(request, db, ...)：尽力而为写一条审计日志——审计写入失败
  绝不影响主操作（与 webhook 推送同容错策略，统一 try/except 只记 warning）。
- is_admin(request, config)：管理员判定——SSO 未启用（本机单用户）恒为
  管理员；启用后按 config.audit_logs.admin_usernames 白名单判定，名单
  为空 = 所有登录用户均视为管理员（平台现状无用户分级，保持默认宽松，
  配置白名单后收紧为仅名单内用户可查看/删除审计日志）。
- 差异摘要工具：settings_section_diff（设置保存 diff 前后值）/
  repo_diff（仓库修改前后值）/ config_diff_summary（config.yaml 外部
  修改差异摘要，掩码敏感字段）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# 敏感字段名提示词（配置差异摘要脱敏用，issue #260）：命中即把值打码，
# 防止审计日志里落明文凭据（webhook_secret / token / api_key 等）
_SECRET_HINTS = (
    "token", "secret", "api_key", "password", "authorization",
    "client_secret", "access_key", "bot_token",
)


def current_actor(request: Request) -> str:
    """取当前执行者标识（issue #260）。

    SSO 启用且已登录 → 会话用户名（优先 username，回退 name / sub）；
    SSO 未启用或未登录 → "local"（本机单用户 / 未走登录的场景）。
    """
    ctx = getattr(request.app.state, "ctx", None)
    sso = getattr(ctx, "sso", None)
    if sso is not None and sso.enabled():
        user = sso.current_user(request)
        if user:
            return (user.get("username") or user.get("name")
                    or user.get("sub") or "unknown")
    return "local"


def client_ip(request: Request) -> str:
    """取请求来源 IP（issue #260）。

    优先 X-Forwarded-For 首段（反代场景真实客户端），回退 X-Real-IP，
    再回退直连 socket 地址。取不到返回空串。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    client = request.client
    return client.host if client is not None else ""


def record_audit(request: Request, db, action: str, target_type: str = "",
                 target_id=None, detail: dict | None = None) -> int | None:
    """写一条审计日志（尽力而为，issue #260）。

    与主操作解耦：任何失败（含 db 异常）只记 warning 不抛出，保证审计
    埋点绝不阻塞/破坏业务操作（与 webhook 推送同容错策略）。
    """
    try:
        return db.add_audit_log(
            actor=current_actor(request),
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip=client_ip(request),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("审计日志写入失败（不影响主操作）: %s", e)
        return None


def is_admin(request: Request, config) -> bool:
    """管理员判定（issue #260）。

    - SSO 未启用 → True（本机单用户场景，无用户分级）；
    - SSO 启用但未登录 → False（受 SsoGuardMiddleware 保护，理论不达）；
    - SSO 启用且已登录 → 用户名在 audit_admin_usernames 白名单内；
      名单为空（未配置）→ 所有登录用户均视为管理员。
    """
    ctx = getattr(request.app.state, "ctx", None)
    sso = getattr(ctx, "sso", None)
    if sso is None or not sso.enabled():
        return True
    user = sso.current_user(request)
    if not user:
        return False
    admins = (config.get().audit_admin_usernames or [])
    if not admins:
        return True
    name = user.get("username") or user.get("name") or user.get("sub") or ""
    return name in admins


def admin_required(request: Request, config) -> None:
    """非管理员访问审计日志接口 → 403（issue #260）。"""
    if not is_admin(request, config):
        raise HTTPException(403, "仅管理员可访问审计日志")


def settings_section_diff(before: dict, after: dict, section: str,
                          patch_keys: set[str]) -> dict:
    """计算设置段保存前后差异（issue #260）。

    只比较本次提交（patch）涉及的键，避免派生字段（engine_health /
    pause_active / placeholders 等）造成噪音；值未变的键不进入结果。
    返回 {字段: [旧值, 新值]}。
    """
    before_sec = before.get(section) or {}
    after_sec = after.get(section) or {}
    diff: dict[str, list] = {}
    for key in patch_keys:
        old = before_sec.get(key)
        new = after_sec.get(key)
        if old != new:
            diff[key] = [old, new]
    return diff


def repo_diff(before: dict, after: dict, fields: set[str]) -> dict:
    """仓库修改前后差异（issue #260）：只比较提交涉及的字段，返回
    {字段: [旧值, 新值]}。before/after 传入已脱敏的仓库行（url 掩码）。"""
    diff: dict[str, list] = {}
    for key in fields:
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diff[key] = [old, new]
    return diff


def _mask_secret_value(key: str, value: Any) -> Any:
    """敏感字段打码（issue #260）：命中提示词且为字符串 → 只留末 4 位。"""
    if value is None or not isinstance(value, str) or not value:
        return value
    if any(hint in key.lower() for hint in _SECRET_HINTS):
        return "***" + value[-4:] if len(value) > 4 else "***"
    return value


def config_diff_summary(old: dict, new: dict) -> dict:
    """config.yaml 外部修改差异摘要（issue #260）。

    输入 ConfigManager 重载前后的原始 yaml 数据（_data）：
    - 顶层段级：列出变化段名（changed_sections）；
    - 标量段：键级 [旧, 新]（敏感字段打码）；
    - 列表段（repos / ai_providers 等）：只记录条数变化 n → m，
      不落全量明细（避免 detail 塞爆）；
    - 附加 webhook_secret_changed 标记（webhook 轮换留痕）。
    """
    old = old or {}
    new = new or {}
    changed: list[str] = []
    diff: dict[str, Any] = {}
    for section in sorted(set(old) | set(new)):
        ov, nv = old.get(section), new.get(section)
        if ov == nv:
            continue
        changed.append(section)
        if isinstance(ov, dict) and isinstance(nv, dict):
            sec_diff: dict[str, Any] = {}
            for key in sorted(set(ov) | set(nv)):
                if ov.get(key) != nv.get(key):
                    sec_diff[key] = [
                        _mask_secret_value(key, ov.get(key)),
                        _mask_secret_value(key, nv.get(key)),
                    ]
            diff[section] = sec_diff
        elif isinstance(ov, list) or isinstance(nv, list):
            diff[section] = {"count": [len(ov or []), len(nv or [])]}
        else:
            diff[section] = [
                _mask_secret_value(section, ov),
                _mask_secret_value(section, nv),
            ]
    gitlab_diff = diff.get("gitlab") or {}
    return {
        "changed_sections": changed,
        "diff": diff,
        # webhook 轮换留痕（issue #260）：webhook_secret 只能通过直接编辑
        # config.yaml 变更（API 只读掩码），外部修改检测命中即标记
        "webhook_secret_changed": "webhook_secret" in gitlab_diff,
    }
