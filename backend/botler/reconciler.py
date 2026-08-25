"""对账兜底调度器（APScheduler）。

每 reconcile_interval_seconds 扫描一次启用中的仓库：
找出「assignee 是 bot 但任务表无活跃记录」的 open issues，补入队。
解决 webhook 丢事件 / 平台重启窗口期漏任务的问题。
"""

from __future__ import annotations

import logging
import random
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import ConfigManager
from .database import Database, STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCEEDED, normalize_issue_created_at, normalize_issue_updated_at
from .gitlab_client import GitLabClient, GitLabError, configure_default_rate_limiter
from .git_remote import build_repo_client_with_username
from .labels import CLAIM_SKIP_LABELS
from .scheduler import TaskScheduler

logger = logging.getLogger(__name__)

# 终态标签（issue #30）：bot-done = 已完成待用户确认关闭；bot-failed = 处理失败
# 待人工介入。用于终态标签对账（_backfill_terminal_labels）的补打判定。
# 补入队过滤见 labels.CLAIM_SKIP_LABELS（终态标签 + need-verify，issue #41）。
TERMINAL_LABELS = ("bot-done", "bot-failed")

# 终态标签对账扫描的任务数上限（issue #40）：每次对账只回看每仓库最近
# BACKFILL_TASKS_LIMIT 条终态任务，避免大仓库反复扫描全量历史
BACKFILL_TASKS_LIMIT = 20


