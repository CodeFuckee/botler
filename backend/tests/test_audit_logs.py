"""操作审计日志（issue #260）测试。

覆盖：
1. 数据库层：audit_logs 表 CRUD / 分页 / 过滤 / 操作类型去重 / v26 迁移；
2. API 层：GET 分页与过滤、actions 下拉、DELETE 权限与 404、管理员门禁
   （SSO 未启用恒管理员；配置 admin_usernames 后仅名单内用户可访问）；
3. 埋点层：设置保存 diff、仓库增删改、任务重试/停止/移出队列/优先级、
   插件安装卸载、备份执行、config.yaml 外部修改（含 webhook 轮换标记）；
4. 容错：审计写入失败不影响主操作；
5. 配置：audit_logs.admin_usernames 解析 / 写回 / 校验。
"""

import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.audit import config_diff_summary
from botler.auth import CSRF_COOKIE, SsoAuth, CsrfGuardMiddleware, SsoGuardMiddleware
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabClient

CONFIG_NO_SSO = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
audit_logs:
  admin_usernames: []
"""

CONFIG_SSO = CONFIG_NO_SSO.replace(
    "worker: {}",
    "worker: {}\nsso:\n  enabled: true\n  well_known_url: "
    "https://nas.example.com/.well-known/openid-configuration\n"
    "  client_id: app-123\n  client_secret: secret-abc\n"
    "  scope: openid profile email\n  session_days: 7")


# ---- 通用夹具 ----

def _oidc_handler(username="zhangsan"):
    """模拟群晖 SSO Server 的 OIDC 端点（与 test_csrf.py 同构，可定制用户名）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": "https://nas.example.com",
                "authorization_endpoint": "https://nas.example.com/oauth/authorize",
                "token_endpoint": "https://nas.example.com/oauth/token",
                "userinfo_endpoint": "https://nas.example.com/oauth/userinfo",
            })
        if url.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer"})
        if url.endswith("/oauth/userinfo"):
            return httpx.Response(200, json={
                "sub": "uid-1", "username": username, "name": "测试用户",
                "email": "u@example.com", "picture": "",
            })
        return httpx.Response(404, json={"error": "not found"})

    return handler


def _build_app(tmp_path, config_text: str, with_sso: bool = False,
               sso_username="zhangsan"):
    """最小测试 app：挂完整 api 路由 + SSO 中间件（可选），ctx 临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    sso = SsoAuth(config, secret_path=str(tmp_path / "session.key"),
                  transport=httpx.MockTransport(_oidc_handler(sso_username)))
    ctx = SimpleNamespace(config=config, db=db, sso=sso)
    app = FastAPI()
    app.state.ctx = ctx
    if with_sso:
        # 与生产一致：SsoGuard → CsrfGuard → api 路由（add 逆序执行）
        app.add_middleware(CsrfGuardMiddleware)
        app.add_middleware(SsoGuardMiddleware)
    app.include_router(api_router)
    return TestClient(app), ctx, tmp_path


@pytest.fixture
def client(tmp_path):
    """SSO 未启用（本机单用户）场景。"""
    tc, ctx, tmp = _build_app(tmp_path, CONFIG_NO_SSO)
    yield tc, ctx, tmp
    ctx.db.close()  # issue #395：显式释放 sqlite 连接，避免全量测试 fd 累积


@pytest.fixture
def sso_client(tmp_path):
    """SSO 启用场景（默认管理员名单空 = 所有登录用户均可访问）。"""
    tc, ctx, tmp = _build_app(tmp_path, CONFIG_SSO, with_sso=True)
    yield tc, ctx, tmp
    ctx.db.close()


def _login(tc) -> None:
    """走完整 SSO 登录流程，TestClient 自动保存会话与 CSRF cookie。"""
    resp = tc.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    state = dict(parse_qsl(urlparse(resp.headers["location"]).query))["state"]
    resp = tc.get(f"/api/auth/callback?code=good-code&state={state}",
                  follow_redirects=False)
    assert resp.status_code == 302


def _csrf(tc) -> dict:
    return {"X-CSRF-Token": tc.cookies.get(CSRF_COOKIE)}


def _config_with_admins(admins: list[str]) -> str:
    """构造配置了管理员名单的 SSO config。"""
    text = CONFIG_SSO.replace("audit_logs:\n  admin_usernames: []",
                              f"audit_logs:\n  admin_usernames: {json.dumps(admins)}")
    return text


# ---- 1. 数据库层 ----

@pytest.fixture
def audit_db(tmp_path):
    """临时 audit_logs 数据库（issue #395：显式关闭连接）。"""
    db = Database(str(tmp_path / "audit.db"))
    yield db
    db.close()


