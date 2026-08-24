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
- POST /api/inspirations/{id}/add-issue（issue #143/#162）：一键提交为
  GitLab issue——正常路径 / 灵感不存在 404 / 仓库未启用 400 /
  仓库软删除 400 / GitLab 故障 502 / 未配置 owner token 400 /
  创建成功后清空概览缓存并删除该灵感（issue #162），失败场景
  （GitLab 故障 / 未配置 owner token / 仓库禁用）灵感保留可重试；
- 边界：仓库不存在 / 软删除仓库 / 空内容 / 纯空白 / 超长内容 /
  记录不存在（404）。
"""

import time
from types import SimpleNamespace

import httpx

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
             remote_username=None, url=None, local_path=None,
             remote_name=None):
    """便捷：插入一个仓库并返回本地 id。remote_username 为仓库用户
    （issue #153：remote url userinfo 用户名）；url / local_path /
    remote_name 可覆盖默认值（issue #159：运行时读取 remote url 兜底
    场景需要构造带 local_path 与内嵌凭据 remote 的仓库）。"""
    return db.upsert_repo(
        project_id, name,
        url or f"https://gitlab.example.com/{name}.git",
        enabled=enabled, priority=priority, remote_username=remote_username,
        local_path=local_path, remote_name=remote_name)


class TestOverview:
    """GET /api/inspirations/overview。"""

    def test_empty_no_repos(self, client):
        tc, db = client
        r = tc.get("/api/inspirations/overview?limit=100")
        assert r.status_code == 200
        assert r.json() == {"repos": []}

    def test_repos_sorted_by_priority_then_id(self, client):
        tc, db = client
        _add_repo(db, 1, "beta", priority=200)
        _add_repo(db, 2, "alpha", priority=100)
        _add_repo(db, 3, "gamma", priority=100)
        r = tc.get("/api/inspirations/overview?limit=100")
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
        r = tc.get("/api/inspirations/overview?limit=100")
        names = [x["repo_name"] for x in r.json()["repos"]]
        assert names == ["alive"]


    def test_includes_disabled_repo_by_default(self, client):
        """默认（show_disabled_repos=true）：未启用仓库照常返回，enabled=false 透传。"""
        tc, db = client
        _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        r = tc.get("/api/inspirations/overview?limit=100")
        repos = r.json()["repos"]
        assert [x["repo_name"] for x in repos] == ["enabled-repo", "disabled-repo"]
        assert repos[1]["enabled"] is False

    def test_hides_disabled_repo_when_setting_off(self, api_app):
        """ui.show_disabled_repos=false：未启用仓库从灵感聚合中过滤（issue #142）。"""
        app, db = api_app
        _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        app.state.ctx.config.update_section("ui", {"show_disabled_repos": False})
        r = TestClient(app).get("/api/inspirations/overview?limit=100")
        assert [x["repo_name"] for x in r.json()["repos"]] == ["enabled-repo"]

    def test_hides_disabled_repo_but_keeps_its_inspirations_data(self, api_app):
        """设置关闭时已启用仓库的灵感列表不受影响（issue #142 边界）。"""
        app, db = api_app
        repo = _add_repo(db, 1, "enabled-repo", enabled=True)
        _add_repo(db, 2, "disabled-repo", enabled=False)
        db.create_inspiration(repo, "只属于启用仓库的灵感")
        app.state.ctx.config.update_section("ui", {"show_disabled_repos": False})
        r = TestClient(app).get("/api/inspirations/overview?limit=100")
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
        r = tc.get("/api/inspirations/overview?limit=100")
        items = r.json()["repos"][0]["inspirations"]
        assert [x["id"] for x in items] == [id1, id2]
        assert items[0]["content"] == "灵感一（更新）"
        assert items[0]["repo_name"] == "botler"

    def test_inspiration_row_fields(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, " 记录一段灵感 ")
        r = tc.get("/api/inspirations/overview?limit=100")
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
        items = tc.get("/api/inspirations/overview?limit=100").json()["repos"][0]["inspirations"]
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
        items = tc.get("/api/inspirations/overview?limit=100").json()["repos"][0]["inspirations"]
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
    tc.app.state.ctx.config.update_section("gitlab", {"owner_token": "owner-token-1"})
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
        # issue #162：创建成功后灵感从列表删除（已转为 GitLab issue，
        # 保留会误导重复提交）；overview 不再展示该条目
        assert db.get_inspiration(insp_id) is None
        items = tc.get("/api/inspirations/overview?limit=100").json()["repos"][0]["inspirations"]
        assert items == []

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

    def test_long_content_title_truncated_description_full(self, edit_env):
        """边界（issue #186）：灵感内容超过 GitLab issue 标题上限（255
        字符）时——标题截断到 255 字符以内（末尾带省略号标记），描述保留
        完整内容（GitLab 描述字段可容纳远超 255 字符，标题才是硬限制）。
        修复前：标题=描述=完整内容，GitLab 创建接口 400 拒绝
        （title is too long），超长灵感无法一键转 issue。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")
        # 约 25 字 × 20 = 500 字，远超 GitLab 标题 255 字符上限
        content = "这是一个超过 GitLab 标题上限的灵感内容。" * 20
        assert len(content) > 255
        insp_id = db.create_inspiration(repo, content)

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert len(stub.create_calls) == 1
        args = stub.create_calls[0][1]
        title, desc = args["title"], args["description"]
        # 标题不超 GitLab 硬上限（255 字符），且带省略号标记截断
        assert len(title) <= 255
        assert title.endswith("…")
        # 截断标题是完整内容的前缀（去掉省略号后）
        assert content.startswith(title[:-1])
        # 描述保留完整内容——GitLab 描述字段支持远超 255 字符
        assert desc == content
        assert args["labels"] == ["feature", "ui"]

    def test_title_truncation_boundary(self, edit_env):
        """边界（issue #186）：恰好 255 字符——标题保持完整不截断；
        256 字符——标题截断到 255 字符（含省略号标记）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler")

        # 恰好 255 字符：不截断，标题=内容
        exact = "界" * 255
        insp_id = db.create_inspiration(repo, exact)
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 201
        assert stub.create_calls[-1][1]["title"] == exact
        assert stub.create_calls[-1][1]["description"] == exact

        # 256 字符：截断为 254 字 + 省略号 = 255 字符，描述保留完整
        over = "超" * 256
        insp_id2 = db.create_inspiration(repo, over)
        resp = tc.post(f"/api/inspirations/{insp_id2}/add-issue")
        assert resp.status_code == 201
        args = stub.create_calls[-1][1]
        assert args["title"] == "超" * 254 + "…"
        assert len(args["title"]) == 255
        assert args["description"] == over

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
        # issue #162：未成功推送不删除，灵感保留可重试
        assert db.get_inspiration(insp_id) is not None

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
        # issue #162：GitLab 创建失败未成功推送，灵感保留可重试
        assert db.get_inspiration(insp_id) is not None

    def test_without_owner_token_blocked(self, client):
        """边界：未配置 owner token → 400 拦截（概览页写操作绝不回退
        bot token，与添加 issue 弹窗行为一致）。"""
        tc, db = client
        repo = _add_repo(db, project_id=42, name="botler")
        insp_id = db.create_inspiration(repo, "灵感")
        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert resp.status_code == 400
        assert "owner token" in resp.json()["detail"]
        # issue #162：未成功推送不删除，灵感保留可重试
        assert db.get_inspiration(insp_id) is not None

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


