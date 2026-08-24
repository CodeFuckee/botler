"""概览页「自省」API 测试（issue #187）。

需求：概览页每个仓库卡片右上角新增「自省」按钮，点击后调用 AI agent
审查该仓库的功能与实现情况，对项目的改进提出建议，并把建议写到对应
仓库的 GitLab issue 里，分配人选择仓库的 owner。

覆盖：
- POST /api/repos/{repo_id}/introspect 正常路径：本地项目文件夹收集
  上下文（文件树 + README + 清单文件）→ AI 审查 → 创建 issue（标题带
  【自省】前缀、标签 optimize、分配人 = 项目 owner id），返回 201 与
  精简 issue + 审查报告；
- 分配人解析：项目 owner 含 id 直接用；owner 只有 username 按用户名
  解析；项目 owner 读取失败兜底仓库 remote 用户名；都失败不指定分配人；
- 上下文兜底：无本地文件夹时走 GitLab 仓库 API（文件树 + README）；
- 边界：仓库不存在 404 / 软删除 400 / 未启用 400 / 未配置 AI 模型
  400 / 未配置 owner token 400 / AI 失败与空回复 502 / GitLab 创建
  issue 失败 502 / 创建成功后清空概览缓存；
- 输出：标题不超过 GitLab 255 字符硬上限（GITLAB_ISSUE_TITLE_MAX_LEN）。
"""

import base64
import os
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
    ctx = SimpleNamespace(config=config, db=db, gitlab=StubGitLab(),
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
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
    """便捷：插入一个仓库并返回本地 id（与灵感 API 测试同构）。"""
    return db.upsert_repo(
        project_id, name,
        url or f"https://gitlab.example.com/{name}.git",
        enabled=enabled, priority=priority, remote_username=remote_username,
        local_path=local_path, remote_name=remote_name)


def _write_project(tmp_path, subdir="proj"):
    """构造一个本地项目文件夹（README + 关键清单 + 源码文件），返回路径。"""
    root = tmp_path / subdir
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# 测试项目\n\n这是一个用于自省测试的示例项目。",
                                    encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo", "scripts": {}}',
                                       encoding="utf-8")
    (root / "src" / "main.py").write_text("def main():\n    print('hi')\n",
                                          encoding="utf-8")
    return root


class StubGitLab:
    """自省 API 的 GitLab 桩：项目 owner / 仓库文件树 / 文件内容 / 创建
    issue，均可配置返回值与故障注入。"""

    def __init__(self):
        self.create_calls: list[tuple[int, dict]] = []
        self.fail_create_projects: set[int] = set()
        self.create_result: dict | None = None
        self.calls: list[str] = []
        # 项目 owner：project_id -> {id, username}
        self.project_owners: dict[int, dict] = {}
        self.fail_project_projects: set[int] = set()
        # GitLab 兜底上下文：project_id -> {"tree": [...], "readme": str}
        self.tree_by_project: dict[int, list[dict]] = {}
        self.readme_by_project: dict[int, str] = {}
        self.fail_tree_projects: set[int] = set()
        # 用户名 → 用户 id（owner 只有 username 时按此解析）
        self.users_by_username: dict[str, int] = {}

    # ---- 项目 owner ----
    def get_project(self, project_id):
        self.calls.append(f"get_project:{project_id}")
        if project_id in self.fail_project_projects:
            raise GitLabError("模拟项目 owner 读取故障")
        return {"owner": dict(self.project_owners.get(project_id) or {})}

    # ---- 仓库文件树 / 文件内容（GitLab 兜底上下文） ----
    def _request(self, method, path, params=None):
        self.calls.append(f"{method} {path}")
        if path.endswith("/repository/tree"):
            if int(path.split("/")[2]) in self.fail_tree_projects:
                raise GitLabError("模拟文件树读取故障")
            return list(self.tree_by_project.get(int(path.split("/")[2]), []))
        if "/repository/files/" in path:
            proj_id = int(path.split("/")[2])
            fname = path.rsplit("/", 1)[1]
            content = self.readme_by_project.get(proj_id)
            if fname == "README.md" and content:
                encoded = base64.b64encode(content.encode("utf-8")).decode()
                return {"content": encoded}
            return {"content": ""}
        raise AssertionError(f"未预期的 GitLab 请求: {method} {path}")

    # ---- 用户解析 ----
    def get_user_id_by_username(self, username):
        self.calls.append(f"users:{username}")
        return self.users_by_username.get(username)

    # ---- 创建 issue ----
    def create_issue(self, project_id, title, description=None,
                     assignee_id=None, labels=None):
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
                "created_at": "2026-08-18T10:00:00.000+08:00",
                "description": description, "author": None,
                "milestone": None, "assignees": [], "user_notes_count": 0}

    # 灵感/概览测试用（概览缓存失效断言复用）
    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        self.calls.append(f"list_open_issues:{project_id}")
        return []

    def list_project_labels(self, project_id):
        return []


