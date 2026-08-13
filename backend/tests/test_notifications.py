"""网页端通知功能测试（issue #21）。

事件类型与产生点：
- task_succeeded: executor 任务成功收尾（issue 已关闭）→ 「issue 完成」通知
- task_failed: executor 任务失败收尾（需人工介入）→ 「任务需要交互」通知
- queue_empty: 对账扫描后某仓库无任何待处理 open issue
- queue_no_work: 对账扫描后有 open issue 但全部已有活跃任务

覆盖：事件落库与游标查询、executor 收尾事件、对账队列事件与节流、
notifications API 增量拉取。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient, GitLabError
from botler.notifier import Notifier
from botler.reconciler import Reconciler
from botler.templates import TemplateRenderer

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

BOT_ID = 99


# ---------- 共享 fixture ----------

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def notifier(db):
    return Notifier(db)


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 7, title: str = "测试 issue") -> int:
    return db.create_task(repo_id, 42, issue_iid, title)


class TestNotificationDb:
    """事件表：落库、游标查询、task 幂等。"""

    def test_add_and_list_back(self, db):
        n = Notifier(db)
        n.record("task_succeeded", "issue 完成", "demo #7 测试 issue",
                 repo_name="demo", task_id=1, data={"issue_iid": 7})
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "task_succeeded"
        assert e["title"] == "issue 完成"
        assert e["body"] == "demo #7 测试 issue"
        assert e["repo_name"] == "demo"
        assert e["task_id"] == 1

    def test_duplicate_task_ignored(self, db):
        """同一 task_id 重复记录不产生第二条（任务事件天然幂等）。"""
        n = Notifier(db)
        first = n.record("task_succeeded", "t", "b", repo_name="demo", task_id=9)
        second = n.record("task_succeeded", "t", "b", repo_name="demo", task_id=9)
        assert first is not None and second is None
        assert len(db.list_notifications(after_id=0)) == 1

    def test_list_after_cursor_incremental(self, db):
        """after 游标增量拉取：只返回 id 更大的事件。"""
        n = Notifier(db)
        for i in range(3):
            n.record("task_succeeded", f"t{i}", "b", task_id=i)
        events = db.list_notifications(after_id=1)
        assert [e["id"] for e in events] == [2, 3]

    def test_list_after_huge_id_empty(self, db):
        n = Notifier(db)
        n.record("task_succeeded", "t", "b", task_id=1)
        assert db.list_notifications(after_id=999) == []

    def test_last_notification_for_repo_type(self, db):
        """节流查询：返回同仓库同类型的最近一条（按 id 降序）。

        直接走 db.add_notification 绕过节流（节流场景由
        test_throttled_* 覆盖），验证 last_notification 取的是最新一条。
        """
        db.add_notification("queue_empty", "t1", "b1", repo_name="demo")
        db.add_notification("queue_empty", "t2", "b2", repo_name="demo")
        db.add_notification("queue_no_work", "t3", "b3", repo_name="demo")
        last = db.last_notification("demo", "queue_empty")
        assert last is not None and last["title"] == "t2"

    def test_last_notification_missing(self, db):
        assert db.last_notification("demo", "queue_empty") is None

    def test_throttled_within_window_skipped(self, db, notifier):
        """同一仓库同类型在节流窗口内重复记录被跳过。"""
        first = notifier.record_throttled("queue_empty", "t", "b", repo_name="demo")
        second = notifier.record_throttled("queue_empty", "t", "b", repo_name="demo")
        assert first is not None and second is None
        assert len(db.list_notifications(after_id=0)) == 1

    def test_throttled_different_types_not_skipped(self, db, notifier):
        """不同类型不互相节流（同一仓库空队列与无新任务可同时提醒）。"""
        a = notifier.record_throttled("queue_empty", "t", "b", repo_name="demo")
        b = notifier.record_throttled("queue_no_work", "t", "b", repo_name="demo")
        assert a is not None and b is not None

    def test_throttled_different_repos_not_skipped(self, db, notifier):
        a = notifier.record_throttled("queue_empty", "t", "b", repo_name="demo")
        b = notifier.record_throttled("queue_empty", "t", "b", repo_name="other")
        assert a is not None and b is not None


# ---------- executor 收尾事件 ----------

@pytest.fixture
def executor(config, db, tmp_path):
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


class TestExecutorNotificationEvents:
    """任务收尾产生通知事件（issue #21 的时机 1/2）。"""

    def test_finish_succeeded_records_event(self, executor, db, tmp_path):
        """任务成功 → task_succeeded 事件，正文含仓库/issue 信息。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=7, title="修复登录 bug")
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.find_commit_for_issue = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_succeeded(task_id, "ok")
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "task_succeeded"
        assert e["task_id"] == task_id
        assert "修复登录 bug" in e["body"]

    def test_finish_failed_records_event(self, executor, db, tmp_path):
        """任务失败 → task_failed 事件，正文含失败原因（需人工介入）。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=8, title="接口报错")
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_failed(task_id, "Claude Code 报告无法解决该 issue")
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "task_failed"
        assert "无法解决" in e["body"]

    def test_finish_twice_only_one_event(self, executor, db, tmp_path):
        """同一任务重复收尾只产生一条事件（幂等，靠 task_id 唯一索引）。"""
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）
        executor._finish_failed(task_id, "重试耗尽")
        executor._finish_failed(task_id, "重试耗尽")
        assert len(db.list_notifications(after_id=0)) == 1

    def test_run_task_success_emits_event(self, executor, db, monkeypatch, tmp_path):
        """端到端：run_task 全流程成功 → 产生 task_succeeded 事件。

        mock 方式对齐 test_executor.py 的端到端测试（git 命令、
        subprocess、gitlab API 全部桩掉，避免真实网络与磁盘操作）。
        """
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=7, title="端到端任务")
        (tmp_path / "workspace" / "demo" / ".git").mkdir(parents=True)
        monkeypatch.setattr(executor, "_git", lambda *a, **k: None)
        monkeypatch.setattr(executor, "_askpass_script", lambda n: tmp_path / "askpass.sh")
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        class _FakeStdout:
            """一次输出 + EOF；EOF 后 poll() 视为进程已退出（同 test_executor.py）。"""

            def __init__(self, text):
                self._lines = [text] if text else []

            def readline(self):
                return self._lines.pop(0) if self._lines else ""

        class _FakeProc:
            def __init__(self, output, exit_code=0):
                self.stdout = _FakeStdout(output)
                self._exit = exit_code

            def poll(self):
                return self._exit if not self.stdout._lines else None

            def wait(self, timeout=None):
                return self._exit

        monkeypatch.setattr("botler.executor.subprocess.Popen",
                            lambda cmd, **kw: _FakeProc(
                                json.dumps({"result": "ok", "session_id": "sid-n"})))
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": "closed", "title": "标题",
                                        "description": "", "web_url": "",
                                        "project_id": pid, "iid": iid},
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda *a, **k: None,
        )
        executor.run_task(task_id)
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "task_succeeded"
        assert events[0]["task_id"] == task_id