# ---- issue #159：仓库表 remote_username 为空时运行时读取 remote url 兜底 ----

class TestAddIssueFromInspirationRuntimeRemoteUser:
    """POST /api/inspirations/{id}/add-issue 的分配人兜底（issue #159）。

    存量仓库（issue #153 之前入库）remote_username 未落库为 NULL，仅靠
    设置页手动「重新读取 remote URL」体验不达预期。修复：提交时存储值
    为空则按仓库 remote url 运行时读取用户名（read_repo_remote_username），
    解析为 GitLab 用户 id 设为分配人并写回仓库表；读不到（URL 无凭据 /
    目录不可读 / 非 git 仓库）保持原行为（不指定分配人），不阻塞创建。
    """

    def test_missing_stored_username_reads_remote_at_runtime(
            self, edit_env, tmp_path, monkeypatch):
        """复现：remote_username 未落库，remote url 内嵌用户名 agent →
        运行时读取并设为分配人，且写回仓库表（设置页可见）。"""
        import subprocess
        # 成员解析客户端回退到桩（本地 git remote 的 host 是测试假地址，
        # 禁止真实网络请求——生产环境 per-repo client 指向真实 GitLab，
        # 语义一致：成员清单 + /users 查询都走同一客户端）
        from botler.api import pipelines
        monkeypatch.setattr(pipelines, "build_repo_client", lambda row, verify_ssl=True: None)
        repo_dir = tmp_path / "remote-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin",
             "https://agent:glpat-test@gitlab.example.com/botler.git"],
            cwd=repo_dir, check=True)
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler",
                         local_path=str(repo_dir))
        # 该实例 members/all 无 user_id（GitLab 19 实测），走 /users 兜底
        stub.members_by_project = {42: [
            {"user_id": None, "username": "agent", "name": "Agent"}]}
        stub.users_by_username = {"agent": 7}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] == 7
        # 运行时读取到的仓库用户已写回仓库表（设置页展示、后续直接命中）
        assert db.get_repo(repo)["remote_username"] == "agent"

    def test_runtime_read_failure_keeps_no_assignee(self, edit_env, monkeypatch):
        """边界：存量仓库 remote url 无凭据（读不到用户名）→ 不指定分配人，
        仍创建成功（与 issue #143 原行为一致），仓库表不被写入空值。"""
        from botler import git_remote
        monkeypatch.setattr(git_remote, "read_repo_remote_username",
                            lambda row: None)
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username=None)
        stub.members_by_project = {42: [
            {"user_id": 7, "username": "agent", "name": "Agent"}]}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None
        assert db.get_repo(repo)["remote_username"] is None

    def test_runtime_read_exception_keeps_no_assignee(self, edit_env, monkeypatch):
        """边界：运行时读取抛异常（目录不可读等）→ 降级不指定分配人，
        不阻塞 issue 创建。"""
        from botler import git_remote
        def boom(row):
            raise OSError("目录不可读")
        monkeypatch.setattr(git_remote, "read_repo_remote_username", boom)
        tc, stub, db = edit_env
        repo = _add_repo(db, project_id=42, name="botler", remote_username=None)
        stub.members_by_project = {42: [
            {"user_id": 7, "username": "agent", "name": "Agent"}]}
        insp_id = db.create_inspiration(repo, "灵感内容")

        resp = tc.post(f"/api/inspirations/{insp_id}/add-issue")

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["assignee_id"] is None


# ---- issue #247：批量将灵感转为 GitLab issue ----