class StubChatClient:
    """AI 审查 ChatModelClient 桩：记录 chat 调用，可注入回复/故障。"""

    instances: list["StubChatClient"] = []
    reply: str = "审查报告正文"
    raise_error: Exception | None = None
    raise_http_error: bool = False

    def __init__(self, **kwargs):
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
def introspect_env(client, monkeypatch):
    """自省测试夹具：配置 owner token + AI 供应商 + 打桩 GitLabClient
    与 ChatModelClient，返回 (tc, stub, db)。"""
    tc, db = client
    tc.app.state.ctx.config.update_section("gitlab", {"owner_token": "owner-token-1"})
    tc.app.state.ctx.config.update_section("ai_providers", [{
        "name": "deepseek", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
    }])
    from botler.api import issues as issues_mod
    monkeypatch.setattr(
        issues_mod, "GitLabClient",
        lambda url, token, verify_ssl=True, webhook_base_url=None: tc.app.state.ctx.gitlab)
    from botler import chat_models as chat_mod
    StubChatClient.instances = []
    StubChatClient.reply = "审查报告正文"
    StubChatClient.raise_error = None
    StubChatClient.raise_http_error = False
    monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)
    return tc, tc.app.state.ctx.gitlab, db


class TestValidation:
    """仓库校验与前置条件。"""

    def test_repo_not_found(self, introspect_env):
        tc, stub, db = introspect_env
        r = tc.post("/api/repos/999/introspect")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_repo_soft_deleted(self, introspect_env):
        tc, stub, db = introspect_env
        repo_id = _add_repo(db, 42, "botler")
        db.soft_delete_repo(repo_id)
        r = tc.post(f"/api/repos/{repo_id}/introspect")
        assert r.status_code == 400
        assert "已删除" in r.json()["detail"]

    def test_repo_disabled(self, introspect_env):
        tc, stub, db = introspect_env
        repo_id = _add_repo(db, 42, "botler", enabled=False)
        r = tc.post(f"/api/repos/{repo_id}/introspect")
        assert r.status_code == 400
        assert "未启用" in r.json()["detail"]

    def test_without_ai_provider(self, client):
        """未配置 AI 对话模型：400 引导设置页（owner token 已配置也先拦）。"""
        tc, db = client
        tc.app.state.ctx.config.update_section("gitlab", {"owner_token": "owner-token-1"})
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/introspect")
        assert r.status_code == 400
        assert "AI 对话模型" in r.json()["detail"]

    def test_without_owner_token(self, client, monkeypatch):
        """未配置 owner token：创建 issue 被 _issue_edit_call 拦截（400），
        与概览页其他 issue 编辑一致，绝不回退 bot token。"""
        tc, db = client
        tc.app.state.ctx.config.update_section("ai_providers", [{
            "name": "deepseek", "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
        }])
        from botler import chat_models as chat_mod
        monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/introspect")
        assert r.status_code == 400
        assert "owner token" in r.json()["detail"]


