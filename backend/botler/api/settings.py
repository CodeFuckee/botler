"""系统设置 API：读取/更新 worker、claude、全局模版（写回 config.yaml）。

凭据（gitlab.bot_token / webhook_secret）不通过 API 回写，只读掩码状态，
避免凭据在界面层反复流转；改凭据请直接编辑 config.yaml / .env。
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import KNOWN_FIELDS
from ..gitlab_client import GitLabClient, GitLabError
from ..labels import validate_label
from ..templates import PLACEHOLDERS

router = APIRouter(prefix="/settings", tags=["settings"])

# SSO 配置指南文档（issue #27 第六轮）：设置页直接展示，避免使用者去
# 查看代码仓库本地文档。路径与 main.py 的 PROJECT_ROOT/docs 对应
# （backend/botler/api/settings.py → 上溯三级到项目根）。
SSO_GUIDE_PATH = Path(__file__).resolve().parents[3] / "docs" / "Synology-SSO-配置指南.md"

# Owner token 申请教程（issue #87）：设置页直接展示（与 SSO 指南同模式）
OWNER_TOKEN_GUIDE_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "GitLab-Owner-Token-申请教程.md")


class WorkerPatch(BaseModel):
    max_concurrent_repos: int | None = None
    task_timeout_seconds: int | None = None
    max_retries: int | None = None
    reconcile_interval_seconds: int | None = None
    # 任务执行引擎（issue #113）：设置页切换后端编写代码的 agent
    engine: str | None = None


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
            "owner_token_masked": _mask(s.gitlab_owner_token),
            "webhook_secret_masked": _mask(s.webhook_secret),
            "verify_ssl": s.verify_ssl,
        },
        "worker": {
            "max_concurrent_repos": s.max_concurrent_repos,
            "task_timeout_seconds": s.task_timeout_seconds,
            "max_retries": s.max_retries,
            "reconcile_interval_seconds": s.reconcile_interval_seconds,
            # issue 标签处理优先级（issue #76）：同仓库队列内按此顺序
            # 选任务派发，越靠前越先处理；未列出的标签排最后
            "issue_priority": s.issue_priority_labels,
            # 任务执行引擎（issue #113）：claude / hermes / dsh，
            # 设置页「任务调度」卡片切换，保存后对后续任务生效
            "engine": s.engine,
        },
        "claude": {
            "command": s.claude_command,
            "args": s.claude_args,
        },
        "dsh": {
            # dsh 引擎（issue #84）：api_key 只返回掩码，明文不流转到界面
            "provider": s.dsh_provider,
            "model": s.dsh_model,
            "max_tokens": s.dsh_max_tokens,
            # 推理等级（issue #123）：off / high / max，空串 = 不设置
            "reasoning_effort": s.dsh_reasoning_effort,
            "session_root": s.dsh_session_root,
            "cordis": s.dsh_cordis,
            "runtime_bin": s.dsh_runtime_bin,
            "base_url": s.dsh_base_url,
            "api_key_masked": _mask(s.dsh_api_key),
        },
        "templates": {
            "default": s.default_template,
            # 中断恢复模版（issue #116）：与全局默认模版同机制可编辑，
            # 未配置/清空时返回内置默认（中断恢复必须有引导语）
            "resume": s.resume_template,
            # 全局模板也可用全部占位符（issue #25：模板页全局视图
            # 占位符表格此前为空，用户误以为占位符未生效）
            "placeholders": PLACEHOLDERS,
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
        "sso": {
            # Synology SSO 登录（issue #27）：凭据只返回掩码
            "enabled": s.sso_enabled,
            "well_known_url": s.sso_well_known_url,
            "client_id": s.sso_client_id,
            "client_secret_masked": _mask(s.sso_client_secret),
            "scope": s.sso_scope,
            "session_days": s.sso_session_days,
            "redirect_uri": s.sso_redirect_uri,
            "verify_ssl": s.sso_verify_ssl,
        },
        "ai_providers": [
            # AI API 供应商（issue #46）：api_key 只返回掩码，明文不流转到界面
            {
                "name": p["name"],
                "provider": p["provider"],
                "base_url": p["base_url"],
                "api_key_masked": _mask(p["api_key"]),
                "model": p["model"],
                "enabled": p["enabled"],
            }
            for p in s.ai_providers
        ],
        "env": {
            # 只读信息：Claude Code 认证来源（服务器环境变量）
            "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
            "anthropic_model": os.environ.get("ANTHROPIC_MODEL", ""),
        },
    }


@router.get("/sso-guide")
def get_sso_guide():
    """SSO 配置指南（issue #27 第六轮）：返回 docs/ 指南 Markdown 原文。

    前端设置页直接渲染展示（单一文档来源，docs/ 改动即页面生效）；
    文档缺失时 404，前端降级提示不阻塞设置页其他功能。
    """
    try:
        content = SSO_GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404, detail="SSO 配置指南文档不存在")
    return {"content": content}


@router.get("/owner-token-guide")
def get_owner_token_guide():
    """Owner token 申请教程（issue #87）：返回 docs/ 教程 Markdown 原文。

    与 SSO 指南同模式：设置页直接渲染；文档缺失 404，前端降级提示。
    """
    try:
        content = OWNER_TOKEN_GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404, detail="Owner token 申请教程文档不存在")
    return {"content": content}


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

    dsh_patch = body.get("dsh")
    if dsh_patch is not None:
        _validate_dsh(dsh_patch)
        c.config.update_dsh(dsh_patch)

    tpl = body.get("templates")
    if tpl is not None:
        if "default" in tpl:
            c.config.update_default_template(tpl["default"])
        if "resume" in tpl:
            # issue #116：中断恢复模版必须是字符串；空白由
            # update_resume_template 归一为内置默认（不允许空模版）
            if not isinstance(tpl["resume"], str):
                raise HTTPException(400, "templates.resume 必须是字符串")
            c.config.update_resume_template(tpl["resume"])

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

    sso = body.get("sso")
    if sso is not None:
        _validate_sso(sso, current=c.config.get())
        c.config.update_sso(sso)

    providers = body.get("ai_providers")
    if providers is not None:
        cleaned = _validate_ai_providers(
            providers, current=c.config.get().ai_providers)
        c.config.update_ai_providers(cleaned)

    gitlab_patch = body.get("gitlab")
    if gitlab_patch is not None:
        _validate_gitlab(gitlab_patch)
        # issue #133：保存前校验真实提交的 owner token（掩码/空串 =
        # 保持现有凭据，跳过校验不覆盖）；校验失败 400 拒绝且不落盘
        token_val = gitlab_patch.get("owner_token")
        if isinstance(token_val, str) and token_val.strip() \
                and "*" not in token_val:
            _validate_owner_token_scope(c.config.get(), token_val.strip())
        c.config.update_gitlab(gitlab_patch)

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


# 任务执行引擎白名单（issue #113）：与 executor._engine 合法集合一致，
# API 层拦截非法值（executor 层回退仅防御手工改坏 config.yaml）
ENGINE_CHOICES = ("claude", "hermes", "dsh")


def _validate_worker(patch: dict) -> None:
    for key in KNOWN_FIELDS["worker"]:
        if key in patch:
            val = patch[key]
            if key == "issue_priority":
                # issue #76：标签优先级顺序单独校验（字符串数组）
                patch[key] = _validate_issue_priority(val)
                continue
            if key == "engine":
                # issue #113：引擎名 strip + 小写归一后校验白名单
                if not isinstance(val, str) or not val.strip():
                    raise HTTPException(
                        400, "worker.engine 必须是字符串（claude / hermes / dsh）")
                val = val.strip().lower()
                if val not in ENGINE_CHOICES:
                    raise HTTPException(
                        400, f"worker.engine 取值非法: {val}（可选 claude / hermes / dsh）")
                patch[key] = val
                continue
            if not isinstance(val, int) or val <= 0:
                raise HTTPException(400, f"{key} 必须是正整数")
            if key == "max_concurrent_repos" and val > 16:
                raise HTTPException(400, "max_concurrent_repos 过大（上限 16）")
            if key == "task_timeout_seconds" and val > 7200:
                raise HTTPException(400, "task_timeout_seconds 过大（上限 7200s）")


def _validate_issue_priority(val) -> list[str]:
    """校验 worker.issue_priority（issue #76）：非空字符串数组、标签名
    合法（复用标记库规则）、无重复；返回 strip 归一化后的列表。"""
    if not isinstance(val, list):
        raise HTTPException(400,
                            "worker.issue_priority 必须是标签名数组（如 [\"bug\", \"test\"]）")
    if not val:
        raise HTTPException(400, "worker.issue_priority 不能为空（至少保留一个标签）")
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in val:
        if not isinstance(item, str):
            raise HTTPException(400, "worker.issue_priority 每项必须是字符串")
        name = item.strip()
        err = validate_label(name)
        if err:
            raise HTTPException(400, f"worker.issue_priority 包含非法标签名 {item!r}: {err}")
        if name in seen:
            raise HTTPException(400, f"worker.issue_priority 标签重复: {name}")
        seen.add(name)
        cleaned.append(name)
    return cleaned


def _validate_claude(patch: dict) -> None:
    if "command" in patch and (not patch["command"] or not isinstance(patch["command"], str)):
        raise HTTPException(400, "claude.command 必须是字符串")
    if "args" in patch and (not isinstance(patch["args"], list) or not all(isinstance(a, str) for a in patch["args"])):
        raise HTTPException(400, "claude.args 必须是字符串数组")


def _validate_dsh(patch: dict) -> None:
    """校验 dsh 段（issue #84）：字符串字段 + max_tokens 正整数或 null。"""
    for key in ("provider", "model", "session_root", "cordis", "runtime_bin",
                "base_url", "api_key"):
        if key in patch and not isinstance(patch[key], str):
            raise HTTPException(400, f"dsh.{key} 必须是字符串")
    if "max_tokens" in patch and patch["max_tokens"] is not None:
        val = patch["max_tokens"]
        if not isinstance(val, int) or val <= 0:
            raise HTTPException(400, "dsh.max_tokens 必须是正整数或 null")
    if "reasoning_effort" in patch:
        # 推理等级（issue #123）：deepseek-harness runtime 仅支持
        # off / high / max（llm-deepseek adapter 配置 schema 白名单），
        # 空串 = 不设置（跟随 SDK 默认）；非法值提前拦截避免任务运行时报错
        val = patch["reasoning_effort"]
        if not isinstance(val, str):
            raise HTTPException(
                400, "dsh.reasoning_effort 必须是字符串（off / high / max，空串=不设置）")
        val = val.strip()
        if val not in ("", "off", "high", "max"):
            raise HTTPException(
                400, f"dsh.reasoning_effort 取值非法: {val}（可选 off / high / max，空串=不设置）")
        patch["reasoning_effort"] = val


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


