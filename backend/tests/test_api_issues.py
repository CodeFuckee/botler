"""概览页开放 issue 聚合 API 测试：GET /api/issues/overview（issue #64）。

遍历所有「已启用」仓库（enabled=1），聚合各仓库的开放（opened）issue：
- 外层按仓库优先级升序（priority 数字小在前，复用 list_repos 的
  ORDER BY priority, id），同优先级按仓库 id；
- 内层按 issue 最后更新时间（updated_at）降序，最新更新在前；
- 只查开放 issue（state=opened），未启用仓库与已软删除仓库不出现在
  结果中；
- 与 pipelines/overview（issue #39）一致：单仓库失败不中断整体
  （HTTP 200），失败明细进 errors 列表；结果带 10 秒 TTL 缓存；
- 每仓库最多取 100 条（limit），防止大仓库翻页打爆 GitLab API。
"""

import time
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


class StubGitLab:
    """issue 查询桩：issues_by_project 按 project_id 配置，可故障注入、记录传参。"""

    def __init__(self):
        self.issues_by_project: dict[int, list[dict]] = {}
        self.fail_projects: set[int] = set()
        # labels 查询（issue #71）：labels_by_project 按 project_id 配置，
        # 可单独故障注入（fail_label_projects）
        self.labels_by_project: dict[int, list[dict]] = {}
        self.fail_label_projects: set[int] = set()
        # (project_id, kwargs)：记录每次 list_open_issues 调用参数
        self.calls: list[tuple[int, dict]] = []
        # 记录每次 list_project_labels 调用的 project_id（issue #71）
        self.label_calls: list[int] = []
        # issue #92：项目成员查询（members_by_project 按 project_id 配置，
        # 可故障注入 fail_member_projects）；create_issue 记录调用参数、
        # 可故障注入 fail_create_projects、可配置返回对象
        self.members_by_project: dict[int, list[dict]] = {}
        self.fail_member_projects: set[int] = set()
        # issue #93：按 username 查用户 id 的桩（users_by_username 配置
        # username→用户 id；未配置返回 None，模拟用户不存在），记录查询顺序
        self.users_by_username: dict[str, int | None] = {}
        self.user_id_lookups: list[str] = []
        self.create_calls: list[tuple[int, dict]] = []
        self.fail_create_projects: set[int] = set()
        self.create_result: dict | None = None

    def list_project_members(self, project_id):
        """项目成员查询（issue #92）：按 project_id 配置，可故障注入。"""
        if project_id in self.fail_member_projects:
            raise GitLabError("模拟成员 API 故障")
        return list(self.members_by_project.get(project_id, []))

    def get_user_id_by_username(self, username):
        """按 username 查用户 id（issue #93 复现桩）：记录查询顺序，
        未配置的用户名返回 None（模拟用户不存在）。"""
        self.user_id_lookups.append(username)
        return self.users_by_username.get(username)

    def create_issue(self, project_id, title, description=None,
                     assignee_id=None, labels=None):
        """创建 issue（issue #92）：记录调用参数，可故障注入、可配置返回。"""
        self.create_calls.append((project_id, {
            "title": title, "description": description,
            "assignee_id": assignee_id, "labels": labels,
        }))
        if project_id in self.fail_create_projects:
            raise GitLabError("模拟创建 issue 故障")
        if self.create_result is not None:
            return self.create_result
        return {"iid": 99, "title": title, "state": "opened",
                "web_url": f"https://gitlab.example.com/x/-/issues/99",
                "labels": labels or [], "updated_at": None,
                "created_at": "2026-08-15T10:00:00.000+08:00",
                "description": description, "author": None,
                "milestone": None, "assignees": [],
                "user_notes_count": 0}

    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        self.calls.append((project_id, {
            "assignee_id": assignee_id, "scope": scope,
            "order_by": order_by, "sort": sort, "limit": limit,
        }))
        if project_id in self.fail_projects:
            raise GitLabError("模拟 GitLab API 故障")
        items = list(self.issues_by_project.get(project_id, []))
        # 模拟真实 GitLabClient._paged 的 limit 截断契约
        return items[:limit] if limit is not None else items

    def list_project_labels(self, project_id):
        """项目标签查询（issue #71）：按 project_id 配置，可故障注入。"""
        self.label_calls.append(project_id)
        if project_id in self.fail_label_projects:
            raise GitLabError("模拟标签 API 故障")
        return list(self.labels_by_project.get(project_id, []))


