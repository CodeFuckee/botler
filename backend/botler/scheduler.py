"""任务调度器。

- 每个仓库一个队列（deque），同一仓库同一时刻最多一个 running
- 跨仓库并行，受 max_concurrent_repos 限制
- 派发选择按仓库优先级（issue #51）：priority 数字小先派发，
  同优先级按队内最优任务排序键比较，再按 repo_id 兜底
- 队内任务排序（issue #76 + #234）：按「issue 标签权重」选任务派发——
  任务 issue_labels 在配置 worker.issue_priority 中首个命中的索引即权重
  （越靠前越先处理），未命中配置标签（或无标签）排最后；同权重按
  issue 创建时间升序（创建早的 issue 先处理，issue #234；创建时间缺失
  时按 issue 更新时间、再按任务提交时间 created_at 兜底）
- 状态机：queued → running → retrying → succeeded / failed
- 重启恢复：queued 重新入队，running/retrying 标记 interrupted 后重新入队
- 任务持久化在 SQLite，调度器线程只负责派发，执行在 worker 线程
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque

from .config import ConfigManager
from .database import Database, DEFAULT_PRIORITY, STATUS_QUEUED, STATUS_RUNNING
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

    @staticmethod
    def _decode_labels(raw: str | None) -> list[str]:
        """解析 tasks.issue_labels（JSON 数组串）。损坏数据按无标签处理。"""
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (ValueError, TypeError):
            return []

    def _task_sort_key(self, task_id: int, cfg) -> tuple[int, str, int]:
        """任务排序键（issue #76 + #234）：(标签权重, issue 创建时间, task_id)。

        权重 = issue_labels 在配置 issue_priority 列表中首个命中的索引；
        未命中任何配置标签（或无标签）排最后（权重 = len(priority)）。
        同权重按 issue 创建时间升序（issue #234：创建时间越早的 issue
        越先处理；UTC 串可直接比较；创建时间缺失的历史任务按 issue 更新
        时间、再按任务提交时间 created_at 兜底），task_id 兜底保证确定性。
        """
        priority = cfg.issue_priority_labels
        unlisted = len(priority)
        task = self.db.get_task(task_id)
        if task is None:
            return (unlisted, "", task_id)
        labels = self._decode_labels(task["issue_labels"])
        weight = unlisted
        for i, name in enumerate(priority):
            if name in labels:
                weight = i
                break
        created = (task["issue_created_at"] or task["issue_updated_at"]
                   or task["created_at"] or "")
        return (weight, created, task_id)

    def _pick_task(self, q: deque[int], cfg) -> int:
        """从仓库队列中选择最优任务并移除（issue #76）。

        按 _task_sort_key 取最小者（标签权重优先、同权重 issue 创建时间升序）。
        队列长度通常很小，线性扫描 + deque.remove 的成本可忽略。
        派发时动态读配置：设置页修改 issue_priority 后无需重新入队，
        已排队任务立即按新顺序派发。
        """
        best = min(q, key=lambda tid: self._task_sort_key(tid, cfg))
        q.remove(best)
        return best

    def _repo_sort_key(self, repo_id: int, q: deque[int], cfg) -> tuple[int, tuple, int]:
        """候选仓库排序键（issue #51）：仓库优先级升序（数字小先），
        同优先级按队内最优任务的排序键（issue #76 + #234：标签权重 →
        issue 创建时间）比较，再按 repo_id 兜底保证确定性。"""
        row = self.db.get_repo(repo_id)
        priority = DEFAULT_PRIORITY
        if row is not None and row["priority"] is not None:
            priority = int(row["priority"])
        best = min(q, key=lambda tid: self._task_sort_key(tid, cfg))
        return (priority, self._task_sort_key(best, cfg), repo_id)

    def _dispatch(self) -> None:
        cfg = self.config.get()
        with self._lock:
            running_count = len(self._running)
            if running_count >= cfg.max_concurrent_repos:
                return
            # 候选：无 running 任务且有排队任务的仓库
            candidates = [
                (repo_id, q) for repo_id, q in self._queues.items()
                if repo_id not in self._running and q
            ]
            if not candidates:
                return
            repo_id, q = min(
                candidates, key=lambda item: self._repo_sort_key(item[0], item[1], cfg))
            task_id = self._pick_task(q, cfg)
            if not q:
                self._queues.pop(repo_id, None)
            self._running[repo_id] = task_id
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
