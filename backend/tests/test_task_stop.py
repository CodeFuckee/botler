"""一键停止所有任务测试（issue #35）。

「任务页面增加一个按钮，一键停止所有任务」的后端实现分四层：
- database.stop_active_tasks：活跃任务（queued/running/retrying）统一落
  interrupted 终态 + 错误信息 + warn 日志；
- executor：进程注册表 + 停止请求集合，request_stop 终止 claude 进程组，
  被停止的任务不再执行也不再重试；
- scheduler.stop_all：清空待派发队列 + 状态落库 + 请求终止运行中任务；
- API POST /api/tasks/stop-all：返回 {stopped, count}。

被停止的任务为 interrupted 终态，不参与平台重启后的自动重新入队
（requeue_interrupted 只捞 running/retrying）。
"""


import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.scheduler import TaskScheduler
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {precheck_enabled: false}
claude: {}
templates: {}
repos: []
"""


@pytest.fixture
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def executor(config, db, tmp_path):
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    """插入一条仓库记录，返回 repo_id。"""
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int, status: str = "queued") -> int:
    """创建任务并按需更新状态，返回 task_id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, f"任务 {issue_iid}",
                             triggered_by="webhook")
    db.set_task_status(task_id, status)
    return task_id


# ---- database.stop_active_tasks ----

class TestStopActiveTasks:
    """db 层：活跃任务统一落 interrupted 终态。"""

    def test_stops_all_active_statuses(self, db):
        repo_id = _mk_repo(db)
        ids = {}
        for iid, status in [(1, "queued"), (2, "running"), (3, "retrying"), (4, "succeeded")]:
            ids[status] = _mk_task(db, repo_id, iid, status)

        stopped = db.stop_active_tasks()

        assert sorted(stopped) == sorted([ids["queued"], ids["running"], ids["retrying"]])
        for status in ["queued", "running", "retrying"]:
            row = db.get_task(ids[status])
            assert row["status"] == "interrupted", f"{status} 任务应落 interrupted"
            assert "停止" in (row["error_message"] or ""), "应写停止原因"
            assert row["finished_at"], "应写完成时间"
        # 终态任务不受影响
        assert db.get_task(ids["succeeded"])["status"] == "succeeded"

    def test_empty_when_no_active(self, db):
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, 1, "succeeded")
        _mk_task(db, repo_id, 2, "failed")
        assert db.stop_active_tasks() == []

    def test_writes_warn_log_per_stopped_task(self, db):
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, 1, "running")
        db.stop_active_tasks()
        logs = db.list_logs(tid)
        assert any(l["level"] == "warn" and "停止" in l["message"] for l in logs), \
            "每个被停止的任务应写 warn 日志"


# ---- scheduler.stop_all ----

class _FakeExecutor:
    """记录 request_stop 调用的假执行器。"""

    def __init__(self):
        self.requested: list[int] = []

    def request_stop(self, task_id: int) -> None:
        self.requested.append(task_id)


class TestSchedulerStopAll:
    """调度层：清空队列 + 落库 + 请求终止运行中任务。"""

    def test_clears_queue_requests_stop_marks_interrupted(self, config, db):
        fake = _FakeExecutor()
        scheduler = TaskScheduler(config, db, fake)
        repo_id = _mk_repo(db)
        queued_id = _mk_task(db, repo_id, 1, "queued")
        running_id = _mk_task(db, repo_id, 2, "queued")
        scheduler.enqueue(queued_id)
        scheduler.enqueue(running_id)
        scheduler._running[repo_id] = running_id  # 模拟已派发的运行中任务

        stopped = scheduler.stop_all()

        assert sorted(stopped) == sorted([queued_id, running_id])
        assert scheduler.stats()["queued"] == 0, "待派发队列应清空"
        assert fake.requested == [running_id], "运行中任务应请求终止"
        assert db.get_task(queued_id)["status"] == "interrupted"
        assert db.get_task(running_id)["status"] == "interrupted"

    def test_no_active_tasks(self, config, db):
        fake = _FakeExecutor()
        scheduler = TaskScheduler(config, db, fake)
        _mk_repo(db)
        assert scheduler.stop_all() == []
        assert fake.requested == []


# ---- executor 停止机制 ----

class _FakeStdoutStop:
    """模拟长时间无输出的 claude 进程：第 3 次读后触发外部一键停止。"""

    def __init__(self, executor, task_id):
        self._executor = executor
        self._task_id = task_id
        self._calls = 0

    def readline(self) -> str:
        self._calls += 1
        if self._calls >= 3:
            # 模拟外部（scheduler.stop_all）在进程运行中登记停止请求
            self._executor.request_stop(self._task_id)
        return ""


class _FakeProcAlive:
    """进程模拟：一直存活（poll 恒 None），直到被 SIGKILL（wait 返回 -9）。"""

    def __init__(self, executor, task_id):
        self.pid = 99999  # 不存在的 pid：os.getpgid 抛 ProcessLookupError 被吞掉
        self.stdout = _FakeStdoutStop(executor, task_id)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return -9