class TestBatchAddIssueFromInspirations:
    """POST /api/inspirations/batch-add-issues（issue #247）：批量转 issue。

    需求：灵感板块多选后批量预览（每条可编辑标题/描述/标签/目标仓库），
    逐条提交——成功项按单条接口语义删除灵感（issue #162），失败项保留
    并逐条给出原因；响应含 succeeded / failed 与 summary 计数，前端
    展示「N 成功 / M 失败」。验收：单条转 issue 行为不变（未覆盖字段的
    条目与单条接口逐字一致）；部分失败不阻断其余条目。
    """

    def _seed(self, db, n=2):
        """插入 n 个仓库，每个仓库一条灵感；返回 (repo_ids, insp_ids)。"""
        repo_ids = [_add_repo(db, 42 + i, f"repo-{i}", priority=10) for i in range(n)]
        insp_ids = [db.create_inspiration(r, f"灵感内容{i}") for i, r in enumerate(repo_ids)]
        return repo_ids, insp_ids

    def test_batch_all_success_defaults(self, edit_env):
        """正常路径：跨仓库批量提交，未传覆盖字段——每条与单条接口语义
        一致（标题=描述=内容、标签 feature+ui、目标仓库=灵感所属仓库），
        全部成功删除灵感，summary 计数正确。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)

        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0]},
                      {"inspiration_id": insp_ids[1]}],
        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 2, "failed": 0}
        assert [s["inspiration_id"] for s in body["succeeded"]] == insp_ids
        assert body["failed"] == []
        # create_issue 调用参数：标题=描述=内容，标签 feature+ui，目标
        # 仓库为各自灵感所属仓库（gitlab_project_id=42/43）
        assert len(stub.create_calls) == 2
        assert stub.create_calls[0][0] == 42
        assert stub.create_calls[0][1]["title"] == "灵感内容0"
        assert stub.create_calls[0][1]["description"] == "灵感内容0"
        assert stub.create_calls[0][1]["labels"] == ["feature", "ui"]
        assert stub.create_calls[1][0] == 43
        # issue #162：创建成功即删除灵感，overview 不再展示
        for iid in insp_ids:
            assert db.get_inspiration(iid) is None
        # succeeded 携带精简 issue 对象（iid/web_url 供前端跳转提示）
        assert body["succeeded"][0]["issue"]["iid"] == 99
        assert body["succeeded"][0]["issue"]["web_url"].startswith("https://")

    def test_batch_per_item_overrides(self, edit_env):
        """每条可单独编辑：标题/描述/标签/目标仓库覆盖生效，未覆盖字段
        沿用默认——批量预览「每条可单独编辑」验收点。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)
        target = _add_repo(db, 99, "target-repo")

        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [
                {
                    "inspiration_id": insp_ids[0],
                    "title": "  批量标题一  ",
                    "description": "批量描述一（自定义）",
                    "labels": ["feature", "bug", "feature"],
                    "repo_id": target,
                },
                {"inspiration_id": insp_ids[1]},
            ],
        })

        assert resp.status_code == 200
        assert resp.json()["summary"] == {"succeeded": 2, "failed": 0}
        # 条目一：覆盖字段全部生效——标题去首尾空白、描述原文、标签
        # 归一化去重（feature 只留一个）、目标仓库 = target（project 99）
        args0 = stub.create_calls[0][1]
        assert stub.create_calls[0][0] == 99
        assert args0["title"] == "批量标题一"
        assert args0["description"] == "批量描述一（自定义）"
        assert args0["labels"] == ["feature", "bug"]
        # 条目二：未覆盖 → 单条接口默认行为（标题=描述=内容、默认标签、
        # 灵感所属仓库）
        args1 = stub.create_calls[1][1]
        assert stub.create_calls[1][0] == 43
        assert args1["title"] == "灵感内容1"
        assert args1["description"] == "灵感内容1"
        assert args1["labels"] == ["feature", "ui"]

    def test_batch_title_override_truncated(self, edit_env):
        """边界（issue #186/#247）：覆盖标题超过 GitLab 标题上限（255
        字符）时同样截断并加省略号标记，描述保留完整覆盖值。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=1)
        long_title = "超" * 300
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0],
                       "title": long_title,
                       "description": "短描述"}],
        })
        assert resp.status_code == 200
        assert resp.json()["summary"] == {"succeeded": 1, "failed": 0}
        args = stub.create_calls[0][1]
        assert len(args["title"]) == 255
        assert args["title"].endswith("…")
        assert long_title.startswith(args["title"][:-1])
        assert args["description"] == "短描述"

    def test_batch_empty_items_400(self, edit_env):
        """空 items → 400（无内容可批量）。"""
        tc, stub, db = edit_env
        resp = tc.post("/api/inspirations/batch-add-issues", json={"items": []})
        assert resp.status_code == 400
        assert "至少选择" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_batch_too_many_items_400(self, edit_env):
        """超过 MAX_BATCH_ITEMS（50）条 → 400 拒绝（防一次性大请求）。"""
        tc, stub, db = edit_env
        repo = _add_repo(db, 42, "botler")
        insp_ids = [db.create_inspiration(repo, f"灵感{i}") for i in range(51)]
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": i} for i in insp_ids],
        })
        assert resp.status_code == 400
        assert "50" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_batch_missing_inspiration_id_422(self, edit_env):
        """条目缺 inspiration_id（必填）→ 422（pydantic 校验）。"""
        tc, stub, db = edit_env
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"title": "只有标题"}],
        })
        assert resp.status_code == 422
        assert stub.create_calls == []

    def test_batch_partial_failure_isolation(self, edit_env):
        """部分失败（验收标准 2）：某条灵感所属仓库禁用 / GitLab 创建
        故障——失败项不阻断其余条目，成功项照常创建并删除，失败项保留
        且逐条给出失败原因。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=3)
        # 条目一：所属仓库未启用 → 400 级失败
        db.update_repo(repo_ids[0], enabled=False)
        # 条目二：GitLab 创建故障 → 502 级失败
        stub.fail_create_projects = {43}

        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": i} for i in insp_ids],
        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 1, "failed": 2}
        assert [s["inspiration_id"] for s in body["succeeded"]] == [insp_ids[2]]
        # 失败原因逐条返回：仓库未启用 / GitLab 创建失败
        failed_by_id = {f["inspiration_id"]: f["error"] for f in body["failed"]}
        assert "未启用" in failed_by_id[insp_ids[0]]
        assert "创建 issue 失败" in failed_by_id[insp_ids[1]]
        # 成功项已删除；失败项保留可重试（issue #162 语义）
        assert db.get_inspiration(insp_ids[2]) is None
        assert db.get_inspiration(insp_ids[0]) is not None
        assert db.get_inspiration(insp_ids[1]) is not None
        # 失败项（仓库禁用）未发起 create_issue；GitLab 故障项发起后被
        # 拒绝（桩先记录后抛错）；仅成功项真正创建
        assert len(stub.create_calls) == 2
        assert stub.create_calls[0][0] == 43
        assert stub.create_calls[1][0] == 44

    def test_batch_inspiration_not_found_isolated(self, edit_env):
        """灵感不存在 → 该条失败并继续处理后续条目。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": 99999},
                      {"inspiration_id": insp_ids[1]}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 1, "failed": 1}
        assert body["failed"][0]["inspiration_id"] == 99999
        assert "不存在" in body["failed"][0]["error"]
        assert body["succeeded"][0]["inspiration_id"] == insp_ids[1]

    def test_batch_duplicate_inspiration_id_deduped(self, edit_env):
        """同一灵感 id 重复出现在 items → 只处理首次（去重，避免重复
        选择产生重复 issue）。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=1)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0]},
                      {"inspiration_id": insp_ids[0]}],
        })
        assert resp.status_code == 200
        assert resp.json()["summary"] == {"succeeded": 1, "failed": 0}
        assert len(stub.create_calls) == 1

    def test_batch_empty_title_override_failed(self, edit_env):
        """覆盖标题去首尾空白后为空 → 该条失败（400 级），其余继续。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0], "title": "   "},
                      {"inspiration_id": insp_ids[1]}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 1, "failed": 1}
        assert "标题不能为空" in body["failed"][0]["error"]
        # 失败项保留
        assert db.get_inspiration(insp_ids[0]) is not None

    def test_batch_empty_labels_override_failed(self, edit_env):
        """覆盖标签归一化后为空（纯空白/空串）→ 该条失败；非法标签
        去空白去重后保留合法部分。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0], "labels": [" ", ""]},
                      {"inspiration_id": insp_ids[1],
                       "labels": [" bug ", "feature", "bug"]}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 1, "failed": 1}
        assert "标签不能为空" in body["failed"][0]["error"]
        # 条目二：去空白去重后保留 ["bug", "feature"]（保序）
        args = stub.create_calls[0][1]
        assert args["labels"] == ["bug", "feature"]

    def test_batch_repo_override_disabled_failed(self, edit_env):
        """目标仓库覆盖为未启用仓库 → 该条失败（不允许向禁用仓库提交），
        灵感保留；其余条目照常。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=2)
        disabled = _add_repo(db, 77, "disabled-repo", enabled=False)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0], "repo_id": disabled},
                      {"inspiration_id": insp_ids[1]}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 1, "failed": 1}
        assert "未启用" in body["failed"][0]["error"]
        assert db.get_inspiration(insp_ids[0]) is not None

    def test_batch_without_owner_token_failed(self, client):
        """未配置 owner token → 每条失败（概览页写操作绝不回退 bot
        token），灵感全部保留。"""
        tc, db = client
        repo_ids, insp_ids = self._seed(db, n=2)
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": i} for i in insp_ids],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"succeeded": 0, "failed": 2}
        for f in body["failed"]:
            assert "owner token" in f["error"]
        for iid in insp_ids:
            assert db.get_inspiration(iid) is not None

    def test_batch_invalidates_overview_cache(self, edit_env):
        """批量创建成功后清空概览缓存（与单条接口一致）：下一次
        overview 请求重新拉取，前端创建成功立即刷新可见新 issue。"""
        tc, stub, db = edit_env
        repo_ids, insp_ids = self._seed(db, n=1)
        stub.issues_by_project = {42: [{
            "iid": 1, "title": "旧 issue", "state": "opened",
            "updated_at": None, "created_at": None,
            "web_url": "https://gitlab.example.com/x/-/issues/1",
            "description": None, "labels": [], "author": None,
            "milestone": None, "assignees": [], "user_notes_count": 0,
        }]}
        tc.get("/api/issues/overview")
        assert len(stub.calls) == 1
        resp = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [{"inspiration_id": insp_ids[0]}],
        })
        assert resp.status_code == 200
        tc.get("/api/issues/overview")
        assert len(stub.calls) == 2  # 缓存已失效，重新拉取


# ---- issue #166：灵感与 AI agent 对话 ----

class StubChatClient:
    """灵感 AI 对话 ChatModelClient 桩：记录构造参数与 chat 调用，可注入回复/故障。

    monkeypatch botler.chat_models.ChatModelClient（端点内延迟导入来源
    模块），与 test_api_settings.py 的生图/识图测试桩同模式。
    """

    instances: list["StubChatClient"] = []
    reply: str = "AI 的探讨回复"
    raise_error: Exception | None = None
    raise_http_error: bool = False

    def __init__(self, **kwargs):
        # 与真实 ChatModelClient 一致：未知 provider 构造即抛错
        # （不支持的模型类型用例依赖此行为触发 502 + 回滚链路）
        from botler.chat_models import DEFAULT_BASE_URLS, ChatModelError
        if str(kwargs.get("provider") or "") not in DEFAULT_BASE_URLS:
            raise ChatModelError(
                f"不支持的 AI 对话模型类型: {kwargs.get('provider')}")
        self.kwargs = kwargs
        self.chat_calls: list[list[dict]] = []
        StubChatClient.instances.append(self)

    def chat(self, messages):
        self.chat_calls.append(messages)
        if StubChatClient.raise_error is not None:
            raise StubChatClient.raise_error
        if StubChatClient.raise_http_error:
            raise httpx.ConnectError("模拟网络故障")
        return StubChatClient.reply


@pytest.fixture
def chat_env(client, monkeypatch):
    """配置 AI 供应商 + 打桩 ChatModelClient，返回 (tc, db, stub 类)。

    默认注入一个启用的 deepseek 供应商；用例可覆盖列表后重新 update。
    """
    tc, db = client
    tc.app.state.ctx.config.update_section("ai_providers", [{
        "name": "deepseek", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
    }])
    from botler import chat_models as chat_mod
    StubChatClient.instances = []
    StubChatClient.reply = "AI 的探讨回复"
    StubChatClient.raise_error = None
    StubChatClient.raise_http_error = False
    monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)
    return tc, db


def _add_inspiration(db, repo_id=1):
    """便捷：插入一个仓库 + 一条灵感，返回灵感 id。"""
    repo = _add_repo(db, repo_id, f"repo-{repo_id}")
    return db.create_inspiration(repo, "灵感内容")


class TestInspirationChatProvider:
    """灵感对话供应商选择接口（issue #249）。"""

    def test_list_enabled_providers_and_saved_selection(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        tc.app.state.ctx.config.update_section("ai_providers", [
            {"name": "DeepSeek 快速", "provider": "deepseek", "model": "deepseek-chat",
             "api_key": "sk-d", "enabled": True},
            {"name": "OpenAI 深度", "provider": "openai", "model": "gpt-4o",
             "api_key": "sk-o", "enabled": True},
            {"name": "无 Key", "provider": "gemini", "model": "gemini-pro",
             "api_key": "", "enabled": True},
            {"name": "已停用", "provider": "qwen", "model": "qwen-max",
             "api_key": "sk-q", "enabled": False},
        ])
        r = tc.get(f"/api/inspirations/{insp_id}/chat-providers")
        assert r.status_code == 200
        assert r.json() == {
            "selected": "deepseek",
            "providers": [
                {"name": "DeepSeek 快速", "provider": "deepseek", "model": "deepseek-chat"},
                {"name": "OpenAI 深度", "provider": "openai", "model": "gpt-4o"},
            ],
        }

    def test_empty_provider_list_guides_settings(self, client):
        tc, db = client
        insp_id = _add_inspiration(db)
        r = tc.get(f"/api/inspirations/{insp_id}/chat-providers")
        assert r.status_code == 200
        assert r.json() == {"selected": None, "providers": []}

    def test_save_provider_persists_and_clear_keeps_history(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        db.add_inspiration_message(insp_id, "user", "历史问题")
        db.add_inspiration_message(insp_id, "assistant", "历史回答")
        tc.app.state.ctx.config.update_section("ai_providers", [
            {"name": "DeepSeek", "provider": "deepseek", "api_key": "sk-d", "enabled": True},
            {"name": "OpenAI", "provider": "openai", "api_key": "sk-o", "enabled": True},
        ])
        r = tc.put(f"/api/inspirations/{insp_id}/chat-provider", json={"provider": "openai"})
        assert r.status_code == 200
        assert r.json()["chat_provider"] == "openai"
        assert db.get_inspiration(insp_id)["chat_provider"] == "openai"
        assert [m["content"] for m in db.list_inspiration_messages(insp_id)] == ["历史问题", "历史回答"]
        r = tc.put(f"/api/inspirations/{insp_id}/chat-provider", json={"provider": None})
        assert r.status_code == 200
        assert r.json()["chat_provider"] is None

    def test_save_unknown_or_unavailable_provider_rejected(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        tc.app.state.ctx.config.update_section("ai_providers", [
            {"name": "DeepSeek", "provider": "deepseek", "api_key": "sk-d", "enabled": True},
            {"name": "停用 OpenAI", "provider": "openai", "api_key": "sk-o", "enabled": False},
        ])
        for provider in ("openai", "missing"):
            r = tc.put(f"/api/inspirations/{insp_id}/chat-provider", json={"provider": provider})
            assert r.status_code == 400
            assert "供应商" in r.json()["detail"]

    def test_saved_unavailable_provider_falls_back_for_send(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        db.set_inspiration_chat_provider(insp_id, "openai")
        tc.app.state.ctx.config.update_section("ai_providers", [
            {"name": "DeepSeek", "provider": "deepseek", "api_key": "sk-d", "enabled": True},
            {"name": "OpenAI", "provider": "openai", "api_key": "sk-o", "enabled": False},
        ])
        r = tc.post(f"/api/inspirations/{insp_id}/messages", json={"content": "回退测试"})
        assert r.status_code == 201
        assert StubChatClient.instances[-1].kwargs["provider"] == "deepseek"

    def test_send_optional_provider_routes_without_losing_context(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        tc.app.state.ctx.config.update_section("ai_providers", [
            {"name": "DeepSeek", "provider": "deepseek", "api_key": "sk-d", "enabled": True},
            {"name": "OpenAI", "provider": "openai", "api_key": "sk-o", "enabled": True},
        ])
        db.add_inspiration_message(insp_id, "user", "旧问题")
        db.add_inspiration_message(insp_id, "assistant", "旧回答")
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "新问题", "provider": "openai"})
        assert r.status_code == 201
        assert StubChatClient.instances[-1].kwargs["provider"] == "openai"
        assert StubChatClient.instances[-1].chat_calls[-1][-3:] == [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "新问题"},
        ]

    def test_send_unavailable_optional_provider_rejected_without_new_messages(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi", "provider": "openai"})
        assert r.status_code == 400
        assert db.list_inspiration_messages(insp_id) == []


class TestInspirationChat:
    """GET/POST /api/inspirations/{id}/messages（issue #166）。"""

    def test_get_empty_history(self, client):
        tc, db = client
        insp_id = _add_inspiration(db)
        r = tc.get(f"/api/inspirations/{insp_id}/messages")
        assert r.status_code == 200
        assert r.json() == {"messages": []}

    def test_get_not_found(self, client):
        tc, db = client
        r = tc.get("/api/inspirations/999/messages")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_get_history_ordered(self, chat_env):
        """历史按时间升序返回（先用户后 AI）。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        db.add_inspiration_message(insp_id, "user", "第一条提问")
        db.add_inspiration_message(insp_id, "assistant", "第一条回复")
        db.add_inspiration_message(insp_id, "user", "第二条提问")
        r = tc.get(f"/api/inspirations/{insp_id}/messages")
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assert [m["content"] for m in msgs] == [
            "第一条提问", "第一条回复", "第二条提问"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]

    def test_send_ok(self, chat_env):
        """正常发送：返回用户消息 + AI 回复，两者落库。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "这个灵感怎么落地？"})
        assert r.status_code == 201
        body = r.json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][0]["content"] == "这个灵感怎么落地？"
        assert body["messages"][1]["content"] == "AI 的探讨回复"
        rows = db.list_inspiration_messages(insp_id)
        assert len(rows) == 2
        assert rows[0]["role"] == "user"
        assert rows[1]["role"] == "assistant"

    def test_send_context_contains_inspiration_and_history(self, chat_env):
        """传给模型的 messages：系统提示含灵感内容与仓库名 + 历史 + 新消息。"""
        tc, db = chat_env
        repo = _add_repo(db, 7, "灵感仓库")
        insp_id = db.create_inspiration(repo, "灵感内容ABC")
        db.add_inspiration_message(insp_id, "user", "旧提问")
        db.add_inspiration_message(insp_id, "assistant", "旧回复")
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "新提问"})
        assert r.status_code == 201
        sent = StubChatClient.instances[-1].chat_calls[-1]
        assert sent[0]["role"] == "system"
        assert "灵感仓库" in sent[0]["content"]
        assert "灵感内容ABC" in sent[0]["content"]
        assert sent[1:] == [
            {"role": "user", "content": "旧提问"},
            {"role": "assistant", "content": "旧回复"},
            {"role": "user", "content": "新提问"},
        ]

    def test_send_client_built_from_ai_providers(self, chat_env):
        """ChatModelClient 构造参数来自 ai_providers 第一项配置。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        tc.post(f"/api/inspirations/{insp_id}/messages", json={"content": "hi"})
        client = StubChatClient.instances[-1]
        assert client.kwargs["provider"] == "deepseek"
        assert client.kwargs["model"] == "deepseek-chat"
        assert client.kwargs["api_key"] == "sk-test"
        assert client.kwargs["base_url"] == "https://api.deepseek.com/v1"
        assert client.kwargs["timeout"] == 60.0

    def test_send_not_found(self, chat_env):
        tc, db = chat_env
        r = tc.post("/api/inspirations/999/messages", json={"content": "hi"})
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_send_empty_content(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        for bad in ("", "   "):
            r = tc.post(f"/api/inspirations/{insp_id}/messages",
                        json={"content": bad})
            assert r.status_code == 400
            assert "不能为空" in r.json()["detail"]

    def test_send_content_too_long(self, chat_env):
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "长" * 2001})
        assert r.status_code == 400
        assert "不能超过" in r.json()["detail"]

    def test_send_without_provider_400(self, client):
        """未配置任何 AI 供应商 → 400 引导设置页。"""
        tc, db = client
        insp_id = _add_inspiration(db)
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi"})
        assert r.status_code == 400
        assert "AI 对话模型" in r.json()["detail"]

    def test_send_disabled_provider_400(self, client):
        """供应商全部未启用 / 无 Key → 400。"""
        tc, db = client
        insp_id = _add_inspiration(db)
        for providers in (
            [{"name": "d", "provider": "deepseek", "base_url": "",
              "api_key": "sk-1", "enabled": False}],
            [{"name": "d", "provider": "deepseek", "base_url": "",
              "api_key": "", "enabled": True}],
            [{"name": "d", "provider": "deepseek", "base_url": "",
              "api_key": None, "enabled": True}],
        ):
            tc.app.state.ctx.config.update_section("ai_providers", providers)
            r = tc.post(f"/api/inspirations/{insp_id}/messages",
                        json={"content": "hi"})
            assert r.status_code == 400
            assert "AI 对话模型" in r.json()["detail"]
        # 数据库无残留
        assert db.list_inspiration_messages(insp_id) == []

    def test_send_ai_error_rolls_back_user_message(self, chat_env):
        """AI 调用失败（ChatModelError）→ 502，用户消息回滚不落库。"""
        from botler.chat_models import ChatModelError
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        StubChatClient.raise_error = ChatModelError("模拟模型故障")
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi"})
        assert r.status_code == 502
        assert "AI 回复失败" in r.json()["detail"]
        assert db.list_inspiration_messages(insp_id) == []

    def test_send_network_error_rolls_back_user_message(self, chat_env):
        """AI 调用网络错误 → 502，用户消息回滚不落库。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        StubChatClient.raise_http_error = True
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi"})
        assert r.status_code == 502
        assert "网络错误" in r.json()["detail"]
        assert db.list_inspiration_messages(insp_id) == []

    def test_send_empty_reply_rolls_back(self, chat_env):
        """AI 返回空白回复 → 502，用户消息回滚。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        StubChatClient.reply = "   "
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi"})
        assert r.status_code == 502
        assert "AI 回复为空" in r.json()["detail"]
        assert db.list_inspiration_messages(insp_id) == []

    def test_send_unsupported_provider_502(self, chat_env):
        """供应商 provider 不受支持 → ChatModelError → 502 且回滚。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        tc.app.state.ctx.config.update_section("ai_providers", [{
            "name": "x", "provider": "unknown_provider",
            "base_url": "", "api_key": "sk-1", "model": "", "enabled": True,
        }])
        r = tc.post(f"/api/inspirations/{insp_id}/messages",
                    json={"content": "hi"})
        assert r.status_code == 502
        assert "AI 回复失败" in r.json()["detail"]
        assert db.list_inspiration_messages(insp_id) == []

    def test_delete_inspiration_cascades_messages(self, chat_env):
        """删除灵感时级联删除其对话消息。"""
        tc, db = chat_env
        insp_id = _add_inspiration(db)
        db.add_inspiration_message(insp_id, "user", "提问")
        db.add_inspiration_message(insp_id, "assistant", "回复")
        r = tc.delete(f"/api/inspirations/{insp_id}")
        assert r.status_code == 204
        assert db.get_inspiration(insp_id) is None
        assert db.list_inspiration_messages(insp_id) == []

    def test_messages_survive_other_inspiration_delete(self, chat_env):
        """删除另一条灵感不影响本条对话消息。"""
        tc, db = chat_env
        insp_a = _add_inspiration(db)
        insp_b = _add_inspiration(db)
        db.add_inspiration_message(insp_a, "user", "A 的提问")
        db.add_inspiration_message(insp_b, "user", "B 的提问")
        tc.delete(f"/api/inspirations/{insp_b}")
        rows = db.list_inspiration_messages(insp_a)
        assert [r["content"] for r in rows] == ["A 的提问"]