class TestAuditDatabase:
    def test_add_audit_log_fields(self, audit_db):
        """写入字段完整：actor/action/target/detail JSON/ip/created_at。"""
        db = audit_db
        rid = db.add_audit_log("alice", "repo.delete", "repo", 7,
                               {"name": "x", "project_id": 42}, "10.0.0.1")
        row = db.get_audit_log(rid)
        assert row is not None
        assert row["actor"] == "alice"
        assert row["action"] == "repo.delete"
        assert row["target_type"] == "repo"
        assert row["target_id"] == "7"
        assert json.loads(row["detail"]) == {"name": "x", "project_id": 42}
        assert row["ip"] == "10.0.0.1"
        assert row["created_at"]  # datetime('now') UTC

    def test_add_audit_log_defaults(self, audit_db):
        """缺省字段落默认值：空串 actor/ip、'{}' detail、None target_id。"""
        db = audit_db
        rid = db.add_audit_log("", "task.retry")
        row = db.get_audit_log(rid)
        assert row["actor"] == "" and row["ip"] == ""
        assert row["detail"] == "{}"
        assert row["target_type"] == "" and row["target_id"] is None

    def test_list_pagination_and_total(self, audit_db):
        """分页：id 倒序 + 总条数正确。"""
        db = audit_db
        for i in range(25):
            db.add_audit_log(f"u{i}", "settings.update")
        rows, total = db.list_audit_logs(0, 10)
        assert total == 25 and len(rows) == 10
        assert rows[0]["id"] == 25 and rows[9]["id"] == 16
        rows2, total2 = db.list_audit_logs(20, 10)
        assert total2 == 25 and len(rows2) == 5 and rows2[-1]["id"] == 1

    def test_list_filters(self, audit_db):
        """按 action / actor / target_type 精确过滤。"""
        db = audit_db
        db.add_audit_log("alice", "repo.add", "repo", 1)
        db.add_audit_log("alice", "repo.delete", "repo", 2)
        db.add_audit_log("bob", "task.retry", "task", 3)
        rows, total = db.list_audit_logs(action="repo.add")
        assert total == 1 and rows[0]["target_id"] == "1"
        rows, total = db.list_audit_logs(actor="alice")
        assert total == 2
        rows, total = db.list_audit_logs(target_type="task")
        assert total == 1 and rows[0]["actor"] == "bob"
        rows, total = db.list_audit_logs(action="nope")
        assert total == 0 and rows == []

    def test_list_audit_actions_distinct_sorted(self, audit_db, tmp_path):
        """操作类型去重升序（过滤下拉数据源）。"""
        db = audit_db
        db.add_audit_log("a", "task.retry")
        db.add_audit_log("b", "repo.add")
        db.add_audit_log("c", "task.retry")
        assert db.list_audit_actions() == ["repo.add", "task.retry"]
        db2 = Database(str(tmp_path / "e2.db"))
        try:
            assert db2.list_audit_actions() == []
        finally:
            db2.close()

    def test_delete_audit_log(self, audit_db):
        """删除存在/不存在：True / False，删除后读取为 None。"""
        db = audit_db
        rid = db.add_audit_log("a", "repo.delete")
        assert db.delete_audit_log(rid) is True
        assert db.get_audit_log(rid) is None
        assert db.delete_audit_log(rid) is False

    def test_migration_v26_creates_audit_logs(self, tmp_path):
        """v25 旧库初始化后应补出 audit_logs 表并推进版本到 26。

        迁移链：v25 → v26（audit_logs 表）→ v27（repo_health 表，issue
        #265）。v25 旧库打开后 audit_logs 与 repo_health 均应存在，最终
        版本号为 27。
        """
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""CREATE TABLE repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gitlab_project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 100);""")
        conn.execute("PRAGMA user_version = 25")
        conn.commit()
        conn.close()
        db = Database(str(path))
        try:
            with db._conn() as conn:
                tables = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                ver = conn.execute("PRAGMA user_version").fetchone()[0]
            assert "audit_logs" in tables
            assert "repo_health" in tables  # issue #265：v27 迁移同链补建
            assert ver == 34
            # 新表可正常写入
            db.add_audit_log("alice", "repo.add", "repo", 1)
        finally:
            db.close()


# ---- 2. API 层（SSO 未启用） ----

class TestAuditApi:
    def test_list_shape_pagination(self, client):
        """GET /api/audit-logs：items/total/page/per_page/actions/admin。"""
        tc, ctx, tmp_path = client
        ctx.db.add_audit_log("alice", "repo.add", "repo", 1)
        ctx.db.add_audit_log("bob", "task.retry", "task", 2)
        r = tc.get("/api/audit-logs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2 and data["page"] == 1 and data["per_page"] == 20
        assert data["actions"] == ["repo.add", "task.retry"]
        assert data["admin"] is True  # SSO 未启用 → 本机恒管理员
        assert data["items"][0]["action"] == "task.retry"  # id 倒序

    def test_list_filters(self, client):
        """按 action / actor / target_type 过滤。"""
        tc, ctx, tmp_path = client
        ctx.db.add_audit_log("alice", "repo.add", "repo", 1)
        ctx.db.add_audit_log("bob", "task.retry", "task", 2)
        r = tc.get("/api/audit-logs", params={"action": "repo.add"})
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["actor"] == "alice"
        r = tc.get("/api/audit-logs", params={"actor": "bob"})
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["action"] == "task.retry"
        r = tc.get("/api/audit-logs", params={"target_type": "repo"})
        assert r.json()["total"] == 1

    def test_list_detail_json_parsed(self, client):
        """detail 以 JSON 对象返回（非法 JSON 容错为空对象）。"""
        tc, ctx, tmp_path = client
        ctx.db.add_audit_log("a", "settings.update", detail={"diff": {"worker": {"max_retries": [2, 5]}}})
        r = tc.get("/api/audit-logs")
        assert r.json()["items"][0]["detail"] == {"diff": {"worker": {"max_retries": [2, 5]}}}

    def test_per_page_bounds(self, client):
        """per_page 越界 400（1~200）。"""
        tc, ctx, tmp_path = client
        assert tc.get("/api/audit-logs", params={"per_page": 0}).status_code == 422
        assert tc.get("/api/audit-logs", params={"per_page": 201}).status_code == 422
        assert tc.get("/api/audit-logs", params={"page": 0}).status_code == 422

    def test_actions_endpoint(self, client):
        """GET /api/audit-logs/actions：去重操作类型。"""
        tc, ctx, tmp_path = client
        ctx.db.add_audit_log("a", "repo.add")
        ctx.db.add_audit_log("b", "repo.add")
        ctx.db.add_audit_log("c", "backup.create")
        r = tc.get("/api/audit-logs/actions")
        assert r.status_code == 200
        assert r.json()["actions"] == ["backup.create", "repo.add"]

    def test_delete_and_404(self, client):
        """DELETE：管理员可删；行不存在 404。"""
        tc, ctx, tmp_path = client
        rid = ctx.db.add_audit_log("a", "repo.delete")
        assert tc.delete(f"/api/audit-logs/{rid}").status_code == 200
        assert ctx.db.get_audit_log(rid) is None
        assert tc.delete(f"/api/audit-logs/{rid}").status_code == 404


# ---- 3. API 层（SSO 管理员门禁） ----

class TestAuditAdminGate:
    def _sso_app(self, tmp_path, admins):
        """构造启用 SSO + 管理员名单的 app，返回 (tc, ctx)。"""
        tc, ctx, _ = _build_app(tmp_path, _config_with_admins(admins),
                                with_sso=True, sso_username="zhangsan")
        _login(tc)
        return tc, ctx

    def test_admin_in_list_can_view_and_delete(self, tmp_path):
        """管理员名单内用户可查看与删除。"""
        tc, ctx = self._sso_app(tmp_path, ["zhangsan"])
        try:
            rid = ctx.db.add_audit_log("alice", "repo.delete")
            r = tc.get("/api/audit-logs")
            assert r.status_code == 200 and r.json()["admin"] is True
            assert tc.delete(f"/api/audit-logs/{rid}", headers=_csrf(tc)).status_code == 200
        finally:
            ctx.db.close()  # issue #395

    def test_non_admin_forbidden_view_and_delete(self, tmp_path):
        """非名单用户查看/删除一律 403（验收标准 3）。"""
        # 登录用户 lisi 不在管理员名单内
        tc, ctx, _ = _build_app(tmp_path, _config_with_admins(["zhangsan"]),
                                with_sso=True, sso_username="lisi")
        _login(tc)
        rid = ctx.db.add_audit_log("alice", "repo.delete")
        assert tc.get("/api/audit-logs").status_code == 403
        assert tc.get("/api/audit-logs/actions").status_code == 403
        assert tc.delete(f"/api/audit-logs/{rid}").status_code == 403
        # 记录仍存在（未越权删除）
        assert ctx.db.get_audit_log(rid) is not None
        ctx.db.close()  # issue #395

    def test_empty_admin_list_allows_all(self, tmp_path):
        """名单为空 = 所有登录用户均可访问（默认宽松，与平台现状一致）。"""
        tc, ctx, _ = _build_app(tmp_path, CONFIG_SSO, with_sso=True,
                                sso_username="lisi")
        _login(tc)
        rid = ctx.db.add_audit_log("alice", "repo.delete")
        assert tc.get("/api/audit-logs").status_code == 200
        assert tc.delete(f"/api/audit-logs/{rid}", headers=_csrf(tc)).status_code == 200
        ctx.db.close()  # issue #395

    def test_audit_logs_admin_required_when_sso_off(self, tmp_path):
        """SSO 未启用：即使配置了管理员名单也不受限（本机单用户）。"""
        text = CONFIG_NO_SSO.replace("audit_logs:\n  admin_usernames: []",
                                     "audit_logs:\n  admin_usernames: ['zhangsan']")
        tc, ctx, _ = _build_app(tmp_path, text, with_sso=False)
        rid = ctx.db.add_audit_log("a", "repo.delete")
        assert tc.get("/api/audit-logs").status_code == 200
        assert tc.delete(f"/api/audit-logs/{rid}").status_code == 200
        ctx.db.close()  # issue #395


# ---- 4. 埋点层 ----

class TestAuditInstrumentation:
    def test_settings_save_records_diff(self, client):
        """设置保存：记录 diff 前后值（仅实际变化的键）。"""
        tc, ctx, tmp_path = client
        r = tc.put("/api/settings", json={"worker": {"max_retries": 5}})
        assert r.status_code == 200
        rows, total = ctx.db.list_audit_logs(action="settings.update")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert detail["sections"] == ["worker"]
        assert detail["diff"]["worker"]["max_retries"] == [2, 5]
        assert rows[0]["actor"] == "local"

    def test_settings_save_no_change_no_audit(self, client):
        """保存相同值不产生审计噪音。"""
        tc, ctx, tmp_path = client
        tc.put("/api/settings", json={"worker": {"max_retries": 2}})
        rows, total = ctx.db.list_audit_logs()
        assert total == 0

    def test_settings_save_masked_secret_not_leaked(self, client):
        """设置保存 diff 不落明文凭据（掩码值比较）。"""
        tc, ctx, tmp_path = client
        # webhook.authorization 是掩码字段：提交掩码占位符应被忽略（保持现状）
        tc.put("/api/settings", json={"webhook": {"url": "https://h.example.com/x"}})
        rows, total = ctx.db.list_audit_logs(action="settings.update")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert detail["diff"]["webhook"]["url"] == ["", "https://h.example.com/x"]

    def test_repo_add_records_audit(self, tmp_path, monkeypatch):
        """添加仓库：repo.add 审计（name/project_id/url）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "repo.db"))
        stub = SimpleNamespace(
            resolve_project=lambda url: {
                "id": 42, "path": "group/project", "name": "project",
                "http_url_to_repo": "https://gitlab.example.com/group/project.git"},
            list_project_labels=lambda pid: [],
            create_project_label=lambda pid, name, color, description=None: {"name": name},
            unregister_webhook=lambda pid: 0,
        )
        monkeypatch.setattr(GitLabClient, "register_webhook",
                            lambda self, project_id, secret: {"id": 1})
        monkeypatch.setattr(GitLabClient, "list_project_labels",
                            lambda self, project_id: stub.list_project_labels(project_id))
        monkeypatch.setattr(GitLabClient, "create_project_label",
                            lambda self, project_id, name, color, description=None:
                                stub.create_project_label(project_id, name, color, description))
        monkeypatch.setattr(GitLabClient, "get_personal_access_token_self",
                            lambda self: {"expires_at": None})
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        r = tc.post("/api/repos", json={"url": "https://gitlab.example.com/group/project.git",
                                        "name": "项目"})
        assert r.status_code == 201
        rows, total = db.list_audit_logs(action="repo.add")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert detail["name"] == "项目" and detail["project_id"] == 42
        db.close()  # issue #395：显式释放 sqlite 连接

    def test_repo_delete_records_audit(self, tmp_path, monkeypatch):
        """删除仓库：repo.delete 审计（含删除前名称）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "repo2.db"))
        repo_id = db.upsert_repo(42, "项目A", "https://gitlab.example.com/group/a.git")
        stub = SimpleNamespace(unregister_webhook=lambda pid: 0)
        ctx = SimpleNamespace(config=config, db=db, gitlab=stub, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        r = tc.delete(f"/api/repos/{repo_id}")
        assert r.status_code == 200
        rows, total = db.list_audit_logs(action="repo.delete")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert detail["name"] == "项目A" and detail["project_id"] == 42
        assert rows[0]["target_id"] == str(repo_id)
        db.close()  # issue #395

    def test_task_retry_stop_dequeue_records_audit(self, tmp_path):
        """任务重试/停止/移出队列：task.retry / task.stop / task.delete 审计。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "tasks.db"))
        repo_id = db.upsert_repo(1, "demo", "https://gitlab.example.com/demo.git")
        # 失败任务可重试
        t_retry = db.create_task(repo_id, 1, 5, "失败任务")
        db.set_task_status(t_retry, "failed", error_message="boom")
        # 排队任务可停止；另一排队任务可移出队列
        t_stop = db.create_task(repo_id, 2, 6, "排队任务")
        t_dequeue = db.create_task(repo_id, 3, 7, "待移出任务")
        executor = SimpleNamespace(clear_stop_request=lambda tid: None,
                                   request_stop=lambda tid: None)
        scheduler = SimpleNamespace(enqueue=lambda tid: None,
                                    remove_queued=lambda tid: None)
        ctx = SimpleNamespace(config=config, db=db, executor=executor,
                              scheduler=scheduler, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)

        assert tc.post(f"/api/tasks/{t_retry}/retry").status_code == 200
        assert tc.post(f"/api/tasks/{t_stop}/stop").status_code == 200
        assert tc.post(f"/api/tasks/{t_dequeue}/dequeue").status_code == 200

        for action in ("task.retry", "task.stop", "task.delete"):
            rows, total = db.list_audit_logs(action=action)
            assert total == 1, action
            detail = json.loads(rows[0]["detail"])
            assert detail["issue_iid"] is not None
        db.close()  # issue #395

    def test_task_priority_records_audit(self, tmp_path):
        """排队任务人工优先级：task.priority 审计（含 action）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "tasks2.db"))
        repo_id = db.upsert_repo(1, "demo", "https://gitlab.example.com/demo.git")
        t = db.create_task(repo_id, 1, 5, "任务")
        ctx = SimpleNamespace(config=config, db=db,
                              executor=SimpleNamespace(),
                              scheduler=SimpleNamespace(),
                              config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        r = tc.post(f"/api/tasks/{t}/priority", params={"action": "top"})
        assert r.status_code == 200
        rows, total = db.list_audit_logs(action="task.priority")
        assert total == 1
        assert json.loads(rows[0]["detail"])["action"] == "top"
        db.close()  # issue #395

    def test_plugin_install_uninstall_records_audit(self, tmp_path):
        """插件安装/卸载：plugin.install / plugin.uninstall 审计。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "plg.db"))
        ctx = SimpleNamespace(config=config, db=db, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)

        # 先安装一个真实可加载的插件模块（临时 py 文件，与 test_api_plugins 同模式）
        mod = tmp_path / "my_plugin.py"
        mod.write_text(
            "from botler.plugins import register_plugin\n"
            "from botler.plugins.base import NotifierPlugin\n"
            "class MyNotifier(NotifierPlugin):\n"
            "    name = 'my-notifier'\n"
            "    description = '测试通知插件'\n"
            "    def send_test(self, context, repo_name='测试仓库'):\n"
            "        return {'ok': True}\n"
            "register_plugin(MyNotifier())\n", encoding="utf-8")
        r = tc.post("/api/plugins/install", json={"path": str(mod)})
        assert r.status_code == 200, r.text
        rows, total = db.list_audit_logs(action="plugin.install")
        assert total == 1
        assert rows[0]["target_id"] == str(mod)

        r = tc.post("/api/plugins/uninstall", json={"path": str(mod)})
        assert r.status_code == 200
        rows, total = db.list_audit_logs(action="plugin.uninstall")
        assert total == 1
        assert rows[0]["target_id"] == str(mod)
        db.close()  # issue #395

    def test_backup_create_records_audit(self, tmp_path, monkeypatch):
        """备份执行：backup.create 审计。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "bak.db"))
        backup = SimpleNamespace(create_backup=lambda trigger="manual": {"name": "x.tar.gz", "size": 1})
        ctx = SimpleNamespace(config=config, db=db, backup=backup,
                              config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        r = tc.post("/api/backups")
        assert r.status_code == 200
        rows, total = db.list_audit_logs(action="backup.create")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert detail == {"trigger": "manual"}
        assert rows[0]["target_id"] == "x.tar.gz"
        db.close()  # issue #395

    def test_config_external_edit_records_audit(self, tmp_path):
        """config.yaml 外部修改：config.external_edit + webhook 轮换标记。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "ext.db"))
        config.get()  # 先加载（生产环境 get() 高频调用，settings 恒已加载）
        config.set_external_change_callback(
            lambda old, new: db.add_audit_log(
                actor="external", action="config.external_edit",
                target_type="config",
                detail=config_diff_summary(old, new), ip=""))
        time.sleep(1.1)  # 保证 mtime 变化可被检测（秒级粒度）
        config_path.write_text(
            CONFIG_NO_SSO.replace("webhook_secret: test-secret",
                                  "webhook_secret: new-secret-1234567890")
            .replace("worker: {}", "worker:\n  max_retries: 9"),
            encoding="utf-8")
        config.get()  # 触发 mtime 检测 → 重载 + 回调
        rows, total = db.list_audit_logs(action="config.external_edit")
        assert total == 1
        detail = json.loads(rows[0]["detail"])
        assert "gitlab" in detail["changed_sections"] and "worker" in detail["changed_sections"]
        assert detail["webhook_secret_changed"] is True
        # 敏感字段打码：不落明文 secret
        masked = detail["diff"]["gitlab"]["webhook_secret"]
        assert "new-secret-1234567890" not in masked[1] and masked[1].startswith("***")
        assert detail["diff"]["worker"]["max_retries"] == [None, 9]
        assert rows[0]["actor"] == "external"
        # 再次 get 不重复触发
        config.get()
        rows, total = db.list_audit_logs(action="config.external_edit")
        assert total == 1
        db.close()  # issue #395

    def test_audit_failure_does_not_break_main(self, tmp_path, monkeypatch):
        """审计写入失败不影响主操作（与 webhook 推送同容错策略）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_NO_SSO, encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "f.db"))
        db.add_audit_log = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
        ctx = SimpleNamespace(config=config, db=db, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        # 设置保存正常成功，审计失败被吞掉
        r = tc.put("/api/settings", json={"worker": {"max_retries": 5}})
        assert r.status_code == 200
        assert r.json()["worker"]["max_retries"] == 5
        db.close()  # issue #395


# ---- 5. 配置层 ----

class TestAuditConfig:
    def test_admin_usernames_parse(self, tmp_path):
        """config.yaml 解析 audit_logs.admin_usernames。"""
        text = CONFIG_NO_SSO.replace("admin_usernames: []",
                                     "admin_usernames: ['zhangsan', ' lisi ', 'zhangsan']")
        p = tmp_path / "config.yaml"
        p.write_text(text, encoding="utf-8")
        config = ConfigManager(str(p))
        s = config.get()
        assert s.audit_admin_usernames == ["zhangsan", "lisi"]

    def test_admin_usernames_writeback_via_api(self, client):
        """PUT /api/settings 写回 admin_usernames（空白归一 + 去重）。"""
        tc, ctx, tmp_path = client
        r = tc.put("/api/settings", json={"audit_logs": {"admin_usernames": [" alice ", "alice", "bob"]}})
        assert r.status_code == 200
        assert r.json()["audit_logs"]["admin_usernames"] == ["alice", "bob"]
        # config.yaml 落盘（唯一事实来源）
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "admin_usernames" in text

    def test_validate_rejects_non_list(self, client):
        """admin_usernames 非字符串列表 → 400。"""
        tc, ctx, tmp_path = client
        assert tc.put("/api/settings", json={"audit_logs": {"admin_usernames": "alice"}}).status_code == 400
        assert tc.put("/api/settings", json={"audit_logs": {"admin_usernames": [1, 2]}}).status_code == 400

    def test_settings_api_exposes_admin_usernames(self, client):
        """GET /api/settings 返回 audit_logs.admin_usernames。"""
        tc, ctx, tmp_path = client
        tc.put("/api/settings", json={"audit_logs": {"admin_usernames": ["boss"]}})
        assert tc.get("/api/settings").json()["audit_logs"]["admin_usernames"] == ["boss"]