# ---------- 对账队列事件 ----------

class StubGitLab:
    """对账用的 GitLab 桩：bot 身份固定，open issues 列表可配置、可故障注入。"""

    def __init__(self, issues_by_project: dict[int, list[dict]] | None = None):
        self.issues_by_project = issues_by_project or {}
        self.fail_projects: set[int] = set()

    def get_bot_id(self):
        return BOT_ID

    def list_open_issues(self, project_id, assignee_id=None):
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab API 故障")
        return self.issues_by_project.get(project_id, [])

    def last_note_author_id(self, project_id, iid):
        return None  # 默认无发言


def make_issue(iid: int, title: str = "测试 issue") -> dict:
    return {"iid": iid, "title": title}


@pytest.fixture
def reconciler(config, db):
    stub = StubGitLab()
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    return Reconciler(config, db, stub, scheduler), stub


class TestReconcilerNotificationEvents:
    """对账扫描产生队列类事件（issue #21 的时机 3/4）。"""

    def test_empty_queue_emits_queue_empty(self, reconciler, db):
        """仓库无任何 open issue → queue_empty 事件。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        stub.issues_by_project[42] = []
        rec.reconcile_once(repo_id=repo_id)
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "queue_empty"
        assert events[0]["repo_name"] == "demo"

    def test_all_active_emits_queue_no_work(self, reconciler, db):
        """有 open issue 但全部已有活跃任务 → queue_no_work 事件。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        stub.issues_by_project[42] = [make_issue(1), make_issue(2)]
        # 两个 issue 都已有活跃任务（对账时不入队）
        task1 = _mk_task(db, repo_id, issue_iid=1)
        task2 = _mk_task(db, repo_id, issue_iid=2)
        db.set_task_status(task1, "running")
        db.set_task_status(task2, "queued")
        rec.reconcile_once(repo_id=repo_id)
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == "queue_no_work"
        assert "2" in events[0]["body"]  # 正文含在途 issue 数

    def test_with_work_emits_no_event(self, reconciler, db):
        """有可入队的 issue → 正常入队，不产生事件。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        stub.issues_by_project[42] = [make_issue(1)]
        rec.reconcile_once(repo_id=repo_id)
        assert db.list_notifications(after_id=0) == []
        assert db.count_tasks() == 1  # 任务正常入队

    def test_throttled_repeat_scan(self, reconciler, db):
        """同仓库同类事件节流：短时间内重复扫描不重复记录。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        stub.issues_by_project[42] = []
        rec.reconcile_once(repo_id=repo_id)
        rec.reconcile_once(repo_id=repo_id)
        assert len(db.list_notifications(after_id=0)) == 1

    def test_disabled_repo_no_event(self, reconciler, db):
        """停用仓库跳过扫描，不产生事件。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        db.update_repo(repo_id, enabled=False)
        stub.issues_by_project[42] = []
        rec.reconcile_once(repo_id=repo_id)
        assert db.list_notifications(after_id=0) == []

    def test_gitlab_error_no_event(self, reconciler, db):
        """GitLab 故障 → 错误记入 errors，不产生队列事件。"""
        rec, stub = reconciler
        repo_id = _mk_repo(db)
        stub.fail_projects.add(42)
        result = rec.reconcile_once(repo_id=repo_id)
        assert db.list_notifications(after_id=0) == []
        assert result["errors"]

    def test_all_repos_scan_emits_per_repo(self, reconciler, db):
        """全局对账：多个空队列仓库各产生一条事件。"""
        rec, stub = reconciler
        _mk_repo(db, project_id=42, name="demo")
        _mk_repo(db, project_id=43, name="other")
        stub.issues_by_project[42] = []
        stub.issues_by_project[43] = []
        rec.reconcile_once()
        events = db.list_notifications(after_id=0)
        assert len(events) == 2
        assert {e["repo_name"] for e in events} == {"demo", "other"}


# ---------- notifications API ----------

@pytest.fixture
def api_client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    reconciler = Reconciler(config, db, stub, scheduler)
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          reconciler=reconciler, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), db


class TestNotificationSettingsApi:
    """设置页「网页通知」卡片：默认值、写回 config.yaml、布尔校验。"""

    def test_get_settings_includes_notifications(self, api_client):
        """未配置时通知段返回默认值（全开）。"""
        tc, _ = api_client
        data = tc.get("/api/settings").json()["notifications"]
        assert data == {
            "enabled": True,
            "task_needs_interaction": True,
            "issue_completed": True,
            "queue_empty": True,
            "queue_no_work": True,
        }

    def test_update_notifications_writes_back(self, api_client, tmp_path):
        """PUT notifications 写回 config.yaml（唯一事实来源）并可读回。"""
        tc, _ = api_client
        resp = tc.put("/api/settings", json={
            "notifications": {"enabled": False, "queue_empty": False},
        })
        assert resp.status_code == 200
        n = resp.json()["notifications"]
        assert n["enabled"] is False and n["queue_empty"] is False
        # 其余开关保持默认
        assert n["task_needs_interaction"] is True
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "notifications:" in config_text and "queue_empty: false" in config_text

    def test_update_notifications_rejects_non_bool(self, api_client):
        """非布尔值 400（设置页 checkbox 只能传布尔）。"""
        tc, _ = api_client
        resp = tc.put("/api/settings", json={"notifications": {"enabled": "yes"}})
        assert resp.status_code == 400


class TestNotificationsApi:
    def test_get_events_empty(self, api_client):
        tc, _ = api_client
        resp = tc.get("/api/notifications/events")
        assert resp.status_code == 200
        assert resp.json() == {"events": [], "latest_id": 0}

    def test_get_events_incremental(self, api_client):
        """after 游标：只返回新事件，latest_id 为最新事件 id。"""
        tc, db = api_client
        n = Notifier(db)
        n.record("task_succeeded", "t1", "b1", task_id=1)
        n.record("task_failed", "t2", "b2", task_id=2)
        n.record("queue_empty", "t3", "b3", repo_name="demo")
        resp = tc.get("/api/notifications/events?after=1")
        data = resp.json()
        assert [e["id"] for e in data["events"]] == [2, 3]
        assert data["latest_id"] == 3

    def test_get_events_after_huge(self, api_client):
        tc, db = api_client
        Notifier(db).record("task_succeeded", "t", "b", task_id=1)
        resp = tc.get("/api/notifications/events?after=999")
        assert resp.json() == {"events": [], "latest_id": 999}

    def test_get_events_limit(self, api_client):
        """limit 限制返回条数，latest_id 为本次返回的最后一条（游标不丢事件）。

        前端把 latest_id 作为下次 after 游标：本次返回 [1,2] → 游标 2，
        下次从 3 继续拉，被 limit 截断的事件不会丢失。
        """
        tc, db = api_client
        n = Notifier(db)
        for i in range(5):
            n.record("task_succeeded", f"t{i}", "b", task_id=i)
        resp = tc.get("/api/notifications/events?limit=2")
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["latest_id"] == 2
