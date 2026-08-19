"""聚合告警检测测试（issue #229）。

功能：对账循环内检测平台异常——近 1 小时任务失败率 > 阈值、队列活跃任务
积压超过 N 条且窗口内无任务收尾（无进度）、GitLab token 失效（401/403）、
数据目录磁盘剩余空间 < 阈值——经现有 notifier（网页通知 in_app 事件 +
webhook 推送）主动通知，阈值设置页可配置（config.yaml alerts 段）。

覆盖：四类告警触发/不触发边界、同类节流去重、总开关关闭、设置 API 校验
与写回、webhook 告警推送、Reconciler 对账集成调用。
"""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.alerts import AlertChecker
from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import (
    Database, STATUS_FAILED, STATUS_QUEUED, STATUS_SUCCEEDED,
)
from botler.gitlab_client import GitLabError
from botler.notifier import (
    ALERT_DISK_LOW,
    ALERT_FAILURE_RATE,
    ALERT_QUEUE_BACKLOG,
    ALERT_TOKEN_INVALID,
    Notifier,
)
from botler.reconciler import Reconciler
from botler.webhook_push import WebhookPusher

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


def _now_ts(ago: int = 0) -> str:
    """now - ago 秒的 UTC 时间字符串（与库内 finished_at 格式一致）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - ago))


class StubGitLab:
    """token 探测桩：test_connection 可配置 正常/401/403/网络故障。"""

    def __init__(self, mode: str = "ok"):
        self.mode = mode

    def test_connection(self):
        if self.mode == "ok":
            return {"id": 99, "username": "bot"}
        if self.mode == "unauthorized":
            raise GitLabError("token 无效或已过期（401）", 401)
        if self.mode == "forbidden":
            raise GitLabError("权限不足（403）", 403)
        raise GitLabError("GitLab 请求失败: 连接超时")  # 传输层故障无 status_code


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
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, status: str = STATUS_SUCCEEDED,
             iid: int = 1, finished_ago: int = 0) -> int | None:
    """创建一条任务；终态时写入 finished_at = now - finished_ago 秒。"""
    task_id = db.create_task(repo_id, 42, iid, f"测试 issue {iid}")
    if task_id is None:
        return None
    if status in (STATUS_FAILED, STATUS_SUCCEEDED):
        db.set_task_status(task_id, status, finished_at=_now_ts(finished_ago))
    else:
        db.set_task_status(task_id, status)
    return task_id


class TestFailureRateAlert:
    """近 1 小时失败率 > 阈值（默认 50%）→ 告警。"""

    def test_triggers_when_rate_above_threshold(self, config, db, notifier):
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier)
        triggered = checker.check(token_status="ok")
        assert ALERT_FAILURE_RATE in triggered
        events = db.list_notifications(after_id=0)
        assert len(events) == 1
        assert events[0]["type"] == ALERT_FAILURE_RATE
        assert "失败率" in events[0]["title"]
        data = json.loads(events[0]["data"] or "{}")
        assert data["failed"] == 2 and data["total"] == 3

    def test_not_triggered_below_threshold(self, config, db, notifier):
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []
        assert len(db.list_notifications(after_id=0)) == 0

    def test_not_triggered_at_exact_threshold(self, config, db, notifier):
        """失败率恰好等于阈值（50%）不触发（严格大于）。"""
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=2)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_not_triggered_without_terminal_tasks(self, config, db, notifier):
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_QUEUED, iid=1)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_ignores_tasks_outside_window(self, config, db, notifier):
        """窗口外（> 1 小时前）的失败不计入失败率。"""
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1, finished_ago=7200)
        _mk_task(db, repo, STATUS_FAILED, iid=2, finished_ago=7200)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_threshold_configurable(self, tmp_path):
        """阈值可配置：80% 阈值下 2/3 失败（66.7%）不触发。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + "alerts:\n  failure_rate_threshold: 80\n",
                               encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, Notifier(db))
        assert checker.check(token_status="ok") == []