def make_issue(iid: int, title: str,
               updated_at: str = "2026-08-14T10:00:00.000+08:00",
               labels: list[str] | None = None,
               milestone: dict | None = None,
               assignees: list[dict] | None = None,
               user_notes_count: int | None = None,
               description: str | None = None,
               author: dict | None = None,
               created_at: str | None = None) -> dict:
    issue = {
        "iid": iid, "title": title, "state": "opened",
        "updated_at": updated_at,
        "web_url": f"https://gitlab.example.com/group/proj/-/issues/{iid}",
        "labels": labels or [],
        "milestone": milestone,
        "assignees": assignees or [],
        "user_notes_count": user_notes_count,
    }
    # issue #85：右边栏详情字段（description/author/created_at）——
    # 仅在显式传入时附加，缺省时模拟字段缺失的旧数据
    if description is not None:
        issue["description"] = description
    if author is not None:
        issue["author"] = author
    if created_at is not None:
        issue["created_at"] = created_at
    return issue


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub,
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    # 每个用例之间清空模块级缓存（结果缓存 + per-repo client 缓存，
    # 后者与 pipelines 模块共享），避免用例互相污染
    from botler.api import pipelines as pipelines_mod
    pipelines_mod.clear_pipeline_cache()
    from botler.api import issues as issues_mod
    issues_mod.clear_issue_cache()
    return app, stub, db, tmp_path


@pytest.fixture
def client(api_app):
    app, stub, db, tmp_path = api_app
    return TestClient(app), stub, db, tmp_path


def _add_repo(db, project_id=42, name="demo", enabled=True, priority=100) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled,
        priority=priority)


# ---- API ----

