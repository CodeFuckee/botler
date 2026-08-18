"""概览页「发掘」API 测试（issue #189）。

需求：概览页每个仓库卡片右上角新增「发掘」按钮，点击后根据该项目实现的
功能去 GitHub 搜索类似仓库，翻找类似仓库里 issue（用户对类似项目提出的
需求），整理成若干条需求后写入该仓库的 GitLab issue，分配人选择仓库的
owner，一条需求一个 issue。

覆盖：
- POST /api/repos/{repo_id}/discover 正常路径：收集项目上下文（本地
  文件夹 / GitLab API 兜底）→ AI 生成 GitHub 搜索关键词 → GitHub 搜索
  类似仓库（跨关键词去重）→ 翻找开放 issue（过滤 PR）→ AI 整理需求 →
  逐条创建 issue（标题带【发掘】前缀、标签 feature、分配人 = 项目 owner
  id），返回 201 与精简 issue 列表 + 数量；
- 分配人解析：项目 owner 含 id 直接用；owner 读取失败兜底仓库 remote
  用户名；
- 边界：仓库不存在 404 / 软删除 400 / 未启用 400 / 未配置 AI 模型 400 /
  未配置 owner token 400 / AI 失败与空回复 502 / 搜索词解析失败 502 /
  GitHub 限流与网络错误 502 / 无相似仓库 502 / 无需求 issue 502 / 需求
  整理解析失败与空结果 502 / GitLab 创建 issue 失败 502 / 标题超长截断 /
  需求标题去重 / 条数封顶 / 创建成功后清空概览缓存；
- GitHub 请求头：配置 GITHUB_TOKEN 时带 Authorization，未配置不带。
"""

import base64
import re
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

# 默认 AI 两轮回复：搜索词 JSON + 需求整理 JSON
DEFAULT_QUERY_REPLY = '["gitlab bot", "ai issue triage"]'
DEFAULT_AGG_REPLY = (
    '[{"title": "需求一：支持自定义标签", "detail": "说明一", '
    '"sources": ["https://github.com/a/b/issues/1"]}, '
    '{"title": "需求二：webhook 重试", "detail": "说明二", "sources": []}]'
)


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
    """便捷：插入一个仓库并返回本地 id（与自省 API 测试同构）。"""
    return db.upsert_repo(
        project_id, name,
        url or f"https://gitlab.example.com/{name}.git",
        enabled=enabled, priority=priority, remote_username=remote_username,
        local_path=local_path, remote_name=remote_name)


def _write_project(tmp_path, subdir="proj"):
    """构造一个本地项目文件夹（README + 关键清单 + 源码文件），返回路径。"""
    root = tmp_path / subdir
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# 测试项目\n\n这是一个用于发掘测试的示例项目。",
                                    encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo", "scripts": {}}',
                                       encoding="utf-8")
    (root / "src" / "main.py").write_text("def main():\n    print('hi')\n",
                                          encoding="utf-8")
    return root


class StubGitLab:
    """发掘 API 的 GitLab 桩：项目 owner / 仓库文件树 / 文件内容 / 创建
    issue / 概览查询，均可配置返回值与故障注入。"""

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

    # 概览缓存失效断言用
    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        self.calls.append(f"list_open_issues:{project_id}")
        return []

    def list_project_labels(self, project_id):
        return []


class StubChatClient:
    """AI 对话 ChatModelClient 桩：按序消费 replies 列表，记录 chat 调用。"""

    instances: list["StubChatClient"] = []
    replies: list[str] = []
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
        if StubChatClient.replies:
            return StubChatClient.replies.pop(0)
        raise AssertionError("StubChatClient.replies 已耗尽")


