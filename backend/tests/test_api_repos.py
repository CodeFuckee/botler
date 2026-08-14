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
from botler.gitlab_client import GitLabClient, GitLabError

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


class TestUrlMasking:
    """仓库 URL 脱敏（issue #60）：token 不出现在 API 返回的 URL 上，
    DB / config.yaml 仍保存真实 URL 供 clone 使用。"""

    def test_list_repos_masks_url(self, client):
        tc, stub, tmp_path = client
        # 直接落一条带 token url 的仓库（模拟 local_path 方式添加、remote 带凭据）
        from botler.database import Database
        db = Database(str(tmp_path / "test.db"))
        # 复用 fixture 的 db：重新打开同一文件即可读到
        db.upsert_repo(project_id=42, name="带凭据",
                       url="https://agent:glpat-secret@gitlab.example.com/group/project.git")
        # 注意 fixture db 与这里打开的是同一 sqlite 文件（test.db）
        resp = tc.get("/api/repos")
        assert resp.status_code == 200
        row = next(r for r in resp.json()["repos"] if r["name"] == "带凭据")
        assert row["url"] == "https://agent:***@gitlab.example.com/group/project.git"
        # 原始 url 仍保留在 DB（真实凭据供执行使用）
        raw = db.get_repo_by_project_id(42)
        assert raw["url"] == "https://agent:glpat-secret@gitlab.example.com/group/project.git"

    def test_list_repos_clean_url_unchanged(self, client):
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        db.upsert_repo(project_id=43, name="干净",
                       url="https://gitlab.example.com/group/project.git")
        resp = tc.get("/api/repos")
        row = next(r for r in resp.json()["repos"] if r["name"] == "干净")
        assert row["url"] == "https://gitlab.example.com/group/project.git"

    def test_discover_remote_masks_url(self, client):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://agent:glpat-x@gitlab.example.com/group/project.git",
        })
        resp = tc.post("/api/repos/discover", json={"local_path": str(repo_dir)})
        assert resp.status_code == 200
        assert resp.json()["remotes"][0]["url"] == \
            "https://agent:***@gitlab.example.com/group/project.git"

    def test_add_repo_local_path_with_token_masks_response(self, client, monkeypatch):
        """local_path 添加（remote 带 token 且 GitLab 不返回干净 url 时）：
        响应 url 脱敏，DB 保留真实 url（供 clone 使用）。"""
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://agent:glpat-add@gitlab.example.com/group/project.git",
        })
        # GitLab 项目对象不带 http_url_to_repo / web_url → url 回退 remote 原始值
        stub.resolve_project = lambda url_or_id: {
            "id": 42, "path": "group/project", "name": "project"}
        registered: list[int] = []
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: registered.append(project_id) or {"id": 1})

        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir), "remote_name": "origin", "name": "本地带凭据"})

        assert resp.status_code == 201
        assert resp.json()["url"] == "https://agent:***@gitlab.example.com/group/project.git"
        db = Database(str(tmp_path / "test.db"))
        raw = db.get_repo_by_project_id(42)
        assert raw["url"] == "https://agent:glpat-add@gitlab.example.com/group/project.git"

    def test_update_repo_masked_url_ignored(self, client):
        """update 回传掩码 url（含 *）：忽略该字段，不污染 DB 真实凭据。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(project_id=42, name="带凭据",
                                 url="https://agent:glpat-keep@gitlab.example.com/group/project.git")
        resp = tc.put(f"/api/repos/{repo_id}", json={
            "name": "改名",
            "url": "https://agent:***@gitlab.example.com/group/project.git",
        })
        assert resp.status_code == 200
        raw = db.get_repo(repo_id)
        assert raw["url"] == "https://agent:glpat-keep@gitlab.example.com/group/project.git"
        assert raw["name"] == "改名"


class TestDeleteRepo:
    """删除仓库（issue #61）：注销 webhook + 从 config 移除 + db 软删除。"""

    def _add_repo(self, client, monkeypatch):
        """添加一个 url 方式的仓库（webhook 注册打桩），返回 (repo_id, project_id)。"""
        tc, stub, _ = client
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: {"id": 1})
        resp = tc.post("/api/repos", json={
            "url": "https://gitlab.example.com/group/graph2plan.git",
            "name": "graph2plan",
        })
        assert resp.status_code == 201
        return resp.json()["id"], resp.json()["gitlab_project_id"]

    def test_delete_repo_success(self, client, monkeypatch):
        """删除仓库：200 + 注销 webhook + config 移除 + db 软删除（复现 issue #61）。"""
        tc, stub, tmp_path = client
        repo_id, project_id = self._add_repo(client, monkeypatch)

        resp = tc.delete(f"/api/repos/{repo_id}")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        # webhook 注销被调用
        assert stub.unregistered == [project_id]
        # config.yaml 中该仓库被移除（config 是唯一事实来源）
        settings = ConfigManager(str(tmp_path / "config.yaml")).get()
        assert all(r.project_id != project_id for r in settings.repos)
        # db 软删除（issue #62：deleted_at 标记 + enabled=False，行保留供任务历史）
        row = tc.app.state.ctx.db.get_repo(repo_id)
        assert row is not None
        assert not row["enabled"]
        assert row["deleted_at"] is not None

    def test_delete_repo_not_found(self, client):
        """删除不存在的仓库返回 404。"""
        tc, stub, tmp_path = client
        resp = tc.delete("/api/repos/999")
        assert resp.status_code == 404

    def test_delete_repo_unregister_webhook_failure_not_blocking(self, client, monkeypatch):
        """注销 webhook 失败（GitLabError）不阻塞删除（尽力而为）。"""
        tc, stub, tmp_path = client
        repo_id, project_id = self._add_repo(client, monkeypatch)

        def boom(_project_id):
            raise GitLabError("token 无效或已过期（401）", 401)
        stub.unregister_webhook = boom

        resp = tc.delete(f"/api/repos/{repo_id}")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        row = tc.app.state.ctx.db.get_repo(repo_id)
        assert not row["enabled"]
        assert row["deleted_at"] is not None

    def test_delete_repo_removed_from_list(self, client, monkeypatch):
        """删除后 GET /api/repos 不再返回该仓库（复现 issue #62）。"""
        tc, stub, tmp_path = client
        repo_id, project_id = self._add_repo(client, monkeypatch)

        resp = tc.delete(f"/api/repos/{repo_id}")
        assert resp.status_code == 200, resp.text

        listing = tc.get("/api/repos")
        assert listing.status_code == 200
        ids = [r["id"] for r in listing.json()["repos"]]
        assert repo_id not in ids

    def test_disabled_repo_still_listed(self, client, monkeypatch):
        """停用（未删除）的仓库仍出现在列表中（可重新启用，与删除区分）。"""
        tc, stub, tmp_path = client
        repo_id, project_id = self._add_repo(client, monkeypatch)

        resp = tc.put(f"/api/repos/{repo_id}", json={"enabled": False})
        assert resp.status_code == 200, resp.text

        listing = tc.get("/api/repos")
        assert listing.status_code == 200
        ids = [r["id"] for r in listing.json()["repos"]]
        assert repo_id in ids

    def test_delete_then_readd_same_project(self, client, monkeypatch):
        """删除后重新添加同 project_id 仓库：201 成功且清除删除标记（issue #62）。"""
        tc, stub, tmp_path = client
        repo_id, project_id = self._add_repo(client, monkeypatch)

        resp = tc.delete(f"/api/repos/{repo_id}")
        assert resp.status_code == 200, resp.text

        resp = tc.post("/api/repos", json={
            "url": "https://gitlab.example.com/group/graph2plan.git",
            "name": "graph2plan",
        })
        assert resp.status_code == 201, resp.text
        readded = resp.json()
        # upsert 复用原行并清除删除标记
        assert readded["id"] == repo_id
        row = tc.app.state.ctx.db.get_repo(repo_id)
        assert row["deleted_at"] is None
        assert row["enabled"]
        # 列表再次可见
        listing = tc.get("/api/repos")
        ids = [r["id"] for r in listing.json()["repos"]]
        assert repo_id in ids