class TestQueueBacklogAlert:
    """队列活跃任务 > 阈值且窗口内无任务收尾 → 告警。"""

    def test_triggers_when_active_over_threshold_no_progress(self, config, db, notifier):
        repo = _mk_repo(db)
        for i in range(6):  # 6 条活跃 > 默认阈值 5
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        checker = AlertChecker(config, db, notifier)
        triggered = checker.check(token_status="ok")
        assert ALERT_QUEUE_BACKLOG in triggered

    def test_not_triggered_when_recent_terminal(self, config, db, notifier):
        """窗口内有任务收尾 = 有进度，不触发。"""
        repo = _mk_repo(db)
        for i in range(6):
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=99)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_not_triggered_below_threshold(self, config, db, notifier):
        repo = _mk_repo(db)
        for i in range(3):
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_triggers_when_terminal_is_stale(self, config, db, notifier):
        """窗口外（> 30 分钟）的收尾不算进度，仍触发。"""
        repo = _mk_repo(db)
        for i in range(6):
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=99, finished_ago=3600)
        checker = AlertChecker(config, db, notifier)
        assert ALERT_QUEUE_BACKLOG in checker.check(token_status="ok")

    def test_threshold_configurable(self, tmp_path):
        """队列阈值可配置：阈值 10 时 6 条活跃不触发。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + "alerts:\n  queue_backlog_threshold: 10\n",
                               encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        repo = _mk_repo(db)
        for i in range(6):
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        checker = AlertChecker(config, db, Notifier(db))
        assert checker.check(token_status="ok") == []


class TestTokenInvalidAlert:
    """GitLab token 失效（401/403）→ 立即告警。"""

    def test_triggers_on_401(self, config, db, notifier):
        checker = AlertChecker(config, db, notifier, gitlab=StubGitLab("unauthorized"))
        triggered = checker.check()
        assert ALERT_TOKEN_INVALID in triggered
        events = db.list_notifications(after_id=0)
        assert len(events) == 1 and events[0]["type"] == ALERT_TOKEN_INVALID

    def test_triggers_on_403(self, config, db, notifier):
        checker = AlertChecker(config, db, notifier, gitlab=StubGitLab("forbidden"))
        assert ALERT_TOKEN_INVALID in checker.check()

    def test_not_triggered_on_network_error(self, config, db, notifier):
        """传输层故障（无 401/403）不算 token 失效，不告警。"""
        checker = AlertChecker(config, db, notifier, gitlab=StubGitLab("network"))
        assert checker.check() == []

    def test_not_triggered_when_token_ok(self, config, db, notifier):
        checker = AlertChecker(config, db, notifier, gitlab=StubGitLab("ok"))
        assert checker.check() == []

    def test_uses_injected_token_status(self, config, db, notifier):
        """调用方已知 token 状态时直接注入，避免重复探测。"""
        checker = AlertChecker(config, db, notifier)  # 无 gitlab 也能用注入状态
        assert ALERT_TOKEN_INVALID in checker.check(token_status="invalid")
        assert checker.check(token_status="ok") == []

    def test_disabled_switch(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + "alerts:\n  notify_token_invalid: false\n",
                               encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        checker = AlertChecker(config, db, Notifier(db), gitlab=StubGitLab("unauthorized"))
        assert checker.check() == []


class TestDiskLowAlert:
    """数据目录磁盘剩余 < 阈值（默认 512 MiB）→ 告警。"""

    def test_triggers_when_disk_low(self, config, db, notifier, monkeypatch):
        import botler.alerts as alerts_mod
        monkeypatch.setattr(
            alerts_mod, "probe_disk",
            lambda *a, **k: {"status": "fail", "free_mb": 100, "detail": "模拟磁盘不足"})
        checker = AlertChecker(config, db, notifier)
        triggered = checker.check(token_status="ok")
        assert ALERT_DISK_LOW in triggered
        events = db.list_notifications(after_id=0)
        assert events[0]["type"] == ALERT_DISK_LOW

    def test_not_triggered_when_disk_ok(self, config, db, notifier, monkeypatch):
        import botler.alerts as alerts_mod
        monkeypatch.setattr(
            alerts_mod, "probe_disk",
            lambda *a, **k: {"status": "ok", "free_mb": 99999, "total_bytes": 1})
        checker = AlertChecker(config, db, notifier)
        assert checker.check(token_status="ok") == []

    def test_threshold_configurable(self, tmp_path, monkeypatch):
        """磁盘阈值可配置：阈值 10 MiB 时剩余 100 MiB 不触发。"""
        import botler.alerts as alerts_mod
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + "alerts:\n  disk_min_free_mb: 10\n",
                               encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        monkeypatch.setattr(
            alerts_mod, "probe_disk",
            lambda *a, **k: {"status": "fail" if a[1] < 100 * 1024 * 1024 else "ok",
                             "free_mb": 100, "total_bytes": 1})
        checker = AlertChecker(config, db, Notifier(db))
        assert checker.check(token_status="ok") == []


class TestAlertThrottle:
    """同类告警在节流窗口内不重复通知（默认 1 小时）。"""

    def test_same_type_throttled_within_window(self, config, db, notifier):
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier)
        assert ALERT_FAILURE_RATE in checker.check(token_status="ok")
        assert len(db.list_notifications(after_id=0)) == 1
        # 第二次检查：条件仍满足但节流窗口内不重复通知
        assert checker.check(token_status="ok") == []
        assert len(db.list_notifications(after_id=0)) == 1

    def test_different_types_independent(self, config, db, notifier):
        """不同类型告警互不节流（各自独立窗口）。"""
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier, gitlab=StubGitLab("unauthorized"))
        triggered = checker.check()
        assert ALERT_FAILURE_RATE in triggered and ALERT_TOKEN_INVALID in triggered
        assert len(db.list_notifications(after_id=0)) == 2

    def test_total_switch_disables_all(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + "alerts:\n  enabled: false\n",
                               encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        repo = _mk_repo(db)
        for i in range(6):
            _mk_task(db, repo, STATUS_QUEUED, iid=i + 1)
        _mk_task(db, repo, STATUS_FAILED, iid=11)
        _mk_task(db, repo, STATUS_FAILED, iid=12)
        checker = AlertChecker(config, db, Notifier(db), gitlab=StubGitLab("unauthorized"))
        assert checker.check() == []
        assert len(db.list_notifications(after_id=0)) == 0


class TestWebhookAlertPush:
    """告警走现有 webhook 通道：send_alert 推送结构化 payload。"""

    def _config_with_webhook(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT + (
            "webhook:\n"
            "  enabled: true\n"
            "  url: https://hook.example.com/notify\n"
            "  content_type: application/json\n"
            "  authorization: Bearer test\n"), encoding="utf-8")
        return ConfigManager(str(config_path))

    def test_send_alert_pushes_when_enabled(self, tmp_path, monkeypatch):
        config = self._config_with_webhook(tmp_path)
        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, content, headers):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                return FakeResp()

        monkeypatch.setattr("botler.webhook_push.httpx.Client", FakeClient)
        pusher = WebhookPusher(config)
        result = pusher.send_alert("alert_failure_rate", "⚠️ 任务失败率过高", "详情")
        assert result is not None and result["status_code"] == 200
        assert captured["url"] == "https://hook.example.com/notify"
        assert captured["headers"]["Authorization"] == "Bearer test"
        payload = json.loads(captured["content"])
        assert payload["type"] == "alert"
        assert payload["alert_type"] == "alert_failure_rate"
        assert payload["title"] == "⚠️ 任务失败率过高"

    def test_send_alert_returns_none_when_disabled(self, tmp_path):
        config = ConfigManager(str(tmp_path / "config.yaml"))
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        assert WebhookPusher(config).send_alert("alert_disk_low", "t", "b") is None

    def test_dispatch_webhook_with_alert(self, config, db, notifier, tmp_path, monkeypatch):
        """AlertChecker 触发后同时分发 webhook 推送（不配置时静默跳过）。"""
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)
        checker = AlertChecker(config, db, notifier)
        assert ALERT_FAILURE_RATE in checker.check(token_status="ok")  # webhook 未配置不抛错


class TestAlertsSettingsAPI:
    """设置 API：GET alerts 段 + PUT 校验与写回（阈值可配置）。"""

    @pytest.fixture
    def client(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        return TestClient(app), tmp_path

    def test_get_settings_includes_alerts_defaults(self, client):
        tc, tmp_path = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        alerts = resp.json()["alerts"]
        assert alerts["enabled"] is True
        assert alerts["failure_rate_threshold"] == 50.0
        assert alerts["failure_rate_window"] == 3600
        assert alerts["queue_backlog_threshold"] == 5
        assert alerts["queue_stall_minutes"] == 30
        assert alerts["disk_min_free_mb"] == 512
        assert alerts["throttle_seconds"] == 3600

    def test_update_alerts_persists(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"alerts": {
            "failure_rate_threshold": 80, "queue_backlog_threshold": 10,
            "disk_min_free_mb": 1024, "enabled": False}})
        assert resp.status_code == 200
        alerts = resp.json()["alerts"]
        assert alerts["failure_rate_threshold"] == 80
        assert alerts["queue_backlog_threshold"] == 10
        assert alerts["disk_min_free_mb"] == 1024
        assert alerts["enabled"] is False
        # config.yaml 是唯一事实来源，应已落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "alerts:" in config_text and "failure_rate_threshold: 80" in config_text

    def test_update_alerts_rejects_out_of_range_threshold(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"alerts": {"failure_rate_threshold": 150}})
        assert resp.status_code == 400

    def test_update_alerts_rejects_non_bool_switch(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"alerts": {"enabled": "yes"}})
        assert resp.status_code == 400

    def test_update_alerts_rejects_non_positive_window(self, client):
        tc, tmp_path = client
        resp = tc.put("/api/settings", json={"alerts": {"failure_rate_window": -1}})
        assert resp.status_code == 400


class TestReconcilerIntegration:
    """对账循环集成：reconcile_once 末尾执行告警检测（issue #229）。"""

    def test_reconcile_once_detects_token_invalid(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))

        class Stub:
            def get_bot_id(self):
                raise GitLabError("token 无效或已过期（401）", 401)

            def test_connection(self):
                raise GitLabError("token 无效或已过期（401）", 401)

        scheduler = SimpleNamespace(enqueue=lambda task_id: True)
        reconciler = Reconciler(config, db, Stub(), scheduler)
        result = reconciler.reconcile_once()
        assert result == {"scanned": 0, "enqueued": 0}
        events = db.list_notifications(after_id=0)
        assert any(e["type"] == ALERT_TOKEN_INVALID for e in events)

    def test_reconcile_once_detects_failure_rate(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        repo = _mk_repo(db)
        _mk_task(db, repo, STATUS_FAILED, iid=1)
        _mk_task(db, repo, STATUS_FAILED, iid=2)
        _mk_task(db, repo, STATUS_SUCCEEDED, iid=3)

        class Stub:
            def get_bot_id(self):
                return 99

            def test_connection(self):
                return {"id": 99, "username": "bot"}

            def list_open_issues(self, project_id, assignee_id=None):
                return []  # 无待处理 issue（失败率告警与扫描无关）

            def get_issue(self, project_id, iid):
                return {"state": "closed", "labels": []}  # 已关闭：终态标签对账跳过

        scheduler = SimpleNamespace(enqueue=lambda task_id: True)
        reconciler = Reconciler(config, db, Stub(), scheduler)
        reconciler.reconcile_once()
        events = db.list_notifications(after_id=0)
        assert any(e["type"] == ALERT_FAILURE_RATE for e in events)
