"""调度器仓库优先级测试（issue #51）。

需求：仓库增加 priority 字段（整数 1~999，默认 100，数字越小越优先）。
多个仓库同时有排队任务时，优先级高（数字小）的仓库先派发；
相同优先级按任务提交时间（tasks.created_at）排序，早提交的先派发。

此前调度器 _dispatch 按 _queues 字典插入顺序遍历（仓库注册顺序），
无优先级概念。本测试先行编写，实现前应全部失败。
"""

import sqlite3
import threading

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.scheduler import TaskScheduler

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""


class FakeExecutor:
    """记录 run_task 调用的执行器桩（不真正执行任务）。

    可选 hold() 让下一次 run_task 阻塞在闸门上（模拟任务运行中），
    测试用 started/done 事件感知「已开始」与「已结束」两个时刻。
    """

    def __init__(self):
        self.run_ids: list[int] = []
        self._started: list[threading.Event] = []
        self._done: list[threading.Event] = []
        self._gates: list[threading.Event] = []

    def run_task(self, task_id: int):
        self.run_ids.append(task_id)
        self._started.pop(0).set()
        if self._gates:
            self._gates.pop(0).wait(timeout=10)
        self._done.pop(0).set()

    def request_stop(self, task_id: int):
        pass

    def expect(self) -> tuple[threading.Event, threading.Event]:
        """登记下一次 run_task 的 (started, done) 事件对。"""
        started = threading.Event()
        done = threading.Event()
        self._started.append(started)
        self._done.append(done)
        return started, done

    def hold(self) -> threading.Event:
        """下一次 run_task 阻塞在此闸门上，直到测试放行（模拟运行中）。"""
        gate = threading.Event()
        self._gates.append(gate)
        return gate


@pytest.fixture
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def executor():
    return FakeExecutor()


def _mk_repo(db, project_id: int, priority: int | None = None) -> int:
    """插入一条仓库记录（可选指定优先级），返回 repo_id。"""
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    db.upsert_repo(project_id, f"repo-{project_id}",
                   f"https://gitlab.example.com/group/repo-{project_id}.git", **kwargs)
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, project_id: int, issue_iid: int,
             created_at: str | None = None) -> int:
    """创建排队任务，可按需改写 created_at（任务提交时间）。"""
    task_id = db.create_task(repo_id, project_id, issue_iid,
                             f"任务 {project_id}#{issue_iid}", triggered_by="webhook")
    if created_at is not None:
        conn = sqlite3.connect(db.path)
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, task_id))
        conn.commit()
        conn.close()
    return task_id


def _scheduler(config, db, executor) -> TaskScheduler:
    return TaskScheduler(config, db, executor)


