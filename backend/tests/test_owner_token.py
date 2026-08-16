"""Owner GitLab Token（issue #87）测试：设置页配置的 owner token 专用于
编辑 issue（评论/标签），严禁用于 git 推送与流水线操作。

断言：
1. 配置/API：GET settings 返回 owner_token_masked；PUT gitlab.owner_token
   明文保存落盘、掩码值/空串保持现有、非字符串 400、${ENV} 引用展开；
2. owner-token-guide 端点返回教程文档，文档缺失 404；
3. executor：_call_with_fallback(prefer_owner=True) 编辑 issue 优先 owner
   token；owner 401/403 回退原链路；默认（非编辑）调用不受影响；
4. executor：_build_env 注入 GITLAB_TOKEN 绝不注入 owner token（issue
   #130：agent 无论如何都不能使用 owner token，只能用自己仓库的认证
   token / 全局 bot token）；_askpass_script（git 推送凭据）始终用 bot
   token 不含 owner token（严禁推送代码）；
5. executor：成功/失败收尾的评论与标签调用传 prefer_owner=True，而查询
   提交/检查最后作者等非编辑调用保持原链路；
6. reconciler：终态标签补打（编辑 issue）优先 owner token，owner 失效
   回退原链路。
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient, GitLabError
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

# executor 测试用：gitlab 段已配置 owner token
CONFIG_TEXT_WITH_OWNER = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  owner_token: owner-token-1
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""


# ---- 配置与设置 API ----

@pytest.fixture
def client(tmp_path):
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path


class TestOwnerTokenSettingsAPI:
    def test_get_settings_owner_token_masked_empty(self, client):
        """未配置 owner token 时返回空掩码。"""
        tc, _ = client
        data = tc.get("/api/settings").json()["gitlab"]
        assert data["owner_token_masked"] == ""

    def test_put_owner_token_persists_and_masks(self, client):
        """PUT gitlab.owner_token 明文保存落盘，GET 只返回掩码。"""
        tc, tmp_path = client
        resp = tc.put("/api/settings",
                      json={"gitlab": {"owner_token": "glpat-owner-1234567890"}})
        assert resp.status_code == 200, resp.text
        gitlab = resp.json()["gitlab"]
        assert gitlab["owner_token_masked"].startswith("glpa")
        assert gitlab["owner_token_masked"].endswith("7890")
        assert "glpat-owner-1234567890" not in gitlab["owner_token_masked"]
        # 落盘 config.yaml 含明文（凭据唯一事实来源是 config.yaml）
        assert "glpat-owner-1234567890" in (tmp_path / "config.yaml").read_text(
            encoding="utf-8")

    def test_put_masked_owner_token_not_overwritten(self, client):
        """回传掩码值（含 *）视为未修改，不覆盖真实凭据（与 sso.client_secret 同模式）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"gitlab": {"owner_token": "glpat-real-1"}})
        masked = tc.get("/api/settings").json()["gitlab"]["owner_token_masked"]
        resp = tc.put("/api/settings", json={"gitlab": {"owner_token": masked}})
        assert resp.status_code == 200
        assert "glpat-real-1" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_put_blank_owner_token_keeps_existing(self, client):
        """空串 = 保持现有凭据（与 SSO secret 一致）。"""
        tc, tmp_path = client
        tc.put("/api/settings", json={"gitlab": {"owner_token": "glpat-real-2"}})
        tc.put("/api/settings", json={"gitlab": {"owner_token": ""}})
        assert "glpat-real-2" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_put_owner_token_rejects_non_string(self, client):
        tc, _ = client
        resp = tc.put("/api/settings", json={"gitlab": {"owner_token": 123}})
        assert resp.status_code == 400

    def test_env_ref_owner_token_expanded_on_read(self, client):
        """config.yaml 中 owner_token 支持 ${ENV} 引用（凭据不落明文）。"""
        tc, tmp_path = client
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "webhook_secret: test-secret",
                "webhook_secret: test-secret\n  owner_token: ${BOTLER_TEST_OWNER_TOKEN}"),
            encoding="utf-8")
        os.environ["BOTLER_TEST_OWNER_TOKEN"] = "glpat-from-env"
        try:
            masked = tc.get("/api/settings").json()["gitlab"]["owner_token_masked"]
            assert masked.endswith("-env")
        finally:
            os.environ.pop("BOTLER_TEST_OWNER_TOKEN", None)

    def test_owner_token_guide_endpoint(self, client):
        """GET /api/settings/owner-token-guide 返回 docs/ 教程文档。"""
        tc, _ = client
        resp = tc.get("/api/settings/owner-token-guide")
        assert resp.status_code == 200, resp.text
        assert "GitLab" in resp.json()["content"]

    def test_owner_token_guide_404_when_doc_missing(self, client, monkeypatch):
        """教程文档缺失时 404，前端降级提示不阻塞设置页。"""
        tc, _ = client
        import botler.api.settings as settings_mod
        monkeypatch.setattr(
            settings_mod, "OWNER_TOKEN_GUIDE_PATH",
            Path("/nonexistent/owner-token-guide.md"))
        resp = tc.get("/api/settings/owner-token-guide")
        assert resp.status_code == 404


