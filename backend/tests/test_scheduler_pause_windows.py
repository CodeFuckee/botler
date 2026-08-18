"""调度器定时暂停窗口测试（issue #169）。

需求：配置时间窗口（如 09:00-12:00、14:00-18:00）后，窗口内调度器
停止开始新任务；已经开始执行的任务继续执行；未开始执行的任务保留
在队列中，等到窗口结束后自动开始执行。

测试方式：不启动调度线程，手动触发 _dispatch；用实例属性覆盖
sched._now 固定当前时间（调度器以 _now() 作为时间来源）。

测试先行：实现前应全部失败（_dispatch 尚无暂停窗口检查）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

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

TZ = ZoneInfo("Asia/Shanghai")


def _at(iso: str) -> datetime:
    """构造固定时刻（Asia/Shanghai，带时区）。"""
    return datetime.fromisoformat(iso).replace(tzinfo=TZ)


class FakeExecutor:
    """记录 run_task 调用的执行器桩（可 hold 模拟运行中任务）。"""

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


def _worker_config(tmp_path, worker_text: str) -> ConfigManager:
    """生成带自定义 worker 段的配置。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.replace("worker: {}", "worker:\n" + worker_text),
        encoding="utf-8",
    )
    return ConfigManager(str(config_path))


def _mk_repo(db, project_id: int) -> int:
    db.upsert_repo(project_id, f"repo-{project_id}",
                   f"https://gitlab.example.com/group/repo-{project_id}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, project_id: int, issue_iid: int) -> int:
    return db.create_task(
        repo_id, project_id, issue_iid, f"任务 {project_id}#{issue_iid}",
        triggered_by="webhook",
        issue_labels=[], issue_created_at="", issue_updated_at="")


class TestDispatchPauseWindow:
    def test_in_window_not_dispatched(self, tmp_path, db, executor):
        """窗口内：任务入队但调度器不派发（等待窗口结束后再执行）。"""
        cfg = _worker_config(tmp_path, "  pause_windows: ['09:00-12:00']\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")  # 窗口内
        sched.enqueue(t)

        sched._dispatch()
        assert executor.run_ids == [], "窗口内不得派发新任务"
        assert sched.stats()["queued"] == 1, "未开始任务应保留在队列"

    def test_outside_window_dispatched(self, tmp_path, db, executor):
        """窗口外：正常派发（12:00 起恢复）。"""
        cfg = _worker_config(tmp_path, "  pause_windows: ['09:00-12:00']\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T13:00:00")
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "窗口外应派发任务"
        assert executor.run_ids == [t]

    def test_queued_task_starts_after_window(self, tmp_path, db, executor):
        """窗口内入队的任务保留，窗口结束后下一次派发自动开始执行。"""
        cfg = _worker_config(tmp_path, "  pause_windows: ['09:00-12:00']\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched.enqueue(t)

        # 窗口内多次派发均不开始
        sched._now = lambda: _at("2026-08-18T09:30:00")
        sched._dispatch()
        sched._dispatch()
        assert executor.run_ids == []
        assert sched.stats()["queued"] == 1

        # 时间推进到窗口外：任务自动开始
        sched._now = lambda: _at("2026-08-18T12:00:00")
        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "窗口结束后应自动开始执行排队任务"
        assert executor.run_ids == [t]

    def test_running_task_not_interrupted_by_window(self, tmp_path, db, executor):
        """窗口开启时运行中的任务不受影响，继续执行完成。"""
        cfg = _worker_config(tmp_path, "  pause_windows: ['09:00-12:00']\n")
        repo = _mk_repo(db, project_id=11)
        t1 = _mk_task(db, repo, project_id=11, issue_iid=1)
        t2 = _mk_task(db, repo, project_id=11, issue_iid=2)

        sched = TaskScheduler(cfg, db, executor)
        # 第一个任务在窗口外启动（hold 闸门模拟运行中）
        sched._now = lambda: _at("2026-08-18T08:00:00")
        sched.enqueue(t1)
        gate = threading.Event()
        executor._gates.append(gate)
        started1, done1 = executor.expect()
        sched._dispatch()
        assert started1.wait(timeout=2), "第一个任务应已开始运行"

        # 进入窗口：调度器不得中断运行中任务，也不得派发新任务
        sched._now = lambda: _at("2026-08-18T09:00:00")
        sched.enqueue(t2)
        sched._dispatch()
        assert executor.run_ids == [t1], "窗口内不得派发新任务、不得影响运行中任务"

        # 运行中任务继续执行到完成
        gate.set()
        assert done1.wait(timeout=2), "运行中任务应继续执行完成"

    def test_no_config_unchanged(self, config, db, executor):
        """未配置暂停窗口：行为与现状一致，正常派发。"""
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(config, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "未配置窗口时任何时间都应正常派发"
        assert executor.run_ids == [t]

    def test_weekday_miss_dispatches(self, tmp_path, db, executor):
        """星期不在生效范围：窗口不生效，正常派发。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_weekdays: [0, 1, 2, 3, 4]\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-22T10:00:00")  # 周六 10:00 窗口内时间
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "非生效星期不应暂停"
        assert executor.run_ids == [t]


class TestPausePriorityExemption:
    """暂停窗口豁免优先级阈值（issue #299）。

    需求：任务调度处增加设置，当优先级高于多少的时候，可以不受定时暂停
    窗口的影响持续开发——仓库调度优先级（repos[].priority，数字越小越
    优先）不差于配置阈值（priority <= pause_priority_threshold）的仓库，
    在暂停窗口内仍可开始新任务；未配置（0）时所有仓库都受暂停窗口约束
    （issue #169 行为不变）。
    """

    def _mk_repo(self, db, project_id: int, priority: int = 100) -> int:
        db.upsert_repo(project_id, f"repo-{project_id}",
                       f"https://gitlab.example.com/group/repo-{project_id}.git",
                       priority=priority)
        return db.get_repo_by_project_id(project_id)["id"]

    def test_high_priority_repo_dispatched_in_window(self, tmp_path, db, executor):
        """窗口内：优先级不差于阈值的仓库任务照常派发（豁免生效）。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 50\n")
        repo = self._mk_repo(db, project_id=11, priority=10)  # 高优先级

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")  # 窗口内
        t = _mk_task(db, repo, project_id=11, issue_iid=1)
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "窗口内高优先级仓库应照常派发"
        assert executor.run_ids == [t]

    def test_low_priority_repo_held_in_window(self, tmp_path, db, executor):
        """窗口内：优先级差于阈值的仓库任务保留，窗口结束后才派发。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 50\n")
        repo = self._mk_repo(db, project_id=11, priority=100)  # 低优先级

        sched = TaskScheduler(cfg, db, executor)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)
        sched.enqueue(t)

        sched._now = lambda: _at("2026-08-18T10:00:00")
        sched._dispatch()
        assert executor.run_ids == [], "窗口内低优先级仓库不得派发"
        assert sched.stats()["queued"] == 1, "任务应保留在队列"

        # 窗口结束后：低优先级任务自动开始
        sched._now = lambda: _at("2026-08-18T12:00:00")
        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "窗口结束后应恢复派发低优先级任务"
        assert executor.run_ids == [t]

    def test_threshold_boundary_inclusive(self, tmp_path, db, executor):
        """边界：priority == 阈值时豁免（不差于阈值 = 小于等于）。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 50\n")
        repo = self._mk_repo(db, project_id=11, priority=50)  # 恰好等于阈值

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")
        t = _mk_task(db, repo, project_id=11, issue_iid=1)
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "priority == 阈值应豁免（小于等于）"
        assert executor.run_ids == [t]

    def test_threshold_zero_keeps_pause_semantics(self, tmp_path, db, executor):
        """阈值 0 = 关闭豁免：窗口内所有仓库（含高优先级）都不派发。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 0\n")
        repo = self._mk_repo(db, project_id=11, priority=1)  # 最高优先级

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")
        t = _mk_task(db, repo, project_id=11, issue_iid=1)
        sched.enqueue(t)

        sched._dispatch()
        assert executor.run_ids == [], "阈值 0 时窗口内不得派发任何任务"
        assert sched.stats()["queued"] == 1

    def test_missing_priority_falls_back_to_default(self, tmp_path, db, executor):
        """优先级缺失（数据库缺省 100）：按 DEFAULT_PRIORITY 参与阈值判断。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 100\n")
        repo = _mk_repo(db, project_id=11)  # 未显式指定 priority（缺省 100）

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")
        t = _mk_task(db, repo, project_id=11, issue_iid=1)
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "缺省优先级 100 == 阈值 100 应豁免"
        assert executor.run_ids == [t]

    def test_high_priority_dispatched_low_held(self, tmp_path, db, executor):
        """窗口内混合队列：高优先级仓库派发，低优先级仓库保留在队列。"""
        cfg = _worker_config(
            tmp_path,
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 50\n")
        hi = self._mk_repo(db, project_id=11, priority=10)
        lo = self._mk_repo(db, project_id=22, priority=100)
        t_hi = _mk_task(db, hi, project_id=11, issue_iid=1)
        t_lo = _mk_task(db, lo, project_id=22, issue_iid=2)

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: _at("2026-08-18T10:00:00")
        sched.enqueue(t_hi)
        sched.enqueue(t_lo)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "窗口内高优先级仓库应派发"
        assert executor.run_ids == [t_hi], "窗口内只应派发高优先级仓库任务"
        assert sched.stats()["queued"] == 1, "低优先级任务应保留在队列"