class TestOverviewPagination:
    """灵感概览与单仓库分页（issue #219）。"""

    def test_overview_only_returns_counts_by_default(self, client):
        """默认概览不可把大量灵感行传给首屏与轮询。"""
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        for i in range(25):
            db.create_inspiration(repo, f"灵感 {i}")

        response = tc.get("/api/inspirations/overview")

        assert response.status_code == 200
        result = response.json()["repos"][0]
        assert result["inspiration_total"] == 25
        assert result["inspirations"] == []
        assert result["inspiration_has_more"] is True

    def test_overview_optional_page_keeps_updated_at_desc_order(self, client):
        """overview 的可选首屏页应限量返回，并保留原有排序语义。"""
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        first = db.create_inspiration(repo, "较早灵感")
        second = db.create_inspiration(repo, "较新灵感")

        response = tc.get("/api/inspirations/overview?limit=1")

        assert response.status_code == 200
        result = response.json()["repos"][0]
        assert result["inspiration_total"] == 2
        assert [item["id"] for item in result["inspirations"]] == [second]
        assert result["inspiration_has_more"] is True
        assert first != second

    def test_repo_page_loads_requested_slice_and_bounds(self, client):
        """展开仓库时按 offset 懒加载，不重复读取此前条目。"""
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        ids = [db.create_inspiration(repo, f"灵感 {i}") for i in range(4)]

        first_page = tc.get(f"/api/inspirations/pages/{repo}?offset=0&limit=2")
        last_page = tc.get(f"/api/inspirations/pages/{repo}?offset=2&limit=2")
        empty_page = tc.get(f"/api/inspirations/pages/{repo}?offset=4&limit=2")

        assert first_page.status_code == 200
        assert first_page.json()["total"] == 4
        assert [item["id"] for item in first_page.json()["inspirations"]] == ids[::-1][:2]
        assert first_page.json()["has_more"] is True
        assert [item["id"] for item in last_page.json()["inspirations"]] == ids[::-1][2:]
        assert last_page.json()["has_more"] is False
        assert empty_page.json()["inspirations"] == []
        assert empty_page.json()["has_more"] is False

    def test_repo_page_rejects_invalid_pagination_and_deleted_repo(self, client):
        """分页参数有边界，已删除仓库不能再被懒加载。"""
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        db.soft_delete_repo(repo)

        assert tc.get(f"/api/inspirations/pages/{repo}?offset=-1").status_code == 422
        assert tc.get(f"/api/inspirations/pages/{repo}?limit=0").status_code == 422
        assert tc.get(f"/api/inspirations/pages/{repo}").status_code == 400


