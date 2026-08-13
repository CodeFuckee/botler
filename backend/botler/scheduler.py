"""任务调度器。

- 每个仓库一个 FIFO 队列（deque），同一仓库同一时刻最多一个 running
- 跨仓库并行，受 max_concurrent_repos 限制
- 状态机：queued → running → retrying → succeeded / failed
- 重启恢复：queued 重新入队，running/retrying 标记 interrupted 后重新入队
- 任务持久化在 SQLite，调度器线程只负责派发，执行在 worker 线程
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from .config import ConfigManager
from .database import Database, STATUS_QUEUED, STATUS_RUNNING
from .executor import ClaudeExecutor

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1.0


class TaskScheduler:
    def __init__(self, config: ConfigManager, db: Database, executor: ClaudeExecutor):
        self.config = config
        self.db = db
        self.executor = executor
        # repo_id -> deque[task_id]
        self._queues: dict[int, deque[int]] = defaultdict(deque)
        # repo_id -> task_id（正在运行）
        self._running: dict[int, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="botler-scheduler", daemon=True)

    # ---- 对外接口 ----

    def enqueue(self, task_id: int) -> bool:
        """入队。若该任务已有活跃状态（并发调用）则忽略。"""
        task = self.db.get_task(task_id)
        if task is None:
            return False
        active = self.db.find_active_task(task["project_id"], task["issue_iid"])
        if active is not None and active["id"] != task_id:
            logger.info("任务 %s 被去重：%s#%s 已有活跃任务 %s",
                        task_id, task["project_id"], task["issue_iid"], active["id"])
            return False
        with self._lock:
            self._queues[task["repo_id"]].append(task_id)
        logger.info("任务 %s 入队（%s#%s）", task_id, task["project_id"], task["issue_iid"])
        return True

    def start(self) -> None:
        """启动调度线程，并恢复重启前遗留的任务。"""
        # 重启恢复：running/retrying → interrupted → queued（重新入队）
        restored = self.db.requeue_interrupted()
        for task_id in restored:
            self.enqueue(task_id)
        # queued 任务直接入队
        for task in self.db.list_tasks(status=STATUS_QUEUED, limit=10000):
            self.enqueue(task["id"])
        self._thread.start()
        logger.info("调度器已启动，恢复 %s 个中断任务", len(restored))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": len(self._running),
                "queued": sum(len(q) for q in self._queues.values()),
                "queues": {str(k): list(v) for k, v in self._queues.items()},
            }

    def stop_all(self) -> list[int]:
        """一键停止所有任务（issue #35）：状态落库 + 清空队列 + 终止运行中进程。

        返回被停止的任务 id 列表（db.stop_active_tasks 的返回）。
        顺序保证：先落库再登记停止请求——worker 感知到停止请求时
        状态必已 interrupted（或由 worker 收尾时条件更新兜底）。
        队列清理与派发循环共用 _lock：清空后不再派发新任务；已从队列
        取出尚未派发的任务在 _running 中登记，会被一并请求终止。
        """
        stopped = self.db.stop_active_tasks()
        with self._lock:
            self._queues.clear()
            running_ids = list(self._running.values())
        for task_id in running_ids:
            self.executor.request_stop(task_id)
        logger.info("一键停止所有任务：%s 个活跃任务已标记 interrupted", len(stopped))
        return stopped

    # ---- 调度循环 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch()
            except Exception:
                logger.exception("调度循环异常")
            time.sleep(_POLL_INTERVAL)

    def _dispatch(self) -> None:
        cfg = self.config.get()
        with self._lock:
            running_count = len(self._running)
            if running_count >= cfg.max_concurrent_repos:
                return
            # 找一个「无 running 任务」的仓库队首
            for repo_id, q in list(self._queues.items()):
                if repo_id in self._running or not q:
                    continue
                task_id = q.popleft()
                if not q:
                    self._queues.pop(repo_id, None)
                self._running[repo_id] = task_id
                break
            else:
                return
            repo_id_copy, task_id_copy = repo_id, task_id

        # 在锁外执行，避免阻塞调度循环
        threading.Thread(
            target=self._run_worker, args=(repo_id_copy, task_id_copy),
            name=f"botler-worker-{task_id_copy}", daemon=True,
        ).start()

    def _run_worker(self, repo_id: int, task_id: int) -> None:
        try:
            self.executor.run_task(task_id)
        except Exception:
            logger.exception("任务 %s 执行器异常", task_id)
        finally:
            with self._lock:
                self._running.pop(repo_id, None)