class TestExecutorStop:
    """执行层：停止请求终止 claude 进程组，任务不再重试。"""
    @pytest.mark.skipif(sys.platform == "win32", reason="os.getpgid 为 POSIX 专属，Windows 无进程组语义（issue #469）")

    def test_kill_process_group_sends_sigkill(self, executor, monkeypatch):
        """_kill_process_group：向进程组发 SIGKILL；进程已消失时静默。"""
        killed = []
        monkeypatch.setattr("botler.executor.os.getpgid", lambda pid: 12345)
        monkeypatch.setattr("botler.executor.os.killpg",
                            lambda pgid, sig: killed.append((pgid, sig)))

        class _FakeProc:
            pid = 99999

        executor._kill_process_group(_FakeProc())
        assert killed == [(12345, __import__("signal").SIGKILL)]

        # 进程已不存在（getpgid 抛 ProcessLookupError）→ 不抛异常
        def _raise(pgid):
            raise ProcessLookupError
        monkeypatch.setattr("botler.executor.os.getpgid", _raise)
        executor._kill_process_group(_FakeProc())  # 不应抛异常

    def test_run_once_terminates_when_stop_requested(self, executor, monkeypatch, tmp_path):
        """运行中收到停止请求 → 终止进程组 → 返回约定退出码 137。"""
        killed = []
        monkeypatch.setattr(executor, "_kill_process_group",
                            lambda proc: killed.append(proc))
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        fake_proc = _FakeProcAlive(executor, 1)
        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kwargs: fake_proc)

        exit_code, output = executor._run_once(
            1, {"name": "demo", "prompt_template": None}, {"project_id": 42, "iid": 7})

        assert exit_code == 125, f"被停止应返回 STOP_EXIT_CODE 125，实际 {exit_code}"
        assert killed, "应终止 claude 进程组"
        assert 1 not in executor._procs, "进程注册表应在退出后清理"

    def test_run_once_stops_immediately_when_requested_before_start(self, executor, monkeypatch, tmp_path):
        """停止请求先于进程创建到达 → Popen 后立即终止，不进入执行。"""
        killed = []
        monkeypatch.setattr(executor, "_kill_process_group",
                            lambda proc: killed.append(proc))
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        class _FakeProcStatic:
            pid = 99999
            stdout = _FakeStdoutStop(executor, 1)

            def poll(self):
                return None

            def wait(self, timeout=None):
                return -9

        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kwargs: _FakeProcStatic())

        executor.request_stop(1)  # 先登记停止请求
        exit_code, _ = executor._run_once(
            1, {"name": "demo", "prompt_template": None}, {"project_id": 42, "iid": 7})

        assert exit_code == 125
        assert killed, "进程创建后应立即被杀"
        assert 1 not in executor._procs

    def test_run_task_stops_before_execution_when_requested(self, executor, monkeypatch):
        """领取后（claim 前）已收到停止请求 → 不执行 _run_once，直接落 interrupted。"""
        repo_id = _mk_repo(executor.db)
        task_id = _mk_task(executor.db, repo_id, 1, "queued")

        run_once_calls = []
        monkeypatch.setattr(executor, "_run_once",
                            lambda *a, **k: run_once_calls.append(a) or (0, "ok"))
        get_issue_calls = []
        monkeypatch.setattr(executor.gitlab, "get_issue",
                            lambda *a: get_issue_calls.append(a) or {"state": "opened"})

        executor.request_stop(task_id)  # 模拟 stop_all 先行登记
        executor.run_task(task_id)

        assert run_once_calls == [], "收到停止请求后不应执行 claude"
        row = executor.db.get_task(task_id)
        assert row["status"] == "interrupted"
        assert "停止" in (row["error_message"] or "")
        logs = executor.db.list_logs(task_id)
        assert any(l["level"] == "warn" and "停止" in l["message"] for l in logs)


# ---- API POST /api/tasks/stop-all ----

@pytest.fixture
def api_app(tmp_path, config, db, executor):
    """最小测试 app：ctx 带真实 scheduler/executor（停机制全链路）。"""
    scheduler = TaskScheduler(config, db, executor)
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, renderer=None,
                          executor=executor, scheduler=scheduler)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


class TestStopAllApi:
    """POST /api/tasks/stop-all 数据契约。"""

    def test_stop_all(self, api_app):
        app, db, _ = api_app
        repo_id = _mk_repo(db)
        tid1 = _mk_task(db, repo_id, 1, "queued")
        tid2 = _mk_task(db, repo_id, 2, "running")

        resp = TestClient(app).post("/api/tasks/stop-all")

        assert resp.status_code == 200
        body = resp.json()
        assert sorted(body["stopped"]) == sorted([tid1, tid2])
        assert body["count"] == 2
        assert db.get_task(tid1)["status"] == "interrupted"
        assert db.get_task(tid2)["status"] == "interrupted"

    def test_stop_all_no_active(self, api_app):
        app, _, _ = api_app
        resp = TestClient(app).post("/api/tasks/stop-all")
        assert resp.status_code == 200
        assert resp.json() == {"stopped": [], "count": 0}
