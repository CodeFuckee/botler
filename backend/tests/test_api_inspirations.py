"""灵感 API 测试（issue #131）：概览页「灵感」板块后端接口。

需求：概览页在「开放 Issue」下方、「CI/CD 流水线」上方增加灵感板块，
用户按仓库随手记录新功能灵感；灵感仅保存在 Botler 本地 SQLite 数据库，
不提交到 GitLab issue（本模块纯本地数据，无 GitLab 依赖）。

覆盖：
- GET  /api/inspirations/overview：聚合所有未软删除仓库（priority 升序、
  同优先级按 id）+ 各自灵感（updated_at 降序）；无灵感的仓库也返回
  （前端空状态 + 添加表单）；
- POST /api/inspirations：创建（repo_id + content 必填）；
- PUT  /api/inspirations/{id}：更新内容并刷新 updated_at；
- DELETE /api/inspirations/{id}：删除；
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
    ctx = SimpleNamespace(config=config, db=db, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db


@pytest.fixture
def client(api_app):
    app, db = api_app
    return TestClient(app), db


def _add_repo(db, project_id, name, priority=100, enabled=True):
    """便捷：插入一个仓库并返回本地 id。"""
    return db.upsert_repo(project_id, name, f"https://gitlab.example.com/{name}.git",
                          enabled=enabled, priority=priority)


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
