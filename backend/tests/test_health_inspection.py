"""仓库健康巡检测试（issue #265）。

功能：定时检查每个启用仓库的 webhook 有效性（存在且 secret 匹配）/
token 有效性（轻量 API 调用）/ 项目可达性（GET project），结果落库
repo_health 表；webhook 缺失/secret 不匹配时自动重新注册（auto_repair）；
异常聚合通知（in_app 事件 + webhook 推送，节流防刷屏）；仓库列表展示
健康徽章，支持手动重检。

覆盖：健康/异常判定、自动修复成功与失败、auto_repair 关闭、token 失效、
项目 404、多失败项聚合错误、停用/关闭跳过、force 手动重检、结果落库与
历史、聚合通知（多仓库一条 + 节流 + webhook 推送）、API（列表 health
字段 / 详情历史 / 手动重检）。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError
from botler.health_inspection import (
    ALERT_REPO_HEALTH,
    HEALTH_ABNORMAL,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    RepoHealthInspector,
)
from botler.notifier import Notifier
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
alerts: {}
inspection:
  enabled: true
  interval_seconds: 21600
  auto_repair: true
repos: []
"""


class StubGitLab:
    """巡检用 GitLab 桩：可配置 webhook 列表 / token 状态 / 项目状态。"""

    def __init__(self):
        self.hooks: list[dict] = []          # 远端项目 webhook 列表
        self.register_calls: list[tuple] = []  # register_webhook(project_id, secret)
        self.token_mode = "ok"               # ok / unauthorized
        self.project_mode = "ok"             # ok / missing
        self.register_mode = "ok"            # ok / error

    def webhook_url(self) -> str:
        return "https://botler.example.com/webhook/gitlab"

    def list_webhooks(self, project_id: int) -> list[dict]:
        return list(self.hooks)

    def register_webhook(self, project_id: int, secret: str) -> dict:
        self.register_calls.append((project_id, secret))
        if self.register_mode == "error":
            raise GitLabError("注册 webhook 失败（422）", 422)
        self.hooks = [{"id": 1, "url": self.webhook_url(), "token": secret,
                       "issues_events": True}]
        return {"id": 1}

    def update_webhook(self, project_id: int, hook_id: int, url: str,
                       secret: str) -> dict:
        self.register_calls.append((project_id, secret))
        if self.register_mode == "error":
            raise GitLabError("更新 webhook 失败（422）", 422)
        self.hooks = [{"id": hook_id, "url": url, "token": secret,
                       "issues_events": True}]
        return {"id": hook_id}

    def test_connection(self) -> dict:
        if self.token_mode == "unauthorized":
            raise GitLabError("token 无效或已过期（401）", 401)
        return {"id": 99, "username": "bot"}

    def get_project(self, project_id: int) -> dict:
        if self.project_mode == "missing":
            raise GitLabError("项目不存在（404）", 404)
        return {"id": project_id, "path_with_namespace": "group/project"}


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


def _mk_repo(db, project_id: int = 42, name: str = "demo",
             enabled: bool = True) -> int:
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/{name}.git",
                   enabled=enabled)
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_inspector(config, db, stub, notifier=None) -> RepoHealthInspector:
    return RepoHealthInspector(config, db, stub, notifier=notifier)


# ---- 巡检判定 ----