class FakeGithub:
    """GitHub REST API 桩：记录调用，按 path 返回搜索/issue 数据，可注入
    故障（限流/网络错误统一经 GitHubApiError 抛出）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.search_results: list[dict] = []
        self.issues_by_repo: dict[str, list[dict]] = {}
        self.raise_error: Exception | None = None

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        if self.raise_error is not None:
            raise self.raise_error
        if path == "/search/repositories":
            return {"items": list(self.search_results)}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues", path)
        if m:
            return list(self.issues_by_repo.get(m.group(1), []))
        raise AssertionError(f"未预期的 GitHub 请求: {path}")


def _default_github(fake):
    """给 FakeGithub 填默认数据：1 个相似仓库 + 1 条用户需求 issue。"""
    fake.search_results = [{
        "full_name": "a/b",
        "html_url": "https://github.com/a/b",
        "description": "相似项目",
        "stars": 100,
    }]
    fake.issues_by_repo = {"a/b": [
        {"title": "用户需求：增加通知",
         "body": "希望支持 webhook 通知",
         "html_url": "https://github.com/a/b/issues/1"},
        # PR 必须被过滤：issues API 返回项带 pull_request 键
        {"title": "feat: 一些改动", "body": "PR 正文",
         "html_url": "https://github.com/a/b/pull/2",
         "pull_request": {"url": "https://api.github.com/repos/a/b/pulls/2"}},
    ]}
    return fake


@pytest.fixture
def discover_env(client, monkeypatch):
    """发掘测试夹具：配置 owner token + AI 供应商 + 打桩 GitLabClient /
    ChatModelClient / GitHub API，返回 (tc, stub, gh, db)。"""
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
    StubChatClient.replies = [DEFAULT_QUERY_REPLY, DEFAULT_AGG_REPLY]
    StubChatClient.raise_error = None
    StubChatClient.raise_http_error = False
    monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)
    from botler.api import discover as discover_mod
    fake = _default_github(FakeGithub())
    monkeypatch.setattr(discover_mod, "_github_api_get", fake)
    return tc, tc.app.state.ctx.gitlab, fake, db


class TestValidation:
    """仓库校验与前置条件。"""

    def test_repo_not_found(self, discover_env):
        tc, stub, gh, db = discover_env
        r = tc.post("/api/repos/999/discover")
        assert r.status_code == 404

    def test_repo_soft_deleted(self, discover_env):
        tc, stub, gh, db = discover_env
        repo_id = _add_repo(db, 42, "botler")
        db.soft_delete_repo(repo_id)
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 400
        assert "已删除" in r.json()["detail"]

    def test_repo_disabled(self, discover_env):
        tc, stub, gh, db = discover_env
        repo_id = _add_repo(db, 42, "botler", enabled=False)
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 400
        assert "未启用" in r.json()["detail"]

    def test_without_ai_provider(self, client):
        """未配置 AI 对话模型：400 引导设置页（owner token 已配置也先拦）。"""
        tc, db = client
        tc.app.state.ctx.config.update_section("gitlab", {"owner_token": "owner-token-1"})
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
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
        StubChatClient.replies = [DEFAULT_QUERY_REPLY, DEFAULT_AGG_REPLY]
        monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)
        from botler.api import discover as discover_mod
        fake = _default_github(FakeGithub())
        monkeypatch.setattr(discover_mod, "_github_api_get", fake)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 400
        assert "owner token" in r.json()["detail"]


class TestDiscover:
    """POST /api/repos/{repo_id}/discover 正常路径。"""

    def test_success_local_context(self, discover_env, tmp_path):
        """本地项目文件夹收集上下文 → AI 搜索词 → GitHub 搜索/翻 issue →
        AI 整理 → 逐条创建 issue（【发掘】前缀 / 标签 feature / 分配人 =
        项目 owner id），返回 201 与 issue 列表。"""
        tc, stub, gh, db = discover_env
        stub.project_owners[42] = {"id": 7, "username": "chenkaidi"}
        repo_id = _add_repo(db, 42, "botler",
                            local_path=str(_write_project(tmp_path)))
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["count"] == 2
        assert len(data["issues"]) == 2
        # 两轮 AI 调用（_chat_once 每次新建 ChatModelClient 实例）：
        # 第一轮搜索词 + 第二轮需求整理，共 2 次 chat
        assert len(StubChatClient.instances) == 2
        assert sum(len(i.chat_calls) for i in StubChatClient.instances) == 2
        # 第一轮系统提示词为搜索词生成
        first = StubChatClient.instances[0].chat_calls[0]
        assert "搜索关键词" in first[0]["content"]
        # GitHub 调用：2 个搜索关键词 → 2 次搜索；1 个相似仓库 → 1 次翻 issue
        paths = [p for p, _ in gh.calls]
        assert paths.count("/search/repositories") == 2
        assert paths.count("/repos/a/b/issues") == 1
        # 创建 issue：标题带【发掘】、标签 feature、分配人 = owner id 7
        created = [c for p, c in stub.create_calls if p == 42]
        assert len(created) == 2
        assert all(c["labels"] == ["feature"] for c in created)
        assert all(c["assignee_id"] == 7 for c in created)
        assert all(c["title"].startswith("【发掘】botler：") for c in created)
        # 需求说明与参考来源写入描述
        desc = created[0]["description"]
        assert "【需求说明】" in desc and "说明一" in desc
        assert "【参考来源】" in desc and "https://github.com/a/b/issues/1" in desc
        # PR 不进入需求上下文（第二轮 AI 上下文不含 PR 标题）
        agg_messages = StubChatClient.instances[1].chat_calls[0]
        assert "增加通知" in agg_messages[1]["content"]
        assert "feat: 一些改动" not in agg_messages[1]["content"]

    def test_success_gitlab_context_fallback(self, discover_env):
        """无本地文件夹时走 GitLab 仓库 API 兜底收集上下文。"""
        tc, stub, gh, db = discover_env
        stub.tree_by_project[42] = [{"path": "README.md"}]
        stub.readme_by_project[42] = "# 兜底项目"
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        assert r.json()["count"] == 2
        assert any(x.startswith("GET /projects/42/repository/tree")
                   for x in stub.calls)

    def test_dedupe_repos_across_queries(self, discover_env):
        """同一相似仓库被多个关键词命中时只考察一次。"""
        tc, stub, gh, db = discover_env
        gh.search_results = [
            {"full_name": "a/b", "html_url": "https://github.com/a/b",
             "description": "sim", "stars": 100},
        ]
        gh.issues_by_repo = {"a/b": [
            {"title": "需求", "body": "正文",
             "html_url": "https://github.com/a/b/issues/1"},
        ]}
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        # 两个关键词各搜索一次，但只翻一次 issue
        paths = [p for p, _ in gh.calls]
        assert paths.count("/search/repositories") == 2
        assert paths.count("/repos/a/b/issues") == 1

    def test_invalidates_overview_cache(self, discover_env):
        """创建成功后清空概览缓存：下一次 overview 请求重新拉取。"""
        tc, stub, gh, db = discover_env
        repo_id = _add_repo(db, 42, "botler")

        def open_issue_calls():
            return [x for x in stub.calls if x.startswith("list_open_issues:")]

        tc.get("/api/issues/overview")
        assert len(open_issue_calls()) == 1  # 首次拉取

        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201

        tc.get("/api/issues/overview")
        assert len(open_issue_calls()) == 2  # 缓存已失效，重新拉取

    def test_title_respects_gitlab_limit(self, discover_env):
        """标题不超过 GitLab 255 字符硬上限（GITLAB_ISSUE_TITLE_MAX_LEN）。"""
        from botler.gitlab_client import GITLAB_ISSUE_TITLE_MAX_LEN
        tc, stub, gh, db = discover_env
        long_title = "很长的需求标题" * 60
        StubChatClient.replies = [
            DEFAULT_QUERY_REPLY,
            f'[{{"title": "{long_title}", "detail": "说明", "sources": []}}]',
        ]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        title = stub.create_calls[0][1]["title"]
        assert len(title) <= GITLAB_ISSUE_TITLE_MAX_LEN
        assert title.endswith("…")

    def test_title_newline_sanitized(self, discover_env):
        """AI 返回的标题含换行/制表符时单行化，GitLab issue 标题保持单行。"""
        tc, stub, gh, db = discover_env
        StubChatClient.replies = [
            DEFAULT_QUERY_REPLY,
            '[{"title": "需求\\n第一行\\n第二行", "detail": "说明", "sources": []}]',
        ]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        title = stub.create_calls[0][1]["title"]
        assert "\n" not in title and "\t" not in title
        assert "需求 第一行 第二行" in title

    def test_dedupe_and_cap_requirements(self, discover_env):
        """需求标题去重 + 条数封顶（MAX_DISCOVER_ISSUES）。"""
        from botler.api.discover import MAX_DISCOVER_ISSUES
        tc, stub, gh, db = discover_env
        items = [{"title": f"需求 {i}", "detail": "说明", "sources": []}
                 for i in range(12)]
        # 第 0 与第 1 条标题重复（大小写不同），应去重
        items[1]["title"] = "需求 0"
        StubChatClient.replies = [
            DEFAULT_QUERY_REPLY,
            f"[{','.join(str(x).replace(chr(39), chr(34)) for x in items)}]",
        ]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        assert r.json()["count"] == MAX_DISCOVER_ISSUES
        assert len(stub.create_calls) == MAX_DISCOVER_ISSUES
        titles = [c["title"] for _, c in stub.create_calls]
        assert len(set(titles)) == len(titles)


class TestDiscoverAssignee:
    """发掘 issue 分配人 = 仓库 owner 的解析行为（issue #189）。"""

    def test_owner_without_id_resolved_by_username(self, discover_env):
        tc, stub, gh, db = discover_env
        stub.project_owners[42] = {"username": "chenkaidi"}
        stub.users_by_username["chenkaidi"] = 7
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        assert all(c["assignee_id"] == 7
                   for _, c in stub.create_calls)

    def test_owner_api_failure_falls_back_to_remote_username(self, discover_env):
        tc, stub, gh, db = discover_env
        stub.fail_project_projects.add(42)
        stub.users_by_username["ckd"] = 9
        repo_id = _add_repo(db, 42, "botler", remote_username="ckd")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        assert all(c["assignee_id"] == 9
                   for _, c in stub.create_calls)

    def test_owner_unresolvable_skips_assignee(self, discover_env):
        tc, stub, gh, db = discover_env
        stub.fail_project_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 201, r.text
        assert all(c["assignee_id"] is None
                   for _, c in stub.create_calls)