class TestDispatchPriority:
    """_dispatch 按优先级选仓库派发（不启动调度线程，手动触发）。"""

    def test_lower_priority_repo_dispatched_first(self, config, db, executor):
        """两个仓库各有排队任务：优先级数字小的仓库先派发。"""
        hi = _mk_repo(db, project_id=11, priority=1)
        lo = _mk_repo(db, project_id=22, priority=100)
        t_hi = _mk_task(db, hi, project_id=11, issue_iid=1)
        t_lo = _mk_task(db, lo, project_id=22, issue_iid=1)

        sched = _scheduler(config, db, executor)
        # 反序入队：低优先级先入队，派发顺序应与入队顺序无关
        sched.enqueue(t_lo)
        sched.enqueue(t_hi)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "应派发一个任务"
        assert executor.run_ids == [t_hi], "优先级高（数字小）的仓库任务应先派发"

    def test_same_priority_ordered_by_task_created_at(self, config, db, executor):
        """相同优先级：按任务提交时间排序，提交早的先派发。"""
        repo_a = _mk_repo(db, project_id=11, priority=50)
        repo_b = _mk_repo(db, project_id=22, priority=50)
        t_a = _mk_task(db, repo_a, project_id=11, issue_iid=1,
                       created_at="2026-08-14 09:00:00")
        t_b = _mk_task(db, repo_b, project_id=22, issue_iid=1,
                       created_at="2026-08-14 10:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_b)  # 提交晚的任务先入队
        sched.enqueue(t_a)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_a], "同优先级应按任务提交时间排序（早提交先派发）"

    def test_running_repo_skipped_even_with_higher_priority(self, config, db, executor):
        """高优先级仓库有 running 任务时跳过，派发空闲仓库（同仓库串行）。"""
        hi = _mk_repo(db, project_id=11, priority=1)
        lo = _mk_repo(db, project_id=22, priority=100)
        t_hi = _mk_task(db, hi, project_id=11, issue_iid=1)
        t_lo = _mk_task(db, lo, project_id=22, issue_iid=1)

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_hi)
        sched.enqueue(t_lo)
        # 高优先级仓库正在运行
        with sched._lock:
            sched._running[hi] = t_hi
            sched._queues[hi].popleft()
            sched._queues.pop(hi, None)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_lo], "running 仓库应跳过（即使优先级更高）"

    def test_same_repo_keeps_fifo_order(self, config, db, executor):
        """同一仓库内多个排队任务保持 FIFO（优先级不改变仓库内顺序）。"""
        repo = _mk_repo(db, project_id=11, priority=1)
        t1 = _mk_task(db, repo, project_id=11, issue_iid=1,
                      created_at="2026-08-14 09:00:00")
        t2 = _mk_task(db, repo, project_id=11, issue_iid=2,
                      created_at="2026-08-14 10:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t1)
        sched.enqueue(t2)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t1, t2], "同仓库任务应保持 FIFO 顺序"

    def test_null_priority_falls_back_to_default(self, config, db, executor):
        """priority 为 NULL（历史数据）时按默认 100 参与比较。"""
        repo_a = _mk_repo(db, project_id=11)  # 默认 100
        repo_b = _mk_repo(db, project_id=22, priority=50)
        conn = sqlite3.connect(db.path)
        conn.execute("UPDATE repos SET priority=NULL WHERE id=?", (repo_a,))
        conn.commit()
        conn.close()
        t_a = _mk_task(db, repo_a, project_id=11, issue_iid=1)
        t_b = _mk_task(db, repo_b, project_id=22, issue_iid=1)

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_a)
        sched.enqueue(t_b)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_b], "NULL 优先级应按默认 100 兜底（50 < 100）"

    def test_max_concurrent_repos_respected(self, config, db, executor, tmp_path):
        """并发上限满时不派发新任务（优先级排序不突破并发限制）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT.replace("worker: {}", "worker:\n  max_concurrent_repos: 1"),
            encoding="utf-8",
        )
        cfg = ConfigManager(str(config_path))
        hi = _mk_repo(db, project_id=11, priority=1)
        lo = _mk_repo(db, project_id=22, priority=100)
        t_hi = _mk_task(db, hi, project_id=11, issue_iid=1)
        t_lo = _mk_task(db, lo, project_id=22, issue_iid=1)

        sched = _scheduler(cfg, db, executor)
        sched.enqueue(t_hi)
        sched.enqueue(t_lo)

        # 闸门阻塞第一次 run_task，模拟高优先级任务运行中
        gate = executor.hold()
        started1, done1 = executor.expect()
        sched._dispatch()
        assert started1.wait(timeout=2), "应派发第一个任务"
        assert executor.run_ids == [t_hi], "首个派发应为高优先级仓库任务"

        # 并发上限 1、高优先级任务仍在运行：不派发第二个
        sched._dispatch()
        assert executor.run_ids == [t_hi], "并发上限满时不应派发新任务"

        # 放行第一个任务（worker 清理 _running）后恢复派发
        gate.set()
        assert done1.wait(timeout=2), "第一个任务应结束"
        started2, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_hi, t_lo], "并发释放后应继续派发剩余任务"