class Reconciler:
    def __init__(self, config: ConfigManager, db: Database,
                 gitlab: GitLabClient, scheduler: TaskScheduler):
        self.config = config
        self.db = db
        self.gitlab = gitlab
        self.scheduler = scheduler
        self._aps = BackgroundScheduler(timezone="UTC")
        # 网页通知事件（issue #21）：对账扫描后产生队列状态通知
        from .notifier import Notifier
        self.notifier = Notifier(db)
        # 聚合告警检测（issue #229）：对账循环内执行异常检测（失败率/
        # 队列堆积/token 失效/磁盘空间），告警经现有 notifier 通知，
        # 阈值设置页可配置（config.yaml alerts 段）
        from .alerts import AlertChecker
        self.alerts = AlertChecker(config, db, self.notifier, gitlab=gitlab)

    def start(self) -> None:
        cfg = self.config.get()
        trigger = IntervalTrigger(seconds=max(60, cfg.reconcile_interval_seconds))
        self._aps.add_job(
            self.reconcile_once, trigger,
            id="botler-reconcile", name="对账兜底扫描",
            coalesce=True, max_instances=1, replace_existing=True,
        )
        self._aps.start()
        logger.info("对账兜底已启动（间隔 %ss）", cfg.reconcile_interval_seconds)
        # 启动后异步先跑一次，缩短首次空窗（不阻塞启动流程）
        import threading
        threading.Thread(target=self.reconcile_once, name="botler-reconcile-first", daemon=True).start()

    def stop(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    def reconcile_once(self, repo_id: int | None = None) -> dict:
        """扫描一轮，返回补入队的任务数。

        repo_id 为 None 时扫描全部启用仓库（定时兜底）；指定时只扫该仓库
        （仓库页「对账」按钮，issue #17）。单仓库扫描时若 GitLab 报错，
        错误信息记入返回值 errors 列表，由 API 层转成 HTTP 错误。
        """
        cfg = self.config.get()
        # 配置可热重载：在本轮的首个 bot 身份查询前更新共享限速。
        configure_default_rate_limiter(cfg.gitlab_api_requests_per_second)
        # 全局 bot 身份（config 优先）。全局 token 失效时不再整体放弃对账
        # （issue #63）：降级为 None，由每仓库用 remote url 内嵌 token
        # 客户端兜底，以其账号作为该仓库的 bot 身份。
        bot_id = cfg.bot_id
        if bot_id is None:
            try:
                bot_id = self.gitlab.get_bot_id()
            except GitLabError as e:
                logger.warning("对账：全局 token 获取 bot 身份失败（%s），"
                               "将对启用仓库逐个尝试 remote token 兜底", e)

        repos = [self.db.get_repo(repo_id)] if repo_id is not None else self.db.list_repos()
        enabled_repos = [repo for repo in repos if repo is not None and repo["enabled"]]
        scanned = enqueued = 0
        errors: list[str] = []
        for index, repo in enumerate(enabled_repos):
            # 仅全量扫描在仓库之间加入随机抖动；单仓库手动对账无需额外等待。
            if repo_id is None and index:
                delay = random.uniform(cfg.reconcile_jitter_min_seconds,
                                       cfg.reconcile_jitter_max_seconds)
                logger.debug("对账扫描抖动：%.2fs 后扫描仓库 %s", delay, repo["name"])
                time.sleep(delay)
            s, e, errs = self._reconcile_repo(repo, cfg, bot_id)
            scanned += s
            enqueued += e
            errors.extend(errs)
        result: dict = {"scanned": scanned, "enqueued": enqueued}
        if errors:
            result["errors"] = errors
        # 聚合告警检测（issue #229）：启动/对账时检测异常（近 1 小时失败率
        # > 阈值、队列堆积且无进度、GitLab token 失效 401、磁盘空间不足），
        # 经现有 notifier（网页通知 in_app + webhook 推送）主动通知；阈值
        # 设置页可配置。检测失败仅记日志，不影响对账结果（告警内部容错）。
        try:
            self.alerts.check()
            # issue #279：复用对账循环检测 owner / 仓库 token 到期时间。
            from .token_expiry import TokenExpiryChecker
            TokenExpiryChecker(self.db, self.notifier, self.config).check()
        except Exception:  # noqa: BLE001
            logger.exception("聚合告警检测失败")
        return result

    def _call_with_fallback(self, repo: dict, verify_ssl: bool, client, call):
        """用 client 执行 call(client)；遇 401/403（token 失效）时尝试从仓库
        remote url 提取内嵌 token 构建 per-repo client 重试一次（issue #63）。

        issue #130 + #132：对账（终态标签补打等）绝不使用 owner token——
        owner token 只允许在概览页 issue 编辑操作时由平台使用（见
        api/issues.py），agent 无论如何都不能使用 owner token（issue #87
        的 prefer_owner 机制已按 #130 移除）。

        返回 (result, client)。client 已是 per-repo 兜底客户端时不再重复
        兜底；remote 无可用 token 或重试仍失败时抛 GitLabError。
        """
        try:
            return call(client), client
        except GitLabError as e:
            if e.status_code not in (401, 403) or client is not self.gitlab:
                raise
            fallback, _ = build_repo_client_with_username(repo, verify_ssl)
            if fallback is None:
                raise
            logger.info("对账仓库 %s：token 失效（%s），改用 remote url 内嵌 token 重试",
                        repo["name"], e)
            return call(fallback), fallback

    def _reconcile_repo(self, repo: dict, cfg, bot_id: int | None) -> tuple[int, int, list[str]]:
        """扫描单个仓库并补入队，返回 (scanned, enqueued, errors)。

        issue #63：默认用全局 client（bot token）；调用遇 401/403 时用仓库
        remote url 内嵌 token 的 per-repo client 兜底重试。全局 bot 身份
        不可用（bot_id 为 None）时，以 per-repo client 的账号作为该仓库
        的 bot 身份；remote 无可用 token 则记错跳过。
        """
        client = self.gitlab
        bot_ids: list[int] = []
        fallback, username = build_repo_client_with_username(repo, cfg.verify_ssl)
        if bot_id is not None:
            bot_ids = [bot_id]
        elif fallback is not None:
            client = fallback
            try:
                bot_ids = [client.get_bot_id()]
            except GitLabError as e:
                return 0, 0, [f"仓库 {repo['name']}: {e}"]
            logger.info("对账仓库 %s：全局 bot 身份不可用，改用 remote 身份 %s",
                        repo["name"], bot_ids)
        else:
            return 0, 0, [f"仓库 {repo['name']}: 全局 token 失效且 remote 无内嵌 token"]
        # issue #65 + #487：remote URL 用户名（如 agent）也作为 bot 身份候选——
        # 用户通常把 issue 分配给该账号，仅以全局 bot 账号扫描会静默漏扫
        # （扫描为 0 且无任何报错）。该合并不再局限于「全局 bot 身份不可用」
        # 的降级路径：全局身份可用时同样纳入 remote 用户名账号，保证分配给
        # @agent 的 issue 在任务结束后能被对账自动补入队、自动开始下一个任务。
        if username:
            try:
                uid = client.get_user_id_by_username(username)
            except GitLabError as e:
                logger.warning("对账仓库 %s：按用户名 %s 解析 bot 身份失败: %s",
                               repo["name"], username, e)
                uid = None
            if uid and uid not in bot_ids:
                bot_ids.append(uid)
        issues: list[dict] = []
        try:
            seen_iids: set[int] = set()
            for uid in bot_ids:
                batch, client = self._call_with_fallback(
                    repo, cfg.verify_ssl, client,
                    lambda c, u=uid: c.list_open_issues(
                        repo["gitlab_project_id"], assignee_id=u))
                for issue in batch:
                    if issue["iid"] not in seen_iids:
                        seen_iids.add(issue["iid"])
                        issues.append(issue)
        except GitLabError as e:
            msg = f"仓库 {repo['name']}: {e}"
            logger.warning("对账失败：%s", msg)
            return 0, 0, [msg]
        scanned = enqueued = 0
        errors: list[str] = []
        active_count = 0
        for issue in issues:
            scanned += 1
            labels = set(issue.get("labels") or [])
            # 领取过滤（issue #30 / #41）：终态标签 + need-verify
            # （用户标记需人工验证，bot 不领取）一律不补入队
            if labels & set(CLAIM_SKIP_LABELS):
                hit = sorted(labels & set(CLAIM_SKIP_LABELS))
                logger.info("对账跳过领取过滤标签 issue %s#%s（%s）",
                            repo["gitlab_project_id"], issue["iid"], hit)
                continue
            # 最后发言人过滤（issue #34）：最后一条非系统评论是 bot 本人时
            # 不补入队——bot 提问后用户未回复，平台重启/手动对账不应重复领取。
            # 用户回复后（或新任务无评论）再领取。
            try:
                last_author, client = self._call_with_fallback(
                    repo, cfg.verify_ssl, client,
                    lambda c: c.last_note_author_id(repo["gitlab_project_id"], issue["iid"]))
            except GitLabError as e:
                logger.warning("对账查询 issue %s#%s 评论失败: %s",
                               repo["gitlab_project_id"], issue["iid"], e)
                errors.append(f"仓库 {repo['name']} issue #{issue['iid']}: {e}")
                continue
            if last_author is not None and last_author in bot_ids:
                # issue #430：人工停止任务后，issue 的最后评论通常仍是 bot
                # 的受理/进度消息。若不区分这种终态，后续手动或定时对账会
                # 永久跳过该 issue，无法重新调度。仅对明确由人工停止的最近
                # 任务放行；其他场景仍保留原有过滤，避免 bot 提问后重复领取。
                latest = self.db.find_latest_task(
                    repo["gitlab_project_id"], issue["iid"])
                manually_stopped = (
                    latest is not None
                    and latest["status"] == "interrupted"
                    and (latest["error_message"] or "").startswith("用户手动停止"))
                if not manually_stopped:
                    logger.info("对账跳过最后发言人为 bot 的 issue %s#%s",
                                repo["gitlab_project_id"], issue["iid"])
                    continue
                logger.info("对账恢复人工停止的 issue %s#%s",
                            repo["gitlab_project_id"], issue["iid"])
            if self.db.find_active_task(repo["gitlab_project_id"], issue["iid"]):
                active_count += 1  # 已有活跃任务（含排队中）
                continue
            # 维护模式（issue #241）：对账照常扫描但不派发。默认（
            # maintenance_hold_events=True）照常建任务入队（调度器拦截
            # 派发，恢复后自动执行）；False = 扫描照常但跳过建任务。
            if cfg.maintenance_mode and not cfg.maintenance_hold_events:
                logger.info("维护模式开启且不保留事件，对账跳过创建任务 "
                            "%s#%s", repo["gitlab_project_id"], issue["iid"])
                continue
            # issue #76 + #234：补入队时记录 issue 标签、更新时间与创建
            # 时间，调度器按配置的标签优先级排序派发（同权重按创建时间
            # 升序，创建早的 issue 先处理）
            task_id = self.db.create_task(
                repo["id"], repo["gitlab_project_id"], issue["iid"],
                issue.get("title") or f"issue #{issue['iid']}",
                triggered_by="reconcile",
                issue_labels=issue.get("labels") or [],
                issue_updated_at=normalize_issue_updated_at(issue.get("updated_at")),
                issue_created_at=normalize_issue_created_at(issue.get("created_at")))
            if task_id is not None:
                self.scheduler.enqueue(task_id)
                enqueued += 1
                logger.info("对账补入队: 任务 %s (%s#%s)",
                            task_id, repo["gitlab_project_id"], issue["iid"])
        # 终态标签对账（issue #40）：任务收尾打标签时平台可能被部署重启
        # 打断（任务 #63 的 PUT /issues/39 未发出进程即被 pm2 delete 杀死），
        # issue 缺 bot-done/bot-failed 标签会被 webhook/对账重复领取。
        # 这里回看终态任务，issue 仍 open 且无终态标签时补打。
        self._backfill_terminal_labels(repo, cfg, client)
        # 失败上报对账（issue #352）：上报 issue 只在失败收尾时由 executor
        # 创建，收尾被打断 / 功能尚未部署 / 进程崩溃时失败上报永久缺失
        # （任务 #505 失败于自动上报功能上线前，未生成上报 issue）。这里
        # 回看最近任务，有失败分类且无上报标记的补建上报 issue（同 issue
        # #40 的补打思路：收尾被打断由对账兜底恢复）。
        self._backfill_failure_reports(repo, cfg, client)
        # 网页通知：队列状态（issue #21，节流由 notifier 负责）
        if not issues:
            self.notifier.queue_empty(repo["name"])
        elif active_count == len(issues) and enqueued == 0:
            self.notifier.queue_no_work(repo["name"], active_count)
        return scanned, enqueued, errors

    def _backfill_terminal_labels(self, repo: dict, cfg, client) -> None:
        """终态标签对账（issue #40）：给缺终态标签的终态任务补打 bot-done/bot-failed。

        收尾打标签依赖 executor 进程存活，而任务 push 的代码触发部署会 pm2
        delete 重启平台——收尾瞬间被打断的任务，issue 上既无 bot-done 也无
        bot-failed（生产任务 #63），webhook/对账会把它当新任务重复领取。
        这里兜底扫描最近 BACKFILL_TASKS_LIMIT 条终态任务：issue 仍 open 且
        无终态标签时，按任务结果补打对应标签。失败不影响主流程（下轮再试）。

        client 为当前仓库生效的 GitLab 客户端（全局或 remote token 兜底，
        issue #63），调用遇 401/403 时同样尝试 remote token 兜底。
        """
        tasks = self.db.list_tasks(
            status=[STATUS_SUCCEEDED, STATUS_FAILED],
            repo_id=repo["id"], limit=BACKFILL_TASKS_LIMIT)
        for task in tasks:
            project_id, iid = task["project_id"], task["issue_iid"]
            try:
                issue, client = self._call_with_fallback(
                    repo, cfg.verify_ssl, client,
                    lambda c: c.get_issue(project_id, iid))
            except GitLabError as e:
                logger.warning("终态标签对账：查询 issue %s#%s 失败: %s",
                               project_id, iid, e)
                continue
            if issue.get("state") != "opened":
                continue  # 已关闭（用户已确认），无需补打
            labels = set(issue.get("labels") or [])
            if labels & set(TERMINAL_LABELS):
                continue  # 已有终态标签，幂等跳过
            want = "bot-done" if task["status"] == STATUS_SUCCEEDED else "bot-failed"
            try:
                _, client = self._call_with_fallback(
                    repo, cfg.verify_ssl, client,
                    lambda c: c.add_labels(project_id, iid, [want]))
            except GitLabError as e:
                logger.warning("终态标签对账：补打 %s 失败（%s#%s）: %s",
                               want, project_id, iid, e)
                continue
            logger.info("终态标签对账：任务 %s（%s#%s）收尾被打断，已补打 %s",
                        task["id"], project_id, iid, want)

    def _backfill_failure_reports(self, repo: dict, cfg, client) -> None:
        """失败上报对账（issue #352）：补建漏报的任务失败上报 issue。

        上报 issue 只在任务失败收尾时由 executor 创建（_finish_failed →
        _emit_task_event → auto_issue 插件，issue #347）；收尾被打断 /
        功能尚未部署 / 进程崩溃时失败上报永久缺失——任务 #505 失败于自动
        上报功能上线前（2026-08-20 04:19 失败 vs 08:11 提交上线），issue
        上只有 bot-failed 标签、没有上报 issue。这里回看最近
        BACKFILL_TASKS_LIMIT 条任务：有失败分类（经历过失败终态，手动
        重试重置状态后仍保留）且任务日志无上报标记的，补建上报 issue——
        与 _backfill_terminal_labels（issue #40）同模式，失败不影响主
        流程（下轮对账再试）。

        client 为当前仓库生效的 GitLab 客户端（全局或 remote token 兜底），
        调用遇 401/403 时同样尝试 remote token 兜底（issue #63）。跳过
        执行中 / 最终成功的任务（失败已随重试解决，补报徒增噪音）；补建
        成功后落与 auto_issue 插件相同的去重标记，保证幂等。
        """
        from .plugins.auto_issue import (
            AUTO_ISSUE_LABELS, DEDUP_LOG_MARK,
            build_issue_description, build_issue_title)
        if not cfg.auto_issue_enabled:
            return
        tasks = self.db.list_tasks(
            repo_id=repo["id"], limit=BACKFILL_TASKS_LIMIT)
        for row in tasks:
            task = dict(row)
            task_id = int(task.get("id") or 0)
            if task.get("status") in (STATUS_RUNNING, STATUS_SUCCEEDED):
                continue  # 执行中 / 最终成功：失败已随重试解决，不补报
            if not (task.get("failure_category") or ""):
                continue  # 无失败分类 = 未经历过失败终态
            project_id = int(task.get("project_id") or 0)
            if not project_id:
                continue
            # 去重（与 auto_issue 插件同标记）：日志已有上报标记则跳过
            try:
                logs = self.db.list_logs(task_id) or []
            except Exception:  # noqa: BLE001 日志查询失败不阻塞补建
                logs = []
            if any(DEDUP_LOG_MARK in (log["message"] or "")
                   for log in logs):
                continue
            title = build_issue_title(task, repo_name=repo["name"])
            description = build_issue_description(
                task, self._failure_reason(task, task_id),
                repo_name=repo["name"], repo_url=repo["url"],
                gitlab_url=cfg.gitlab_url,
                category=task.get("failure_category") or "",
                detail=task.get("error_detail"))
            # 负责人（issue #347）：解析失败 / 未配置时不指定，不阻塞补建
            assignee_id = None
            username = (cfg.auto_issue_assignee or "").strip()
            if username:
                try:
                    uid, client = self._call_with_fallback(
                        repo, cfg.verify_ssl, client,
                        lambda c: c.get_user_id_by_username(username))
                    assignee_id = int(uid) if uid else None
                except (GitLabError, TypeError, ValueError):
                    logger.warning("失败上报对账：解析负责人 %s 失败，"
                                   "跳过指定负责人", username)
                    assignee_id = None
            try:
                issue, client = self._call_with_fallback(
                    repo, cfg.verify_ssl, client,
                    lambda c: c.create_issue(
                        project_id, title, description=description,
                        assignee_id=assignee_id,
                        labels=list(AUTO_ISSUE_LABELS)))
            except GitLabError as e:
                logger.warning("失败上报对账：任务 %s 创建上报 issue 失败: %s",
                               task_id, e)
                continue
            iid = issue.get("iid") or ""
            url = issue.get("web_url") or ""
            try:
                self.db.add_log(
                    task_id, "info", f"{DEDUP_LOG_MARK} #{iid}（{url}）")
            except Exception:  # noqa: BLE001 日志落库失败忽略
                pass
            logger.info("失败上报对账：任务 %s 补建失败上报 issue #%s",
                        task_id, iid)

    def _failure_reason(self, task: dict, task_id: int) -> str:
        """从任务日志提取失败原因（补报场景没有收尾时的 reason 参数）。

        失败收尾时 executor 会写「任务失败: <原因>」日志；对账补报优先
        用它还原正文失败原因，缺失时回退任务 error_message，再兜底通用
        文案（与 auto_issue 插件 build_issue_description 的兜底一致）。
        """
        try:
            for log in reversed(list(self.db.list_logs(task_id) or [])):
                msg = (log["message"] or "").strip()
                if msg.startswith("任务失败:"):
                    return msg.split(":", 1)[1].strip() or "（无失败原因）"
        except Exception:  # noqa: BLE001
            pass
        return (task.get("error_message") or "").strip() or "（失败分类对账补报）"