# ---- issue #246：灵感标签分类、筛选与归档 ----
# 灵感表新增 label（单标签）与 archived（软删除）；概览默认隐藏归档，
# 可按标签筛选；转 issue 时可选择保留灵感并关联（不删除）。

class TestInspirationLabel:
    """灵感打标签：创建/更新带 label，空与超长边界。"""

    def test_create_with_label(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations",
                    json={"repo_id": repo, "content": "灵感", "label": "待验证"})
        assert r.status_code == 201
        assert r.json()["label"] == "待验证"
        assert r.json()["archived"] == 0

    def test_create_label_stripped(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations",
                    json={"repo_id": repo, "content": "灵感", "label": "  待验证  "})
        assert r.status_code == 201
        assert r.json()["label"] == "待验证"

    def test_create_without_label_defaults_none(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations", json={"repo_id": repo, "content": "灵感"})
        assert r.status_code == 201
        assert r.json()["label"] is None

    def test_create_label_too_long(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        r = tc.post("/api/inspirations",
                    json={"repo_id": repo, "content": "灵感", "label": "长" * 51})
        assert r.status_code == 400

    def test_update_sets_and_clears_label(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "灵感")
        r = tc.put(f"/api/inspirations/{insp_id}", json={"content": "灵感", "label": "已规划"})
        assert r.status_code == 200
        assert r.json()["label"] == "已规划"
        # 传空串清除标签
        r = tc.put(f"/api/inspirations/{insp_id}", json={"content": "灵感", "label": ""})
        assert r.status_code == 200
        assert r.json()["label"] is None

    def test_update_label_too_long(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "灵感")
        r = tc.put(f"/api/inspirations/{insp_id}",
                   json={"content": "灵感", "label": "长" * 51})
        assert r.status_code == 400

    def test_row_fields_include_label_and_archived(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "灵感", label="待验证")
        r = tc.get("/api/inspirations/overview?limit=100")
        item = r.json()["repos"][0]["inspirations"][0]
        assert item["id"] == insp_id
        assert item["label"] == "待验证"
        assert item["archived"] == 0
        assert item["linked_issue_iid"] is None
        assert item["linked_issue_url"] is None


class TestInspirationArchive:
    """归档/取消归档：默认隐藏归档、可开关查看。"""

    def test_archive_and_unarchive(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        insp_id = db.create_inspiration(repo, "灵感")
        r = tc.post(f"/api/inspirations/{insp_id}/archive")
        assert r.status_code == 200
        assert r.json()["archived"] == 1
        r = tc.post(f"/api/inspirations/{insp_id}/unarchive")
        assert r.status_code == 200
        assert r.json()["archived"] == 0

    def test_archive_not_found(self, client):
        tc, db = client
        r = tc.post("/api/inspirations/999/archive")
        assert r.status_code == 404
        r = tc.post("/api/inspirations/999/unarchive")
        assert r.status_code == 404

    def test_overview_hides_archived_by_default(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "未归档灵感")
        id2 = db.create_inspiration(repo, "已归档灵感")
        db.set_inspiration_archived(id2, True)
        r = tc.get("/api/inspirations/overview?limit=100")
        items = r.json()["repos"][0]["inspirations"]
        assert [x["id"] for x in items] == [id1]
        assert r.json()["repos"][0]["inspiration_total"] == 1
        assert r.json()["repos"][0]["archived_total"] == 1

    def test_overview_archived_view(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        db.create_inspiration(repo, "未归档灵感")
        id2 = db.create_inspiration(repo, "已归档灵感")
        db.set_inspiration_archived(id2, True)
        r = tc.get("/api/inspirations/overview?limit=100&archived=1")
        items = r.json()["repos"][0]["inspirations"]
        assert [x["id"] for x in items] == [id2]

    def test_pages_archived_filter(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "未归档灵感")
        id2 = db.create_inspiration(repo, "已归档灵感")
        db.set_inspiration_archived(id2, True)
        r = tc.get(f"/api/inspirations/pages/{repo}")
        assert [x["id"] for x in r.json()["inspirations"]] == [id1]
        assert r.json()["total"] == 1
        r = tc.get(f"/api/inspirations/pages/{repo}?archived=1")
        assert [x["id"] for x in r.json()["inspirations"]] == [id2]
        assert r.json()["total"] == 1


class TestInspirationFilter:
    """按标签筛选（overview 与分页）。"""

    def test_overview_label_filter(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "A", label="待验证")
        db.create_inspiration(repo, "B", label="已规划")
        id3 = db.create_inspiration(repo, "C", label="待验证")
        r = tc.get("/api/inspirations/overview?limit=100&label=待验证")
        items = r.json()["repos"][0]["inspirations"]
        assert sorted(x["id"] for x in items) == [id1, id3]
        r = tc.get("/api/inspirations/overview?limit=100&label=不存在")
        assert r.json()["repos"][0]["inspirations"] == []

    def test_overview_label_filter_respects_archived(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "A", label="待验证")
        id2 = db.create_inspiration(repo, "B", label="待验证")
        db.set_inspiration_archived(id2, True)
        r = tc.get("/api/inspirations/overview?limit=100&label=待验证")
        assert [x["id"] for x in r.json()["repos"][0]["inspirations"]] == [id1]
        r = tc.get("/api/inspirations/overview?limit=100&label=待验证&archived=1")
        assert [x["id"] for x in r.json()["repos"][0]["inspirations"]] == [id2]

    def test_pages_label_filter(self, client):
        tc, db = client
        repo = _add_repo(db, 1, "botler")
        id1 = db.create_inspiration(repo, "A", label="待验证")
        db.create_inspiration(repo, "B", label="已规划")
        r = tc.get(f"/api/inspirations/pages/{repo}?label=待验证")
        assert [x["id"] for x in r.json()["inspirations"]] == [id1]
        assert r.json()["total"] == 1


class TestAddIssueKeepInspiration:
    """转 issue 可选保留灵感并关联（issue #246，默认仍删除保持旧行为）。"""

    def test_default_still_deletes_inspiration(self, edit_env):
        tc, gitlab, db = edit_env
        repo = _add_repo(db, 1, "botler", enabled=True)
        insp_id = db.create_inspiration(repo, "默认删除")
        r = tc.post(f"/api/inspirations/{insp_id}/add-issue")
        assert r.status_code == 201
        assert db.get_inspiration(insp_id) is None

    def test_keep_inspiration_preserves_and_links(self, edit_env):
        tc, gitlab, db = edit_env
        repo = _add_repo(db, 1, "botler", enabled=True)
        insp_id = db.create_inspiration(repo, "保留灵感")
        r = tc.post(f"/api/inspirations/{insp_id}/add-issue",
                    json={"keep_inspiration": True})
        assert r.status_code == 201
        row = db.get_inspiration(insp_id)
        assert row is not None, "keep_inspiration=true 时应保留灵感"
        assert row["linked_issue_iid"] == 99
        assert row["linked_issue_url"] == (
            "https://gitlab.example.com/x/-/issues/99")

    def test_keep_inspiration_response_keeps_issue_contract(self, edit_env):
        """响应契约不变：仍返回精简 issue 对象（前端展示成功提示与跳转
        链接）；保留的灵感关联通过列表刷新读到（数据库行断言见
        test_keep_inspiration_preserves_and_links）。"""
        tc, gitlab, db = edit_env
        repo = _add_repo(db, 1, "botler", enabled=True)
        insp_id = db.create_inspiration(repo, "保留灵感")
        r = tc.post(f"/api/inspirations/{insp_id}/add-issue",
                    json={"keep_inspiration": True})
        assert r.status_code == 201
        item = r.json()
        assert item["iid"] == 99
        assert item["web_url"] == "https://gitlab.example.com/x/-/issues/99"
        assert db.get_inspiration(insp_id) is not None

    def test_keep_inspiration_overwrites_previous_link(self, edit_env):
        """已有关联的灵感再次转 issue 时更新为新 issue 关联。"""
        tc, gitlab, db = edit_env
        repo = _add_repo(db, 1, "botler", enabled=True)
        insp_id = db.create_inspiration(repo, "保留灵感")
        tc.post(f"/api/inspirations/{insp_id}/add-issue",
                json={"keep_inspiration": True})
        tc.post(f"/api/inspirations/{insp_id}/add-issue",
                json={"keep_inspiration": True})
        row = db.get_inspiration(insp_id)
        assert row["linked_issue_iid"] == 99
        assert len(gitlab.create_calls) == 2

    def test_batch_keep_inspiration_per_item(self, edit_env):
        tc, gitlab, db = edit_env
        repo = _add_repo(db, 1, "botler", enabled=True)
        keep_id = db.create_inspiration(repo, "保留")
        delete_id = db.create_inspiration(repo, "删除")
        r = tc.post("/api/inspirations/batch-add-issues", json={
            "items": [
                {"inspiration_id": keep_id, "keep_inspiration": True},
                {"inspiration_id": delete_id, "keep_inspiration": False},
            ],
        })
        assert r.status_code == 200
        assert r.json()["summary"] == {"succeeded": 2, "failed": 0}
        assert db.get_inspiration(keep_id) is not None
        assert db.get_inspiration(keep_id)["linked_issue_iid"] == 99
        assert db.get_inspiration(delete_id) is None