def _validate_owner_token_scope(cfg, token: str) -> None:
    """保存前校验 Owner token（issue #133）：必须有效且含 api scope。

    根因：GitLab REST 写操作（添加/回复评论、添加/关闭 issue、编辑标签）
    要求 PAT 具备 api scope；只勾 read_api 等只读 scope 的 token 提交
    写操作会被 GitLab 拒绝，403 响应体为
    {"error":"insufficient_scope","error_description":"The request
    requires higher privileges than provided by the access token."}。
    此前设置页保存时不校验，不可用的 token 直接落盘，用户反复重新保存
    后概览页编辑仍持续 403（issue #133 实测复现）。

    这里在保存前调 /personal_access_tokens/self 校验有效性 + api scope：
    - 401 → token 无效/已过期，400 拒绝；
    - 403 → 连 scope 自检都被拒绝（必然缺 read_api，从而也缺 api），400 拒绝；
    - 404 → 旧版 GitLab（< 15.7）无 self 端点，降级只校验 token 有效性；
    - 有 self 端点但 scopes 不含 api → 400 拒绝并列出当前 scopes。
    校验失败一律不落盘（update_gitlab 不执行），避免把不可用 token 存进
    config.yaml。
    """
    client = GitLabClient(cfg.gitlab_url, token, verify_ssl=cfg.verify_ssl)
    try:
        info = client.get_personal_access_token_self()
    except GitLabError as e:
        if e.status_code == 404:
            # 旧版 GitLab：降级为仅校验 token 有效性
            try:
                client.test_connection()
            except GitLabError as e2:
                raise HTTPException(
                    400,
                    f"Owner token 无效或已过期（{e2.status_code}）：请重新生成 "
                    "GitLab Personal Access Token（glpat-xxxx）后保存") from e2
            return
        if e.status_code == 401:
            raise HTTPException(
                400, "Owner token 无效或已过期（401）：请重新生成 "
                "GitLab Personal Access Token（glpat-xxxx）后保存") from e
        if e.status_code == 403:
            raise HTTPException(
                400, "Owner token 缺少 api scope（GitLab 拒绝自检）：请在 "
                "GitLab 用户设置 → Access Tokens 重新生成 token 并勾选 "
                "api scope 后保存") from e
        raise HTTPException(400, f"无法验证 Owner token：{e}") from e
    scopes = info.get("scopes") or []
    if "api" not in scopes:
        raise HTTPException(
            400,
            "Owner token 缺少 api scope（当前 scopes：" + ("、".join(scopes) or "无")
            + "）：只有 api scope 才能写评论/编辑 issue，read_api 等只读 "
            "scope 不够。请在 GitLab 用户设置 → Access Tokens 重新生成 "
            "token，Scopes 勾选 api 后保存")


