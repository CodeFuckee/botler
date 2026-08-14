"""仓库管理 API 测试：本地文件夹方式添加仓库（discover + add_repo with local_path）。"""

import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabClient

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


class StubGitLab:
    """记录调用并返回固定项目信息的 GitLab 桩。"""

    def __init__(self):
        self.resolved_urls: list[str] = []
        self.unregistered: list[int] = []

    def resolve_project(self, url_or_id):
        self.resolved_urls.append(url_or_id)
        return {
            "id": 42,
            "path": "group/project",
            "name": "project",
            "http_url_to_repo": "https://gitlab.example.com/group/project.git",
        }

    def unregister_webhook(self, project_id):
        self.unregistered.append(project_id)
        return 0


@pytest.fixture
def api_app(tmp_path):
    """最小测试 app：只挂 repos 路由，ctx 用临时 config + db + 桩 gitlab。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    # 用 SimpleNamespace 而非 botler.main.AppContext：import main 会触发
    # 模块级 create_app() 加载真实 config.yaml（凭据未设置时报错）。
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    # 挂完整 api 路由（/api 前缀与生产一致）
    app.include_router(api_router)
    return app, stub, tmp_path


@pytest.fixture
def client(api_app):
    app, stub, tmp_path = api_app
    return TestClient(app), stub, tmp_path


def _init_repo_with_remotes(path, remotes: dict) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)


class TestDiscoverRemote:
    def test_returns_all_remotes(self, client):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "git@gitlab.example.com:group/project.git",
            "upstream": "https://gitlab.example.com/upstream/project.git",
        })
        resp = tc.post("/api/repos/discover", json={"local_path": str(repo_dir)})
        assert resp.status_code == 200
        remotes = resp.json()["remotes"]
        assert [r["name"] for r in remotes] == ["origin", "upstream"]
        assert remotes[0]["url"] == "git@gitlab.example.com:group/project.git"

    def test_not_a_git_repo(self, client):
        tc, stub, tmp_path = client
        resp = tc.post("/api/repos/discover", json={"local_path": str(tmp_path / "nope")})
        assert resp.status_code == 400
        assert "读取本地仓库 remote 失败" in resp.json()["detail"]


class TestAddRepoWithLocalPath:
    def test_add_repo_from_local_path(self, client, monkeypatch):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {"origin": "git@gitlab.example.com:group/project.git"})

        registered: list[int] = []
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: registered.append(project_id) or {"id": 1})

        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir),
            "remote_name": "origin",
            "name": "本地项目",
            "webhook_url": "https://hooks.example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["gitlab_project_id"] == 42
        assert data["local_path"] == str(repo_dir)
        assert data["remote_name"] == "origin"
        # 识别项目用的是从本地 remote 读出的 URL（scp-like 形态）
        assert stub.resolved_urls == ["git@gitlab.example.com:group/project.git"]
        assert registered == [42]
        # 写回 config.yaml（config 是唯一事实来源）
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "local_path" in config_text
        assert str(repo_dir) in config_text

    def test_add_repo_local_path_not_a_git_repo(self, client):
        tc, stub, tmp_path = client
        resp = tc.post("/api/repos", json={
            "local_path": str(tmp_path / "nope"), "remote_name": "origin"})
        assert resp.status_code == 400

    def test_add_repo_missing_remote_name(self, client):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {"origin": "git@gitlab.example.com:group/project.git"})
        resp = tc.post("/api/repos", json={"local_path": str(repo_dir)})
        assert resp.status_code == 400

    def test_add_repo_no_url_no_local_path(self, client):
        tc, stub, tmp_path = client
        resp = tc.post("/api/repos", json={"name": "无来源"})
        assert resp.status_code == 400


class TestBrowseDirectories:
    """目录浏览 API（供前端目录选择对话框使用）。"""

    def test_browse_returns_subdirs(self, client):
        tc, stub, tmp_path = client
        (tmp_path / "repo").mkdir()
        (tmp_path / "plain").mkdir()
        (tmp_path / "file.txt").write_text("x")
        resp = tc.get("/api/repos/browse", params={"path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == str(tmp_path)
        assert data["parent"] == str(tmp_path.parent)
        names = [d["name"] for d in data["subdirs"]]
        assert names == ["plain", "repo"]
        assert all(d["is_git"] is False for d in data["subdirs"])
        assert all(d["readable"] is True for d in data["subdirs"])

    def test_browse_marks_git_repo(self, client):
        tc, stub, tmp_path = client
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        resp = tc.get("/api/repos/browse", params={"path": str(tmp_path)})
        assert resp.status_code == 200
        by_name = {d["name"]: d for d in resp.json()["subdirs"]}
        assert by_name["repo"]["is_git"] is True

    def test_browse_without_path_uses_home(self, client):
        """不带 path 时初始定位到服务器用户主目录（默认初始目录）。"""
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse")
        assert resp.status_code == 200
        assert resp.json()["path"] == os.path.expanduser("~")

    def test_browse_blank_path_uses_default(self, client):
        """空 path 参数走默认初始目录（而非解析到进程工作目录）。"""
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse", params={"path": "  "})
        assert resp.status_code == 200
        assert resp.json()["path"] == os.path.expanduser("~")

    def test_browse_missing_path_returns_400(self, client):
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse", params={"path": str(tmp_path / "nope")})
        assert resp.status_code == 400
        assert "路径" in resp.json()["detail"]

    def test_browse_file_path_returns_400(self, client):
        tc, stub, tmp_path = client
        f = tmp_path / "file.txt"
        f.write_text("x")
        resp = tc.get("/api/repos/browse", params={"path": str(f)})
        assert resp.status_code == 400


class TestPriority:
    """仓库优先级字段（issue #51）：整数 1~999，默认 100，数字越小越优先。"""

    def _add_repo(self, client, monkeypatch, **extra):
        """添加一个 url 方式的仓库（webhook 注册打桩），返回响应。"""
        tc, stub, _ = client
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: {"id": 1})
        return tc.post("/api/repos", json={"url": "https://gitlab.example.com/group/p.git",
                                           **extra})

    def test_add_repo_default_priority(self, client, monkeypatch):
        """不带 priority 添加时默认 100。"""
        resp = self._add_repo(client, monkeypatch, name="demo")
        assert resp.status_code == 201
        assert resp.json()["priority"] == 100

    def test_add_repo_custom_priority(self, client, monkeypatch):
        """带 priority 添加时按指定值落库。"""
        resp = self._add_repo(client, monkeypatch, name="demo", priority=1)
        assert resp.status_code == 201
        assert resp.json()["priority"] == 1

    def test_add_repo_invalid_priority_rejected(self, client, monkeypatch):
        """越界/非整数 priority 一律拒绝（pydantic 校验 422）。"""
        for bad in [0, 1000, -5, "high", 1.5]:
            resp = self._add_repo(client, monkeypatch, name="demo", priority=bad)
            assert resp.status_code == 422, f"priority={bad!r} 应返回 422"

    def test_update_priority(self, client):
        """PUT 更新优先级生效并返回。"""
        tc, stub, tmp_path = client
        repo_id = tc.app.state.ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        resp = tc.put(f"/api/repos/{repo_id}", json={"priority": 50})
        assert resp.status_code == 200
        assert resp.json()["priority"] == 50
        row = tc.app.state.ctx.db.get_repo(repo_id)
        assert row["priority"] == 50, "优先级应落库"

    def test_update_invalid_priority_rejected(self, client):
        """更新时越界值拒绝，原值不变。"""
        tc, stub, tmp_path = client
        repo_id = tc.app.state.ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        for bad in [0, 1000, "high"]:
            resp = tc.put(f"/api/repos/{repo_id}", json={"priority": bad})
            assert resp.status_code == 422, f"priority={bad!r} 应返回 422"
        assert tc.app.state.ctx.db.get_repo(repo_id)["priority"] == 100

    def test_update_name_and_enabled_with_priority(self, client):
        """编辑弹窗一次提交 name/enabled/priority 全部生效。"""
        tc, stub, tmp_path = client
        repo_id = tc.app.state.ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        resp = tc.put(f"/api/repos/{repo_id}",
                      json={"name": "新名字", "enabled": False, "priority": 30})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新名字"
        assert data["enabled"] is False
        assert data["priority"] == 30

    def test_list_repos_sorted_by_priority(self, client):
        """列表按 priority 升序（数字小在前），同优先级按 id。"""
        tc, stub, tmp_path = client
        db = tc.app.state.ctx.db
        db.upsert_repo(11, "low", "https://gitlab.example.com/group/low.git", priority=100)
        db.upsert_repo(22, "mid", "https://gitlab.example.com/group/mid.git", priority=50)
        db.upsert_repo(33, "high", "https://gitlab.example.com/group/high.git", priority=1)
        resp = tc.get("/api/repos")
        assert resp.status_code == 200
        repos = resp.json()["repos"]
        assert [r["priority"] for r in repos] == [1, 50, 100]
        assert [r["name"] for r in repos] == ["high", "mid", "low"]


class TestBrowseDefaultPath:
    def test_configured_default_path_used(self, client, tmp_path):
        """配置了 browse.default_path 时，不带 path 请求定位到该目录。"""
        tc, stub, _ = client
        target = tmp_path / "custom-start"
        target.mkdir()
        # 在配置文件中写入 default_path 后重新加载
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT.replace("worker: {}", f"worker: {{}}\nbrowse:\n  default_path: {target}"),
            encoding="utf-8",
        )
        resp = tc.get("/api/repos/browse")
        assert resp.status_code == 200
        assert resp.json()["path"] == str(target)

    def test_explicit_root_still_works(self, client):
        """显式 path=/ 仍浏览根目录（用户手动跳转不受影响）。"""
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse", params={"path": "/"})
        assert resp.status_code == 200
        assert resp.json()["path"] == "/"
        assert resp.json()["parent"] is None
