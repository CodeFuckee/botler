"""技能管理 API 测试（issue #282）：技能页面的后端接口。

覆盖：
- GET /api/skills：按执行引擎分组返回技能（目录根 + exists 标记 +
  含 SKILL.md 的技能列表 + frontmatter description；默认引擎标记；
  嵌套技能名含 / 走 query 参数可寻址）；
- GET /api/skills/{engine}/files?skill=...：技能目录内 md 文件列表
  （递归相对路径）；
- GET /api/skills/{engine}/file?skill=...&path=...：读取 md 文件内容；
- PUT /api/skills/{engine}/file：保存 md 文件内容；
- 安全：未注册引擎 404、技能不存在 / 非法技能名 404、路径穿越 /
  绝对路径 / 非 md 文件 400、文件不存在 404。

测试用临时 HOME / HERMES_HOME / DSH_HOME 隔离，不触碰真实部署目录。
"""

from pathlib import Path
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


def make_skill(root: Path, name: str, description: str,
               extra_md: list[str] | None = None) -> None:
    """在技能根下建技能目录（SKILL.md + 可选附加 md 文件）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {Path(name).name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8")
    for rel in extra_md or []:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n", encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 HOME / 引擎数据目录到临时目录，并预置三引擎技能。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / ".dsh"))
    # claude：两个技能（一扁平一嵌套）
    make_skill(tmp_path / ".claude" / "skills", "animate", "做动画",
               extra_md=["RECIPES.md"])
    make_skill(tmp_path / ".claude" / "skills", "nested/group/spike", "嵌套技能")
    # hermes
    make_skill(tmp_path / ".hermes" / "skills", "grill-me", "拷问设计",
               extra_md=["docs/guide.md"])
    # dsh：~/.dsh/skills 空 + ~/.agents/skills 一个技能
    (tmp_path / ".dsh" / "skills").mkdir(parents=True)
    make_skill(tmp_path / ".agents" / "skills", "find-skills", "查找技能")
    # 配置文件与测试 app（与 test_api_plugins.py 同款 fixture）
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    yield TestClient(app), tmp_path


class TestListSkills:
    def test_grouped_by_engine(self, client):
        tc, _ = client
        resp = tc.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "claude"
        by_name = {e["name"]: e for e in data["engines"]}
        assert set(by_name) == {"claude", "hermes", "dsh"}
        # 默认引擎标记（worker.engine=claude）
        assert by_name["claude"]["default"] is True
        assert by_name["hermes"]["default"] is False
        # claude 技能：扁平 + 嵌套（嵌套名含 /）
        claude = by_name["claude"]
        assert [s["name"] for s in claude["skills"]] == [
            "animate", "nested/group/spike"]
        assert claude["skills"][0]["description"] == "做动画"
        assert claude["skills"][0]["root"] == str(
            Path.home() / ".claude" / "skills")
        # 根 exists 标记
        assert claude["roots"] == [{
            "path": str(Path.home() / ".claude" / "skills"),
            "exists": True}]
        # dsh：~/.dsh/skills 存在但无技能、~/.agents/skills 有技能
        dsh = by_name["dsh"]
        assert len(dsh["roots"]) == 2
        assert all(r["exists"] for r in dsh["roots"])
        assert [s["name"] for s in dsh["skills"]] == ["find-skills"]

    def test_missing_root_exists_false(self, tmp_path, monkeypatch):
        """HERMES_HOME 指向不存在目录 → hermes 根 exists=False、无技能。"""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
        make_skill(tmp_path / ".claude" / "skills", "animate", "做动画")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        ctx = SimpleNamespace(config=config,
                              db=Database(str(tmp_path / "test.db")))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        data = tc.get("/api/skills").json()
        hermes = next(e for e in data["engines"] if e["name"] == "hermes")
        assert hermes["roots"] == [{
            "path": str(tmp_path / "no-hermes" / "skills"),
            "exists": False}]
        assert hermes["skills"] == []


class TestListSkillFiles:
    def test_recursive_md_files(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/files", params={"skill": "animate"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "claude"
        assert data["skill"] == "animate"
        assert data["files"] == ["RECIPES.md", "SKILL.md"]

    def test_nested_skill_files(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/files",
                      params={"skill": "nested/group/spike"})
        assert resp.status_code == 200
        assert resp.json()["files"] == ["SKILL.md"]

    def test_unknown_engine_404(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/nope/files", params={"skill": "animate"})
        assert resp.status_code == 404

    def test_unknown_skill_404(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/files", params={"skill": "no-such"})
        assert resp.status_code == 404

    def test_invalid_skill_name_404(self, client):
        tc, _ = client
        for bad in ("../x", "a/../../x", "/etc", "a\\b"):
            resp = tc.get("/api/skills/claude/files", params={"skill": bad})
            assert resp.status_code == 404, bad


class TestReadSkillFile:
    def test_read_skill_md(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/file",
                      params={"skill": "animate", "path": "SKILL.md"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "SKILL.md"
        assert "# animate" in data["content"]

    def test_read_nested_md(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/hermes/file",
                      params={"skill": "grill-me", "path": "docs/guide.md"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "# docs/guide.md\n"

    def test_read_nested_skill(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/file",
                      params={"skill": "nested/group/spike", "path": "SKILL.md"})
        assert resp.status_code == 200
        assert "# nested/group/spike" in resp.json()["content"]

    def test_missing_file_404(self, client):
        tc, _ = client
        resp = tc.get("/api/skills/claude/file",
                      params={"skill": "animate", "path": "no-such.md"})
        assert resp.status_code == 404

    def test_traversal_400(self, client):
        tc, _ = client
        for bad in ("../evil.md", "../../x.md", "/etc/passwd"):
            resp = tc.get("/api/skills/claude/file",
                          params={"skill": "animate", "path": bad})
            assert resp.status_code == 400, bad

    def test_non_md_400(self, client):
        tc, _ = client
        for bad in ("script.py", "SKILL.txt"):
            resp = tc.get("/api/skills/claude/file",
                          params={"skill": "animate", "path": bad})
            assert resp.status_code == 400, bad


class TestWriteSkillFile:
    def test_save_existing_md(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/claude/file", json={
            "skill": "animate", "path": "SKILL.md", "content": "# 新标题\n"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["is_skill_md"] is True
        # 重新读取验证落盘
        got = tc.get("/api/skills/claude/file",
                     params={"skill": "animate", "path": "SKILL.md"}).json()
        assert got["content"] == "# 新标题\n"

    def test_save_new_nested_md(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/hermes/file", json={
            "skill": "grill-me", "path": "notes/new.md", "content": "备忘"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        files = tc.get("/api/skills/hermes/files",
                       params={"skill": "grill-me"}).json()["files"]
        assert "notes/new.md" in files

    def test_save_traversal_400(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/claude/file", json={
            "skill": "animate", "path": "../escape.md", "content": "x"})
        assert resp.status_code == 400

    def test_save_non_md_400(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/claude/file", json={
            "skill": "animate", "path": "run.py", "content": "x"})
        assert resp.status_code == 400

    def test_save_unknown_skill_404(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/claude/file", json={
            "skill": "no-such", "path": "SKILL.md", "content": "x"})
        assert resp.status_code == 404

    def test_save_empty_path_400(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/claude/file", json={
            "skill": "animate", "path": "", "content": "x"})
        assert resp.status_code == 400

    def test_unknown_engine_404(self, client):
        tc, _ = client
        resp = tc.put("/api/skills/nope/file", json={
            "skill": "animate", "path": "SKILL.md", "content": "x"})
        assert resp.status_code == 404