def _validate_gitlab(patch: dict) -> None:
    """校验 gitlab 段（issue #87）：owner_token 必须是字符串或 null。

    掩码值/空串 = 保持现有凭据（update_gitlab 处理），此处只查类型。
    """
    if "owner_token" in patch and patch["owner_token"] is not None \
            and not isinstance(patch["owner_token"], str):
        raise HTTPException(400, "gitlab.owner_token 必须是字符串")


def _validate_sso(patch: dict, current) -> None:
    """校验 sso 段（issue #27）：类型、URL 格式、启用时必填项。

    current 为当前 Settings：启用校验看"补丁后的最终值"而不是补丁本身，
    保证单独提交 enabled=true 也能正确拒绝缺参。
    """
    enabled = patch.get("enabled", current.sso_enabled)
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise HTTPException(400, "sso.enabled 必须是布尔值")
    if "verify_ssl" in patch and not isinstance(patch["verify_ssl"], bool):
        raise HTTPException(400, "sso.verify_ssl 必须是布尔值")

    for key in ("well_known_url", "client_id", "client_secret", "scope", "redirect_uri"):
        if key in patch and patch[key] is not None and not isinstance(patch[key], str):
            raise HTTPException(400, f"sso.{key} 必须是字符串")

    for key in ("well_known_url", "redirect_uri"):
        if key in patch and patch[key]:
            val = patch[key]
            if not val.startswith(("http://", "https://")):
                raise HTTPException(400, f"sso.{key} 必须以 http(s):// 开头")

    if "session_days" in patch:
        val = patch["session_days"]
        if not isinstance(val, int) or isinstance(val, bool) or not 1 <= val <= 365:
            raise HTTPException(400, "sso.session_days 必须是 1~365 的整数（天）")

    if enabled:
        # 启用 SSO 时关键配置必填（掩码占位视为已有值）
        has_secret = bool(current.sso_client_secret) or (
            "client_secret" in patch and patch["client_secret"]
            and "*" not in str(patch["client_secret"])
        )
        missing = []
        if not (patch.get("well_known_url") or current.sso_well_known_url):
            missing.append("well_known_url")
        if not (patch.get("client_id") or current.sso_client_id):
            missing.append("client_id")
        if not has_secret:
            missing.append("client_secret")
        if missing:
            raise HTTPException(400, f"启用 SSO 前请先填写: {', '.join(missing)}")


