"""灵感 API 测试（issue #131）：概览页「灵感」板块后端接口。

需求：概览页在「开放 Issue」下方、「CI/CD 流水线」上方增加灵感板块，
用户按仓库随手记录新功能灵感；灵感默认仅保存在 Botler 本地 SQLite
数据库（issue #131），issue #143 起支持一键将灵感提交为 GitLab issue
（灵感内容作为标题与描述，默认标签 feature + ui，走 owner token）。

覆盖：
- GET  /api/inspirations/overview：聚合所有未软删除仓库（priority 升序、
  同优先级按 id）+ 各自灵感（updated_at 降序）；无灵感的仓库也返回
  （前端空状态 + 添加表单）；
- POST /api/inspirations：创建（repo_id + content 必填）；
- PUT  /api/inspirations/{id}：更新内容并刷新 updated_at；
- DELETE /api/inspirations/{id}：删除；
- POST /api/inspirations/{id}/add-issue（issue #143）：一键提交为
  GitLab issue——正常路径 / 灵感不存在 404 / 仓库未启用 400 /
  仓库软删除 400 / GitLab 故障 502 / 未配置 owner token 400 /
  创建成功后清空概览缓存；
- 边界：仓库不存在 / 软删除仓库 / 空内容 / 纯空白 / 超长内容 /
  记录不存在（404）。
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError

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


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    # issue #143：ctx.gitlab 桩供「灵感一键提交 issue」测试使用
    # （add-issue 走 owner client，桩经 edit_env 重定向；概览聚合
    # 回退全局客户端时用到 ctx.gitlab）
    ctx = SimpleNamespace(config=config, db=db, gitlab=StubGitLab(),
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    # 清空 issues 模块级缓存（概览结果缓存 + owner client 缓存），
    # 避免用例互相污染（与 test_api_issues.py 的 api_app 一致）
    from botler.api import issues as issues_mod
    issues_mod.clear_issue_cache()
    return app, db


@pytest.fixture
def client(api_app):
    app, db = api_app
    return TestClient(app), db


def _add_repo(db, project_id, name, priority=100, enabled=True,
             remote_username=None):
    """便捷：插入一个仓库并返回本地 id。remote_username 为仓库用户
    （issue #153：remote url userinfo 用户名）。"""
    return db.upsert_repo(project_id, name, f"https://gitlab.example.com/{name}.git",
                          enabled=enabled, priority=priority,
                          remote_username=remote_username)


