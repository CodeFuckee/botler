"""仓库管理 API 测试：本地文件夹方式添加仓库（discover + add_repo with local_path）。"""

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

    def test_browse_default_path_is_root(self, client):
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/"

    def test_browse_blank_path_uses_root(self, client):
        """空 path 参数不应解析到服务进程工作目录，回退到根目录。"""
        tc, stub, tmp_path = client
        resp = tc.get("/api/repos/browse", params={"path": "  "})
        assert resp.status_code == 200
        assert resp.json()["path"] == "/"

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