class TestInspectionJudgement:
    def test_healthy_repo_persists_healthy(self, config, db, notifier):
        stub = StubGitLab()
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["checked"] == 1
        assert result["abnormal"] == []
        row = db.latest_repo_health(repo_id)
        assert row is not None
        assert row["health_status"] == HEALTH_HEALTHY
        # webhook 未注册会被自动修复：修复成功后 webhook_ok=1、repaired=1
        assert row["webhook_ok"] == 1
        assert row["repaired"] == 1
        assert row["token_ok"] == 1
        assert row["project_ok"] == 1

    def test_unknown_before_first_check(self, config, db, notifier):
        stub = StubGitLab()
        repo_id = _mk_repo(db)
        assert db.latest_repo_health(repo_id) is None
        assert db.latest_health_by_repo() == {}
        _mk_inspector(config, db, stub, notifier).inspect_once()
        assert db.latest_repo_health(repo_id) is not None

    def test_webhook_registered_healthy(self, config, db, notifier):
        stub = StubGitLab()
        stub.hooks = [{"id": 7, "url": stub.webhook_url(),
                       "token": config.get().webhook_secret, "issues_events": True}]
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == []
        row = db.latest_repo_health(repo_id)
        assert row["health_status"] == HEALTH_HEALTHY
        assert row["webhook_ok"] == 1
        assert row["repaired"] == 0  # 已注册且匹配，无需修复

    def test_webhook_missing_auto_repair(self, config, db, notifier):
        """webhook 缺失 → 异常判定 + 自动重新注册成功（验收标准 2）。"""
        stub = StubGitLab()  # hooks 为空 = webhook 缺失
        repo_id = _mk_repo(db)
        inspector = _mk_inspector(config, db, stub, notifier)
        result = inspector.inspect_once()
        # 自动修复成功：webhook 检查通过、标记 repaired、整体健康
        assert result["abnormal"] == []
        assert stub.register_calls == [(42, config.get().webhook_secret)]
        row = db.latest_repo_health(repo_id)
        assert row["health_status"] == HEALTH_HEALTHY
        assert row["repaired"] == 1
        assert row["webhook_ok"] == 1

    def test_webhook_secret_mismatch_auto_repair(self, config, db, notifier):
        """secret 不匹配 → 自动重新注册（注册按最新 secret 覆盖）。"""
        stub = StubGitLab()
        stub.hooks = [{"id": 7, "url": stub.webhook_url(), "token": "旧-secret", "issues_events": True}]
        repo_id = _mk_repo(db)
        inspector = _mk_inspector(config, db, stub, notifier)
        result = inspector.inspect_once()
        assert result["abnormal"] == []
        assert len(stub.register_calls) == 1
        assert stub.register_calls[0][1] == config.get().webhook_secret
        row = db.latest_repo_health(repo_id)
        assert row["repaired"] == 1

    def test_webhook_missing_auto_repair_disabled(self, config, db, notifier):
        """auto_repair=false：缺失不修复，判异常并落库。"""
        config_path = config.path
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(text + "  auto_repair: false\n" if "auto_repair" not in text
                    else text.replace("auto_repair: true", "auto_repair: false"))
        config = ConfigManager(str(config_path))
        stub = StubGitLab()
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == ["demo"]
        assert stub.register_calls == []  # 不自动修复
        row = db.latest_repo_health(repo_id)
        assert row["health_status"] == HEALTH_ABNORMAL
        assert row["webhook_ok"] == 0
        assert "webhook 未注册" in (row["last_error"] or "")

    def test_auto_repair_failure_marks_abnormal(self, config, db, notifier):
        stub = StubGitLab()
        stub.register_mode = "error"
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == ["demo"]
        row = db.latest_repo_health(repo_id)
        assert row["health_status"] == HEALTH_ABNORMAL
        assert "自动修复 webhook 失败" in (row["last_error"] or "")

    def test_token_invalid_401(self, config, db, notifier):
        stub = StubGitLab()
        stub.hooks = [{"id": 1, "url": stub.webhook_url(),
                       "token": config.get().webhook_secret, "issues_events": True}]
        stub.token_mode = "unauthorized"
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == ["demo"]
        row = db.latest_repo_health(repo_id)
        assert row["health_status"] == HEALTH_ABNORMAL
        assert row["token_ok"] == 0
        assert "token 无效或已过期" in (row["last_error"] or "")

    def test_project_404(self, config, db, notifier):
        stub = StubGitLab()
        stub.hooks = [{"id": 1, "url": stub.webhook_url(),
                       "token": config.get().webhook_secret, "issues_events": True}]
        stub.project_mode = "missing"
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == ["demo"]
        row = db.latest_repo_health(repo_id)
        assert row["project_ok"] == 0
        assert "项目不存在" in (row["last_error"] or "")

    def test_multiple_failures_aggregated_error(self, config, db, notifier):
        """多检查项失败：last_error 以「；」聚合全部失败描述。"""
        stub = StubGitLab()
        stub.token_mode = "unauthorized"
        stub.project_mode = "missing"
        stub.register_mode = "error"  # webhook 缺失且自动修复失败 → 三项全失败
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == ["demo"]
        row = db.latest_repo_health(repo_id)
        assert "webhook" in (row["last_error"] or "")
        assert "token" in (row["last_error"] or "")
        assert "项目" in (row["last_error"] or "")

    def test_disabled_repo_skipped(self, config, db, notifier):
        stub = StubGitLab()
        _mk_repo(db, project_id=42, name="disabled", enabled=False)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["checked"] == 0
        assert db.latest_repo_health(1) is None

    def test_inspection_disabled_skips_all(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT + "inspection:\n  enabled: false\n",
            encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        stub = StubGitLab()
        _mk_repo(db)
        result = _mk_inspector(config, db, stub, Notifier(db)).inspect_once()
        assert result["checked"] == 0
        assert stub.register_calls == []

    def test_force_runs_when_inspection_disabled(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT + "inspection:\n  enabled: false\n",
            encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        stub = StubGitLab()
        _mk_repo(db)
        # 手动重检（force=True）：即使总开关关闭也立即执行
        result = _mk_inspector(config, db, stub, Notifier(db)).inspect_once(force=True)
        assert result["checked"] == 1

    def test_inspect_single_repo(self, config, db, notifier):
        stub = StubGitLab()
        repo_a = _mk_repo(db, project_id=42, name="a")
        _mk_repo(db, project_id=43, name="b")
        result = _mk_inspector(config, db, stub, notifier).inspect_once(repo_id=repo_a)
        assert result["checked"] == 1
        assert db.latest_repo_health(repo_a) is not None
        # 另一仓库未被巡检
        other = db.get_repo_by_project_id(43)
        assert db.latest_repo_health(other["id"]) is None

    def test_history_kept_latest_wins(self, config, db, notifier):
        stub = StubGitLab()
        repo_id = _mk_repo(db)
        inspector = _mk_inspector(config, db, stub, notifier)
        inspector.inspect_once()
        # 第二次巡检前 webhook 被删 → 异常
        stub.hooks = []
        stub.register_mode = "error"
        inspector.inspect_once()
        history = db.list_repo_health(repo_id)
        assert len(history) == 2
        latest = db.latest_repo_health(repo_id)
        assert latest["health_status"] == HEALTH_ABNORMAL
        assert latest["id"] == history[0]["id"]  # 倒序最新在前
        assert history[1]["health_status"] == HEALTH_HEALTHY

    def test_latest_health_by_repo(self, config, db, notifier):
        stub = StubGitLab()
        stub.token_mode = "unauthorized"
        repo_a = _mk_repo(db, project_id=42, name="a")
        stub2 = StubGitLab()
        repo_b = _mk_repo(db, project_id=43, name="b")
        _mk_inspector(config, db, stub, notifier).inspect_once(repo_id=repo_a)
        _mk_inspector(config, db, stub2, notifier).inspect_once(repo_id=repo_b)
        latest = db.latest_health_by_repo()
        assert set(latest) == {repo_a, repo_b}
        assert latest[repo_a]["health_status"] == HEALTH_ABNORMAL
        assert latest[repo_b]["health_status"] == HEALTH_HEALTHY


# ---- 聚合通知 ----

class TestAggregatedNotification:
    def test_abnormal_aggregated_single_event(self, config, db, notifier):
        """多仓库异常汇总为一条告警（验收标准 3：不刷屏）。"""
        stub = StubGitLab()
        stub.token_mode = "unauthorized"  # 所有仓库 token 检查失败
        _mk_repo(db, project_id=42, name="repo-a")
        _mk_repo(db, project_id=43, name="repo-b")
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert len(result["abnormal"]) == 2
        events = db.list_notifications(after_id=0)
        assert len(events) == 1  # 一条聚合告警
        assert events[0]["type"] == ALERT_REPO_HEALTH
        assert "repo-a" in events[0]["body"]
        assert "repo-b" in events[0]["body"]
        data = json.loads(events[0]["data"] or "{}")
        assert data["count"] == 2
        assert len(data["repos"]) == 2

    def test_healthy_round_no_notification(self, config, db, notifier):
        stub = StubGitLab()
        stub.hooks = [{"id": 1, "url": stub.webhook_url(),
                       "token": config.get().webhook_secret, "issues_events": True}]
        _mk_repo(db)
        _mk_inspector(config, db, stub, notifier).inspect_once()
        assert len(db.list_notifications(after_id=0)) == 0

    def test_notification_throttled_within_window(self, config, db, notifier):
        """节流窗口内重复巡检不重复通知（默认 1 小时）。"""
        stub = StubGitLab()
        stub.token_mode = "unauthorized"
        _mk_repo(db)
        inspector = _mk_inspector(config, db, stub, notifier)
        inspector.inspect_once()
        inspector.inspect_once()  # 窗口内再次巡检
        events = db.list_notifications(after_id=0)
        assert len(events) == 1

    def test_webhook_push_on_abnormal(self, config, db, notifier, monkeypatch):
        """异常时经 WebhookPusher 推送（webhook 启用场景）。"""
        config_path = config.path
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(text + "\nwebhook:\n  enabled: true\n  url: https://push.example.com/hook\n")
        config = ConfigManager(str(config_path))

        pushed = []
        monkeypatch.setattr(
            WebhookPusher, "send_alert",
            lambda self, alert_type, title, body="", detail="":
                pushed.append({"type": alert_type, "title": title,
                               "body": body, "detail": detail}))
        stub = StubGitLab()
        stub.token_mode = "unauthorized"
        _mk_repo(db, name="repo-a")
        _mk_inspector(config, db, stub, notifier).inspect_once()
        assert len(pushed) == 1
        assert pushed[0]["type"] == ALERT_REPO_HEALTH
        assert "repo-a" in pushed[0]["detail"]

    def test_no_push_when_throttled(self, config, db, notifier, monkeypatch):
        """节流窗口内：in_app 不重复落库，webhook 也不重复推送。"""
        config_path = config.path
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(text + "\nwebhook:\n  enabled: true\n  url: https://push.example.com/hook\n")
        config = ConfigManager(str(config_path))

        pushed = []
        monkeypatch.setattr(
            WebhookPusher, "send_alert",
            lambda self, alert_type, title, body="", detail="":
                pushed.append(alert_type))
        stub = StubGitLab()
        stub.token_mode = "unauthorized"
        _mk_repo(db)
        inspector = _mk_inspector(config, db, stub, notifier)
        inspector.inspect_once()
        inspector.inspect_once()  # 窗口内再次巡检
        assert len(pushed) == 1


# ---- API ----

class TestHealthApi:
    @pytest.fixture
    def api_env(self, tmp_path):
        """最小测试 app：repos 路由 + 带 health_inspection 的 ctx。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "test.db"))
        stub = StubGitLab()
        inspector = RepoHealthInspector(config, db, stub, notifier=Notifier(db))
        from types import SimpleNamespace

        ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                              health_inspection=inspector,
                              config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        return app, stub, tmp_path

    def test_list_repos_health_unknown(self, api_env):
        app, stub, tmp = api_env
        _mk_repo(app.state.ctx.db)
        tc = TestClient(app)
        resp = tc.get("/api/repos")
        assert resp.status_code == 200
        repo = resp.json()["repos"][0]
        assert repo["health"]["status"] == HEALTH_UNKNOWN  # 未巡检 = 未知

    def test_list_repos_health_after_inspection(self, api_env):
        app, stub, tmp = api_env
        stub.hooks = [{"id": 1, "url": stub.webhook_url(),
                       "token": app.state.ctx.config.get().webhook_secret}]
        _mk_repo(app.state.ctx.db)
        tc = TestClient(app)
        tc.post("/api/repos/1/health-check")
        resp = tc.get("/api/repos")
        assert resp.status_code == 200
        repo = resp.json()["repos"][0]
        assert repo["health"]["status"] == HEALTH_HEALTHY
        assert repo["health"]["check_time"] is not None

    def test_health_detail_history(self, api_env):
        app, stub, tmp = api_env
        _mk_repo(app.state.ctx.db)
        tc = TestClient(app)
        tc.post("/api/repos/1/health-check")
        resp = tc.get("/api/repos/1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["latest"]["status"] == HEALTH_HEALTHY
        assert body["latest"]["webhook_ok"] is True
        assert len(body["history"]) == 1

    def test_health_detail_not_found(self, api_env):
        app, stub, tmp = api_env
        tc = TestClient(app)
        assert tc.get("/api/repos/999/health").status_code == 404
        assert tc.post("/api/repos/999/health-check").status_code == 404

    def test_manual_check_disabled_repo(self, api_env):
        app, stub, tmp = api_env
        _mk_repo(app.state.ctx.db, enabled=False)
        tc = TestClient(app)
        resp = tc.post("/api/repos/1/health-check")
        assert resp.status_code == 200
        assert resp.json()["note"] == "仓库已停用，未巡检"

    def test_manual_check_persists_and_returns(self, api_env):
        app, stub, tmp = api_env
        _mk_repo(app.state.ctx.db)
        tc = TestClient(app)
        resp = tc.post("/api/repos/1/health-check")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["checked"] == 1
        assert body["abnormal"] == []
        assert app.state.ctx.db.latest_repo_health(1) is not None


# ---- 设置 API：inspection 段（间隔 / auto_repair 可配置）----

class TestInspectionSettingsApi:
    @pytest.fixture
    def settings_env(self, tmp_path):
        from types import SimpleNamespace

        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        return TestClient(app), tmp_path

    def test_get_settings_includes_inspection_defaults(self, settings_env):
        """GET /api/settings 返回 inspection 段（默认值）。"""
        tc, tmp = settings_env
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        sec = resp.json()["inspection"]
        assert sec["enabled"] is True
        assert sec["interval_seconds"] == 21600
        assert sec["auto_repair"] is True

    def test_update_inspection_persists(self, settings_env):
        """PUT inspection 段写回 config.yaml 并可读回（间隔/auto_repair 可配置）。"""
        tc, tmp = settings_env
        resp = tc.put("/api/settings", json={
            "inspection": {"interval_seconds": 7200, "auto_repair": False}})
        assert resp.status_code == 200
        sec = resp.json()["inspection"]
        assert sec["interval_seconds"] == 7200
        assert sec["auto_repair"] is False
        config_text = (tmp / "config.yaml").read_text(encoding="utf-8")
        assert "inspection:" in config_text
        assert "7200" in config_text
        # 新 ConfigManager 从磁盘读到新值（配置是唯一事实来源）
        reloaded = ConfigManager(str(tmp / "config.yaml")).get()
        assert reloaded.inspection_interval_seconds == 7200
        assert reloaded.inspection_auto_repair is False

    def test_update_inspection_rejects_small_interval(self, settings_env):
        """间隔低于下限 300 秒 → 400（避免误配极短间隔压垮 GitLab API）。"""
        tc, tmp = settings_env
        resp = tc.put("/api/settings", json={
            "inspection": {"interval_seconds": 60}})
        assert resp.status_code == 400
        assert "300" in resp.json()["detail"]

    def test_update_inspection_rejects_non_bool(self, settings_env):
        tc, tmp = settings_env
        resp = tc.put("/api/settings", json={
            "inspection": {"auto_repair": "yes"}})
        assert resp.status_code == 400
        assert "布尔值" in resp.json()["detail"]


# ---- webhook 判定补充（GitLab 掩码 token / host 无关匹配 / 事件开关）----

class TestWebhookJudgementDetails:
    def test_masked_token_treated_as_configured(self, config, db, notifier):
        """GitLab 对 hook token 做安全掩码（list 接口返回 null）：无法比对
        secret 时视为已配置（webhook 存在性已确认），不误报不误修复。"""
        stub = StubGitLab()
        stub.hooks = [{"id": 7, "url": stub.webhook_url(),
                       "token": None, "issues_events": True}]
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == []
        assert stub.register_calls == []  # 不误触发修复
        assert db.latest_repo_health(repo_id)["health_status"] == HEALTH_HEALTHY

    def test_host_agnostic_hook_match(self, config, db, notifier):
        """平台 webhook 回调地址与 gitlab_url 不同（如内网 IP）：按回调路径
        后缀匹配，host 无关也能正确识别为已注册。"""
        stub = StubGitLab()
        stub.hooks = [{"id": 7, "url": "http://10.0.0.122:8000/webhook/gitlab",
                       "token": None, "issues_events": True}]
        _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == []
        assert stub.register_calls == []

    def test_issues_events_disabled_repaired(self, config, db, notifier):
        """webhook 存在但 issues_events 被关闭 → 判异常并自动修复（恢复事件
        开关，保持原回调 URL）。"""
        stub = StubGitLab()
        stub.hooks = [{"id": 7, "url": stub.webhook_url(),
                       "token": None, "issues_events": False}]
        repo_id = _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == []
        assert stub.register_calls == [(42, config.get().webhook_secret)]
        row = db.latest_repo_health(repo_id)
        assert row["repaired"] == 1
        assert stub.hooks[0]["issues_events"] is True  # 事件开关已恢复
        assert stub.hooks[0]["url"] == "https://botler.example.com/webhook/gitlab"

    def test_repair_preserves_existing_hook_url(self, config, db, notifier):
        """secret 不匹配修复时保留已存在 hook 的回调 URL（部署环境回调地址
        与 gitlab_url 不同，重建会注册到错误地址）。"""
        stub = StubGitLab()
        stub.hooks = [{"id": 9, "url": "http://10.0.0.122:8000/webhook/gitlab",
                       "token": "旧-secret", "issues_events": True}]
        _mk_repo(db)
        result = _mk_inspector(config, db, stub, notifier).inspect_once()
        assert result["abnormal"] == []
        assert stub.register_calls == [(42, config.get().webhook_secret)]
        assert stub.hooks[0]["id"] == 9  # 更新而非新建
        assert stub.hooks[0]["url"] == "http://10.0.0.122:8000/webhook/gitlab"
        assert stub.hooks[0]["token"] == config.get().webhook_secret
