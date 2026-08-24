"""模板版本历史与回滚测试（issue #262）。

验收标准对应：
1. 保存模板生成版本，历史可查看；
2. 回滚到旧版后任务使用旧版模板；
3. 相同内容重复保存不产生新版本；
4. 有数据库迁移与单元测试，全量测试无 regression。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.report import DEFAULT_COMMENT_TEMPLATE
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


@pytest.fixture
def api_client(tmp_path):
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db,
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), ctx


def _add_repo(db, project_id=42, name="demo", url="https://gitlab.example.com/demo.git"):
    """构造仓库并返回本地 repo_id。"""
    db.upsert_repo(project_id, name, url)
    return db.get_repo_by_project_id(project_id)["id"]


# ---- 数据库层 ----

class TestDatabaseTemplateVersions:
    def test_record_creates_version_1(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        vid = db.record_template_version("global:default", "模板A")
        assert vid is not None
        ver = db.get_template_version(vid)
        assert ver["template_key"] == "global:default"
        assert ver["content"] == "模板A"
        assert ver["version_no"] == 1
        assert ver["note"] == ""

    def test_record_same_content_skips(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.record_template_version("global:default", "模板A")
        assert db.record_template_version("global:default", "模板A") is None
        rows = db.list_template_versions("global:default")
        assert len(rows) == 1

    def test_record_different_content_appends(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.record_template_version("global:default", "模板A")
        db.record_template_version("global:default", "模板B")
        rows = db.list_template_versions("global:default")
        assert [r["version_no"] for r in rows] == [2, 1]
        assert [r["content"] for r in rows] == ["模板B", "模板A"]

    def test_version_no_isolated_per_key(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.record_template_version("global:default", "A")
        db.record_template_version("global:resume", "R")
        assert db.list_template_versions("global:default")[0]["version_no"] == 1
        assert db.list_template_versions("global:resume")[0]["version_no"] == 1
        db.record_template_version("global:default", "B")
        assert db.list_template_versions("global:default")[0]["version_no"] == 2

    def test_record_empty_content_allowed(self, tmp_path):
        """空内容可记录（仓库级覆盖清空 = 回退全局，历史可追溯）。"""
        db = Database(str(tmp_path / "t.db"))
        vid = db.record_template_version("repo:1", "")
        assert vid is not None
        assert db.get_template_version(vid)["content"] == ""

    def test_latest_and_get(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.record_template_version("global:comment", "C1", note="首次")
        vid2 = db.record_template_version("global:comment", "C2")
        latest = db.latest_template_version("global:comment")
        assert latest["id"] == vid2
        assert latest["version_no"] == 2
        assert db.get_template_version(vid2)["content"] == "C2"
        assert db.get_template_version(99999) is None

    def test_list_limit(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        for i in range(5):
            db.record_template_version("global:default", f"v{i}")
        rows = db.list_template_versions("global:default", limit=2)
        assert len(rows) == 2
        assert rows[0]["content"] == "v4"
        assert rows[1]["content"] == "v3"

    def test_note_preserved(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.record_template_version("global:default", "A", note="回滚到版本 1")
        assert db.list_template_versions("global:default")[0]["note"] == "回滚到版本 1"


# ---- 保存埋点：settings（全局 default / resume / comment）----

class TestSettingsTemplateVersions:
    def test_save_default_creates_version(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"default": "新版默认模板"}})
        rows = ctx.db.list_template_versions("global:default")
        assert len(rows) == 1
        assert rows[0]["content"] == "新版默认模板"

    def test_save_default_same_content_no_new_version(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"default": "模板"}})
        tc.put("/api/settings", json={"templates": {"default": "模板"}})
        assert len(ctx.db.list_template_versions("global:default")) == 1
        # 内容变化后产生新版本
        tc.put("/api/settings", json={"templates": {"default": "模板2"}})
        assert len(ctx.db.list_template_versions("global:default")) == 2

    def test_save_resume_creates_version(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"resume": "恢复引导语"}})
        rows = ctx.db.list_template_versions("global:resume")
        assert len(rows) == 1
        assert rows[0]["content"] == "恢复引导语"

    def test_save_resume_blank_records_builtin_default(self, api_client):
        """中断恢复模版留空保存 = 恢复内置默认，版本记录最终生效内容。"""
        tc, ctx = api_client
        from botler.config import DEFAULT_RESUME_PROMPT
        tc.put("/api/settings", json={"templates": {"resume": "自定义"}})
        tc.put("/api/settings", json={"templates": {"resume": "   "}})
        rows = ctx.db.list_template_versions("global:resume")
        assert len(rows) == 2
        assert rows[0]["content"] == DEFAULT_RESUME_PROMPT

    def test_save_comment_creates_version(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"comment": "评论模板"}})
        rows = ctx.db.list_template_versions("global:comment")
        assert len(rows) == 1
        assert rows[0]["content"] == "评论模板"

    def test_save_comment_blank_records_builtin_default(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"comment": "自定义"}})
        tc.put("/api/settings", json={"templates": {"comment": "   "}})
        rows = ctx.db.list_template_versions("global:comment")
        assert len(rows) == 2
        assert rows[0]["content"] == DEFAULT_COMMENT_TEMPLATE

    def test_list_versions_api(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"default": "v1"}})
        tc.put("/api/settings", json={"templates": {"default": "v2"}})
        resp = tc.get("/api/template-versions", params={"key": "global:default"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "global:default"
        assert len(data["versions"]) == 2
        assert data["versions"][0]["content"] == "v2"
        assert data["versions"][0]["version_no"] == 2
        assert data["latest"]["version_no"] == 2

    def test_list_versions_empty(self, api_client):
        tc, _ = api_client
        resp = tc.get("/api/template-versions", params={"key": "global:default"})
        assert resp.status_code == 200
        assert resp.json()["versions"] == []
        assert resp.json()["latest"] is None

    def test_list_versions_requires_key(self, api_client):
        tc, _ = api_client
        assert tc.get("/api/template-versions").status_code == 422


# ---- 回滚：settings（全局模板）----

class TestRollbackGlobalTemplates:
    def test_rollback_default_restores_content(self, api_client):
        """回滚到旧版后，生效模板变为旧版内容（任务使用旧版模板）。"""
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"default": "第一版"}})
        tc.put("/api/settings", json={"templates": {"default": "第二版"}})
        v1 = ctx.db.list_template_versions("global:default")[1]  # 旧版
        assert v1["version_no"] == 1
        resp = tc.post(f"/api/template-versions/{v1['id']}/rollback")
        assert resp.status_code == 200
        # config 生效内容回到第一版
        assert ctx.config.get().default_template == "第一版"
        # 任务渲染使用回滚后的模板
        renderer = TemplateRenderer(ctx.config)
        repo = {"prompt_template": None, "name": "botler"}
        assert renderer.resolve_template(repo) == "第一版"
        # 回滚本身产生新版本（备注标明来源），且与当前生效内容一致
        rows = ctx.db.list_template_versions("global:default")
        assert len(rows) == 3
        assert rows[0]["content"] == "第一版"
        assert rows[0]["note"] == "回滚到版本 1"
        assert ctx.config.get().default_template == rows[0]["content"]

    def test_rollback_duplicate_no_new_version(self, api_client):
        """回滚后内容与最新版本相同（连续回滚同一目标）不再产生新版本。"""
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"default": "A"}})
        tc.put("/api/settings", json={"templates": {"default": "B"}})
        v1 = ctx.db.list_template_versions("global:default")[1]
        tc.post(f"/api/template-versions/{v1['id']}/rollback")
        count_after_first = len(ctx.db.list_template_versions("global:default"))
        resp = tc.post(f"/api/template-versions/{v1['id']}/rollback")
        assert resp.status_code == 200
        assert len(ctx.db.list_template_versions("global:default")) == count_after_first

    def test_rollback_resume_restores_content(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"resume": "恢复1"}})
        tc.put("/api/settings", json={"templates": {"resume": "恢复2"}})
        v1 = ctx.db.list_template_versions("global:resume")[1]
        resp = tc.post(f"/api/template-versions/{v1['id']}/rollback")
        assert resp.status_code == 200
        assert ctx.config.get().resume_template == "恢复1"

    def test_rollback_comment_restores_content(self, api_client):
        tc, ctx = api_client
        tc.put("/api/settings", json={"templates": {"comment": "评论1"}})
        tc.put("/api/settings", json={"templates": {"comment": "评论2"}})
        v1 = ctx.db.list_template_versions("global:comment")[1]
        resp = tc.post(f"/api/template-versions/{v1['id']}/rollback")
        assert resp.status_code == 200
        assert ctx.config.get().comment_template == "评论1"

    def test_rollback_resume_to_builtin_normalizes(self, api_client):
        """回滚到「内置默认」版本时归一为空串保存（恢复内置默认语义）。"""
        tc, ctx = api_client
        from botler.config import DEFAULT_RESUME_PROMPT
        tc.put("/api/settings", json={"templates": {"resume": "自定义"}})
        tc.put("/api/settings", json={"templates": {"resume": "   "}})  # 恢复内置默认
        # 回滚到「自定义」版本
        v_custom = ctx.db.list_template_versions("global:resume")[1]
        tc.post(f"/api/template-versions/{v_custom['id']}/rollback")
        assert ctx.config.get().resume_template == "自定义"
        # 再回滚到「内置默认」版本 → 归一为空串（config 不留冗余全文）
        v_default = ctx.db.list_template_versions("global:resume")[1]
        resp = tc.post(f"/api/template-versions/{v_default['id']}/rollback")
        assert resp.status_code == 200
        assert ctx.config.get().resume_template == DEFAULT_RESUME_PROMPT
        config_text = __import__("pathlib").Path(ctx.config_path).read_text(encoding="utf-8")
        # config 中 resume 键被移除（归一为空串 = 恢复内置默认）
        assert DEFAULT_RESUME_PROMPT[:40] not in config_text or "resume:" not in config_text

    def test_rollback_missing_version_404(self, api_client):
        tc, _ = api_client
        assert tc.post("/api/template-versions/99999/rollback").status_code == 404

    def test_rollback_unknown_key_400(self, api_client):
        tc, ctx = api_client
        ctx.db.record_template_version("unknown:key", "x")
        rows = ctx.db.list_template_versions("unknown:key")
        assert tc.post(f"/api/template-versions/{rows[0]['id']}/rollback").status_code == 400


# ---- 保存埋点：仓库级覆盖 ----

class TestRepoTemplateVersions:
    def test_save_repo_template_creates_version(self, api_client):
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板1"})
        rows = ctx.db.list_template_versions(f"repo:{rid}")
        assert len(rows) == 1
        assert rows[0]["content"] == "仓库模板1"

    def test_save_repo_template_same_content_no_new_version(self, api_client):
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板"})
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板"})
        assert len(ctx.db.list_template_versions(f"repo:{rid}")) == 1
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板2"})
        assert len(ctx.db.list_template_versions(f"repo:{rid}")) == 2

    def test_save_repo_template_blank_clears_override(self, api_client):
        """清空覆盖（回退全局）也记录版本（空内容），历史可追溯。"""
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板"})
        tc.put(f"/api/repos/{rid}/template", json={"template": "   "})
        rows = ctx.db.list_template_versions(f"repo:{rid}")
        assert len(rows) == 2
        assert rows[0]["content"] == ""
        assert ctx.db.get_repo(rid)["prompt_template"] is None

    def test_rollback_repo_template_restores_content(self, api_client):
        """回滚仓库级模板后任务使用旧版仓库模板（resolve_template）。"""
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库1"})
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库2"})
        v1 = ctx.db.list_template_versions(f"repo:{rid}")[1]
        resp = tc.post(f"/api/template-versions/{v1['id']}/rollback")
        assert resp.status_code == 200
        assert ctx.db.get_repo(rid)["prompt_template"] == "仓库1"
        renderer = TemplateRenderer(ctx.config)
        repo = ctx.db.get_repo(rid)
        assert renderer.resolve_template(repo) == "仓库1"

    def test_rollback_repo_to_blank_clears_override(self, api_client):
        """回滚到「清空覆盖」版本 → prompt_template 为 None（回退全局）。"""
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板"})
        tc.put(f"/api/repos/{rid}/template", json={"template": "   "})  # 清空
        v_blank = ctx.db.list_template_versions(f"repo:{rid}")[0]  # 最新 = 空内容版本
        resp = tc.post(f"/api/template-versions/{v_blank['id']}/rollback")
        assert resp.status_code == 200
        assert ctx.db.get_repo(rid)["prompt_template"] is None

    def test_rollback_repo_missing_404(self, api_client):
        """仓库已删除时回滚其模板版本返回 404。"""
        tc, ctx = api_client
        rid = _add_repo(ctx.db)
        tc.put(f"/api/repos/{rid}/template", json={"template": "仓库模板"})
        v = ctx.db.list_template_versions(f"repo:{rid}")[0]
        ctx.db.soft_delete_repo(rid)
        resp = tc.post(f"/api/template-versions/{v['id']}/rollback")
        assert resp.status_code == 404
