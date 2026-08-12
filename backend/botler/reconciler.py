"""对账兜底调度器（APScheduler）。

每 reconcile_interval_seconds 扫描一次启用中的仓库：
找出「assignee 是 bot 但任务表无活跃记录」的 open issues，补入队。
解决 webhook 丢事件 / 平台重启窗口期漏任务的问题。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import ConfigManager
from .database import Database
from .gitlab_client import GitLabClient, GitLabError
from .scheduler import TaskScheduler

logger = logging.getLogger(__name__)

# 终态标签（issue #30）：agent 只处理「没有 bot-done / bot-failed 标签且未关闭」的
# issue。bot-done = 已完成待用户确认关闭；bot-failed = 处理失败待人工介入。
# 带这两个标签的 issue 不再补入队，避免重复处理已完成的 issue、失败 issue 无限重试。
TERMINAL_LABELS = ("bot-done", "bot-failed")


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
        try:
            bot_id = cfg.bot_id or self.gitlab.get_bot_id()
        except GitLabError as e:
            logger.warning("对账失败：无法获取 bot 身份: %s", e)
            return {"scanned": 0, "enqueued": 0, "errors": [f"无法获取 bot 身份: {e}"]}

        repos = [self.db.get_repo(repo_id)] if repo_id is not None else self.db.list_repos()
        scanned = enqueued = 0
        errors: list[str] = []
        for repo in repos:
            if repo is None or not repo["enabled"]:
                continue
            try:
                issues = self.gitlab.list_open_issues(repo["gitlab_project_id"], assignee_id=bot_id)
            except GitLabError as e:
                msg = f"仓库 {repo['name']}: {e}"
                logger.warning("对账失败：%s", msg)
                errors.append(msg)
                continue
            active_count = 0
            for issue in issues:
                scanned += 1
                labels = set(issue.get("labels") or [])
                if labels & set(TERMINAL_LABELS):
                    hit = sorted(labels & set(TERMINAL_LABELS))
                    logger.info("对账跳过终态标签 issue %s#%s（%s）",
                                repo["gitlab_project_id"], issue["iid"], hit)
                    continue
                if self.db.find_active_task(repo["gitlab_project_id"], issue["iid"]):
                    active_count += 1  # 已有活跃任务（含排队中）
                    continue
                task_id = self.db.create_task(
                    repo["id"], repo["gitlab_project_id"], issue["iid"],
                    issue.get("title") or f"issue #{issue['iid']}",
                    triggered_by="reconcile")
                if task_id is not None:
                    self.scheduler.enqueue(task_id)
                    enqueued += 1
                    logger.info("对账补入队: 任务 %s (%s#%s)",
                                task_id, repo["gitlab_project_id"], issue["iid"])
            # 网页通知：队列状态（issue #21，节流由 notifier 负责）
            if not issues:
                self.notifier.queue_empty(repo["name"])
            elif active_count == len(issues) and enqueued == 0:
                self.notifier.queue_no_work(repo["name"], active_count)
        result: dict = {"scanned": scanned, "enqueued": enqueued}
        if errors:
            result["errors"] = errors
        return result
