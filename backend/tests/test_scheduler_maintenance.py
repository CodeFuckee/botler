"""调度器维护模式测试（issue #241）。

需求：维护模式为人工总开关——开启后调度器停止开始新任务（新事件只入队
不派发），运行中任务继续执行完，关闭后自动恢复派发。

测试方式：不启动调度线程，手动触发 _dispatch；配置 worker 段写入
maintenance_mode（与设置页保存同路径，ConfigManager 内存态读取）。

测试先行：实现前应全部失败（_dispatch 尚无维护模式检查）。
"""

from __future__ import annotations

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


class TestDispatchMaintenanceMode:
    def test_maintenance_on_not_dispatched(self, tmp_path, db, executor):
        """维护模式开启：任务入队但调度器不派发（等待恢复后执行）。"""
        cfg = _worker_config(tmp_path, "  maintenance_mode: true\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched.enqueue(t)

        sched._dispatch()
        assert executor.run_ids == [], "维护模式开启时不得派发新任务"
        assert sched.stats()["queued"] == 1, "未开始任务应保留在队列"

    def test_maintenance_off_dispatched(self, tmp_path, db, executor):
        """维护模式关闭（显式 false）：正常派发。"""
        cfg = _worker_config(tmp_path, "  maintenance_mode: false\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "维护模式关闭应正常派发"
        assert executor.run_ids == [t]

    def test_queued_task_starts_after_off(self, tmp_path, db, executor):
        """维护模式开启时入队的任务保留，关闭后下一次派发自动开始。"""
        cfg = _worker_config(tmp_path, "  maintenance_mode: true\n")
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched.enqueue(t)

        # 开启时多次派发均不开始
        sched._dispatch()
        sched._dispatch()
        assert executor.run_ids == []
        assert sched.stats()["queued"] == 1

        # 关闭维护模式（config 内存态切换，等价设置页保存）：任务自动开始
        cfg.update_section("worker", {"maintenance_mode": False})
        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "关闭维护模式后应自动开始执行排队任务"
        assert executor.run_ids == [t]

    def test_running_task_not_interrupted(self, tmp_path, db, executor):
        """维护模式开启时运行中的任务不受影响，继续执行完成。"""
        cfg = _worker_config(tmp_path, "  maintenance_mode: false\n")
        repo = _mk_repo(db, project_id=11)
        t1 = _mk_task(db, repo, project_id=11, issue_iid=1)
        t2 = _mk_task(db, repo, project_id=11, issue_iid=2)

        sched = TaskScheduler(cfg, db, executor)
        # 第一个任务在维护模式关闭时启动（hold 闸门模拟运行中）
        sched.enqueue(t1)
        gate = threading.Event()
        executor._gates.append(gate)
        started1, done1 = executor.expect()
        sched._dispatch()
        assert started1.wait(timeout=2), "第一个任务应已开始运行"

        # 开启维护模式：调度器不得中断运行中任务，也不得派发新任务
        cfg.update_section("worker", {"maintenance_mode": True})
        sched.enqueue(t2)
        sched._dispatch()
        assert executor.run_ids == [t1], "维护模式开启时不得派发新任务、不得影响运行中任务"

        # 运行中任务继续执行到完成
        gate.set()
        assert done1.wait(timeout=2), "运行中任务应继续执行完成"

    def test_no_config_unchanged(self, tmp_path, db, executor):
        """未配置维护模式：行为与现状一致，正常派发。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        cfg = ConfigManager(str(config_path))
        repo = _mk_repo(db, project_id=11)
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched.enqueue(t)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "未配置维护模式时任何时间都应正常派发"
        assert executor.run_ids == [t]

    def test_maintenance_overrides_pause_exemption(self, tmp_path, db, executor):
        """维护模式优先级高于暂停窗口豁免阈值：开启时高优先级仓库也不派发。"""
        cfg = _worker_config(
            tmp_path,
            "  maintenance_mode: true\n"
            "  pause_windows: ['09:00-12:00']\n"
            "  pause_priority_threshold: 50\n")
        db.upsert_repo(11, "repo-11",
                       "https://gitlab.example.com/group/repo-11.git",
                       priority=10)  # 高优先级（pause 豁免场景会派发）
        repo = db.get_repo_by_project_id(11)["id"]
        t = _mk_task(db, repo, project_id=11, issue_iid=1)

        sched = TaskScheduler(cfg, db, executor)
        sched._now = lambda: datetime.fromisoformat(
            "2026-08-18T10:00:00").replace(tzinfo=TZ)  # 暂停窗口内
        sched.enqueue(t)

        sched._dispatch()
        assert executor.run_ids == [], "维护模式应无条件拦截派发（高于暂停窗口豁免）"
        assert sched.stats()["queued"] == 1