class TestIssuesOverview:
    def test_repos_sorted_by_priority(self, client):
        """外层排序：仓库按优先级升序（入库顺序无关）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="low", priority=200)
        _add_repo(db, project_id=43, name="high", priority=10)

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert [r["repo_name"] for r in data["repos"]] == ["high", "low"]
        assert data["total"] == 0

    def test_issues_sorted_by_updated_at_desc(self, client):
        """内层排序：仓库内 issue 按最后更新时间降序（API 返回乱序时兜底）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [
            make_issue(1, "旧 issue", updated_at="2026-08-10T09:00:00.000+08:00"),
            make_issue(3, "最新更新", updated_at="2026-08-14T18:30:00.000+08:00"),
            make_issue(2, "中间", updated_at="2026-08-12T12:00:00.000+08:00"),
        ]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["errors"] == []
        assert [i["iid"] for i in data["repos"][0]["issues"]] == [3, 2, 1]
        assert data["total"] == 3

    def test_only_open_issues_requested(self, client):
        """只查询开放 issue：list_open_issues 收到 order_by=updated_at、
        sort=desc、limit=100（服务端排序 + 每仓库上限，issue #64）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "a")]}

        tc.get("/api/issues/overview")

        assert stub.calls == [(42, {
            "assignee_id": None, "scope": "all",
            "order_by": "updated_at", "sort": "desc", "limit": 100,
        })]

    def test_skips_disabled_repo(self, client):
        """未启用仓库不出现、不查 GitLab（需求：只读已启用的仓库）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="on")
        _add_repo(db, project_id=43, name="off", enabled=False)
        stub.issues_by_project = {42: [make_issue(1, "a")],
                                  43: [make_issue(2, "b")]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert [r["repo_name"] for r in data["repos"]] == ["on"]
        assert [pid for pid, _ in stub.calls] == [42]

    def test_skips_soft_deleted_repo(self, client):
        """已软删除仓库不出现也不查询（list_repos 默认过滤，issue #62 语义）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="keep")
        gone_id = _add_repo(db, project_id=43, name="gone")
        db.soft_delete_repo(gone_id)
        stub.issues_by_project = {42: [make_issue(1, "a")],
                                  43: [make_issue(2, "b")]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert [r["repo_name"] for r in data["repos"]] == ["keep"]
        # 只查询未删除的仓库，已删除仓库不发起 GitLab 调用
        assert [pid for pid, _ in stub.calls] == [42]

    def test_repo_without_issues(self, client):
        """仓库无开放 issue：issues 为空列表，不影响其他仓库。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="empty")
        _add_repo(db, project_id=43, name="has")
        stub.issues_by_project = {43: [make_issue(5, "x")]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["errors"] == []
        assert data["repos"][0]["repo_name"] == "empty"
        assert data["repos"][0]["issues"] == []
        assert len(data["repos"][1]["issues"]) == 1
        assert data["total"] == 1

    def test_partial_repo_failure(self, client):
        """部分仓库 GitLab 故障：正常仓库照常返回，失败明细进 errors。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        stub.fail_projects = {43}

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert [r["repo_name"] for r in data["repos"]] == ["a", "b"]
        assert len(data["repos"][0]["issues"]) == 1
        assert data["repos"][1]["issues"] == []
        assert len(data["errors"]) == 1
        assert "仓库 b" in data["errors"][0]

    def test_all_repos_failed_still_200(self, client):
        """全部仓库失败：仍返回 200，errors 记录全部失败明细。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        _add_repo(db, project_id=43, name="b")
        stub.fail_projects = {42, 43}

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 2
        assert all(r["issues"] == [] for r in data["repos"])

    def test_without_any_repo(self, client):
        """边界：没有任何仓库时返回空结果（不 500）。"""
        tc, stub, db, tmp_path = client

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        assert resp.json() == {"repos": [], "errors": [], "total": 0}

    def test_limit_truncation(self, client):
        """每仓库最多 100 条：大仓库截断为前 100 条（最新更新的）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [
            make_issue(i, f"issue-{i}") for i in range(1, 151)
        ]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert len(data["repos"][0]["issues"]) == 100
        assert data["total"] == 100

    def test_missing_updated_at_fallback(self, client):
        """边界：issue 缺 updated_at 字段时兜底排序不崩（按空串排最后）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [
            {"iid": 1, "title": "no-time", "state": "opened",
             "web_url": "https://gitlab.example.com/x/-/issues/1"},
            make_issue(2, "with-time"),
        ]}

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert [i["iid"] for i in data["repos"][0]["issues"]] == [2, 1]

    def test_same_priority_sorted_by_repo_id(self, client):
        """同优先级仓库按 id 升序（list_repos 的 ORDER BY priority, id）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=43, name="second")
        _add_repo(db, project_id=42, name="first")

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        # second 先插入（id 小）→ 同优先级时排前
        assert [r["repo_name"] for r in data["repos"]] == ["second", "first"]

    def test_cache_within_ttl(self, client):
        """10s TTL 缓存：连续两次请求只打一轮 GitLab API。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "x")]}

        first = tc.get("/api/issues/overview").json()
        second = tc.get("/api/issues/overview").json()

        assert first == second
        assert len(stub.calls) == 1

    def test_cache_expires_after_ttl(self, client):
        """TTL 过期后重新拉取（缓存按时间失效）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "x")]}

        tc.get("/api/issues/overview")
        from botler.api import issues as issues_mod
        issues_mod._CACHE["expires_at"] = time.monotonic() - 1
        tc.get("/api/issues/overview")

        assert len(stub.calls) == 2

    def test_issue_fields_trimmed(self, client):
        """issue 精简字段透传：iid/title/updated_at/web_url + 美化字段
        labels/milestone/assignees/user_notes_count（issue #71 扩展）；
        updated_at 转 UTC 无后缀（前端 fmtAgo 解析约定，与流水线
        commit_time 一致）。无对应数据时 labels 空列表、其余为 None。
        issue #85 起 description/author/state/created_at 一并透传
        （右边栏详情），缺失时 description/author/created_at 为 None。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [{
            **make_issue(7, "精简"),
            "description": "很长很长的描述……",
            "author": {"id": 1, "username": "someone"},
            "labels": ["bug"],
        }]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        issue = data["repos"][0]["issues"][0]
        assert issue == {
            "iid": 7, "title": "精简",
            "state": "opened",
            "updated_at": "2026-08-14 02:00:00",
            "web_url": "https://gitlab.example.com/group/proj/-/issues/7",
            "description": "很长很长的描述……",
            "author": {"name": None, "username": "someone"},
            "created_at": None,
            "labels": [{"name": "bug", "color": None, "text_color": None}],
            "milestone": None,
            "assignees": [],
            "user_notes_count": None,
        }

    def test_extended_fields_trimmed(self, client):
        """美化字段透传（issue #71）：里程碑只留 title；assignees 每条只留
        name/username/avatar_url；评论数原样透传。issue #85 起 description
        原样透传、author 精简为 name/username、created_at 转 UTC 无后缀。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [{
            **make_issue(7, "扩展"),
            "labels": ["bug"],
            "milestone": {"id": 1, "iid": 1, "title": "v1.0", "state": "active"},
            "assignees": [{
                "id": 1, "username": "agent", "name": "Agent",
                "avatar_url": "https://gitlab.example.com/a.png",
                "state": "active",
            }],
            "user_notes_count": 3,
            "author": {"id": 2, "username": "someone", "name": "Someone",
                       "avatar_url": "https://gitlab.example.com/s.png"},
            "description": "正文原样透传",
            "created_at": "2026-08-01T09:00:00.000+08:00",
        }]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        issue = data["repos"][0]["issues"][0]
        assert issue == {
            "iid": 7, "title": "扩展",
            "state": "opened",
            "updated_at": "2026-08-14 02:00:00",
            "web_url": "https://gitlab.example.com/group/proj/-/issues/7",
            "description": "正文原样透传",
            "author": {"name": "Someone", "username": "someone"},
            "created_at": "2026-08-01 01:00:00",
            "labels": [{"name": "bug", "color": None, "text_color": None}],
            "milestone": "v1.0",
            "assignees": [{
                "name": "Agent", "username": "agent",
                "avatar_url": "https://gitlab.example.com/a.png",
            }],
            "user_notes_count": 3,
        }

    def test_label_colors_attached(self, client):
        """标签颜色（issue #71）：labels API 提供的 color/text_color 挂到
        对应标签上；labels API 中不存在的标签降级为无色。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.labels_by_project = {42: [
            {"name": "feature", "color": "428BCA", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69D100", "text_color": "FFFFFF"},
        ]}
        stub.issues_by_project = {42: [make_issue(
            1, "a", labels=["feature", "ui", "unknown"])]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["errors"] == []
        labels = data["repos"][0]["issues"][0]["labels"]
        assert labels == [
            {"name": "feature", "color": "428BCA", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69D100", "text_color": "FFFFFF"},
            {"name": "unknown", "color": None, "text_color": None},
        ]
        # 每个仓库各查一次 labels API（与 issues 查询同一 per-repo client）
        assert stub.label_calls == [42]

    def test_label_colors_failure_degrades(self, client):
        """labels API 故障（issue #71）：issue 照常返回、标签降级无色、
        errors 不记录（标签色只是视觉增强，不构成数据不可用）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.fail_label_projects = {42}
        stub.issues_by_project = {42: [make_issue(1, "a", labels=["bug"])]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["errors"] == []
        labels = data["repos"][0]["issues"][0]["labels"]
        assert labels == [{"name": "bug", "color": None, "text_color": None}]

    def test_label_color_invalid_ignored(self, client):
        """安全兜底（issue #71）：labels API 返回非 6 位 hex 的颜色值时
        不透传给前端（避免样式注入），按无色处理。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.labels_by_project = {42: [
            {"name": "x", "color": "not-hex", "text_color": "FFFFFF"},
            {"name": "y", "color": "428BCA", "text_color": "333333"},
        ]}
        stub.issues_by_project = {42: [make_issue(1, "a", labels=["x", "y"])]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        labels = data["repos"][0]["issues"][0]["labels"]
        assert labels == [
            {"name": "x", "color": None, "text_color": None},
            {"name": "y", "color": "428BCA", "text_color": "333333"},
        ]

    def test_repo_entry_carries_priority(self, client):
        """每条仓库结果带 priority 字段供前端展示优先级徽章。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="p", priority=5)

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["repos"][0]["priority"] == 5


# ---- issue #85：右边栏详情字段透传（description/author/state/created_at）----

class TestIssueDetailFields:
    def test_description_preserved_as_is(self, client):
        """正文原样透传：含 Markdown 语法与多行的 description 不做任何
        加工（前端 Markdown 组件负责渲染）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        description = "**需求**\n\n- 要点一\n- 要点二\n\n```\ncode\n```"
        stub.issues_by_project = {42: [make_issue(1, "右边栏", description=description)]}

        resp = tc.get("/api/issues/overview")

        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["description"] == description

    def test_author_trimmed_to_name_username(self, client):
        """author 精简为 {name, username}：丢弃 id/avatar_url 等冗余字段，
        与 assignees 的精简风格一致。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(
            1, "a",
            author={"id": 9, "name": "Chen", "username": "chenkaidi",
                    "avatar_url": "https://gitlab.example.com/a.png",
                    "state": "active"})]}

        resp = tc.get("/api/issues/overview")

        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["author"] == {"name": "Chen", "username": "chenkaidi"}

    def test_created_at_converted_to_utc(self, client):
        """created_at 与 updated_at 同规则转 UTC 无后缀（前端 fmtTime
        解析约定）；state 原样透传。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(
            1, "a", created_at="2026-08-10T09:00:00.000+08:00")]}

        resp = tc.get("/api/issues/overview")

        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["created_at"] == "2026-08-10 01:00:00"
        assert issue["state"] == "opened"

    def test_detail_fields_missing_fallback(self, client):
        """边界：description/author/created_at 全部缺失（旧版后端或缓存
        数据）→ 均为 None，不 500；前端按占位文案兜底。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "旧数据")]}

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["description"] is None
        assert issue["author"] is None
        assert issue["created_at"] is None
        assert issue["state"] == "opened"

    def test_author_empty_object_fallback(self, client):
        """边界：author 为空对象 / 字段缺失 → name/username 为 None，
        前端按「—」兜底。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "a", author={})]}

        resp = tc.get("/api/issues/overview")

        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["author"] == {"name": None, "username": None}

    def test_created_at_invalid_format_fallback(self, client):
        """边界：created_at 非法格式（_commit_time_utc 解析失败）→ None，
        不抛异常不 500。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.issues_by_project = {42: [make_issue(1, "a", created_at="not-a-date")]}

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        issue = resp.json()["repos"][0]["issues"][0]
        assert issue["created_at"] is None


# ---- per-repo token（与 pipelines 概览共用 _repo_client，issue #60）----

class TestOverviewIssuesPerRepoToken:
    def test_uses_per_repo_client(self, client, monkeypatch):
        """remote 带 token 的仓库：issue 查询走 per-repo client，全局桩不被调用。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        per = StubGitLab()
        per.issues_by_project = {42: [make_issue(1, "x")]}
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert data["repos"][0]["issues"][0]["iid"] == 1
        assert len(per.calls) == 1
        assert len(stub.calls) == 0

    def test_fallback_to_global_client(self, client, monkeypatch):
        """remote 无 token 的仓库：回退全局 client（旧行为）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client", lambda c, row: None)

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        assert resp.json()["errors"] == []
        assert len(stub.calls) == 1

    def test_per_repo_token_invalid_goes_to_errors(self, client, monkeypatch):
        """per-repo token 失效（GitLabError）：该仓库进 errors，不中断整体。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        bad = StubGitLab()
        bad.fail_projects = {42}
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client", lambda c, row: bad)

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) == 1
        assert "仓库 a" in data["errors"][0]