class TestOverview:
    """GET /api/inspirations/overview。"""

    def test_empty_no_repos(self, client):
        tc, db = client
        r = tc.get("/api/inspirations/overview")
        assert r.status_code == 200
        assert r.json() == {"repos": []}

    def test_repos_sorted_by_priority_then_id(self, client):
        tc, db = client
        b = _add_repo(db, 1, "beta", priority=200)
        a = _add_repo(db, 2, "alpha", priority=100)
        c = _add_repo(db, 3, "gamma", priority=100)
        r = tc.get("/api/inspirations/overview")
        repos = r.json()["repos"]
        assert [x["repo_name"] for x in repos] == ["alpha", "gamma", "beta"]
        # 无灵感仓库也返回（前端展示空状态 + 添加表单）
        assert [x["inspirations"] for x in repos] == [[], [], []]
        assert repos[0]["enabled"] is True
        assert repos[0]["priority"] == 100

    def test_skips_soft_deleted_repo(self, client):
        tc, db = client
        _add_repo(db, 1, "alive")
        gone = _add_repo(db, 2, "gone")
        db.soft_delete_repo(gone)
        r = tc.get("/api/inspirations/overview")
        names = [x["repo_name"] for x in r.json()["repos"]]
        assert names == ["alive"]


    def test_includes_disabled_repo_by_default(self, client):
        """默认（show_disabled_repos=true）：未启用仓库照常返回，enabled=false 透传。"""
        tc, db = client
        _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        r = tc.get("/api/inspirations/overview")
        repos = r.json()["repos"]
        assert [x["repo_name"] for x in repos] == ["enabled-repo", "disabled-repo"]
        assert repos[1]["enabled"] is False

    def test_hides_disabled_repo_when_setting_off(self, api_app):
        """ui.show_disabled_repos=false：未启用仓库从灵感聚合中过滤（issue #142）。"""
        app, db = api_app
        _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        app.state.ctx.config.update_ui({"show_disabled_repos": False})
        r = TestClient(app).get("/api/inspirations/overview")
        assert [x["repo_name"] for x in r.json()["repos"]] == ["enabled-repo"]

    def test_hides_disabled_repo_but_keeps_its_inspirations_data(self, api_app):
        """设置关闭时已启用仓库的灵感列表不受影响（issue #142 边界）。"""
        app, db = api_app
        repo = _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        db.create_inspiration(repo, "只属于启用仓库的灵感")
        app.state.ctx.config.update_ui({"show_disabled_repos": False})
        r = TestClient(app).get("/api/inspirations/overview")
        repos = r.json()["repos"]
        assert [x["repo_name"] for x in repos] == ["enabled-repo"]
        assert repos[0]["inspirations"][0]["content"] == "只属于启用仓库的灵感"

    def test_inspirations_ordered_by_updated_at_desc(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "灵感一")
        id2 = db.create_inspiration(repo, "灵感二")
        # 更新较早的记录 → 其 updated_at 最新，应排最前
        time.sleep(1.1)
        db.update_inspiration(id1, "灵感一（更新）")
        r = tc.get("/api/inspirations/overview")
        items = r.json()["repos"][0]["inspirations"]
        assert [x["id"] for x in items] == [id1, id2]
        assert items[0]["content"] == "灵感一（更新）"
        assert items[0]["repo_name"] == "botler"

    def test_inspiration_row_fields(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, " 记录一段灵感 ")
        r = tc.get("/api/inspirations/overview")
        item = r.json()["repos"][0]["inspirations"][0]
        assert item["id"] == insp_id
        assert item["repo_id"] == repo
        assert item["content"] == " 记录一段灵感 "
        assert item["created_at"]
        assert item["updated_at"] == item["created_at"]


