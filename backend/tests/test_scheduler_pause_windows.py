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