# ---- executor：编辑 issue 优先 owner token ----

@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT_WITH_OWNER, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


_REPO = {"name": "demo", "prompt_template": None}


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "测试任务")


class TestExecutorOwnerToken:
    def test_issue_edit_prefers_owner_token(self, executor):
        """prefer_owner=True 且已配置 owner token：call 收到 owner client。"""
        seen = []

        def call(client):
            seen.append(client.token)
            return "ok"

        result, used = executor._call_with_fallback(_REPO, call, prefer_owner=True)
        assert result == "ok"
        assert seen == ["owner-token-1"], f"编辑 issue 应优先 owner token（实际 {seen}）"
        assert used.token == "owner-token-1"

    def test_issue_edit_owner_401_falls_back_to_global(self, executor):
        """owner token 失效（401）：回退原链路（全局 bot token）重试。"""
        seen = []

        def call(client):
            if client.token == "owner-token-1":
                raise GitLabError("owner token 失效", 401)
            seen.append(client.token)
            return "global-ok"

        result, used = executor._call_with_fallback(_REPO, call, prefer_owner=True)
        assert result == "global-ok"
        assert seen == ["test-token"], f"owner 401 后应回退全局 token（实际 {seen}）"

    def test_issue_edit_owner_403_falls_back_to_global(self, executor):
        """owner token 权限不足（403）：同样回退原链路。"""
        seen = []

        def call(client):
            if client.token == "owner-token-1":
                raise GitLabError("owner 权限不足", 403)
            seen.append(client.token)
            return "global-ok"

        result, _ = executor._call_with_fallback(_REPO, call, prefer_owner=True)
        assert result == "global-ok"
        assert seen == ["test-token"]

    def test_default_call_ignores_owner_token(self, executor):
        """默认（非编辑）调用不传 prefer_owner：即使配置了 owner token 也不用。"""
        seen = []

        def call(client):
            seen.append(client.token)
            return "ok"

        result, _ = executor._call_with_fallback(_REPO, call)
        assert result == "ok"
        assert seen == ["test-token"], "非编辑调用不应使用 owner token"

    def test_prefer_owner_without_token_configured(self, executor, monkeypatch):
        """未配置 owner token 时 prefer_owner=True 行为同普通调用。"""
        executor.config.get()  # 先加载 _data
        executor.config._data["gitlab"]["owner_token"] = ""
        executor.config.settings = executor.config._to_settings(executor.config._data)
        seen = []

        def call(client):
            seen.append(client.token)
            return "ok"

        result, _ = executor._call_with_fallback(_REPO, call, prefer_owner=True)
        assert result == "ok"
        assert seen == ["test-token"]

    def test_build_env_never_injects_owner_token(self, executor, monkeypatch):
        """issue #130：agent 会话绝不注入 owner token（owner token 只允许
        在概览页 issue 编辑操作时由平台使用），配置了 owner token 时
        GITLAB_TOKEN 仍用 remote/全局 bot token。"""
        monkeypatch.setattr(executor, "_task_gitlab_token", lambda repo: None)
        env = executor._build_env(_REPO, {"project_id": 42, "iid": 1})
        assert env["GITLAB_TOKEN"] == "test-token"
        assert "owner-token-1" not in env["GITLAB_TOKEN"]

    def test_build_env_falls_back_to_remote_then_global(self, executor, monkeypatch):
        """未配置 owner token 时保持既有优先级：remote token → 全局 bot token。"""
        executor.config.get()  # 先加载 _data
        executor.config._data["gitlab"]["owner_token"] = ""
        executor.config.settings = executor.config._to_settings(executor.config._data)
        monkeypatch.setattr(executor, "_task_gitlab_token", lambda repo: "remote-token")
        env = executor._build_env(_REPO, {"project_id": 42, "iid": 1})
        assert env["GITLAB_TOKEN"] == "remote-token"
        monkeypatch.setattr(executor, "_task_gitlab_token", lambda repo: None)
        env = executor._build_env(_REPO, {"project_id": 42, "iid": 1})
        assert env["GITLAB_TOKEN"] == "test-token"

    def test_askpass_uses_bot_token_not_owner(self, executor, tmp_path):
        """严禁推送代码：git 推送凭据（GIT_ASKPASS）始终用 bot token。"""
        Path(executor.workspace_root).mkdir(parents=True, exist_ok=True)
        script = executor._askpass_script("demo")
        content = script.read_text(encoding="utf-8")
        assert "test-token" in content, "askpass 应注入 bot token"
        assert "owner-token-1" not in content, "owner token 严禁用于 git 推送"

    def test_finish_succeeded_issue_edits_prefer_owner(self, executor, tmp_path, monkeypatch):
        """成功收尾：打 bot-done 与写报告评论 prefer owner；查询提交/检查
        最后作者等非编辑调用保持原链路。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = _mk_task(db, repo_id)
        db.claim_task(task_id)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        calls = []
        current_prefer = {"value": False}

        class FakeClient:
            token = "owner-token-1"

            def _rec(self, name):
                calls.append((name, current_prefer["value"]))

            def add_labels(self, pid, iid, labels, remove=None):
                self._rec("labels")

            def add_comment(self, pid, iid, body):
                self._rec("comment")

            def last_note_author_id(self, pid, iid):
                self._rec("last_author")
                return None

            def find_commit_for_issue(self, pid, iid):
                self._rec("commit")
                return None

            def get_bot_id(self):
                return 99

        def fake_cfw(repo, call, prefer_owner=False):
            current_prefer["value"] = prefer_owner
            result = call(FakeClient())
            return result, FakeClient()

        monkeypatch.setattr(executor, "_call_with_fallback", fake_cfw)
        executor._finish_succeeded(task_id, "ok", repo=_REPO)

        by_method: dict[str, list[bool]] = {}
        for name, prefer in calls:
            by_method.setdefault(name, []).append(prefer)
        assert by_method.get("labels") == [True], "打 bot-done 标签应 prefer owner"
        assert by_method.get("comment") == [True], "写完成报告评论应 prefer owner"
        assert by_method.get("commit") == [False], "查询提交应保持原链路"
        assert by_method.get("last_author") == [False], "检查最后评论作者应保持原链路"

    def test_finish_failed_issue_edits_prefer_owner(self, executor, tmp_path, monkeypatch):
        """失败收尾：失败评论与 bot-failed 标签同样 prefer owner。"""
        db = executor.db
        repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = _mk_task(db, repo_id)
        db.claim_task(task_id)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"

        calls = []
        current_prefer = {"value": False}

        class FakeClient:
            token = "owner-token-1"

            def _rec(self, name):
                calls.append((name, current_prefer["value"]))

            def add_labels(self, pid, iid, labels, remove=None):
                self._rec("labels")

            def add_comment(self, pid, iid, body):
                self._rec("comment")

        def fake_cfw(repo, call, prefer_owner=False):
            current_prefer["value"] = prefer_owner
            result = call(FakeClient())
            return result, FakeClient()

        monkeypatch.setattr(executor, "_call_with_fallback", fake_cfw)
        executor._finish_failed(task_id, "无法解决", output="失败输出")

        by_method: dict[str, list[bool]] = {}
        for name, prefer in calls:
            by_method.setdefault(name, []).append(prefer)
        assert by_method.get("comment") == [True], "失败评论应 prefer owner"
        assert by_method.get("labels") == [True], "打 bot-failed 标签应 prefer owner"


# ---- reconciler：终态标签补打（编辑 issue）优先 owner token ----

@pytest.fixture
def ctx(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    # 对账桩：bot 身份固定、open issues 为空、补打标签记录（对齐 test_reconciler 桩）
    stub = SimpleNamespace(
        labels_added=[],
        get_bot_id=lambda: 99,
        get_user_id_by_username=lambda username: None,
        list_open_issues=lambda project_id, assignee_id=None: [],
        last_note_author_id=lambda project_id, iid: None,
        get_issue=lambda project_id, iid: {"state": "opened", "labels": []},
        add_labels=lambda project_id, iid, labels, remove=None:
            stub.labels_added.append((project_id, iid, labels)) or {},
    )
    scheduler = SimpleNamespace(enqueue=lambda task_id: True)
    reconciler = Reconciler(config, db, stub, scheduler)
    return SimpleNamespace(config=config, db=db, gitlab=stub, reconciler=reconciler)


def _mk_terminal_task(db, repo_id: int, issue_iid: int, status: str) -> int:
    task_id = db.create_task(repo_id, 42, issue_iid, f"终态任务 {issue_iid}")
    db.set_task_status(task_id, status)
    return task_id


class TestReconcilerOwnerToken:
    def test_call_prefer_owner_uses_owner_client(self, ctx, monkeypatch):
        """prefer_owner=True 时先尝试 owner client，失败（401）回退原 client。"""
        seen = []

        def call(client):
            seen.append(getattr(client, "token", None))
            return "ok"

        owner = SimpleNamespace(token="owner-token-1")
        monkeypatch.setattr(ctx.reconciler, "_owner_client",
                            lambda verify_ssl: owner)
        result, used = ctx.reconciler._call_with_fallback(
            {"name": "demo"}, False, ctx.gitlab, call, prefer_owner=True)
        assert result == "ok"
        assert seen == ["owner-token-1"], f"编辑 issue 应优先 owner（实际 {seen}）"
        assert used is owner

    def test_call_prefer_owner_401_falls_back(self, ctx, monkeypatch):
        """owner 401 时回退原 client 重试。"""
        seen = []

        def call(client):
            if getattr(client, "token", None) == "owner-token-1":
                raise GitLabError("owner 失效", 401)
            seen.append("stub")
            return "stub-ok"

        owner = SimpleNamespace(token="owner-token-1")
        monkeypatch.setattr(ctx.reconciler, "_owner_client",
                            lambda verify_ssl: owner)
        result, used = ctx.reconciler._call_with_fallback(
            {"name": "demo"}, False, ctx.gitlab, call, prefer_owner=True)
        assert result == "stub-ok"
        assert seen == ["stub"], f"owner 401 后应回退原 client（实际 {seen}）"
        assert used is ctx.gitlab

    def test_backfill_labels_prefer_owner(self, ctx, monkeypatch):
        """终态标签补打（编辑 issue）走 owner client。"""
        repo_id = ctx.db.upsert_repo(42, "demo", "https://gitlab.example.com/demo.git")
        _mk_terminal_task(ctx.db, repo_id, 1, "succeeded")

        class FakeOwner:
            def __init__(self):
                self.labels_added = []

            def add_labels(self, pid, iid, labels, remove=None):
                self.labels_added.append((pid, iid, labels))
                return {}

        owner = FakeOwner()
        monkeypatch.setattr(ctx.reconciler, "_owner_client",
                            lambda verify_ssl: owner)
        ctx.reconciler.reconcile_once(repo_id=repo_id)
        assert (42, 1, ["bot-done"]) in owner.labels_added, (
            f"补打标签应走 owner client（实际 {owner.labels_added}）")

    def test_backfill_owner_401_falls_back_to_stub(self, ctx, monkeypatch):
        """owner token 失效时补打回退原链路，不影响对账主流程。"""
        repo_id = ctx.db.upsert_repo(42, "demo", "https://gitlab.example.com/demo.git")
        _mk_terminal_task(ctx.db, repo_id, 2, "failed")

        class FakeOwner:
            def __init__(self):
                self.labels_added = []

            def add_labels(self, pid, iid, labels, remove=None):
                raise GitLabError("owner 失效", 401)

        monkeypatch.setattr(ctx.reconciler, "_owner_client",
                            lambda verify_ssl: FakeOwner())
        ctx.reconciler.reconcile_once(repo_id=repo_id)
        assert (42, 2, ["bot-failed"]) in ctx.gitlab.labels_added