class TestDiscoverErrors:
    """AI / GitHub / GitLab 故障与边界。"""

    def test_ai_query_failure_returns_502(self, discover_env):
        from botler.chat_models import ChatModelError
        tc, stub, gh, db = discover_env
        StubChatClient.raise_error = ChatModelError("模拟 AI 故障")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "AI 调用失败" in r.json()["detail"]

    def test_ai_query_empty_reply_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        StubChatClient.replies = ["", DEFAULT_AGG_REPLY]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "AI 回复为空" in r.json()["detail"]

    def test_ai_query_unparseable_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        StubChatClient.replies = ["不是 JSON", DEFAULT_AGG_REPLY]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "搜索关键词" in r.json()["detail"]

    def test_ai_query_empty_list_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        StubChatClient.replies = ["[]", DEFAULT_AGG_REPLY]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "搜索关键词" in r.json()["detail"]

    def test_github_rate_limit_returns_502(self, discover_env):
        from botler.api.discover import GitHubApiError
        tc, stub, gh, db = discover_env
        gh.raise_error = GitHubApiError(
            "GitHub API 限流（HTTP 403）：匿名限额已用完")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "限流" in r.json()["detail"]

    def test_github_network_error_returns_502(self, discover_env):
        from botler.api.discover import GitHubApiError
        tc, stub, gh, db = discover_env
        gh.raise_error = GitHubApiError("网络错误: 连接失败")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "网络错误" in r.json()["detail"]

    def test_no_similar_repos_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        gh.search_results = []
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "未找到功能相似的仓库" in r.json()["detail"]

    def test_no_issues_found_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        gh.issues_by_repo = {"a/b": []}
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "未翻找到用户需求 issue" in r.json()["detail"]

    def test_aggregate_unparseable_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        StubChatClient.replies = [DEFAULT_QUERY_REPLY, "整理失败：不是 JSON"]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "未整理出有效的需求" in r.json()["detail"]

    def test_aggregate_empty_list_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        StubChatClient.replies = [DEFAULT_QUERY_REPLY, "[]"]
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "未整理出有效的需求" in r.json()["detail"]

    def test_create_issue_failure_returns_502(self, discover_env):
        tc, stub, gh, db = discover_env
        stub.fail_create_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/discover")
        assert r.status_code == 502
        assert "创建需求 issue 失败" in r.json()["detail"]


class TestGithubHeaders:
    """GitHub 请求头：GITHUB_TOKEN 可选提升限额。"""

    def test_headers_without_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from botler.api.discover import _github_headers
        headers = _github_headers()
        assert headers["Accept"] == "application/vnd.github+json"
        assert "Authorization" not in headers

    def test_headers_with_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        from botler.api.discover import _github_headers
        headers = _github_headers()
        assert headers["Authorization"] == "Bearer ghp_test123"


class TestParseJsonArray:
    """AI 回复 JSON 数组解析：兼容代码围栏与前后缀说明。"""

    def test_plain_array(self):
        from botler.api.discover import _parse_json_array
        assert _parse_json_array('["a", "b"]') == ["a", "b"]

    def test_code_fence(self):
        from botler.api.discover import _parse_json_array
        assert _parse_json_array('```json\n["a"]\n```') == ["a"]

    def test_with_prefix_suffix_text(self):
        from botler.api.discover import _parse_json_array
        assert _parse_json_array('结果如下：["a"]（以上）') == ["a"]

    def test_invalid_returns_none(self):
        from botler.api.discover import _parse_json_array
        assert _parse_json_array("不是数组") is None
        assert _parse_json_array("") is None
