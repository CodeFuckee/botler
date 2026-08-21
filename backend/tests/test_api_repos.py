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
from botler.labels import DEFAULT_LABELS

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
        # issue #157 默认标签同步：existing_labels 为远端已有标签，
        # created_labels 记录本次创建调用
        self.existing_labels: list[dict] = []
        self.created_labels: list[dict] = []

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

    def list_project_labels(self, project_id):
        """远端项目已有标签（issue #157 默认标签补齐的比对基准）。"""
        return list(self.existing_labels)

    def create_project_label(self, project_id, name, color, description=None):
        """记录默认标签创建调用（issue #157）。"""
        self.created_labels.append(
            {"name": name, "color": color, "description": description})
        return {"name": name, "color": color}


@pytest.fixture
def api_app(tmp_path, monkeypatch):
    """最小测试 app：只挂 repos 路由，ctx 用临时 config + db + 桩 gitlab。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    # issue #157：添加仓库时的默认标签同步经由 add_repo 内部新建的
    # GitLabClient（temp_client / fallback_client）调用 list_project_labels /
    # create_project_label——统一打桩到 stub，记录调用供断言（webhook 注册
    # 沿用各用例 monkeypatch 的既有模式）。
    monkeypatch.setattr(
        GitLabClient, "list_project_labels",
        lambda self, project_id: stub.list_project_labels(project_id))
    monkeypatch.setattr(
        GitLabClient, "create_project_label",
        lambda self, project_id, name, color, description=None:
            stub.create_project_label(project_id, name, color, description))
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
        # issue #157：远端无任何默认标签 → 14 个内置默认标签全部补齐
        assert [c["name"] for c in stub.created_labels] == [
            l["name"] for l in DEFAULT_LABELS]
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


class TestRemoteUser:
    """POST /api/repos/{id}/remote-user（issue #153）：读取 remote url 获取仓库用户。

    仓库用户 = remote URL userinfo 里的用户名（https://user:token@host/...），
    是用户在 git 配置里填写的账号（如 agent）；灵感一键提交 issue 时作为
    默认分配人。读取顺序：local_path 的 git remote -v → workspace 克隆 →
    仓库存储 url 的 userinfo；读取结果落库并随 /api/repos 返回。
    """

    def test_local_path_repo_reads_and_persists(self, client, tmp_path):
        """local_path 仓库：从本地 git remote -v 的 URL userinfo 读到用户名并落库。"""
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://agent:glpat-x@gitlab.example.com/group/project.git",
        })
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(
            project_id=42, name="demo", url="https://gitlab.example.com/group/project.git",
            local_path=str(repo_dir), remote_name="origin")
        resp = tc.post(f"/api/repos/{repo_id}/remote-user")
        assert resp.status_code == 200
        assert resp.json()["remote_username"] == "agent"
        assert db.get_repo(repo_id)["remote_username"] == "agent"

    def test_url_repo_reads_from_stored_url_userinfo(self, client, tmp_path):
        """URL 方式仓库（无 local_path 克隆）：从存储的 url userinfo 读到用户名。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(
            project_id=42, name="demo",
            url="https://bob:glpat-x@gitlab.example.com/group/project.git")
        resp = tc.post(f"/api/repos/{repo_id}/remote-user")
        assert resp.status_code == 200
        assert resp.json()["remote_username"] == "bob"
        assert db.get_repo(repo_id)["remote_username"] == "bob"

    def test_clean_url_returns_null_and_clears(self, client, tmp_path):
        """URL 无凭据：返回 null 并清除旧值（幂等，不报错）。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(
            project_id=42, name="demo",
            url="https://gitlab.example.com/group/project.git",
            remote_username="stale")
        resp = tc.post(f"/api/repos/{repo_id}/remote-user")
        assert resp.status_code == 200
        assert resp.json()["remote_username"] is None
        assert db.get_repo(repo_id)["remote_username"] is None

    def test_local_path_broken_falls_back_to_url(self, client, tmp_path):
        """local_path 不再是 git 仓库/目录不存在：尽力回退到存储 url 的 userinfo。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(
            project_id=42, name="remote-user-test",
            url="https://carol:glpat-x@gitlab.example.com/group/project.git",
            local_path=str(tmp_path / "nope"))
        resp = tc.post(f"/api/repos/{repo_id}/remote-user")
        assert resp.status_code == 200
        assert resp.json()["remote_username"] == "carol"

    def test_repo_not_found(self, client):
        tc, stub, tmp_path = client
        resp = tc.post("/api/repos/999/remote-user")
        assert resp.status_code == 404

    def test_list_repos_includes_remote_username(self, client, tmp_path):
        """/api/repos 列表返回 remote_username（前端设置弹窗展示）。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        db.upsert_repo(project_id=42, name="demo",
                       url="https://gitlab.example.com/group/project.git",
                       remote_username="agent")
        resp = tc.get("/api/repos")
        assert resp.status_code == 200
        row = next(r for r in resp.json()["repos"] if r["name"] == "demo")
        assert row["remote_username"] == "agent"

    def test_add_repo_captures_remote_username(self, client, monkeypatch, tmp_path):
        """添加仓库时自动从原始 remote url 捕获仓库用户（local_path 带凭据）。"""
        tc, stub, tmp_path = client
        repo_dir = tmp_path / "repo"
        _init_repo_with_remotes(repo_dir, {
            "origin": "https://agent:glpat-add@gitlab.example.com/group/project.git",
        })
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: {"id": 1})
        resp = tc.post("/api/repos", json={
            "local_path": str(repo_dir), "remote_name": "origin", "name": "demo"})
        assert resp.status_code == 201
        assert resp.json()["remote_username"] == "agent"
        db = Database(str(tmp_path / "test.db"))
        assert db.get_repo_by_project_id(42)["remote_username"] == "agent"

    def test_update_repo_url_recomputes_remote_username(self, client, tmp_path):
        """更新仓库 url（带凭据）：remote_username 随新 url 重新推导。"""
        tc, stub, tmp_path = client
        db = Database(str(tmp_path / "test.db"))
        repo_id = db.upsert_repo(
            project_id=42, name="demo",
            url="https://old-user:glpat-a@gitlab.example.com/group/project.git")
        resp = tc.put(f"/api/repos/{repo_id}", json={
            "url": "https://new-user:glpat-b@gitlab.example.com/group/project.git"})
        assert resp.status_code == 200
        assert resp.json()["remote_username"] == "new-user"
        assert db.get_repo(repo_id)["remote_username"] == "new-user"


class TestAddRepoSyncDefaultLabels:
    """添加仓库时补齐标记库内置默认标签（issue #157）。

    目标 GitLab 项目上缺失的默认标签逐个创建；已存在的标签保持不变
    （不覆盖用户已有颜色/描述）；读取/创建失败为尽力而为，不阻塞仓库添加。
    """

    def _add(self, client, monkeypatch, **extra):
        tc, stub, _ = client
        monkeypatch.setattr(
            GitLabClient, "register_webhook",
            lambda self, project_id, secret: {"id": 1})
        return tc.post("/api/repos", json={
            "url": "https://gitlab.example.com/group/p.git", "name": "demo",
            **extra})

    def test_creates_all_default_labels_when_none_exist(self, client, monkeypatch):
        """远端无任何默认标签 → 14 个内置默认标签全部创建，颜色/描述与规范一致。"""
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 201
        created = [c["name"] for c in client[1].created_labels]
        assert created == [l["name"] for l in DEFAULT_LABELS]
        assert client[1].created_labels[0] == {
            "name": "bug", "color": "#d9534f", "description": "缺陷修复"}

    def test_only_creates_missing_labels(self, client, monkeypatch):
        """远端已存在部分默认标签 → 只创建缺失的，已存在的不重复创建。"""
        tc, stub, _ = client
        stub.existing_labels = [
            {"name": "bug", "color": "#d9534f", "description": "缺陷修复"},
            {"name": "feature", "color": "#009966", "description": "新功能"},
            {"name": "自定义", "color": "#123456", "description": "自定义标签"},
        ]
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 201
        created_names = [c["name"] for c in stub.created_labels]
        assert "bug" not in created_names and "feature" not in created_names
        assert "自定义" not in created_names
        assert len(created_names) == len(DEFAULT_LABELS) - 2

    def test_existing_label_with_different_color_not_overwritten(self, client, monkeypatch):
        """已存在同名标签（颜色/描述与规范不同）→ 不覆盖，仅补缺失。"""
        tc, stub, _ = client
        stub.existing_labels = [
            {"name": "feature", "color": "#000000", "description": "用户自定义"},
        ]
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 201
        assert all(c["name"] != "feature" for c in stub.created_labels)

    def test_label_list_failure_does_not_block_add(self, client, monkeypatch):
        """读取远端标签失败 → 跳过同步，仓库仍添加成功（尽力而为）。"""
        tc, stub, _ = client
        monkeypatch.setattr(
            GitLabClient, "list_project_labels",
            lambda self, project_id: (_ for _ in ()).throw(GitLabError("网络错误", 500)))
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 201
        assert stub.created_labels == []

    def test_label_create_failure_does_not_block_add(self, client, monkeypatch):
        """单个标签创建失败 → 记日志跳过，其余标签照常创建，仓库仍添加成功。"""
        tc, stub, _ = client
        def failing_create(self, project_id, name, color, description=None):
            if name == "bug":
                raise GitLabError("权限不足", 403)
            return stub.create_project_label(project_id, name, color, description)
        monkeypatch.setattr(GitLabClient, "create_project_label", failing_create)
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 201
        created_names = [c["name"] for c in stub.created_labels]
        assert "bug" not in created_names
        assert len(created_names) == len(DEFAULT_LABELS) - 1

    def test_duplicate_repo_skips_label_sync(self, client, monkeypatch):
        """仓库已存在（409）→ 不做标签同步。"""
        tc, stub, _ = client
        tc.app.state.ctx.db.upsert_repo(
            42, "已存在", "https://gitlab.example.com/group/p.git")
        resp = self._add(client, monkeypatch)
        assert resp.status_code == 409
        assert stub.created_labels == []


class TestRepoTaskParamsOverride:
    """仓库级重试/引擎覆盖（issue #237，issue #424 移除执行超时）。"""

    def _mk_repo(self, tc) -> int:
        return tc.app.state.ctx.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")

    def test_update_saves_retry_and_engine(self, client):
        tc, stub, tmp_path = client
        repo_id = self._mk_repo(tc)
        resp = tc.put(f"/api/repos/{repo_id}", json={
            "max_retries": 5, "engine": "dsh"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_retries"] == 5
        assert data["engine"] == "dsh"

    def test_legacy_timeout_is_ignored(self, client):
        """旧客户端提交超时字段不报错，也不能保存为新配置。"""
        tc, stub, tmp_path = client
        repo_id = self._mk_repo(tc)
        resp = tc.put(f"/api/repos/{repo_id}", json={"timeout_seconds": 7201})
        assert resp.status_code == 200
        assert "timeout_seconds" not in resp.json()

    def test_update_out_of_range_retries_rejected(self, client):
        tc, stub, tmp_path = client
        repo_id = self._mk_repo(tc)
        for bad in [{"max_retries": -1}, {"max_retries": 21}]:
            assert tc.put(f"/api/repos/{repo_id}", json=bad).status_code == 422