class TestAddRepoGlobalToken401Fallback:
    """issue #77：全局 bot token 失效（401）时，remote URL 内嵌 token 兜底。

    用户场景：本地仓库 remote url 带用户名和 token（git pull/push 正常），
    但平台全局 token 失效，添加本地仓库时报「无法识别项目: token 无效或
    已过期（401）」。修复后：识别项目与注册 webhook 均改用 remote 内嵌
    token 的临时 client 重试。
    """

    def test_add_repo_global_401_falls_back_to_remote_token(self, client, monkeypatch):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://user:secret-token@gitlab.example.com/group/project.git",
        })

        # 全局 client 桩：token 失效，识别项目抛 401
        def resolve_401(url_or_id):
            raise GitLabError("token 无效或已过期（401）", 401)
        stub.resolve_project = resolve_401

        # 真实 GitLabClient 网络层按 token 区分：
        # remote 内嵌 token 有效；全局 token（test-token）仍 401
        def fake_request(self, method, path, **kwargs):
            if self.token == "secret-token":
                return {"id": 42, "path": "group/project", "name": "project",
                        "http_url_to_repo": "https://gitlab.example.com/group/project.git"}
            raise GitLabError("token 无效或已过期（401）", 401)
        monkeypatch.setattr(GitLabClient, "_request", fake_request)

        # 记录 webhook 注册所用 client 的 token
        webhook_tokens: list[str] = []
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: webhook_tokens.append(self.token) or {"id": 1})

        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir),
            "remote_name": "origin",
            "name": "graph2plan",
            "webhook_url": "https://hooks.example.com",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["gitlab_project_id"] == 42
        # 识别与 webhook 注册均走 remote 内嵌 token，而非失效的全局 token
        assert webhook_tokens == ["secret-token"]

    def test_add_repo_global_401_without_remote_token_still_fails(self, client):
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://gitlab.example.com/group/project.git",
        })

        def resolve_401(url_or_id):
            raise GitLabError("token 无效或已过期（401）", 401)
        stub.resolve_project = resolve_401

        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir), "remote_name": "origin"})
        assert resp.status_code == 400
        assert "无法识别项目" in resp.json()["detail"]

    def test_add_repo_webhook_register_401_falls_back_to_remote_token(self, client, monkeypatch):
        """识别成功（全局 token 有效）但 webhook 注册 401 时同样兜底。

        识别成功后入库 url 会被替换为 API 返回的干净 url（无 token），
        兜底必须用原始 remote URL 解析 token（识别前的值）。
        """
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://user:secret-token@gitlab.example.com/group/project.git",
        })

        # 全局 token 注册 webhook 失效；remote 内嵌 token 有效
        def fake_register(self, project_id, secret):
            if self.token == "secret-token":
                return {"id": 1}
            raise GitLabError("token 无效或已过期（401）", 401)
        monkeypatch.setattr(GitLabClient, "register_webhook", fake_register)

        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir), "remote_name": "origin"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["gitlab_project_id"] == 42