class TestCreate:
    """POST /api/inspirations。"""

    def test_create_success(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations", json={"repo_id": repo, "content": "支持批量处理 issue"})
        assert r.status_code == 201
        body = r.json()
        assert body["repo_id"] == repo
        assert body["repo_name"] == "botler"
        assert body["content"] == "支持批量处理 issue"
        assert body["id"] > 0
        # 创建后 overview 可见
        items = tc.get("/api/inspirations/overview").json()["repos"][0]["inspirations"]
        assert len(items) == 1

    def test_create_strips_whitespace(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations", json={"repo_id": repo, "content": "  随手记录  "})
        assert r.status_code == 201
        assert r.json()["content"] == "随手记录"

    def test_create_repo_not_found(self, client):
        tc, db = client
        r = tc.post("/api/inspirations", json={"repo_id": 999, "content": "内容"})
        assert r.status_code == 400
        assert "仓库不存在" in r.json()["detail"]

    def test_create_repo_soft_deleted(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "gone")
        db.soft_delete_repo(repo)
        r = tc.post("/api/inspirations", json={"repo_id": repo, "content": "内容"})
        assert r.status_code == 400
        assert "仓库不存在" in r.json()["detail"]

    def test_create_missing_repo_id(self, client):
        tc, db = client
        r = tc.post("/api/inspirations", json={"content": "内容"})
        assert r.status_code == 422

    def test_create_empty_content(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        for bad in ("", "   "):
            r = tc.post("/api/inspirations", json={"repo_id": repo, "content": bad})
            assert r.status_code == 400, f"内容 {bad!r} 应被拒绝"
            assert "不能为空" in r.json()["detail"]

    def test_create_content_too_long(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations",
                    json={"repo_id": repo, "content": "长" * 5001})
        assert r.status_code == 400
        assert "不能超过" in r.json()["detail"]

    def test_create_content_boundary_5000(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations",
                    json={"repo_id": repo, "content": "长" * 5000})
        assert r.status_code == 201
        assert len(r.json()["content"]) == 5000


class TestUpdate:
    """PUT /api/inspirations/{id}。"""

    def test_update_success(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "旧内容")
        r = tc.put(f"/api/inspirations/{insp_id}", json={"content": "新内容"})
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "新内容"
        assert body["repo_id"] == repo
        assert body["repo_name"] == "botler"

    def test_update_not_found(self, client):
        tc, db = client
        r = tc.put("/api/inspirations/999", json={"content": "内容"})
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_update_empty_content(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "旧内容")
        for bad in ("", "   "):
            r = tc.put(f"/api/inspirations/{insp_id}", json={"content": bad})
            assert r.status_code == 400
            assert "不能为空" in r.json()["detail"]

    def test_update_content_too_long(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "旧内容")
        r = tc.put(f"/api/inspirations/{insp_id}",
                   json={"content": "长" * 5001})
        assert r.status_code == 400
        assert "不能超过" in r.json()["detail"]


class TestDelete:
    """DELETE /api/inspirations/{id}。"""

    def test_delete_success(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "内容")
        r = tc.delete(f"/api/inspirations/{insp_id}")
        assert r.status_code == 204
        assert db.get_inspiration(insp_id) is None
        items = tc.get("/api/inspirations/overview").json()["repos"][0]["inspirations"]
        assert items == []

    def test_delete_not_found(self, client):
        tc, db = client
        r = tc.delete("/api/inspirations/999")
        assert r.status_code == 404

    def test_delete_then_recreate(self, client):
        """删除后同一仓库可再创建新灵感（无残留状态）。"""
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "内容")
        tc.delete(f"/api/inspirations/{insp_id}")
        r = tc.post("/api/inspirations", json={"repo_id": repo, "content": "新灵感"})
        assert r.status_code == 201
        assert r.json()["content"] == "新灵感"


# ---- issue #143：灵感一键提交为 GitLab issue ----

class StubGitLab:
    """灵感一键提交 issue 的 GitLab 桩（issue #143）：记录 create_issue
    调用参数，可故障注入；list_open_issues 供概览缓存失效断言复用。
    """

    def __init__(self):
        self.create_calls: list[tuple[int, dict]] = []
        self.fail_create_projects: set[int] = set()
        self.create_result: dict | None = None
        self.issues_by_project: dict[int, list[dict]] = {}
        self.calls: list[tuple[int, dict]] = []
        # issue #153：仓库用户 → 分配人解析桩
        self.members_by_project: dict[int, list[dict]] = {}
        self.users_by_username: dict[str, int] = {}
        self.fail_members_projects: set[int] = set()

    def create_issue(self, project_id, title, description=None,
                     assignee_id=None, labels=None):
        """创建 issue 桩：记录 (project_id, 参数)，可故障注入、可配置返回。"""
        self.create_calls.append((project_id, {
            "title": title, "description": description,
            "assignee_id": assignee_id, "labels": labels,
        }))
        if project_id in self.fail_create_projects:
            raise GitLabError("模拟创建 issue 故障")
        if self.create_result is not None:
            return self.create_result
        return {"iid": 99, "title": title, "state": "opened",
                "web_url": "https://gitlab.example.com/x/-/issues/99",
                "labels": labels or [], "updated_at": None,
                "created_at": "2026-08-15T10:00:00.000+08:00",
                "description": description, "author": None,
                "milestone": None, "assignees": [], "user_notes_count": 0}

    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        """开放 issue 查询桩（概览聚合用）：记录调用，按 project_id 配置。"""
        self.calls.append((project_id, {
            "assignee_id": assignee_id, "scope": scope,
            "order_by": order_by, "sort": sort, "limit": limit,
        }))
        items = list(self.issues_by_project.get(project_id, []))
        return items[:limit] if limit is not None else items

    def list_project_labels(self, project_id):
        """项目标签桩：概览标签色映射查询，返回空列表即可。"""
        return []

    def list_project_members(self, project_id):
        """项目成员桩（issue #153）：按 project_id 配置返回，缺省空列表；
        fail_members_projects 中的项目抛 GitLabError（模拟成员接口故障）。"""
        if project_id in self.fail_members_projects:
            raise GitLabError("模拟成员接口故障")
        return list(self.members_by_project.get(project_id, []))

    def get_user_id_by_username(self, username):
        """按用户名查用户 id 桩（issue #153）：缺省查不到返回 None。"""
        return self.users_by_username.get(username)


@pytest.fixture
def edit_env(client, monkeypatch):
    """灵感一键提交 issue 测试夹具（issue #143）：已配置 owner token，
    且 issues 模块的 GitLabClient 构造重定向到 ctx.gitlab 桩——提交
    issue 必须使用 owner token（与 test_api_issues.py::client_edit
    同思路，概览页写操作绝不回退 bot token）。"""
    tc, db = client
    tc.app.state.ctx.config.update_gitlab({"owner_token": "owner-token-1"})
    from botler.api import issues as issues_mod
    monkeypatch.setattr(
        issues_mod, "GitLabClient",
        lambda url, token, verify_ssl=True, webhook_base_url=None: tc.app.state.ctx.gitlab)
    return tc, tc.app.state.ctx.gitlab, db


class TestAddIssueFromInspiration:
    """POST /api/inspirations/{id}/add-issue（issue #143）：把灵感一键
    提交为 GitLab issue——灵感内容同时作为标题与描述，默认标签
    feature + ui，不指定分配人；写操作走 owner token。"""

    def test_success(self, edit_env):
        """正常路径：create_issue 传参正确（标题=描述=灵感内容去首尾
        空白，labels 默认 feature+ui，assignee 不传），返回 201 与精简
        issue 对象（含 iid/web_url 供前端提示）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")
        insp_id = db.create_inspiration(repo, " 灵感内容 ")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls == [(42, {
            "title": "灵感内容", "description": "灵感内容",
            "assignee_id": None, "labels": ["feature", "ui"],
        })]
        issue = resp.json()
        assert issue["iid"] == 99
        assert issue["title"] == "灵感内容"
        assert issue["state"] == "opened"
        assert issue["web_url"].startswith("https://")

    def test_multiline_content_preserved_in_description(self, edit_env):
        """边界：多行灵感——标题/描述保留内部换行，仅去首尾空白。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")
        insp_id = db.create_inspiration(
            repo, "支持批量处理 issue\n第二行：默认标签 feature 与 ui")
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 201
        title, desc = stub.create_calls[0][1]["title"], stub.create_calls[0][1]["description"]
        assert title == "支持批量处理 issue\n第二行：默认标签 feature 与 ui"
        assert desc == title

    def test_inspiration_not_found(self, edit_env):
        """边界：灵感不存在 → 404，不发起 GitLab 调用。"""
        tc, stub, db = edit_env
        resp = tc.post("/api/inspirations/999/add-issue")
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_repo_disabled(self, edit_env):
        """边界：灵感所属仓库未启用 → 400（与概览页添加 issue 弹窗一致）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="off", enabled=False)
        insp_id = db.create_inspiration(repo, "灵感")
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 400
        assert "未启用" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_repo_soft_deleted(self, edit_env):
        """边界：灵感所属仓库已软删除 → 400（不允许向已删除仓库提交）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="gone")
        insp_id = db.create_inspiration(repo, "灵感")
        db.soft_delete_repo(repo)
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 400
        assert "仓库不存在" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_gitlab_failure_returns_502(self, edit_env):
        """GitLab 创建失败 → 502，错误信息透出。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")
        insp_id = db.create_inspiration(repo, "灵感")
        stub.fail_create_projects = {42}
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 502
        assert "创建 issue 失败" in resp.json()["detail"]

    def test_without_owner_token_blocked(self, client):
        """边界：未配置 owner token → 400 拦截（概览页写操作绝不回退
        bot token，与添加 issue 弹窗行为一致）。"""
        tc, db = client
        repo = _add_repo(db, project_id=42, name="botler")
        insp_id = db.create_inspiration(repo, "灵感")
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 400
        assert "owner token" in resp.json()["detail"]

    def test_invalidates_overview_cache(self, edit_env):
        """创建成功后清空概览缓存：下一次 overview 请求重新拉取
        （前端创建成功立即刷新列表，不能拿到 10 秒 TTL 旧缓存）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")
        stub.issues_by_project = {42: [{
            "iid": 1, "title": "旧 issue", "state": "opened",
            "updated_at": None, "created_at": None,
            "web_url": "https://gitlab.example.com/x/-/issues/1",
            "description": None, "labels": [], "author": None,
            "milestone": None, "assignees": [], "user_notes_count": 0,
        }]}
        insp_id = db.create_inspiration(repo, "灵感")

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 1  # 首次拉取

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 201

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 2  # 缓存已失效，重新拉取

# ---- issue #153：灵感提交 issue 分配人 = 仓库用户（remote url 用户名） ----

class TestAddIssueFromInspirationAssignee:
    """POST /api/inspirations/{id}/add-issue 的分配人行为（issue #153）。

    仓库设置页读取 remote url 得到的仓库用户（如 agent）作为灵感提交
    issue 时的默认分配人：后端按用户名在项目成员里解析为 GitLab 用户
    id 传入 create_issue；未配置 / 解析不到 / 成员接口故障时保持原行为
    （不指定分配人），不阻塞 issue 创建。
    """

    def test_remote_username_sets_assignee(self, edit_env):
        """仓库配置了 remote_username：按项目成员解析为用户 id 传入 create_issue。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="agent")
        stub.members_by_project = {42: [
            {"user_id": 7, "username": "agent", "name": "Agent"},
            {"user_id": 8, "username": "other", "name": "Other"},
        ]}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] == 7
        assert stub.create_calls[0][1]["labels"] == ["feature", "ui"]

    def test_member_without_user_id_resolved_via_users(self, edit_env):
        """边界：成员项缺 user_id（GitLab 19 实测）时按用户名查 /users 补齐。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="agent")
        stub.members_by_project = {42: [
            {"user_id": None, "username": "agent", "name": "Agent"},
        ]}
        stub.users_by_username = {"agent": 99}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] == 99

    def test_username_not_in_members_falls_back_to_users(self, edit_env):
        """边界：仓库用户不在项目成员列表（成员接口权限范围外）→ 查 /users 兜底。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="agent")
        stub.members_by_project = {42: [
            {"user_id": 8, "username": "other", "name": "Other"},
        ]}
        stub.users_by_username = {"agent": 11}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] == 11

    def test_unresolvable_username_skips_assignee(self, edit_env):
        """边界：仓库用户查不到（已删除/非成员）→ 不指定分配人，仍创建成功。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="ghost")
        stub.members_by_project = {42: []}
        stub.users_by_username = {}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None

    def test_member_list_failure_degrades_to_no_assignee(self, edit_env):
        """边界：成员接口故障（GitLab 异常）→ 降级不指定分配人，不阻塞创建。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="agent")
        stub.fail_members_projects = {42}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None

    def test_without_remote_username_no_assignee(self, edit_env):
        """边界：仓库未配置仓库用户 → 不指定分配人（保持 issue #143 原行为）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username=None)
        stub.members_by_project = {42: [
            {"user_id": 7, "username": "agent", "name": "Agent"},
        ]}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None

    def test_empty_username_ignored(self, edit_env):
        """边界：remote_username 为空白串 → 不指定分配人。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username="   ")
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None