# ---- issue #92：概览页添加 Issue ----

class TestIssueFormMeta:
    """GET /api/issues/form-meta/{repo_id}：添加 issue 弹窗所需元数据
    （项目成员下拉 + 项目标签多选）。"""

    def test_returns_members_and_labels(self, client):
        """正常路径：成员精简为 id/username/name（id 取 user_id——
        GitLab 创建 issue 的 assignee_ids 需要用户 id，而 members API
        顶层 id 是成员关系 id）；标签带颜色精简透传。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 113, "user_id": 20, "username": "agent",
             "name": "Agent", "access_level": 40},
            {"id": 114, "user_id": 21, "username": "dev",
             "name": "Dev", "access_level": 30},
        ]}
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69D100", "text_color": "FFFFFF"},
        ]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["members"] == [
            {"id": 20, "username": "agent", "name": "Agent"},
            {"id": 21, "username": "dev", "name": "Dev"},
        ]
        assert data["labels"] == [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69D100", "text_color": "FFFFFF"},
        ]

    def test_repo_not_found(self, client):
        """仓库不存在 → 404。"""
        tc, stub, db, tmp_path = client

        resp = tc.get("/api/issues/form-meta/999")

        assert resp.status_code == 404

    def test_repo_disabled(self, client):
        """仓库未启用 → 400（概览页只展示启用仓库，后端兜底拦截）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="off", enabled=False)

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 400

    def test_members_query_failure_returns_502(self, client):
        """成员查询失败 → 502（成员为必填字段来源，不可降级）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.fail_member_projects = {42}
        stub.labels_by_project = {42: [{"name": "bug", "color": "FF0000",
                                        "text_color": "FFFFFF"}]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 502
        assert "成员" in resp.json()["detail"]

    def test_labels_query_failure_returns_502(self, client):
        """标签查询失败 → 502（标签为必填字段来源，不可降级）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.fail_label_projects = {42}
        stub.members_by_project = {42: [
            {"id": 1, "user_id": 20, "username": "agent", "name": "Agent"}]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 502
        assert "标签" in resp.json()["detail"]

    def test_empty_members_and_labels(self, client):
        """边界：仓库无成员/无标签 → 空数组，200（前端按空状态提示）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json() == {"members": [], "labels": []}

    def test_invalid_member_entries_filtered(self, client):
        """边界：成员元素非对象/缺 user_id 且 username 查不到时被过滤，不 500。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            None, "junk", {"username": "no-id"},
            {"id": 1, "user_id": 20, "username": "agent", "name": "Agent"},
        ]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["members"] == [
            {"id": 20, "username": "agent", "name": "Agent"}]

    # ---- issue #93：members/all 返回项无 user_id 字段（GitLab 19 实测
    # 行为），成员按 username 查 /users 补齐用户 id，下拉不再为空 ----

    def test_members_without_user_id_resolved_by_username(self, client):
        """复现 issue #93：成员对象只有顶层 id（成员关系 id）与 username，
        无 user_id——按 username 查 /users 补齐真实用户 id。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 1, "username": "chenkaidi", "name": "chenkaidi",
             "access_level": 50},
            {"id": 3, "username": "agent", "name": "agent",
             "access_level": 40},
        ]}
        stub.users_by_username = {"chenkaidi": 1, "agent": 3}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["members"] == [
            {"id": 1, "username": "chenkaidi", "name": "chenkaidi"},
            {"id": 3, "username": "agent", "name": "agent"},
        ]
        assert stub.user_id_lookups == ["chenkaidi", "agent"]

    def test_members_without_user_id_unresolvable_filtered(self, client):
        """边界：username 查不到（用户已删除）的成员剔除，不 500、不影响其余。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 9, "username": "ghost", "name": "Ghost"},
            {"id": 3, "username": "agent", "name": "agent"},
        ]}
        stub.users_by_username = {"agent": 3}  # ghost 未配置 → None

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["members"] == [
            {"id": 3, "username": "agent", "name": "agent"}]

    def test_members_with_user_id_skip_username_lookup(self, client):
        """user_id 存在的成员不发起 /users 查询（避免无谓的 N+1 请求）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 113, "user_id": 20, "username": "agent", "name": "Agent"},
            {"id": 114, "user_id": 21, "username": "dev", "name": "Dev"},
        ]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["members"] == [
            {"id": 20, "username": "agent", "name": "Agent"},
            {"id": 21, "username": "dev", "name": "Dev"},
        ]
        assert stub.user_id_lookups == []

    def test_uses_per_repo_client(self, client, monkeypatch):
        """per-repo client 优先（与 issue 查询一致，issue #60 模式）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        per = StubGitLab()
        per.members_by_project = {42: [
            {"id": 1, "user_id": 20, "username": "agent", "name": "Agent"}]}
        per.labels_by_project = {42: [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"}]}
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 1
        # 全局桩未被调用
        assert len(stub.calls) == 0


class TestCreateIssue:
    """POST /api/issues：在指定仓库创建 issue（标题/描述/分配人/标签）。"""

    def _post(self, tc, **overrides):
        body = {"repo_id": 1, "title": "新 issue",
                "description": "描述", "assignee_id": 20,
                "labels": ["bug", "ui"]}
        body.update(overrides)
        return tc.post("/api/issues", json=body)

    def test_create_success(self, client):
        """正常路径：调用 create_issue 传参正确（labels 保持数组由
        GitLabClient 拼逗号），返回 201 与精简后的 issue 对象。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        resp = self._post(tc)

        assert resp.status_code == 201
        assert stub.create_calls == [(42, {
            "title": "新 issue", "description": "描述",
            "assignee_id": 20, "labels": ["bug", "ui"],
        })]
        issue = resp.json()
        assert issue["iid"] == 99
        assert issue["title"] == "新 issue"
        assert issue["state"] == "opened"

    def test_title_required(self, client):
        """边界：标题缺失/纯空白 → 400（GitLab 创建 issue 的硬性要求）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        assert self._post(tc, title="").status_code == 400
        assert self._post(tc, title="   ").status_code == 400

        assert stub.create_calls == []

    def test_labels_required(self, client):
        """边界：标签空列表/全空白元素 → 400（用户确认标签必填）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        assert self._post(tc, labels=[]).status_code == 400
        assert self._post(tc, labels=["  ", ""]).status_code == 400

        assert stub.create_calls == []

    def test_blank_label_elements_filtered(self, client):
        """边界：标签含空白元素时过滤后仍合法（空白项忽略）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        resp = self._post(tc, labels=["bug", " ", "ui"])

        assert resp.status_code == 201
        assert stub.create_calls[0][1]["labels"] == ["bug", "ui"]

    def test_assignee_required(self, client):
        """边界：分配人缺失 → 400（用户确认分配人必填）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        assert self._post(tc, assignee_id=None).status_code == 400
        assert stub.create_calls == []

    def test_description_optional(self, client):
        """边界：描述选填——缺失/空白时透传 None，不阻塞创建。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")

        assert self._post(tc, description="").status_code == 201
        assert self._post(tc, description=None).status_code == 201

        assert stub.create_calls[0][1]["description"] is None
        assert stub.create_calls[1][1]["description"] is None

    def test_repo_not_found(self, client):
        """仓库不存在 → 404。"""
        tc, stub, db, tmp_path = client

        assert self._post(tc, repo_id=999).status_code == 404
        assert stub.create_calls == []

    def test_repo_disabled(self, client):
        """仓库未启用 → 400。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="off", enabled=False)

        assert self._post(tc).status_code == 400
        assert stub.create_calls == []

    def test_create_failure_returns_502(self, client):
        """GitLab 创建失败 → 502，错误信息透出。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.fail_create_projects = {42}

        resp = self._post(tc)

        assert resp.status_code == 502
        assert "创建 issue 失败" in resp.json()["detail"]

    def test_uses_per_repo_client(self, client, monkeypatch):
        """per-repo client 优先（与 issue 查询一致，issue #60 模式）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        per = StubGitLab()
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = self._post(tc)

        assert resp.status_code == 201
        assert len(per.create_calls) == 1
        assert stub.create_calls == []

    def test_create_invalidates_overview_cache(self, client):
        """创建成功后清空 overview 缓存：下一次 overview 请求重新拉取
        （前端创建成功立即刷新列表，不能拿到 10 秒 TTL 旧缓存）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.issues_by_project = {42: [make_issue(1, "旧 issue")]}

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 1  # 首次拉取

        resp = self._post(tc)
        assert resp.status_code == 201

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 2  # 缓存已失效，重新拉取
