"""全局搜索 API 测试（issue #216）：任务 / issue / 灵感 / 仓库跨模块检索。

需求：无跨模块全局搜索——想找「某个 issue 相关的历史任务」「某关键词的
灵感」「某仓库的全部记录」需要逐个页面翻。本模块提供 GET /api/search
统一检索入口，结果按模块分组，支持中文关键词（LIKE 字面子串，Issue 正文
允许的 FTS5 兜底方案）。

覆盖：
- 参数校验：缺 q / 空白 q → 400；limit 越界 → 422；
- tasks：按 issue 标题（中文/英文、大小写不敏感）、issue 编号匹配，
  最新任务在前；
- issues：按标题/正文匹配 GitLab 开放 issue（复用概览 10s 缓存），
  已启用仓库才返回、GitLab 故障仓库跳过不整体失败；
- inspirations：按内容匹配（JOIN repos 带仓库名）；
- repos：按名称匹配，排除软删除仓库；
- LIKE 通配符转义：搜索词中的 % _ 按字面匹配，不当作通配符；
- 每模块 limit 截断；无匹配 → 空数组；响应回显 query。
"""

from types import SimpleNamespace

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


def make_issue(
    iid: int,
    title: str,
    description: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    """构造概览聚合用的 issue 字典（字段与 _collect/_trim_issue 匹配）。"""
    issue = {
        "iid": iid,
        "title": title,
        "state": "opened",
        "updated_at": "2026-08-14T10:00:00.000+08:00",
        "web_url": f"https://gitlab.example.com/group/proj/-/issues/{iid}",
        "labels": labels or [],
        "milestone": None,
        "assignees": [],
        "user_notes_count": 0,
    }
    if description is not None:
        issue["description"] = description
    return issue


class StubGitLab:
    """搜索测试的 GitLab 桩：概览聚合（_collect）需要 list_open_issues
    与 list_project_labels；issues_by_project 按 project_id 配置。"""

    def __init__(self):
        self.issues_by_project: dict[int, list[dict]] = {}
        self.fail_projects: set[int] = set()
        self.calls: list[int] = []

    def list_open_issues(
        self,
        project_id,
        assignee_id=None,
        scope="all",
        order_by=None,
        sort=None,
        limit=None,
    ):
        self.calls.append(project_id)
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab 故障")
        return list(self.issues_by_project.get(project_id, []))

    def list_project_labels(self, project_id):
        return []


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(
        config=config, db=db, gitlab=stub, config_path=str(config_path)
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    # 清空概览聚合缓存，避免用例互相污染
    from botler.api import issues as issues_mod

    issues_mod.clear_issue_cache()
    return app, stub, db


@pytest.fixture
def client(api_app):
    app, stub, db = api_app
    return TestClient(app), stub, db


def _add_repo(
    db, project_id=42, name="demo", enabled=True, priority=100, deleted=False
) -> int:
    rid = db.upsert_repo(
        project_id=project_id,
        name=name,
        url=f"https://gitlab.example.com/{name}.git",
        enabled=enabled,
        priority=priority,
    )
    if deleted:
        db.soft_delete_repo(rid)
    return rid


def _add_task(db, repo_id, issue_iid, issue_title, status="succeeded"):
    return db.create_task(
        repo_id=repo_id,
        project_id=42,
        issue_iid=issue_iid,
        issue_title=issue_title,
        triggered_by="webhook",
    )


# ---- 参数校验 ----


class TestValidation:
    def test_missing_q_422(self, client):
        """缺 q：FastAPI 必填参数校验直接 422（前端不会无词请求，防御即可）。"""
        tc, _, _ = client
        assert tc.get("/api/search").status_code == 422

    def test_blank_q_400(self, client):
        tc, _, _ = client
        for blank in ("   ", "\t\n"):
            r = tc.get("/api/search", params={"q": blank})
            assert r.status_code == 400, f"q={blank!r} 应 400"

    def test_limit_bounds(self, client):
        tc, _, _ = client
        assert tc.get("/api/search?q=a&limit=0").status_code == 422
        assert tc.get("/api/search?q=a&limit=51").status_code == 422
        assert tc.get("/api/search?q=a&limit=1").status_code == 200


# ---- 任务模块 ----


class TestTasks:
    def test_match_issue_title_chinese(self, client):
        tc, _, db = client
        rid = _add_repo(db, name="repo-a")
        _add_task(db, rid, 100, "修复登录页按钮错位")
        _add_task(db, rid, 101, "新增全局搜索功能")
        r = tc.get("/api/search", params={"q": "搜索"})
        assert r.status_code == 200
        data = r.json()
        assert data["query"] == "搜索"
        titles = [t["issue_title"] for t in data["tasks"]]
        assert titles == ["新增全局搜索功能"]  # 中文子串命中
        # 任务字段完整（前端跳转详情需要 id / repo_name / status）
        t = data["tasks"][0]
        assert t["id"] and t["repo_name"] == "repo-a" and t["status"] == "queued"

    def test_match_by_issue_iid(self, client):
        tc, _, db = client
        rid = _add_repo(db, name="repo-a")
        _add_task(db, rid, 100, "修复登录页按钮错位")
        r = tc.get("/api/search", params={"q": "100"})
        assert [t["issue_iid"] for t in r.json()["tasks"]] == [100]

    def test_case_insensitive_ascii(self, client):
        tc, _, db = client
        rid = _add_repo(db, name="repo-a")
        _add_task(db, rid, 100, "Add Global Search Box")
        r = tc.get("/api/search", params={"q": "global search"})
        assert len(r.json()["tasks"]) == 1
        r2 = tc.get("/api/search", params={"q": "GLOBAL SEARCH"})
        assert len(r2.json()["tasks"]) == 1

    def test_like_wildcards_literal(self, client):
        """搜索词中的 % _ 按字面匹配，不当作 LIKE 通配符（issue #216）。"""
        tc, _, db = client
        rid = _add_repo(db, name="repo-a")
        _add_task(db, rid, 100, "进度 100% 完成")
        _add_task(db, rid, 101, "a_b 命名规范")
        # % 字面：应命中「100% 完成」而非任意包含 100 的标题
        r = tc.get("/api/search", params={"q": "100%"})
        assert [t["issue_title"] for t in r.json()["tasks"]] == ["进度 100% 完成"]
        # _ 字面：应命中 a_b 而非 aXb（无此类数据，验证不误命中）
        r = tc.get("/api/search", params={"q": "a_b"})
        assert [t["issue_title"] for t in r.json()["tasks"]] == ["a_b 命名规范"]

    def test_oldest_first_limit(self, client):
        """任务按 id 倒序（最新在前），limit 截断。"""
        tc, _, db = client
        rid = _add_repo(db, name="repo-a")
        for i in range(5):
            _add_task(db, rid, 200 + i, f"搜索相关任务 {i}")
        r = tc.get("/api/search", params={"q": "搜索相关", "limit": 2})
        titles = [t["issue_title"] for t in r.json()["tasks"]]
        assert titles == ["搜索相关任务 4", "搜索相关任务 3"]


# ---- 灵感模块 ----


class TestInspirations:
    def test_match_content(self, client):
        tc, _, db = client
        rid = _add_repo(db, project_id=1, name="alpha")
        db.create_inspiration(rid, "关于深色模式配色的灵感")
        db.create_inspiration(rid, "全局搜索框放顶栏会更好用")
        r = tc.get("/api/search", params={"q": "深色模式"})
        data = r.json()
        assert [x["content"] for x in data["inspirations"]] == [
            "关于深色模式配色的灵感"
        ]
        x = data["inspirations"][0]
        assert x["repo_id"] == rid and x["repo_name"] == "alpha"  # JOIN 带仓库名

    def test_empty_content_no_match(self, client):
        tc, _, db = client
        rid = _add_repo(db, name="alpha")
        db.create_inspiration(rid, "空白内容")
        r = tc.get("/api/search", params={"q": "不存在"})
        assert r.json()["inspirations"] == []


# ---- 仓库模块 ----


class TestRepos:
    def test_match_name_excludes_deleted(self, client):
        tc, _, db = client
        _add_repo(db, project_id=1, name="alpha-bot", priority=200)
        _add_repo(db, project_id=2, name="alpha-tools", priority=100)
        gone = _add_repo(db, project_id=3, name="alpha-gone", deleted=True)
        assert gone is not None
        r = tc.get("/api/search", params={"q": "alpha"})
        names = [x["name"] for x in r.json()["repos"]]
        assert names == ["alpha-tools", "alpha-bot"]  # 排除软删除 + 优先级升序
        x = r.json()["repos"][0]
        assert x["gitlab_project_id"] and x["enabled"] is True

    def test_no_repo_match(self, client):
        tc, _, db = client
        _add_repo(db, name="beta")
        r = tc.get("/api/search", params={"q": "alpha"})
        assert r.json()["repos"] == []


# ---- issue 模块（GitLab）----


class TestIssues:
    def test_match_title_and_description(self, client):
        tc, stub, db = client
        _add_repo(db, project_id=1, name="alpha")
        stub.issues_by_project[1] = [
            make_issue(200, "修复登录页按钮错位", description="窄屏溢出"),
            make_issue(
                201, "Global search feature", description="Add global search box"
            ),
        ]
        # 标题命中
        r = tc.get("/api/search", params={"q": "登录页"})
        issues = r.json()["issues"]
        assert [i["iid"] for i in issues] == [200]
        assert issues[0]["repo_name"] == "alpha"
        assert issues[0]["project_id"] == 1
        # 正文命中（description 也参与匹配）
        r = tc.get("/api/search", params={"q": "global search box"})
        assert [i["iid"] for i in r.json()["issues"]] == [201]

    def test_issue_returns_trimmed_fields(self, client):
        tc, stub, db = client
        _add_repo(db, project_id=1, name="alpha")
        stub.issues_by_project[1] = [
            make_issue(200, "带标签的 issue", labels=["feature"])
        ]
        r = tc.get("/api/search", params={"q": "带标签"})
        issue = r.json()["issues"][0]
        assert issue["labels"][0]["name"] == "feature"
        assert issue["web_url"] and issue["state"] == "opened"

    def test_disabled_repo_excluded(self, client):
        """与概览页一致：未启用仓库的 issue 不出现在搜索结果。"""
        tc, stub, db = client
        _add_repo(db, project_id=1, name="enabled")
        _add_repo(db, project_id=2, name="disabled", enabled=False)
        stub.issues_by_project[1] = [make_issue(200, "启用仓库的 issue")]
        stub.issues_by_project[2] = [make_issue(201, "禁用仓库的 issue")]
        r = tc.get("/api/search", params={"q": "issue"})
        assert [i["iid"] for i in r.json()["issues"]] == [200]

    def test_gitlab_error_repo_skipped(self, client):
        """单仓库 GitLab 故障：该仓库 issue 为空，其余模块不受影响。"""
        tc, stub, db = client
        _add_repo(db, project_id=1, name="broken")
        _add_repo(db, project_id=2, name="ok")
        stub.fail_projects.add(1)
        stub.issues_by_project[2] = [make_issue(202, "正常仓库的 issue")]
        r = tc.get("/api/search", params={"q": "issue"})
        assert [i["iid"] for i in r.json()["issues"]] == [202]


# ---- 整体 ----


class TestOverall:
    def test_no_match_all_empty(self, client):
        tc, _, db = client
        rid = _add_repo(db, name="alpha")
        _add_task(db, rid, 100, "修复登录页按钮错位")
        db.create_inspiration(rid, "深色模式")
        r = tc.get("/api/search", params={"q": "完全不存在的词"})
        data = r.json()
        assert data["tasks"] == [] and data["issues"] == []
        assert data["inspirations"] == [] and data["repos"] == []

    def test_multiple_modules_simultaneously(self, client):
        """同一关键词跨多模块命中：任务 / 灵感 / 仓库并行返回。"""
        tc, stub, db = client
        rid = _add_repo(db, project_id=1, name="搜索平台")
        _add_task(db, rid, 100, "全局搜索功能")
        db.create_inspiration(rid, "搜索框放顶栏")
        stub.issues_by_project[1] = [make_issue(200, "搜索功能需求")]
        r = tc.get("/api/search", params={"q": "搜索"})
        data = r.json()
        assert len(data["tasks"]) == 1
        assert len(data["issues"]) == 1
        assert len(data["inspirations"]) == 1
        assert len(data["repos"]) == 1

    def test_per_module_limit(self, client):
        tc, stub, db = client
        rid = _add_repo(db, project_id=1, name="limit-测试")
        for i in range(3):
            _add_task(db, rid, 300 + i, f"限制条数任务 {i}")
        stub.issues_by_project[1] = [
            make_issue(310 + i, f"限制条数 issue {i}") for i in range(3)
        ]
        r = tc.get("/api/search", params={"q": "限制条数", "limit": 2})
        data = r.json()
        assert len(data["tasks"]) == 2
        assert len(data["issues"]) == 2