def _validate_ai_providers(patch, current: list[dict]) -> list[dict]:
    """校验 ai_providers 段（issue #46）：整体替换列表。

    - name 必填非空且不重复；base_url 非空时须以 http(s):// 开头
    - api_key 回传掩码值（含 *）或留空 = 保持现有（按 name 匹配旧配置，
      与 sso.client_secret 同模式）；新增条目匹配不到则存空串
    - provider 缺省归一为 custom；enabled 必须是布尔值
    """
    if not isinstance(patch, list):
        raise HTTPException(400, "ai_providers 必须是数组")
    by_name = {p["name"]: p for p in current if p.get("name")}
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in patch:
        if not isinstance(item, dict):
            raise HTTPException(400, "ai_providers 每项必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "ai_providers.name 必填非空")
        if name in seen:
            raise HTTPException(400, f"供应商名称重复: {name}")
        seen.add(name)
        provider = str(item.get("provider") or "").strip() or "custom"
        base_url = str(item.get("base_url") or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, f"{name}.base_url 必须以 http(s):// 开头")
        api_key = item.get("api_key")
        if api_key is None:
            api_key = ""
        if not isinstance(api_key, str):
            raise HTTPException(400, f"{name}.api_key 必须是字符串")
        model = str(item.get("model") or "").strip()
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HTTPException(400, f"{name}.enabled 必须是布尔值")
        if not api_key.strip() or "*" in api_key:
            api_key = by_name[name]["api_key"] if name in by_name else ""
        cleaned.append({
            "name": name,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "enabled": enabled,
        })
    return cleaned


def ctx_of(request: Request):
    return request.app.state.ctx