class TestIntrospect:
    """POST /api/repos/{repo_id}/introspect 正常路径。"""

    def test_success_local_context(self, introspect_env, tmp_path):
        """本地项目文件夹收集上下文 → AI 审查 → 创建 issue：标题带
        【自省】前缀、标签 optimize、分配人 = 项目 owner id；chat 收到
        系统提示 + 含文件树/README 的用户上下文。"""
        tc, stub, db = introspect_env
        root = _write_project(tmp_path)
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        repo_id = _add_repo(db, 42, "botler", local_path=str(root))

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["review"] == "审查报告正文"
        assert body["issue"]["iid"] == 99
        assert body["issue"]["title"].startswith("【自省】")
        assert "项目审查与改进建议" in body["issue"]["title"]
        assert len(stub.create_calls) == 1
        project_id, params = stub.create_calls[0]
        assert project_id == 42
        assert params["assignee_id"] == 7
        assert params["labels"] == ["optimize"]
        assert "审查报告正文" in params["description"]
        # 审查上下文应包含项目文件树与 README（本地收集）
        assert StubChatClient.instances
        messages = StubChatClient.instances[0].chat_calls[0]
        assert messages[0]["role"] == "system"
        user_content = messages[1]["content"]
        # Windows 兼容（issue #469）：本地文件树路径分隔符随平台
        # （src\main.py），断言改为平台无关的 os.sep 拼接。
        assert "src" + os.sep + "main.py" in user_content
        assert "README.md" in user_content
        assert "测试项目" in user_content
        assert "package.json" in user_content

    def test_success_gitlab_context_fallback(self, introspect_env):
        """无本地文件夹（local_path 为空且工作区不存在）：走 GitLab 仓库
        API 收集文件树 + README，审查仍正常完成。"""
        tc, stub, db = introspect_env
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        stub.tree_by_project[42] = [
            {"path": "README.md", "type": "blob"},
            {"path": "src/main.py", "type": "blob"},
        ]
        stub.readme_by_project[42] = "GitLab 兜底 README 内容"
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        assert "GitLab 兜底 README 内容" in \
            StubChatClient.instances[0].chat_calls[0][1]["content"]
        assert stub.create_calls[0][1]["assignee_id"] == 7
        assert stub.create_calls[0][1]["labels"] == ["optimize"]

    def test_gitlab_context_failure_degrades(self, introspect_env):
        """本地无文件夹且 GitLab 兜底读取失败：审查仍基于仓库元信息完成
        （不阻塞），issue 正常创建。"""
        tc, stub, db = introspect_env
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        stub.fail_tree_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        user_content = StubChatClient.instances[0].chat_calls[0][1]["content"]
        assert "仓库信息" in user_content

    def test_title_respects_gitlab_limit(self, introspect_env, tmp_path):
        """仓库名超长时标题截断到 GitLab 255 字符上限内（issue #186 同规）。"""
        tc, stub, db = introspect_env
        root = _write_project(tmp_path)
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        repo_id = _add_repo(db, 42, "长" * 300, local_path=str(root))

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        title = stub.create_calls[0][1]["title"]
        assert len(title) <= 255
        assert title.endswith("…")

    def test_invalidates_overview_cache(self, introspect_env):
        """创建成功后清空概览缓存：下一次 overview 请求重新拉取。"""
        tc, stub, db = introspect_env
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        repo_id = _add_repo(db, 42, "botler")

        def open_issue_calls():
            return [x for x in stub.calls if x.startswith("list_open_issues:")]

        tc.get("/api/issues/overview")
        assert len(open_issue_calls()) == 1  # 首次拉取

        r = tc.post(f"/api/repos/{repo_id}/introspect")
        assert r.status_code == 201

        tc.get("/api/issues/overview")
        assert len(open_issue_calls()) == 2  # 缓存已失效，重新拉取


class TestIntrospectAssignee:
    """自省 issue 分配人 = 仓库 owner 的解析行为（issue #187）。"""

    def test_owner_without_id_resolved_by_username(self, introspect_env):
        """项目 owner 只有 username：按用户名解析为用户 id。"""
        tc, stub, db = introspect_env
        stub.project_owners[42] = {"username": "chenkaidi"}
        stub.users_by_username["chenkaidi"] = 7
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        assert stub.create_calls[0][1]["assignee_id"] == 7

    def test_owner_unresolvable_falls_back_to_remote_username(self, introspect_env):
        """项目 owner 缺失（get_project 无 owner 字段）：兜底仓库 remote
        用户名解析为分配人。"""
        tc, stub, db = introspect_env
        stub.project_owners[42] = {}
        stub.users_by_username["agent"] = 9
        repo_id = _add_repo(db, 42, "botler", remote_username="agent")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        assert stub.create_calls[0][1]["assignee_id"] == 9

    def test_owner_api_failure_skips_assignee(self, introspect_env):
        """项目 owner 接口故障且无 remote_username：不指定分配人，
        issue 仍创建（不阻塞）。"""
        tc, stub, db = introspect_env
        stub.fail_project_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 201, r.text
        assert stub.create_calls[0][1]["assignee_id"] is None


class TestIntrospectFailures:
    """AI 审查 / GitLab 创建失败路径。"""

    def test_chat_failure_returns_502(self, introspect_env):
        tc, stub, db = introspect_env
        from botler.chat_models import ChatModelError
        StubChatClient.raise_error = ChatModelError("模型不可用")
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 502
        assert "AI 审查失败" in r.json()["detail"]
        assert stub.create_calls == []  # 未创建 issue

    def test_chat_http_error_returns_502(self, introspect_env):
        tc, stub, db = introspect_env
        StubChatClient.raise_http_error = True
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 502
        assert "网络错误" in r.json()["detail"]
        assert stub.create_calls == []

    def test_chat_empty_reply_returns_502(self, introspect_env):
        tc, stub, db = introspect_env
        StubChatClient.reply = "   "
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 502
        assert "为空" in r.json()["detail"]
        assert stub.create_calls == []

    def test_create_issue_failure_returns_502(self, introspect_env):
        tc, stub, db = introspect_env
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        stub.fail_create_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")

        r = tc.post(f"/api/repos/{repo_id}/introspect")

        assert r.status_code == 502
        assert "创建 issue 失败" in r.json()["detail"]
