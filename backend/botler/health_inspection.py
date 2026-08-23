"""仓库健康巡检（issue #265）。

背景：仓库添加时有「测试连通性」按钮（POST /api/repos/{id}/test，人工
触发），但对账调度之外的日常运行中 webhook 是否仍有效、token 是否过期、
仓库是否还能访问，平台不主动检查——webhook 失效后新 issue 事件收不到，
只能靠对账兜底且不告警，是静默故障。

功能：
1. 定时巡检（复用 APScheduler，间隔 inspection.interval_seconds 可配置，
   默认 6 小时）：对每个启用仓库检查
   a) webhook 存在且 secret 匹配（GET /projects/{id}/hooks 比对 URL 与
      token 字段）；
   b) token 有效性（轻量 API 调用 GET /user）；
   c) 仓库可达（GET /projects/{id}）。
2. 巡检结果写 repo_health 表（health_status / check_time / last_error /
   各检查项明细 / 自动修复标记），历史保留，仓库列表徽章取最新一行；
   未巡检过的仓库健康状态为「未知」。
3. webhook 缺失 / secret 不匹配时自动重新注册（inspection.auto_repair，
   默认开），注册成功后在当轮结果标记 repaired。
4. 异常聚合通知（in_app 网页通知 + webhook 推送）：同一轮巡检所有异常
   仓库汇总为一条告警，按 alerts.throttle_seconds 节流（默认 1 小时），
   避免每轮巡检反复提醒刷屏。
5. 仓库列表页展示健康徽章（正常/异常/未知），异常可点击查看详情与手动
   重检（POST /api/repos/{id}/health-check）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .database import Database
from .gitlab_client import GitLabClient, GitLabError, HOOK_URL_PATH

logger = logging.getLogger(__name__)

# 健康状态三态（与仓库列表徽章一一对应）：
# healthy = 最新一次巡检全部检查项通过；abnormal = 存在失败检查项；
# unknown = 从未巡检过（无记录）
HEALTH_HEALTHY = "healthy"
HEALTH_ABNORMAL = "abnormal"
HEALTH_UNKNOWN = "unknown"

# 仓库健康告警事件类型（聚合通知，前端 NOTIFY_TYPE_MAP 缺省视为开启）
ALERT_REPO_HEALTH = "alert_repo_health"

# 聚合通知正文最多展示的异常仓库数（其余折叠为「等 N 个仓库…」）
_ABNORMAL_DISPLAY_LIMIT = 5


def _utc_now_str() -> str:
    """当前 UTC 时间字符串（YYYY-MM-DD HH:MM:SS，与库内时间格式一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RepoHealthInspector:
    """仓库健康巡检器：定时检查 + 自动修复 + 聚合告警。

    :param config:   ConfigManager（inspection 段阈值与开关）
    :param db:       Database（巡检结果落库 repo_health 表）
    :param gitlab:   GitLabClient（webhook / token / 项目检查）
    :param notifier: Notifier（in_app 网页通知事件记录；None = 自动创建）
    """

    def __init__(self, config, db: Database, gitlab: GitLabClient,
                 notifier=None):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        if notifier is None:
            from .notifier import Notifier
            notifier = Notifier(db)
        self.notifier = notifier
        self._aps = BackgroundScheduler(timezone="UTC")

    # ---- 定时调度 ----

    def start_scheduler(self) -> None:
        """启动定时巡检（幂等）。间隔读配置，重启/热重载后按新间隔生效。"""
        cfg = self.config.get()
        if self._aps.running:
            return
        self._aps.add_job(
            self._scheduled_inspect,
            IntervalTrigger(seconds=max(300, cfg.inspection_interval_seconds)),
            id="botler-health-inspection", name="仓库健康巡检",
            coalesce=True, max_instances=1, replace_existing=True,
        )
        self._aps.start()
        logger.info("仓库健康巡检已启动（间隔 %ss，auto_repair=%s）",
                    cfg.inspection_interval_seconds, cfg.inspection_auto_repair)
        # 启动后异步先跑一次，缩短首次空窗（与对账首轮同模式，不阻塞启动流程）
        import threading

        threading.Thread(
            target=self._scheduled_inspect,
            name="botler-health-inspection-first", daemon=True,
        ).start()

    def stop_scheduler(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    def _scheduled_inspect(self) -> None:
        try:
            self.inspect_once()
        except Exception as exc:  # noqa: BLE001 定时任务失败不能中断服务
            logger.exception("定时仓库健康巡检失败: %s", exc)

    # ---- 巡检入口 ----

    def inspect_once(self, repo_id: int | None = None,
                     force: bool = False) -> dict:
        """巡检一轮，返回 {"checked", "abnormal", "repaired", "errors"}。

        repo_id 为 None 时巡检全部启用仓库（定时）；指定时只检该仓库
        （仓库列表页「重新巡检」按钮）。每仓库检查结果写 repo_health 表；
        巡检完成后把本轮所有异常仓库聚合为一条告警通知（节流防刷屏）。
        单仓库巡检失败只记日志并计入 errors，不影响其他仓库。

        force=True 时忽略 inspection.enabled 总开关（手动重检是用户显式
        操作，即使关闭了定时巡检也应立即生效；定时任务走默认 False）。
        """
        cfg = self.config.get()
        if not force and not cfg.inspection_enabled:
            return {"checked": 0, "abnormal": [], "repaired": 0, "errors": []}
        if repo_id is not None:
            repos = [self.db.get_repo(repo_id)]
        else:
            repos = self.db.list_repos()
        enabled = [r for r in repos if r is not None and r["enabled"]]
        checked = repaired = 0
        abnormal: list[dict] = []
        errors: list[str] = []
        for repo in enabled:
            try:
                result = self._inspect_repo(repo, cfg)
            except Exception as exc:  # noqa: BLE001 单仓库异常不影响其他仓库
                logger.exception("巡检仓库 %s 失败", repo["name"])
                errors.append(f"{repo['name']}: {exc}")
                continue
            checked += 1
            if result["repaired"]:
                repaired += 1
            if result["status"] == HEALTH_ABNORMAL:
                abnormal.append({
                    "repo_id": repo["id"],
                    "name": repo["name"],
                    "error": result["last_error"] or "",
                })
        if abnormal:
            self._notify_abnormal(abnormal, cfg)
        return {
            "checked": checked,
            "abnormal": [a["name"] for a in abnormal],
            "repaired": repaired,
            "errors": errors,
        }

    # ---- 单仓库检查 ----

    def _inspect_repo(self, repo, cfg) -> dict:
        """单个仓库巡检：webhook / token / 项目可达，结果落库。

        返回 {"status", "last_error", "webhook_ok", "token_ok",
        "project_ok", "repaired"}。任一检查项失败 → abnormal，
        last_error 为失败项描述（多项以「；」连接）。
        """
        project_id = repo["gitlab_project_id"]
        now = _utc_now_str()
        webhook_ok, webhook_err = self._check_webhook(project_id, cfg)
        repaired = False
        if not webhook_ok and cfg.inspection_auto_repair:
            # webhook 缺失 / secret 不匹配 / 事件开关被改 → 自动修复（issue
            # #265 验收标准 2）；修复成功即视为本轮 webhook 检查通过并标记
            # repaired
            try:
                self._repair_webhook(project_id, cfg.webhook_secret)
                repaired = True
                webhook_ok, webhook_err = True, None
                logger.info("巡检仓库 %s：webhook 异常，已自动修复", repo["name"])
            except GitLabError as exc:
                webhook_err = f"自动修复 webhook 失败: {exc}"
        token_ok, token_err = self._check_token()
        project_ok, project_err = self._check_project(project_id)

        failures: list[str] = []
        if not webhook_ok:
            failures.append(f"webhook {webhook_err}")
        if not token_ok:
            failures.append(f"token {token_err}")
        if not project_ok:
            failures.append(f"项目 {project_err}")
        status = HEALTH_ABNORMAL if failures else HEALTH_HEALTHY
        last_error = "；".join(failures) or None
        self.db.add_repo_health(
            repo["id"], status, now, last_error=last_error,
            webhook_ok=webhook_ok, token_ok=token_ok,
            project_ok=project_ok, repaired=repaired)
        return {
            "status": status, "last_error": last_error,
            "webhook_ok": webhook_ok, "token_ok": token_ok,
            "project_ok": project_ok, "repaired": repaired,
        }

    @staticmethod
    def _find_platform_hook(hooks: list[dict]) -> dict | None:
        """在项目 hook 列表中查找平台注册的 webhook。

        按回调路径后缀（/webhook/gitlab）匹配而非 URL 全等：部署环境
        Botler 回调地址（如 http://10.0.0.122:8000）与 gitlab_url 通常
        不同，client.webhook_url() 是按其 webhook_base_url 拼接的，可能
        与真实注册地址不一致；后缀匹配 host 无关，能正确识别任何
        地址上注册的平台 webhook（与 add_repo 注册时按访问地址动态
        生成的行为兼容）。
        """
        return next(
            (h for h in hooks if (h.get("url") or "").endswith(HOOK_URL_PATH)),
            None)

    def _check_webhook(self, project_id: int, cfg) -> tuple[bool, str | None]:
        """webhook 存在且 secret 匹配（GET /projects/{id}/hooks 比对）。

        GitLab 出于安全对 hook 的 token 字段做掩码（list/single 接口均
        返回 null），仅当接口返回明文 token 时才做严格比对；掩码时无法
        比对 secret，视为已配置（webhook 存在性已确认）。同时校验 issue
        事件开关（issues_events=false 时新 issue 事件同样收不到）。
        """
        try:
            hooks = self.gitlab.list_webhooks(project_id)
        except GitLabError as exc:
            return False, f"读取 webhook 列表失败: {exc}"
        hook = self._find_platform_hook(hooks)
        if hook is None:
            return False, "webhook 未注册"
        token = hook.get("token")
        if token and token != cfg.webhook_secret:
            return False, "webhook secret 不匹配"
        if hook.get("issues_events") is False:
            return False, "webhook 未开启 issue 事件"
        return True, None

    def _repair_webhook(self, project_id: int, secret: str) -> None:
        """自动修复 webhook：优先更新已存在的平台 hook，缺失时新建。

        更新已存在的 hook 时保留其回调 URL（以注册时实际地址为准），只
        刷新 issues_events 开关与 secret——若按 client.webhook_url() 重建
        会注册到错误的默认地址（部署环境回调地址与 gitlab_url 不同）。
        缺失时回退 register_webhook 新建（与添加仓库行为一致）。
        """
        hooks = self.gitlab.list_webhooks(project_id)
        existing = self._find_platform_hook(hooks)
        if existing is not None:
            self.gitlab.update_webhook(
                project_id, existing["id"], existing["url"], secret)
            return
        self.gitlab.register_webhook(project_id, secret)

    def _check_token(self) -> tuple[bool, str | None]:
        """token 有效性：轻量 API 调用（GET /user）。

        401/403 明确判定为 token 失效；其余异常（传输层故障等）同样记
        失败并带错误描述，由聚合通知与节流避免误报刷屏。
        """
        try:
            self.gitlab.test_connection()
            return True, None
        except GitLabError as exc:
            if exc.status_code in (401, 403):
                return False, "token 无效或已过期"
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 非 GitLabError 异常同样判失败
            return False, str(exc)

    def _check_project(self, project_id: int) -> tuple[bool, str | None]:
        """仓库可达：GET /projects/{id}。"""
        try:
            self.gitlab.get_project(project_id)
            return True, None
        except GitLabError as exc:
            if exc.status_code == 404:
                return False, "项目不存在（可能已被删除或无权限）"
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ---- 聚合告警（issue #265 验收标准 3：不刷屏）----

    def _notify_abnormal(self, abnormal: list[dict], cfg) -> None:
        """异常聚合通知：一轮巡检的所有异常仓库汇总为一条告警。

        in_app 经 Notifier.record_alert 落库网页通知事件（前端轮询弹系统
        通知），webhook 经 WebhookPusher.send_alert 推送；同类型按
        alerts.throttle_seconds 节流（默认 1 小时）——窗口内重复巡检不再
        分发，杜绝刷屏。任一通道失败仅记日志，不影响巡检。
        """
        title = f"⚠️ {len(abnormal)} 个仓库健康异常"
        shown = abnormal[:_ABNORMAL_DISPLAY_LIMIT]
        lines = [f"- {a['name']}：{a['error']}" for a in shown]
        more = len(abnormal) - len(shown)
        if more > 0:
            lines.append(f"- 等 {more} 个仓库…")
        body = "仓库健康巡检发现以下异常（详情见仓库列表健康徽章）：\n" + \
               "\n".join(lines)
        detail = "; ".join(f"{a['name']}({a['error']})" for a in shown)
        event_id = self.notifier.record_alert(
            ALERT_REPO_HEALTH, title, body,
            data={"count": len(abnormal), "repos": abnormal},
            window_seconds=cfg.alert_throttle_seconds)
        if event_id is None:
            # 节流窗口内已通知过：in_app 不重复落库，webhook 也不重复推送
            return
        try:
            from .webhook_push import WebhookPusher, WebhookPushError

            WebhookPusher(self.config).send_alert(
                ALERT_REPO_HEALTH, title, body, detail=detail)
        except WebhookPushError as exc:
            logger.warning("仓库健康巡检告警 webhook 推送失败: %s", exc)
        except Exception:  # noqa: BLE001 推送异常不影响巡检
            logger.exception("仓库健康巡检告警 webhook 推送异常")


__all__ = [
    "RepoHealthInspector",
    "HEALTH_HEALTHY",
    "HEALTH_ABNORMAL",
    "HEALTH_UNKNOWN",
    "ALERT_REPO_HEALTH",
    "_utc_now_str",
]
