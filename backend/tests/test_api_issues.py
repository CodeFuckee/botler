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

import json
import time
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


class StubScheduler:
    """调度器桩（issue #117）：记录 enqueue 调用，供重试端点断言入队。"""

    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue(self, task_id: int) -> bool:
        self.enqueued.append(task_id)
        return True


class StubExecutor:
    """执行器桩（issue #117）：记录 clear_stop_request 调用（issue #69
    停止请求残留清理由任务页手动重试沿用，issue 级重试一并清理）。"""

    def __init__(self):
        self.cleared_stop: list[int] = []

    def clear_stop_request(self, task_id: int) -> None:
        self.cleared_stop.append(task_id)


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
        # issue #94：close 桩——记录 (project_id, iid) 调用参数；
        # close_errors[project_id] 注入关闭异常（None 表示成功）
        self.close_calls: list[tuple[int, int]] = []
        self.close_errors: dict[int, Exception] = {}
        # issue #108：add_labels 桩——记录 (project_id, iid, labels,
        # remove) 调用参数；labels_update_errors[(project_id, iid)]
        # 注入更新异常（None 表示成功）；labels_update_result 配置
        # 返回对象（None 时用默认 issue 对象，labels 为空）
        self.labels_update_calls: list[tuple[int, int, list, list | None]] = []
        self.labels_update_errors: dict[tuple[int, int], Exception] = {}
        self.labels_update_result: dict | None = None
        # issue #303：更新负责人桩——assignee_update_calls 记录
        # (project_id, iid, assignee_ids) 调用参数；
        # assignee_update_errors[(project_id, iid)] 注入异常（None 表示
        # 成功）；assignee_update_result 配置返回对象（None 时用默认
        # issue 对象，assignees 为空）
        self.assignee_update_calls: list[tuple[int, int, list[int]]] = []
        self.assignee_update_errors: dict[tuple[int, int], Exception] = {}
        self.assignee_update_result: dict | None = None
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
        # issue #97：评论/活动查询桩——notes_by_issue 按 (project_id,
        # iid) 配置；fail_notes_errors[(project_id, iid)] 注入异常
        # （None 表示成功）；notes_calls 记录 (project_id, iid, limit)
        self.notes_by_issue: dict[tuple[int, int], list[dict]] = {}
        self.fail_notes_errors: dict[tuple[int, int], Exception] = {}
        self.notes_calls: list[tuple[int, int, int | None]] = []
        # issue #117：get_issue 桩——issue_by_key 按 (project_id, iid)
        # 配置返回对象；fail_get_issue_errors 注入异常（None 表示成功）；
        # get_issue_calls 记录 (project_id, iid) 调用参数
        self.issue_by_key: dict[tuple[int, int], dict] = {}
        self.fail_get_issue_errors: dict[tuple[int, int], Exception] = {}
        self.get_issue_calls: list[tuple[int, int]] = []
        # issue #125：评论/回复桩——add_comment_calls 记录 (project_id,
        # iid, body)；add_comment_errors[(project_id, iid)] 注入异常；
        # add_comment_result 配置返回对象（None 时用默认 note）；
        # reply_calls 记录 (project_id, iid, note_id, body)；
        # reply_errors[(project_id, iid)] 注入异常；reply_result 配置
        # 返回对象
        self.add_comment_calls: list[tuple[int, int, str]] = []
        self.add_comment_errors: dict[tuple[int, int], Exception] = {}
        self.add_comment_result: dict | None = None
        self.reply_calls: list[tuple[int, int, int, str]] = []
        self.reply_errors: dict[tuple[int, int], Exception] = {}
        self.reply_result: dict | None = None

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

    def close_issue(self, project_id, iid):
        """关闭 issue 桩（issue #94）：记录参数，可注入异常。"""
        self.close_calls.append((project_id, iid))
        err = self.close_errors.get(project_id)
        if err is not None:
            raise err
        return {"iid": iid, "state": "closed"}

    def add_labels(self, project_id, iid, labels, remove=None):
        """加/删标签桩（issue #108）：记录参数，可注入异常、可配置返回。"""
        self.labels_update_calls.append((project_id, iid, labels, remove))
        err = self.labels_update_errors.get((project_id, iid))
        if err is not None:
            raise err
        if self.labels_update_result is not None:
            return self.labels_update_result
        return {"iid": iid, "title": "x", "state": "opened",
                "web_url": f"https://gitlab.example.com/x/-/issues/{iid}",
                "labels": labels or [], "updated_at": None,
                "created_at": None, "description": None, "author": None,
                "milestone": None, "assignees": [],
                "user_notes_count": 0}

    def update_issue_assignee(self, project_id, iid, assignee_ids):
        """更新负责人桩（issue #303）：记录参数，可注入异常、可配置返回。"""
        self.assignee_update_calls.append((project_id, iid, list(assignee_ids)))
        err = self.assignee_update_errors.get((project_id, iid))
        if err is not None:
            raise err
        if self.assignee_update_result is not None:
            return self.assignee_update_result
        return {"iid": iid, "title": "x", "state": "opened",
                "web_url": f"https://gitlab.example.com/x/-/issues/{iid}",
                "labels": [], "updated_at": None, "created_at": None,
                "description": None, "author": None, "milestone": None,
                "assignees": [], "user_notes_count": 0}

    def list_issue_notes(self, project_id, iid, limit=None):
        """评论/活动查询桩（issue #97）：记录参数，可注入异常。"""
        self.notes_calls.append((project_id, iid, limit))
        err = self.fail_notes_errors.get((project_id, iid))
        if err is not None:
            raise err
        items = list(self.notes_by_issue.get((project_id, iid), []))
        # 模拟真实 GitLabClient._paged 的 limit 截断契约
        return items[:limit] if limit is not None else items

    def get_issue(self, project_id, iid):
        """单 issue 查询桩（issue #117：重试新建任务时拉取标题/标签）：
        按 (project_id, iid) 配置，可故障注入、记录调用参数。"""
        self.get_issue_calls.append((project_id, iid))
        err = self.fail_get_issue_errors.get((project_id, iid))
        if err is not None:
            raise err
        return dict(self.issue_by_key.get((project_id, iid), {
            "iid": iid, "title": f"issue #{iid}", "state": "opened",
            "labels": [], "updated_at": None, "web_url": None,
            "description": None, "author": None, "milestone": None,
            "assignees": [], "user_notes_count": 0,
        }))

    def add_comment(self, project_id, iid, body):
        """添加评论桩（issue #125）：记录参数，可注入异常、可配置返回。"""
        self.add_comment_calls.append((project_id, iid, body))
        err = self.add_comment_errors.get((project_id, iid))
        if err is not None:
            raise err
        if self.add_comment_result is not None:
            return self.add_comment_result
        return {"id": 9001, "body": body, "system": False,
                "author": {"name": "code01", "username": "project_bot",
                           "avatar_url": "https://gitlab.example.com/a.png"},
                "created_at": "2026-08-16T10:00:00.000+08:00"}

    def reply_to_note(self, project_id, iid, note_id, body):
        """回复评论桩（issue #125）：记录参数，可注入异常、可配置返回。"""
        self.reply_calls.append((project_id, iid, note_id, body))
        err = self.reply_errors.get((project_id, iid))
        if err is not None:
            raise err
        if self.reply_result is not None:
            return self.reply_result
        return {"id": 9002, "body": body, "system": False,
                "author": {"name": "code01", "username": "project_bot",
                           "avatar_url": "https://gitlab.example.com/a.png"},
                "created_at": "2026-08-16T11:00:00.000+08:00"}


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
                          config_path=str(config_path),
                          scheduler=StubScheduler(), executor=StubExecutor())
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


@pytest.fixture
def client_edit(client, monkeypatch):
    """概览页编辑测试夹具（issue #132）：已配置 owner token，且 owner
    client 构造重定向到同一 stub——概览页编辑必须使用 owner token，未
    配置时直接 400 拦截；编辑用例统一走 owner 路径（写操作与旧回退
    路径共用同一 stub，错误映射/缓存失效断言不变）。"""
    tc, stub, db, tmp_path = client
    tc.app.state.ctx.config.update_gitlab({"owner_token": "owner-token-1"})
    from botler.api import issues as issues_mod
    monkeypatch.setattr(
        issues_mod, "GitLabClient",
        lambda url, token, verify_ssl=True, webhook_base_url=None: stub)
    return tc, stub, db, tmp_path


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
        （右边栏详情），缺失时 description/author/created_at 为 None。
        issue #94 起注入 project_id（关闭按钮定位仓库用）。"""
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
            "project_id": 42,
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
            "project_id": 42,
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

    def test_label_colors_with_hash_prefix_normalized(self, client):
        """issue #100：GitLab labels API 实际返回的颜色带 # 前缀（实测
        {color: "#6699cc", text_color: "#FFFFFF"}），后端归一化为不带 # 的
        6 位 hex 透传（前端内联样式自行拼 #，双重 # 会失效）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "#FF0000", "text_color": "#FFFFFF"},
            {"name": "ui", "color": "#69d100", "text_color": "#FFFFFF"},
        ]}
        stub.issues_by_project = {42: [make_issue(1, "a", labels=["bug", "ui"])]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        labels = data["repos"][0]["issues"][0]["labels"]
        assert labels == [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69d100", "text_color": "FFFFFF"},
        ]

    def test_label_color_invalid_hash_variants_ignored(self, client):
        """issue #100 安全兜底扩展：# 前缀合法化后，仍拒绝畸形值
        （# 后不足 6 位、重复 #、非 hex 字符、带空白）——防样式注入
        的校验边界不能因兼容 # 前缀而放宽。"""
        tc, stub, db, tmp_path = client
        _add_repo(db)
        stub.labels_by_project = {42: [
            {"name": "a", "color": "#12345", "text_color": "#FFFFFF"},
            {"name": "b", "color": "##123456", "text_color": "#FFFFFF"},
            {"name": "c", "color": "#GGGGGG", "text_color": "#FFFFFF"},
            {"name": "d", "color": "#12 345", "text_color": "#FFFFFF"},
            {"name": "e", "color": 123456, "text_color": None},
        ]}
        stub.issues_by_project = {42: [make_issue(1, "a",
                                                  labels=["a", "b", "c", "d", "e"])]}

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        labels = data["repos"][0]["issues"][0]["labels"]
        assert labels == [
            {"name": "a", "color": None, "text_color": None},
            {"name": "b", "color": None, "text_color": None},
            {"name": "c", "color": None, "text_color": None},
            {"name": "d", "color": None, "text_color": None},
            {"name": "e", "color": None, "text_color": None},
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


class TestCloseIssue:
    """POST /api/issues/{project_id}/{iid}/close：概览页右边栏关闭 issue
    按钮（issue #94）。

    定位仓库：按 GitLab project_id 匹配「已启用」仓库，客户端选择与
    概览聚合一致（per-repo token 优先，回退全局）；成功后清空概览
    缓存，下一轮轮询立即反映关闭状态。错误映射：仓库不存在/未启用
    → 404，GitLab 404（issue 不存在）→ 404，GitLab 其他错误与网络
    错误 → 502。
    """

    def test_close_success(self, client_edit):
        """正常关闭：stub 收到正确参数、返回 ok 与 closed 状态。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/42/64/close")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "state": "closed"}
        assert stub.close_calls == [(42, 64)]

    def test_close_clears_overview_cache(self, client_edit):
        """关闭成功后清空概览缓存：下次 overview 重新聚合、新数据生效。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        assert tc.get("/api/issues/overview").json()["total"] == 1
        # 10 秒 TTL 内命中缓存：改数据后仍返回旧结果（证明缓存生效）
        stub.issues_by_project = {42: []}
        assert tc.get("/api/issues/overview").json()["total"] == 1

        tc.post("/api/issues/42/64/close")

        # 缓存被清 → 重新聚合，读到新的（空）数据
        assert tc.get("/api/issues/overview").json()["total"] == 0

    def test_close_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，且不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/999/64/close")

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]
        assert stub.close_calls == []

    def test_close_repo_disabled(self, client):
        """仓库未启用 → 404（与概览聚合只聚合启用仓库一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.post("/api/issues/42/64/close")

        assert resp.status_code == 404
        assert stub.close_calls == []

    def test_close_issue_missing(self, client_edit):
        """GitLab 返回 404（issue 不存在/已被删除）→ 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.close_errors[42] = GitLabError("404 Not Found", status_code=404)

        resp = tc.post("/api/issues/42/64/close")

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_close_gitlab_server_error(self, client_edit):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.close_errors[42] = GitLabError("500 Internal Server Error",
                                            status_code=500)

        resp = tc.post("/api/issues/42/64/close")

        assert resp.status_code == 502

    def test_close_network_error(self, client_edit):
        """网络错误（httpx.HTTPError，per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.close_errors[42] = httpx.HTTPError("connect timeout")

        resp = tc.post("/api/issues/42/64/close")

        assert resp.status_code == 502

    def test_repeat_close_idempotent(self, client_edit):
        """重复关闭同一 issue（如双标签页并发点击）：接口幂等成功。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        r1 = tc.post("/api/issues/42/64/close")
        r2 = tc.post("/api/issues/42/64/close")

        assert r1.status_code == 200 and r2.status_code == 200
        assert stub.close_calls == [(42, 64), (42, 64)]

    def test_overview_issues_carry_project_id(self, client):
        """overview 聚合结果的每条 issue 带 project_id（前端关闭按钮
        定位仓库用，issue #94）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        _add_repo(db, project_id=7, name="other")
        stub.issues_by_project = {
            42: [make_issue(1, "x")],
            7: [make_issue(2, "y")],
        }

        resp = tc.get("/api/issues/overview")

        assert resp.status_code == 200
        pairs = [(r["repo_name"], it.get("project_id"))
                 for r in resp.json()["repos"] for it in r["issues"]]
        assert pairs == [("demo", 42), ("other", 7)]


# ---- issue #117：概览页右边栏重试按钮 ----

class TestRetryIssue:
    """POST /api/issues/{project_id}/{iid}/retry：概览页右边栏「重试」按钮。

    语义：按 project_id+iid 定位该 issue 的任务——
    - 已有活跃任务（queued/running/retrying）→ 409（防重复执行）；
    - 最近任务为 failed/interrupted → 复用任务记录重试（重置 queued
      入队，与任务页手动重试一致，保留断点续跑）；
    - 无任务记录或最近任务已终态成功 → 新建任务入队（triggered_by=
      manual，记录 issue 标签/更新时间供调度器排序）。
    仓库定位/客户端选择/缓存清理与关闭接口一致；GitLab 404 → 404、
    其他错误与网络错误 → 502。
    """

    @staticmethod
    def _mk_repo(db, project_id=42, name="demo", enabled=True) -> int:
        return _add_repo(db, project_id=project_id, name=name, enabled=enabled)

    @staticmethod
    def _mk_task(db, repo_id, iid=64, status="failed", **fields) -> int:
        task_id = db.create_task(repo_id, 42, iid, f"任务 {iid}",
                                 triggered_by="webhook")
        db.set_task_status(task_id, status, **fields)
        return task_id

    def test_retry_failed_task_requeues(self, client):
        """最近任务为 failed → 复用任务记录重试：重置 queued、triggered_by
        标记 manual（与任务页手动重试一致），不触碰 GitLab。"""
        tc, stub, db, _ = client
        repo_id = self._mk_repo(db)
        tid = self._mk_task(db, repo_id, status="failed", error_message="原因")

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 200
        assert resp.json() == {"task_id": tid, "status": "queued", "mode": "retried"}
        row = db.get_task(tid)
        assert row["status"] == "queued"
        assert row["triggered_by"] == "manual"
        assert row["error_message"] is None, "失败原因应清空"
        assert stub.get_issue_calls == [], "重试既有任务不应拉取 issue"

    def test_retry_enqueues_and_clears_stop_request(self, client):
        """重试成功后任务重新入队；历史停止请求残留一并清除（issue #69，
        与任务页手动重试链路一致）。"""
        tc, stub, db, _ = client
        repo_id = self._mk_repo(db)
        tid = self._mk_task(db, repo_id, status="interrupted",
                            error_message="用户手动停止")
        ctx = tc.app.state.ctx

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 200
        assert ctx.scheduler.enqueued == [tid], "重试任务应重新入队"
        assert ctx.executor.cleared_stop == [tid], "应清除历史停止请求残留"

    def test_retry_no_task_creates_new(self, client):
        """无任务记录（如 bot-failed 标签手动补打）→ 新建任务入队，
        标题/标签/更新时间取自 GitLab issue。"""
        tc, stub, db, _ = client
        repo_id = self._mk_repo(db)
        stub.issue_by_key = {(42, 64): make_issue(
            64, "重试我", updated_at="2026-08-14T18:30:00.000+08:00",
            created_at="2026-08-14T08:00:00.000+08:00",
            labels=["feature", "bot-failed"])}

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "created"
        assert body["status"] == "queued"
        assert stub.get_issue_calls == [(42, 64)]
        row = db.get_task(body["task_id"])
        assert row is not None
        assert row["issue_title"] == "重试我"
        assert row["triggered_by"] == "manual"
        assert row["issue_updated_at"] == "2026-08-14 10:30:00",             "issue 更新时间应归一化为 UTC 无后缀（+08:00 → UTC）"
        assert row["issue_created_at"] == "2026-08-14 00:00:00",             "issue 创建时间应归一化为 UTC 无后缀（+08:00 → UTC）"
        assert set(json.loads(row["issue_labels"])) == {"feature", "bot-failed"}
        assert tc.app.state.ctx.scheduler.enqueued == [body["task_id"]],             "新建任务应重新入队"
        assert db.count_tasks(repo_id=repo_id) == 1

    def test_retry_latest_succeeded_creates_new(self, client):
        """最近任务已终态成功（bot-failed 标签残留但任务实际成功）→
        新建任务重新执行（用户显式点击重试）。"""
        tc, stub, db, _ = client
        repo_id = self._mk_repo(db)
        self._mk_task(db, repo_id, status="succeeded")
        stub.issue_by_key = {(42, 64): make_issue(64, "重试我")}

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 200
        assert resp.json()["mode"] == "created"
        assert db.count_tasks(repo_id=repo_id) == 2, "应新建任务而非复用成功任务"

    def test_retry_active_task_conflict(self, client):
        """已有活跃任务（排队/执行/重试中）→ 409，不改动任何任务。"""
        tc, stub, db, _ = client
        repo_id = self._mk_repo(db)
        active_id = self._mk_task(db, repo_id, status="running")

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 409
        assert "执行中" in resp.json()["detail"]
        assert db.get_task(active_id)["status"] == "running", "冲突时任务保持原状"
        assert tc.app.state.ctx.scheduler.enqueued == []
        assert stub.get_issue_calls == []

    def test_retry_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，不触碰 GitLab/任务表。"""
        tc, stub, db, _ = client
        self._mk_repo(db, project_id=42)

        resp = tc.post("/api/issues/999/64/retry")

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]
        assert stub.get_issue_calls == []

    def test_retry_repo_disabled(self, client):
        """仓库未启用 → 404（与概览聚合只聚合启用仓库一致）。"""
        tc, stub, db, _ = client
        self._mk_repo(db, enabled=False)

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 404
        assert stub.get_issue_calls == []

    def test_retry_issue_missing(self, client):
        """新建任务路径下 GitLab 返回 404（issue 不存在）→ 404。"""
        tc, stub, db, _ = client
        self._mk_repo(db)
        stub.fail_get_issue_errors[(42, 64)] = GitLabError(
            "404 Not Found", status_code=404)

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_retry_gitlab_server_error(self, client):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client
        self._mk_repo(db)
        stub.fail_get_issue_errors[(42, 64)] = GitLabError(
            "500 Internal Server Error", status_code=500)

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 502

    def test_retry_network_error(self, client):
        """网络错误（httpx.HTTPError，per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client
        self._mk_repo(db)
        stub.fail_get_issue_errors[(42, 64)] = httpx.HTTPError("connect timeout")

        resp = tc.post("/api/issues/42/64/retry")

        assert resp.status_code == 502

    def test_retry_clears_overview_cache(self, client):
        """重试成功后清空概览缓存：下次 overview 重新聚合、新数据生效。"""
        tc, stub, db, _ = client
        self._mk_repo(db)
        stub.issues_by_project = {42: [make_issue(64, "x", labels=["bot-failed"])]}
        assert tc.get("/api/issues/overview").json()["total"] == 1
        # 10 秒 TTL 内命中缓存：改数据后仍返回旧结果（证明缓存生效）
        stub.issues_by_project = {42: []}
        assert tc.get("/api/issues/overview").json()["total"] == 1

        tc.post("/api/issues/42/64/retry")

        assert tc.get("/api/issues/overview").json()["total"] == 0,             "重试成功后缓存应被清空"


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

    def test_labels_color_with_hash_prefix_normalized(self, client):
        """issue #100：GitLab labels API 实际返回带 # 前缀的颜色
        （#6699cc），添加 issue 弹窗的标签多选应拿到归一化后的无 # 6 位
        hex——前端 label-pill 自行拼 #，不透传原样才能正确着色。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 1, "user_id": 20, "username": "agent", "name": "Agent"}]}
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "#d9534f", "text_color": "#FFFFFF"},
            {"name": "ui", "color": "#428BCA", "text_color": "#FFFFFF"},
        ]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["labels"] == [
            {"name": "bug", "color": "d9534f", "text_color": "FFFFFF"},
            {"name": "ui", "color": "428BCA", "text_color": "FFFFFF"},
        ]

    def test_labels_color_invalid_ignored(self, client):
        """issue #100 安全兜底：标签颜色非法（含畸形 # 前缀）→ 无色降级，
        不因兼容 # 前缀而放宽防样式注入校验。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: [
            {"id": 1, "user_id": 20, "username": "agent", "name": "Agent"}]}
        stub.labels_by_project = {42: [
            {"name": "ok", "color": "#6699cc", "text_color": "#FFFFFF"},
            {"name": "bad", "color": "<script>", "text_color": "#FFFFFF"},
            {"name": "half", "color": "#abc", "text_color": "#FFFFFF"},
        ]}

        resp = tc.get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        assert resp.json()["labels"] == [
            {"name": "ok", "color": "6699cc", "text_color": "FFFFFF"},
            {"name": "bad", "color": None, "text_color": None},
            {"name": "half", "color": None, "text_color": None},
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

    def test_create_success(self, client_edit):
        """正常路径：调用 create_issue 传参正确（labels 保持数组由
        GitLabClient 拼逗号），返回 201 与精简后的 issue 对象。"""
        tc, stub, db, tmp_path = client_edit
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

    def test_blank_label_elements_filtered(self, client_edit):
        """边界：标签含空白元素时过滤后仍合法（空白项忽略）。"""
        tc, stub, db, tmp_path = client_edit
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

    def test_description_optional(self, client_edit):
        """边界：描述选填——缺失/空白时 API 层透传 None，不阻塞创建；
        请求发送前的标题填充由 GitLabClient 层兜底（issue #103，见
        test_gitlab_client.py::TestCreateIssue）。"""
        tc, stub, db, tmp_path = client_edit
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

    def test_create_failure_returns_502(self, client_edit):
        """GitLab 创建失败 → 502，错误信息透出。"""
        tc, stub, db, tmp_path = client_edit
        _add_repo(db, project_id=42, name="a")
        stub.fail_create_projects = {42}

        resp = self._post(tc)

        assert resp.status_code == 502
        assert "创建 issue 失败" in resp.json()["detail"]

    def test_uses_owner_client(self, client_edit, monkeypatch):
        """issue #132：创建 issue 走 owner client（配置了 owner token 时
        per-repo client 不再参与写操作）。"""
        tc, stub, db, tmp_path = client_edit
        _add_repo(db, project_id=42, name="a")
        per = StubGitLab()
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = self._post(tc)

        assert resp.status_code == 201, resp.text
        assert len(stub.create_calls) == 1, "创建 issue 应走 owner client"
        assert per.create_calls == [], "per-repo client 不得用于概览页写操作"

    def test_create_invalidates_overview_cache(self, client_edit):
        """创建成功后清空 overview 缓存：下一次 overview 请求重新拉取
        （前端创建成功立即刷新列表，不能拿到 10 秒 TTL 旧缓存）。"""
        tc, stub, db, tmp_path = client_edit
        _add_repo(db, project_id=42, name="a")
        stub.issues_by_project = {42: [make_issue(1, "旧 issue")]}

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 1  # 首次拉取

        resp = self._post(tc)
        assert resp.status_code == 201

        tc.get("/api/issues/overview")
        assert len(stub.calls) == 2  # 缓存已失效，重新拉取


# ---- issue #97：概览页右边栏 issue 评论与活动 ----

def make_note(note_id: int, body: str, system: bool = False,
              author: dict | None = None,
              created_at: str | None = "2026-08-15T10:00:00.000+08:00") -> dict:
    """构造 GitLab note 对象（评论 system=False / 活动 system=True）。"""
    return {"id": note_id, "body": body, "system": system,
            "author": author, "created_at": created_at}


class TestIssueDetail:
    """GET /api/issues/{project_id}/{iid}/detail：右边栏评论与活动数据
    （issue #97）。

    定位仓库与关闭接口一致（project_id 匹配「已启用」仓库，客户端选择
    per-repo token 优先）；notes 升序拉取、最多 100 条；精简字段：
    id/body/system/author{name,username,avatar_url}/created_at（UTC 无
    后缀）。错误映射：仓库不存在/未启用 → 404，GitLab 404（issue
    不存在）→ 404，GitLab 其他错误与网络错误 → 502。
    """

    def test_returns_notes_with_trimmed_fields(self, client):
        """正常：返回评论与系统活动，字段精简且时间转 UTC 无后缀。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): [
            make_note(101, "assigned to @agent", system=True,
                      author={"id": 3, "name": "agent", "username": "agent",
                              "avatar_url": "https://g.example.com/a.png"},
                      created_at="2026-08-15T10:00:00.000+08:00"),
            make_note(102, "**确认** 可行", system=False,
                      author={"id": 11, "name": "code01",
                              "username": "project_bot",
                              "avatar_url": "https://g.example.com/b.png",
                              "state": "active"},
                      created_at="2026-08-15T11:30:00.000+08:00"),
        ]}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        notes = resp.json()["notes"]
        assert len(notes) == 2
        # 评论（system=false）与活动（system=true）均透传，供前端分区渲染
        assert notes[0]["system"] is True
        assert notes[1]["system"] is False
        # author 精简：只留 name/username/avatar_url（丢弃 id/state）
        assert notes[0]["author"] == {"name": "agent", "username": "agent",
                                      "avatar_url": "https://g.example.com/a.png"}
        assert notes[1]["author"]["name"] == "code01"
        # created_at 转 UTC 无后缀（前端 fmtTime 解析约定）
        assert notes[0]["created_at"] == "2026-08-15 02:00:00"
        assert notes[1]["created_at"] == "2026-08-15 03:30:00"
        # body 原样透传（前端 Markdown 渲染）
        assert notes[1]["body"] == "**确认** 可行"

    def test_notes_limit_passed_to_client(self, client):
        """每 issue 最多拉 100 条：list_issue_notes 收到 limit=100。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}

        tc.get("/api/issues/42/64/detail")

        assert stub.notes_calls == [(42, 64, 100)]

    def test_empty_notes(self, client):
        """边界：无评论无活动 → notes 为空列表；无任务记录 → engine 回退
        全局 worker.engine（默认 claude，前端显示 Claude Code CLI）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json() == {"notes": [], "engine": "claude",
                            "task_id": None, "task_duration_seconds": None}

    # ---- issue #120：执行引擎按 issue 展示（回退链：任务落库 engine
    # > 断点续跑会话字段推断 > 全局 worker.engine）----

    def test_returns_engine_from_latest_task(self, client):
        """有任务记录：返回该 issue 最近任务实际落库的执行引擎。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "历史任务")
        db.set_task_status(task_id, "succeeded", engine="claude")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["engine"] == "claude"

    def test_engine_falls_back_to_global_when_no_task(self, client):
        """无任务记录（issue 从未处理）→ 回退全局 worker.engine。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        tc.app.state.ctx.config.get().engine = "dsh"

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["engine"] == "dsh"

    def test_engine_inferred_from_session_fields_for_legacy_task(self, client):
        """旧任务无 engine 落库 → 按断点续跑会话字段推断（claude_session_id
        → claude），全局引擎已切 dsh 也不受影响。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        tc.app.state.ctx.config.get().engine = "dsh"
        task_id = db.create_task(repo_id, 42, 64, "旧任务")
        db.set_task_status(task_id, "succeeded", claude_session_id="sess-legacy")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["engine"] == "claude", "旧 claude 任务不应随全局引擎误显 dsh"

    def test_engine_prefers_recorded_value_over_session_field(self, client):
        """engine 已落库时优先使用落库值，不做会话字段推断。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "任务")
        db.set_task_status(task_id, "succeeded", engine="hermes",
                           dsh_session_id="dsh-sess-x")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.json()["engine"] == "hermes"

    # ---- issue #290：任务id展示——detail 返回该 issue 最近任务 id ----

    def test_returns_task_id_from_latest_task(self, client):
        """已执行（有任务记录）→ 返回该 issue 最近任务 id，供前端侧边栏
        展示对应任务 id。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "历史任务")
        db.set_task_status(task_id, "succeeded", engine="claude")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_id"] == task_id
        assert resp.json()["engine"] == "claude"

    def test_task_id_is_latest_of_multiple(self, client):
        """同 issue 多条任务记录（重新指派/对账补入队/手动重试）→
        task_id 取最新一条（id 倒序最新在前，与任务列表排序约定一致）。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        first = db.create_task(repo_id, 42, 64, "第一次任务")
        db.set_task_status(first, "succeeded", engine="claude")
        second = db.create_task(repo_id, 42, 64, "第二次任务")
        db.set_task_status(second, "succeeded", engine="dsh")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_id"] == second
        assert resp.json()["engine"] == "dsh", "引擎同样取最近任务的落库值"

    def test_task_id_none_when_no_task(self, client):
        """从未执行（无任务记录）→ task_id 为 null，前端显示「—」。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_id"] is None

    # ---- issue #300：完成耗时展示——detail 返回最近任务完成耗时 ----

    @staticmethod
    def _mk_task_times(db, task_id, created_at, finished_at):
        """写入受控 created_at / finished_at（UTC 无后缀串，与
        TestCompletionStats._mk_succeeded 同法）。"""
        db.set_task_status(task_id, "succeeded", finished_at=finished_at)
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                         (created_at, task_id))

    def test_returns_duration_from_succeeded_task(self, client):
        """任务已完成（succeeded 且有合法 created_at/finished_at）→ 返回
        完成耗时秒数（finished_at - created_at，与 issue #180 语义一致），
        供前端侧边栏「完成耗时」行展示。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "已完成任务")
        self._mk_task_times(db, task_id, "2026-08-12 02:00:00",
                            "2026-08-12 03:00:00")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_duration_seconds"] == 3600.0
        assert resp.json()["task_id"] == task_id

    def test_duration_none_when_task_not_completed(self, client):
        """任务未完成（failed 终态即使带 finished_at）→ 完成耗时为 null，
        前端显示「—」（完成耗时只对成功终态有意义）。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "失败任务")
        db.set_task_status(task_id, "failed", finished_at="2026-08-12 03:00:00")
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                         ("2026-08-12 02:00:00", task_id))

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_duration_seconds"] is None
        # 任务 id 照常返回（任务行展示不受影响，只有完成耗时隐藏）
        assert resp.json()["task_id"] == task_id

    def test_duration_none_when_no_task(self, client):
        """从未执行（无任务记录）→ 完成耗时为 null。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_duration_seconds"] is None

    def test_duration_none_when_times_missing(self, client):
        """succeeded 但缺 finished_at / created_at 非法 → 无法计算 → null。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        # 缺 finished_at（空串）
        t1 = db.create_task(repo_id, 42, 64, "缺完成时间")
        db.set_task_status(t1, "succeeded", finished_at="")
        # created_at 非法格式
        t2 = db.create_task(repo_id, 42, 65, "非法创建时间")
        db.set_task_status(t2, "succeeded", finished_at="2026-08-12 03:00:00")
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                         ("not-a-time", t2))

        r1 = tc.get("/api/issues/42/64/detail").json()
        r2 = tc.get("/api/issues/42/65/detail").json()
        assert r1["task_duration_seconds"] is None
        assert r2["task_duration_seconds"] is None

    def test_duration_none_when_negative(self, client):
        """用时为负（时钟异常）→ null，不展示错误耗时。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): []}
        task_id = db.create_task(repo_id, 42, 64, "时钟异常任务")
        self._mk_task_times(db, task_id, "2026-08-12 10:00:00",
                            "2026-08-12 09:00:00")

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert resp.json()["task_duration_seconds"] is None

    def test_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，且不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/999/64/detail")

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]
        assert stub.notes_calls == []

    def test_repo_disabled(self, client):
        """仓库未启用 → 404（与概览聚合只聚合启用仓库一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 404
        assert stub.notes_calls == []

    def test_issue_missing(self, client):
        """GitLab 返回 404（issue 不存在/已被删除）→ 404。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_notes_errors = {(42, 64): GitLabError("404 Not Found",
                                                        status_code=404)}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_gitlab_server_error(self, client):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_notes_errors = {(42, 64): GitLabError("500 Internal Server Error",
                                                        status_code=500)}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 502

    def test_network_error(self, client):
        """网络错误（httpx.HTTPError，per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_notes_errors = {(42, 64): httpx.HTTPError("connect timeout")}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 502

    def test_note_missing_fields_fallback(self, client):
        """边界：note 缺 author/created_at/body（异常数据）→ None 兜底
        不崩溃（前端显示占位符）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.notes_by_issue = {(42, 64): [
            {"id": 1, "body": None, "system": False, "author": None,
             "created_at": None},
            {"id": 2, "body": "changed due date", "system": True,
             "author": None, "created_at": "2026-08-15T09:00:00.000+08:00"},
        ]}

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        notes = resp.json()["notes"]
        assert notes[0]["body"] is None
        assert notes[0]["author"] is None
        assert notes[0]["created_at"] is None
        assert notes[1]["system"] is True

    def test_uses_per_repo_client(self, client, monkeypatch):
        """per-repo client 优先（与概览聚合/关闭接口一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="a")
        per = StubGitLab()
        from botler.api import issues as issues_mod
        monkeypatch.setattr(issues_mod, "_repo_client",
                            lambda c, row: per if row["name"] == "a" else None)

        resp = tc.get("/api/issues/42/64/detail")

        assert resp.status_code == 200
        assert len(per.notes_calls) == 1
        assert stub.notes_calls == []


class TestIssueTasks:
    """GET /api/issues/{project_id}/{iid}/tasks（issue #167）：概览页
    issue 右边栏「查看执行的详情」数据源——该 issue 的全部任务执行记录。

    仓库定位与 detail 接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；按 project_id + issue_iid 查任务表（id 倒序、最新
    在前），任务字典复用任务列表接口序列化（status/engine/commit_url/
    时间等，同 issue 多条任务记录——重新指派/对账补入队/手动重试——
    全部返回，供前端第二层右边栏切换查看）。
    """

    def test_empty_no_task_records(self, client):
        """边界：issue 从未执行过 → tasks 空列表、total 0。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/42/64/tasks")

        assert resp.status_code == 200
        assert resp.json() == {"tasks": [], "total": 0}

    def test_lists_all_tasks_latest_first(self, client):
        """多条任务记录（重新指派/对账补入队）全部返回，按 id 倒序最新在前。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        # create_task 对已有活跃任务去重：每建一条先置终态再建下一条
        t1 = db.create_task(repo_id, 42, 64, "第一次执行", triggered_by="reconcile")
        db.set_task_status(t1, "succeeded", engine="claude",
                           commit_sha="a" * 40)
        t2 = db.create_task(repo_id, 42, 64, "第二次执行", triggered_by="manual")
        db.set_task_status(t2, "failed", engine="hermes")
        t3 = db.create_task(repo_id, 42, 64, "第三次执行")
        db.set_task_status(t3, "running", engine="dsh", dsh_session_id="sess-1")

        resp = tc.get("/api/issues/42/64/tasks")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        ids = [t["id"] for t in body["tasks"]]
        assert ids == sorted(ids, reverse=True), "应按 id 倒序（最新在前）"
        # 序列化字段：id/status/engine/issue/commit/时间齐备
        latest = body["tasks"][0]
        assert latest["id"] == t3
        assert latest["status"] == "running"
        assert latest["engine"] == "dsh"
        assert latest["repo_name"] == "demo"
        assert latest["project_id"] == 42
        assert latest["issue_iid"] == 64
        assert latest["issue_title"] == "第三次执行"
        assert latest["triggered_by"] == "webhook"
        assert latest["commit_url"] is None
        assert body["tasks"][1]["engine"] == "hermes"
        assert body["tasks"][1]["status"] == "failed"
        assert body["tasks"][2]["status"] == "succeeded"

    def test_commit_url_joined_from_repo_url(self, client):
        """commit_sha 存在时拼接 GitLab 提交地址（复用任务列表序列化）。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        sha = "0123456789abcdef"
        t1 = db.create_task(repo_id, 42, 64, "任务")
        db.set_task_status(t1, "succeeded", engine="claude", commit_sha=sha)

        resp = tc.get("/api/issues/42/64/tasks")

        task = resp.json()["tasks"][0]
        assert task["commit_sha"] == sha
        assert task["commit_url"] == f"https://gitlab.example.com/demo/-/commit/{sha}"

    def test_other_issue_tasks_not_included(self, client):
        """只返回该 issue 的任务，不串扰同仓库其他 issue / 其他仓库。"""
        tc, stub, db, _ = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        t1 = db.create_task(repo_id, 42, 64, "issue 64 任务")
        db.set_task_status(t1, "succeeded", engine="claude")
        t2 = db.create_task(repo_id, 42, 65, "issue 65 任务")
        db.set_task_status(t2, "succeeded", engine="claude")

        resp = tc.get("/api/issues/42/64/tasks")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["tasks"][0]["issue_iid"] == 64

    def test_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/999/64/tasks")

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]

    def test_disabled_repo_not_found(self, client):
        """仓库未启用（概览聚合不展示）→ 404，与 detail/close 一致。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.get("/api/issues/42/64/tasks")

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]


class TestIssueLabels:
    """GET /api/issues/{project_id}/labels 与 PUT /api/issues/
    {project_id}/{iid}/labels：概览页右边栏标记编辑（issue #108）。

    GET 返回项目标记池（归一化颜色，复用 form-meta 的 _form_meta_labels
    精简逻辑），供右边栏「编辑标记」多选数据源；PUT 以 add/remove
    一次提交加删标签（复用 GitLabClient.add_labels 的 add_labels/
    remove_labels 同请求语义），成功后清空概览缓存并返回更新后的
    标签列表。

    定位仓库与关闭接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；错误映射：GitLab 404（issue 不存在）→ 404，
    GitLab 其他错误与网络错误 → 502。
    """

    def test_get_labels_success(self, client):
        """正常获取：返回归一化标签池（# 前缀颜色 → 无 # 6 位 hex，
        非法颜色 → None 中性降级，与 issue #100 约定一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.labels_by_project = {42: [
            {"id": 1, "name": "feature", "color": "#6699cc",
             "text_color": "#FFFFFF", "description": None},
            {"id": 2, "name": "bug", "color": "#ff0000",
             "text_color": "#FFFFFF", "description": "缺陷"},
            {"id": 3, "name": "plain", "color": "not-a-color",
             "text_color": "#FFFFFF", "description": None},
        ]}

        resp = tc.get("/api/issues/42/labels")

        assert resp.status_code == 200
        labels = resp.json()["labels"]
        assert [l["name"] for l in labels] == ["feature", "bug", "plain"]
        assert labels[0]["color"] == "6699cc"
        assert labels[0]["text_color"] == "FFFFFF"
        assert labels[2]["color"] is None
        assert labels[2]["text_color"] is None
        assert stub.label_calls == [42]

    def test_get_labels_empty(self, client):
        """边界：项目无任何标签 → 200 空列表（前端提示暂无标记）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/42/labels")

        assert resp.status_code == 200
        assert resp.json() == {"labels": []}

    def test_get_labels_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/999/labels")

        assert resp.status_code == 404
        assert stub.label_calls == []

    def test_get_labels_repo_disabled(self, client):
        """仓库未启用 → 404（与概览聚合只聚合启用仓库一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.get("/api/issues/42/labels")

        assert resp.status_code == 404
        assert stub.label_calls == []

    def test_get_labels_gitlab_error(self, client):
        """GitLab 标签 API 失败 → 502（标记池是编辑数据源，不可降级为空）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_label_projects = {42}

        resp = tc.get("/api/issues/42/labels")

        assert resp.status_code == 502

    def test_get_labels_network_error(self, client):
        """网络错误（per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_label_projects = {42}
        # 网络错误与 GitLabError 走同一 502 分支，桩改为抛网络异常验证
        from unittest.mock import patch
        with patch.object(stub, "list_project_labels",
                          side_effect=httpx.HTTPError("connect timeout")):

            resp = tc.get("/api/issues/42/labels")

        assert resp.status_code == 502

    def test_put_labels_success(self, client_edit):
        """正常更新：stub 收到 add/remove 参数，返回更新后标签（带颜色）。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.labels_by_project = {42: [
            {"id": 1, "name": "feature", "color": "#6699cc",
             "text_color": "#FFFFFF"},
            {"id": 2, "name": "bug", "color": "#ff0000",
             "text_color": "#FFFFFF"},
        ]}
        stub.labels_update_result = {
            "iid": 64, "title": "x", "state": "opened",
            "web_url": "https://gitlab.example.com/x/-/issues/64",
            "labels": ["feature"], "updated_at": None, "created_at": None,
            "description": None, "author": None, "milestone": None,
            "assignees": [], "user_notes_count": 0,
        }

        resp = tc.put("/api/issues/42/64/labels",
                      json={"add": ["feature"], "remove": ["bug"]})

        assert resp.status_code == 200
        assert stub.labels_update_calls == [(42, 64, ["feature"], ["bug"])]
        labels = resp.json()["labels"]
        assert [l["name"] for l in labels] == ["feature"]
        assert labels[0]["color"] == "6699cc"
        assert labels[0]["text_color"] == "FFFFFF"

    def test_put_labels_add_only(self, client_edit):
        """仅添加（remove 缺失）→ remove 传 None，不触发移除语义。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 200
        assert stub.labels_update_calls == [(42, 64, ["feature"], None)]

    def test_put_labels_remove_only(self, client_edit):
        """仅移除（add 缺失）→ add 传空列表。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/labels", json={"remove": ["bug"]})

        assert resp.status_code == 200
        assert stub.labels_update_calls == [(42, 64, [], ["bug"])]

    def test_put_labels_no_change(self, client):
        """add/remove 均为空 → 400，不触碰 GitLab（空提交无意义，
        也规避 GitLab 对空 add_labels/remove_labels 的 400）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/labels", json={"add": [], "remove": []})

        assert resp.status_code == 400
        assert stub.labels_update_calls == []

    def test_put_labels_normalize(self, client_edit):
        """标签名归一化：去空白、去重（"feature"、"feature "、"" →
        ["feature"]）后传给 GitLab。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/labels",
                      json={"add": ["feature", " feature ", "", "feature"],
                            "remove": ["bug ", ""]})

        assert resp.status_code == 200
        assert stub.labels_update_calls == [(42, 64, ["feature"], ["bug"])]

    def test_put_labels_normalize_all_blank(self, client):
        """边界：add/remove 归一化后全空（全是空白串）→ 400。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/labels",
                      json={"add": ["  "], "remove": [""]})

        assert resp.status_code == 400
        assert stub.labels_update_calls == []

    def test_put_labels_clears_overview_cache(self, client_edit):
        """更新成功后清空概览缓存：下次 overview 重新聚合、新标签生效。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        assert tc.get("/api/issues/overview").json()["total"] == 1
        # 10 秒 TTL 内命中缓存：改数据后仍返回旧结果（证明缓存生效）
        stub.issues_by_project = {42: []}
        assert tc.get("/api/issues/overview").json()["total"] == 1

        tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        # 缓存被清 → 重新聚合，读到新的（空）数据
        assert tc.get("/api/issues/overview").json()["total"] == 0

    def test_put_labels_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/999/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 404
        assert stub.labels_update_calls == []

    def test_put_labels_repo_disabled(self, client):
        """仓库未启用 → 404。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 404
        assert stub.labels_update_calls == []

    def test_put_labels_issue_missing(self, client_edit):
        """GitLab 返回 404（issue 不存在/已被删除）→ 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.labels_update_errors[(42, 64)] = GitLabError(
            "404 Not Found", status_code=404)

        resp = tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_put_labels_gitlab_server_error(self, client_edit):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.labels_update_errors[(42, 64)] = GitLabError(
            "500 Internal Server Error", status_code=500)

        resp = tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 502

    def test_put_labels_network_error(self, client_edit):
        """网络错误（httpx.HTTPError，per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.labels_update_errors[(42, 64)] = httpx.HTTPError("connect timeout")

        resp = tc.put("/api/issues/42/64/labels", json={"add": ["feature"]})

        assert resp.status_code == 502


class TestIssueMembers:
    """GET /api/issues/{project_id}/members：概览页右边栏负责人下拉数据源
    （issue #303）。

    仓库定位与标签池接口一致（project_id 匹配「已启用」仓库，不存在/
    未启用 → 404）；客户端选择与聚合一致（per-repo token 优先，回退
    全局 bot token，只读查询）。成员精简为 {id, username, name}（id
    为 GitLab 用户 id，复用 form-meta 的 _trim_member + issue #93
    user_id 补齐）；查询失败 → 502（下拉数据源不可降级为空）。
    """

    def test_members_success(self, client):
        """正常返回：成员精简为 {id, username, name}，id 取 user_id
        （GitLab 用户 id，更新负责人 assignee_ids 需要该值）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.members_by_project = {42: [
            {"id": 10, "user_id": 3, "username": "agent", "name": "agent",
             "access_level": 50},
            {"id": 11, "user_id": 7, "username": "dev", "name": "开发",
             "access_level": 30},
        ]}

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 200
        assert resp.json()["members"] == [
            {"id": 3, "username": "agent", "name": "agent"},
            {"id": 7, "username": "dev", "name": "开发"},
        ]

    def test_members_user_id_completed_by_username(self, client):
        """issue #93：members/all 返回项缺 user_id 时按 username 查
        /users 补齐真实用户 id（与添加 issue 弹窗成员处理一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.members_by_project = {42: [
            {"id": 10, "username": "agent", "name": "agent"},  # 无 user_id
            {"id": 11, "user_id": 7, "username": "dev", "name": "dev"},
        ]}
        stub.users_by_username = {"agent": 3}

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 200
        assert stub.user_id_lookups == ["agent"]
        assert resp.json()["members"] == [
            {"id": 3, "username": "agent", "name": "agent"},
            {"id": 7, "username": "dev", "name": "dev"},
        ]

    def test_members_user_id_lookup_missing_filtered(self, client):
        """用户 id 查询失败（用户已删除等）→ 成员剔除，下拉不出现
        无法分配的条目。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.members_by_project = {42: [
            {"id": 10, "username": "ghost", "name": "ghost"},
            {"id": 11, "user_id": 7, "username": "dev", "name": "dev"},
        ]}
        stub.users_by_username = {"ghost": None}

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 200
        assert stub.user_id_lookups == ["ghost"]
        assert resp.json()["members"] == [
            {"id": 7, "username": "dev", "name": "dev"},
        ]

    def test_members_abnormal_element_filtered(self, client):
        """既无 user_id 也无 username 的异常成员元素过滤。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.members_by_project = {42: [
            {"id": 10},  # 无 user_id/username
            {"id": 11, "user_id": 7, "username": "dev", "name": "dev"},
        ]}

        resp = tc.get("/api/issues/42/members")

        assert resp.json()["members"] == [
            {"id": 7, "username": "dev", "name": "dev"},
        ]

    def test_members_empty(self, client):
        """仓库无成员 → 空数组（前端下拉提示「该仓库暂无成员」）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 200
        assert resp.json()["members"] == []

    def test_members_repo_not_found(self, client):
        """仓库不存在/未添加 → 404（与标签池接口一致）。"""
        tc, stub, db, _ = client

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 404

    def test_members_repo_disabled(self, client):
        """仓库未启用 → 404（与概览聚合只聚合启用仓库一致）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 404

    def test_members_gitlab_error(self, client):
        """GitLab 成员 API 失败 → 502（下拉数据源不可降级为空）。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        stub.fail_member_projects = {42}

        resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 502

    def test_members_network_error(self, client):
        """网络错误（per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")
        from unittest.mock import patch
        with patch.object(stub, "list_project_members",
                          side_effect=httpx.HTTPError("connect timeout")):

            resp = tc.get("/api/issues/42/members")

        assert resp.status_code == 502


class TestUpdateIssueAssignee:
    """PUT /api/issues/{project_id}/{iid}/assignee：概览页右边栏负责人
    下拉修改（issue #303）。

    assignee_id 为 GitLab 用户 id（项目成员接口返回的 id），None 清除
    负责人（assignee_ids 置空数组）；编辑操作走 owner token
    （_issue_edit_call，issue #130）；成功后清空概览缓存并返回更新后
    issue 的精简负责人列表。错误映射：仓库不存在/未启用 → 404，
    GitLab 404（issue 不存在）→ 404，GitLab 其他错误与网络错误 → 502。
    """

    def test_update_success(self, client_edit):
        """正常更新：stub 收到 assignee_ids=[id]，返回更新后负责人列表。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.assignee_update_result = {
            "iid": 64, "title": "x", "state": "opened",
            "web_url": "https://gitlab.example.com/x/-/issues/64",
            "labels": [], "updated_at": None, "created_at": None,
            "description": None, "author": None, "milestone": None,
            "assignees": [
                {"name": "开发", "username": "dev",
                 "avatar_url": "https://gitlab.example.com/dev.png"},
            ],
            "user_notes_count": 0,
        }

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 200
        assert stub.assignee_update_calls == [(42, 64, [7])]
        assert resp.json()["assignees"] == [
            {"name": "开发", "username": "dev",
             "avatar_url": "https://gitlab.example.com/dev.png"},
        ]

    def test_update_clear(self, client_edit):
        """清除负责人：assignee_id=None → assignee_ids 置空数组。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": None})

        assert resp.status_code == 200
        assert stub.assignee_update_calls == [(42, 64, [])]
        assert resp.json()["assignees"] == []

    def test_update_repo_not_found(self, client_edit):
        """仓库不存在/未添加 → 404。"""
        tc, stub, db, _ = client_edit

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 404
        assert stub.assignee_update_calls == []

    def test_update_repo_disabled(self, client_edit):
        """仓库未启用 → 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 404
        assert stub.assignee_update_calls == []

    def test_update_issue_missing(self, client_edit):
        """GitLab 返回 404（issue 不存在）→ 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.assignee_update_errors[(42, 64)] = GitLabError(
            "404 Not Found", status_code=404)

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_update_gitlab_server_error(self, client_edit):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.assignee_update_errors[(42, 64)] = GitLabError(
            "500 Internal Server Error", status_code=500)

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 502

    def test_update_network_error(self, client_edit):
        """网络错误（httpx.HTTPError，per-repo host 不可达）→ 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.assignee_update_errors[(42, 64)] = httpx.HTTPError("connect timeout")

        resp = tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        assert resp.status_code == 502

    def test_update_clears_overview_cache(self, client_edit):
        """更新成功后清空概览缓存：下次 overview 重新聚合、新负责人生效。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        assert tc.get("/api/issues/overview").json()["total"] == 1
        # 10 秒 TTL 内命中缓存：改数据后仍返回旧结果（证明缓存生效）
        stub.issues_by_project = {42: []}
        assert tc.get("/api/issues/overview").json()["total"] == 1

        tc.put("/api/issues/42/64/assignee", json={"assignee_id": 7})

        # 缓存被清 → 重新聚合，读到新的（空）数据
        assert tc.get("/api/issues/overview").json()["total"] == 0


class TestIssueComments:
    """POST /api/issues/{project_id}/{iid}/comments 与
    POST /api/issues/{project_id}/{iid}/comments/{note_id}/reply：
    概览页右边栏「添加评论」与「回复评论」（issue #125）。

    添加评论复用 GitLabClient.add_comment（notes API）；回复评论由
    GitLabClient.reply_to_note 先解析目标评论所在 discussion 再追加
    note（notes API 响应不含 discussion_id）。两者共用校验：正文去
    空白后为空 → 400（GitLab 对空正文同样拒绝，提前校验）。成功后
    清空概览缓存（user_notes_count/updated_at 已变化）并返回新建
    评论的精简对象（与 detail 的 notes 条目同结构，前端本地追加）。
    错误映射：仓库不存在/未启用 → 404，GitLab 404（issue/评论不存在）
    → 404，GitLab 其他错误与网络错误 → 502。
    """

    def test_add_comment_success(self, client_edit):
        """正常添加：stub 收到正确参数、返回精简 note（时间转 UTC）。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/42/64/comments",
                       json={"body": "  新评论内容  "})

        assert resp.status_code == 201
        assert stub.add_comment_calls == [(42, 64, "新评论内容")]
        note = resp.json()["note"]
        assert note["id"] == 9001
        assert note["body"] == "新评论内容"
        assert note["system"] is False
        assert note["author"] == {
            "name": "code01", "username": "project_bot",
            "avatar_url": "https://gitlab.example.com/a.png"}
        # 时间统一转 UTC 无后缀（前端 fmtTime 解析约定）
        assert note["created_at"] == "2026-08-16 02:00:00"

    def test_add_comment_empty_body(self, client):
        """正文为空/纯空白 → 400，且不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/42/64/comments", json={"body": "   "})

        assert resp.status_code == 400
        assert "不能为空" in resp.json()["detail"]
        assert stub.add_comment_calls == []

    def test_add_comment_repo_not_found(self, client):
        """GitLab project_id 无对应启用仓库 → 404，不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/999/64/comments",
                       json={"body": "hi"})

        assert resp.status_code == 404
        assert "仓库" in resp.json()["detail"]
        assert stub.add_comment_calls == []

    def test_add_comment_repo_disabled(self, client):
        """仓库未启用 → 404。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo", enabled=False)

        resp = tc.post("/api/issues/42/64/comments", json={"body": "hi"})

        assert resp.status_code == 404
        assert stub.add_comment_calls == []

    def test_add_comment_issue_missing(self, client_edit):
        """GitLab 返回 404（issue 不存在）→ 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.add_comment_errors[(42, 64)] = GitLabError(
            "404 Not Found", status_code=404)

        resp = tc.post("/api/issues/42/64/comments", json={"body": "hi"})

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_add_comment_gitlab_server_error(self, client_edit):
        """GitLab 上游 5xx → 502，不假装成功。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.add_comment_errors[(42, 64)] = GitLabError(
            "500 Internal Server Error", status_code=500)

        resp = tc.post("/api/issues/42/64/comments", json={"body": "hi"})

        assert resp.status_code == 502

    def test_add_comment_network_error(self, client_edit):
        """网络错误（httpx.HTTPError）→ 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.add_comment_errors[(42, 64)] = httpx.HTTPError("connect timeout")

        resp = tc.post("/api/issues/42/64/comments", json={"body": "hi"})

        assert resp.status_code == 502

    def test_add_comment_clears_overview_cache(self, client_edit):
        """添加评论成功后清空概览缓存（user_notes_count 已变化）。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.issues_by_project = {42: [make_issue(1, "x")]}
        assert tc.get("/api/issues/overview").json()["total"] == 1
        stub.issues_by_project = {42: []}

        tc.post("/api/issues/42/64/comments", json={"body": "hi"})

        assert tc.get("/api/issues/overview").json()["total"] == 0

    def test_reply_success(self, client_edit):
        """正常回复：stub 收到 (project_id, iid, note_id, body)。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/42/64/comments/201/reply",
                       json={"body": "回复内容"})

        assert resp.status_code == 201
        assert stub.reply_calls == [(42, 64, 201, "回复内容")]
        note = resp.json()["note"]
        assert note["id"] == 9002
        assert note["body"] == "回复内容"
        assert note["created_at"] == "2026-08-16 03:00:00"

    def test_reply_empty_body(self, client):
        """回复正文为空/纯空白 → 400，不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/42/64/comments/201/reply",
                       json={"body": ""})

        assert resp.status_code == 400
        assert stub.reply_calls == []

    def test_reply_repo_not_found(self, client):
        """仓库不存在 → 404，不触碰 GitLab。"""
        tc, stub, db, _ = client
        _add_repo(db, project_id=42, name="demo")

        resp = tc.post("/api/issues/999/64/comments/201/reply",
                       json={"body": "hi"})

        assert resp.status_code == 404
        assert stub.reply_calls == []

    def test_reply_note_missing(self, client_edit):
        """被回复评论不存在（GitLab 404）→ 404。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.reply_errors[(42, 64)] = GitLabError(
            "评论 201 不存在", status_code=404)

        resp = tc.post("/api/issues/42/64/comments/201/reply",
                       json={"body": "hi"})

        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_reply_gitlab_server_error(self, client_edit):
        """GitLab 上游 5xx → 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.reply_errors[(42, 64)] = GitLabError(
            "500 Internal Server Error", status_code=500)

        resp = tc.post("/api/issues/42/64/comments/201/reply",
                       json={"body": "hi"})

        assert resp.status_code == 502

    def test_reply_network_error(self, client_edit):
        """网络错误（httpx.HTTPError）→ 502。"""
        tc, stub, db, _ = client_edit
        _add_repo(db, project_id=42, name="demo")
        stub.reply_errors[(42, 64)] = httpx.HTTPError("connect timeout")

        resp = tc.post("/api/issues/42/64/comments/201/reply",
                       json={"body": "hi"})

        assert resp.status_code == 502


# ---- issue #130：概览页 issue 编辑操作优先 owner token ----

class TestIssueEditOwnerToken:
    """概览页 issue 编辑操作（关闭/编辑标签/添加评论/回复评论/添加
    issue）必须使用 owner gitlab token（issue #130 + issue #132）。

    owner token 只允许在概览页面上编辑 issue、添加 issue、关闭 issue、
    在 issue 添加评论以及回复 issue 评论的时候使用，其他场景都不得
    使用；agent 无论如何都不能使用 owner token。

    issue #132 修正：未配置 owner token 或 owner 401/403 时**不得**静默
    回退 bot token——否则用户经概览页发的评论/回复会以 code01（bot）
    身份发布（实测复现）。必须返回明确错误提示先配置/更新 owner token。
    """

    @staticmethod
    def _enable_owner(ctx, token="owner-token-1"):
        ctx.config.update_gitlab({"owner_token": token})

    def test_close_prefers_owner_token(self, api_app, monkeypatch):
        """关闭 issue：配置 owner token 时优先使用 owner client。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        seen = []
        from botler.api import issues as issues_mod

        def fake_client(url, token, verify_ssl=True, webhook_base_url=None):
            seen.append(token)
            return stub

        monkeypatch.setattr(issues_mod, "GitLabClient", fake_client)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/close")
        assert resp.status_code == 200, resp.text
        assert seen == ["owner-token-1"], f"关闭 issue 应优先 owner token（实际 {seen}）"
        assert stub.close_calls == [(42, 64)]

    def test_close_without_owner_blocked(self, client):
        """issue #132：未配置 owner token 时关闭 issue 必须报错提示配置，
        不得静默回退 bot token（否则操作以 code01 身份发布）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="demo")
        resp = tc.post("/api/issues/42/64/close")
        assert resp.status_code == 400, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.close_calls == [], "未配置 owner token 时不得回退 bot token"

    def test_close_owner_401_blocked(self, api_app, monkeypatch):
        """issue #132：owner token 失效（401）同样报错，不回退 bot token。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        from botler.api import issues as issues_mod

        class OwnerStub:
            def __init__(self, url, token, verify_ssl=True, webhook_base_url=None):
                self.token = token
            def close_issue(self, project_id, iid):
                raise GitLabError("owner 401", 401)

        monkeypatch.setattr(issues_mod, "GitLabClient", OwnerStub)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/close")
        assert resp.status_code == 502, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.close_calls == [], "owner 401 后不得回退 bot token"

    def test_labels_prefers_owner_token(self, api_app, monkeypatch):
        """编辑 issue 标签：配置 owner token 时优先 owner client。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        seen = []
        from botler.api import issues as issues_mod

        def fake_client(url, token, verify_ssl=True, webhook_base_url=None):
            seen.append(token)
            return stub

        monkeypatch.setattr(issues_mod, "GitLabClient", fake_client)
        tc = TestClient(app)
        resp = tc.put("/api/issues/42/64/labels",
                      json={"add": ["bot-done"], "remove": []})
        assert resp.status_code == 200, resp.text
        assert seen == ["owner-token-1"]
        assert stub.labels_update_calls == [(42, 64, ["bot-done"], None)]

    def test_comment_prefers_owner_token(self, api_app, monkeypatch):
        """添加 issue 评论：配置 owner token 时优先 owner client。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        seen = []
        from botler.api import issues as issues_mod

        def fake_client(url, token, verify_ssl=True, webhook_base_url=None):
            seen.append(token)
            return stub

        monkeypatch.setattr(issues_mod, "GitLabClient", fake_client)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/comments", json={"body": "测试评论"})
        assert resp.status_code == 201, resp.text
        assert seen == ["owner-token-1"]
        assert stub.add_comment_calls == [(42, 64, "测试评论")]

    def test_reply_prefers_owner_token(self, api_app, monkeypatch):
        """回复 issue 评论：配置 owner token 时优先 owner client。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        seen = []
        from botler.api import issues as issues_mod

        def fake_client(url, token, verify_ssl=True, webhook_base_url=None):
            seen.append(token)
            return stub

        monkeypatch.setattr(issues_mod, "GitLabClient", fake_client)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/comments/55/reply",
                       json={"body": "回复内容"})
        assert resp.status_code == 201, resp.text
        assert seen == ["owner-token-1"]
        assert stub.reply_calls == [(42, 64, 55, "回复内容")]

    def test_create_issue_prefers_owner_token(self, api_app, monkeypatch):
        """添加 issue：配置 owner token 时优先 owner client。"""
        app, stub, db, tmp_path = api_app
        repo_id = _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        seen = []
        from botler.api import issues as issues_mod

        def fake_client(url, token, verify_ssl=True, webhook_base_url=None):
            seen.append(token)
            return stub

        monkeypatch.setattr(issues_mod, "GitLabClient", fake_client)
        tc = TestClient(app)
        resp = tc.post("/api/issues", json={
            "repo_id": repo_id, "title": "新 issue",
            "assignee_id": 7, "labels": ["feature"],
        })
        assert resp.status_code == 201, resp.text
        assert seen == ["owner-token-1"]
        assert stub.create_calls[0][0] == 42
        assert stub.create_calls[0][1]["title"] == "新 issue"

    def test_labels_without_owner_blocked(self, client):
        """issue #132：未配置 owner token 时编辑标签必须报错。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="demo")
        resp = tc.put("/api/issues/42/64/labels",
                      json={"add": ["bot-done"], "remove": []})
        assert resp.status_code == 400, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.labels_update_calls == []

    def test_comment_without_owner_blocked(self, client):
        """issue #132：未配置 owner token 时添加评论必须报错——用户经
        概览页的最新回复正是因此以 code01 身份发布（复现用例）。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="demo")
        resp = tc.post("/api/issues/42/64/comments", json={"body": "测试评论"})
        assert resp.status_code == 400, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.add_comment_calls == []

    def test_reply_without_owner_blocked(self, client):
        """issue #132：未配置 owner token 时回复评论必须报错。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="demo")
        resp = tc.post("/api/issues/42/64/comments/55/reply",
                       json={"body": "回复内容"})
        assert resp.status_code == 400, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.reply_calls == []

    def test_create_issue_without_owner_blocked(self, client):
        """issue #132：未配置 owner token 时添加 issue 必须报错。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db, project_id=42, name="demo")
        resp = tc.post("/api/issues", json={
            "repo_id": repo_id, "title": "新 issue",
            "assignee_id": 7, "labels": ["feature"],
        })
        assert resp.status_code == 400, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.create_calls == []

    def test_comment_owner_401_blocked(self, api_app, monkeypatch):
        """issue #132：owner token 失效（401）时评论同样报错，不回退 bot。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        from botler.api import issues as issues_mod

        class OwnerStub:
            def __init__(self, url, token, verify_ssl=True, webhook_base_url=None):
                self.token = token
            def add_comment(self, project_id, iid, body):
                raise GitLabError("owner 401", 401)

        monkeypatch.setattr(issues_mod, "GitLabClient", OwnerStub)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/comments", json={"body": "测试评论"})
        assert resp.status_code == 502, resp.text
        assert "owner token" in resp.json()["detail"]
        assert stub.add_comment_calls == []

    def test_comment_owner_403_insufficient_scope_hint(self, api_app, monkeypatch):
        """issue #133：owner 403 且 GitLab 明确返回 insufficient_scope（token
        缺 api scope，实测响应体）时，错误信息直接指明根因——而不是笼统的
        「请在设置页更新 Owner GitLab Token」让用户反复重试（issue #133
        用户配置只读 scope 的 token 后概览页评论/回复持续 403 的根因）。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        from botler.api import issues as issues_mod

        class OwnerStub:
            def __init__(self, url, token, verify_ssl=True,
                         webhook_base_url=None):
                self.token = token

            def add_comment(self, project_id, iid, body):
                raise GitLabError(
                    '权限不足（403）: {"error":"insufficient_scope",'
                    '"error_description":"The request requires higher '
                    'privileges than provided by the access token.",'
                    '"scope":"ai_workflows api read_api"}', 403)

        monkeypatch.setattr(issues_mod, "GitLabClient", OwnerStub)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/comments", json={"body": "测试评论"})
        assert resp.status_code == 502, resp.text
        assert "api scope" in resp.json()["detail"]
        assert stub.add_comment_calls == []

    def test_comment_owner_403_generic_keeps_hint(self, api_app, monkeypatch):
        """issue #133：owner 403 无 insufficient_scope 特征时保留原有
        通用提示（不影响既有文案的语义）。"""
        app, stub, db, tmp_path = api_app
        _add_repo(db, project_id=42, name="demo")
        self._enable_owner(app.state.ctx)
        from botler.api import issues as issues_mod

        class OwnerStub:
            def __init__(self, url, token, verify_ssl=True,
                         webhook_base_url=None):
                self.token = token

            def add_comment(self, project_id, iid, body):
                raise GitLabError("权限不足（403）: 403 Forbidden", 403)

        monkeypatch.setattr(issues_mod, "GitLabClient", OwnerStub)
        tc = TestClient(app)
        resp = tc.post("/api/issues/42/64/comments", json={"body": "测试评论"})
        assert resp.status_code == 502, resp.text
        assert "owner token" in resp.json()["detail"]
        assert "api scope" not in resp.json()["detail"], \
            "通用 403 不应臆测为缺 api scope"


# ---- issue #180：概览页「Issue 完成耗时」统计 ----

class TestCompletionStats:
    """GET /api/issues/completion-stats：平均完成耗时 + 逐日走势。

    数据源为本地 tasks 表成功终态（succeeded）任务，完成耗时 =
    finished_at - created_at（与任务详情「处理用时」issue #49 语义
    一致：系统接收时间 → bot-done 打标时间）。
    """

    @staticmethod
    def _mk_succeeded(db, repo_id, issue_iid, created_at, finished_at,
                      status="succeeded"):
        """创建任务并写入受控的 created_at / finished_at（UTC 无后缀串）。"""
        task_id = db.create_task(repo_id, 42, issue_iid, f"issue #{issue_iid}",
                                 triggered_by="webhook")
        db.set_task_status(task_id, status, finished_at=finished_at)
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                         (created_at, task_id))
        return task_id

    def test_empty(self, client):
        """无任何任务 → completed_count=0、avg_seconds=None、trend=[]。"""
        tc, stub, db, tmp_path = client
        resp = tc.get("/api/issues/completion-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"completed_count": 0, "avg_seconds": None, "trend": [], "repos": []}

    def test_only_succeeded_counted(self, client):
        """只有 succeeded 任务计入统计，其他终态（failed/interrupted/
        queued/running）一律排除。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        self._mk_succeeded(db, repo_id, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")  # 3600 秒
        self._mk_succeeded(db, repo_id, 2, "2026-08-12 02:00:00",
                           "2026-08-12 02:30:00", status="failed")
        self._mk_succeeded(db, repo_id, 3, "2026-08-12 02:00:00",
                           "2026-08-12 02:30:00", status="interrupted")
        self._mk_succeeded(db, repo_id, 4, "2026-08-12 02:00:00",
                           "2026-08-12 02:30:00", status="queued")
        body = tc.get("/api/issues/completion-stats").json()
        assert body["completed_count"] == 1
        assert body["avg_seconds"] == 3600.0
        assert body["trend"] == [{"date": "2026-08-12", "count": 1,
                                  "avg_seconds": 3600.0}]

    def test_overall_average_and_daily_trend(self, client):
        """多条 succeeded：overall 平均 = 全部用时均值；trend 按完成日
        分组求日平均，按日期升序。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        # 08-12 完成两条：3600s + 1800s → 日平均 2700s
        self._mk_succeeded(db, repo_id, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")
        self._mk_succeeded(db, repo_id, 2, "2026-08-12 10:00:00",
                           "2026-08-12 10:30:00")
        # 08-13 完成一条：7200s
        self._mk_succeeded(db, repo_id, 3, "2026-08-13 02:00:00",
                           "2026-08-13 04:00:00")
        body = tc.get("/api/issues/completion-stats").json()
        assert body["completed_count"] == 3
        # 总体平均 = (3600 + 1800 + 7200) / 3 = 4200
        assert body["avg_seconds"] == 4200.0
        assert body["trend"] == [
            {"date": "2026-08-12", "count": 2, "avg_seconds": 2700.0},
            {"date": "2026-08-13", "count": 1, "avg_seconds": 7200.0},
        ]

    def test_invalid_and_negative_durations_skipped(self, client):
        """缺时间字段 / 解析失败 / 用时为负（时钟异常）的行不计入统计，
        有效行仍正常聚合。"""
        tc, stub, db, tmp_path = client
        repo_id = _add_repo(db)
        # 有效行：120 秒
        self._mk_succeeded(db, repo_id, 1, "2026-08-12 02:00:00",
                           "2026-08-12 02:02:00")
        # finished_at 为空 → 跳过
        t2 = db.create_task(repo_id, 42, 2, "issue #2", triggered_by="webhook")
        db.set_task_status(t2, "succeeded", finished_at="")
        # 用时为负（finished_at 早于 created_at）→ 跳过
        self._mk_succeeded(db, repo_id, 3, "2026-08-12 10:00:00",
                           "2026-08-12 09:00:00")
        # created_at 非法格式 → 跳过
        t4 = db.create_task(repo_id, 42, 4, "issue #4", triggered_by="webhook")
        db.set_task_status(t4, "succeeded", finished_at="2026-08-12 03:00:00")
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at='not-a-time' WHERE id=?",
                         (t4,))
        body = tc.get("/api/issues/completion-stats").json()
        assert body["completed_count"] == 1
        assert body["avg_seconds"] == 120.0
        assert body["trend"] == [{"date": "2026-08-12", "count": 1,
                                  "avg_seconds": 120.0}]

    def test_repos_breakdown_per_repo(self, client):
        """repos：每个已启用仓库独立聚合 completed_count/avg_seconds/trend，
        全局统计不受影响。"""
        tc, stub, db, tmp_path = client
        repo_a = _add_repo(db, project_id=42, name="alpha", priority=10)
        repo_b = _add_repo(db, project_id=43, name="beta", priority=20)
        # alpha：08-12 完成两条 3600s + 1800s → 日平均 2700s
        self._mk_succeeded(db, repo_a, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")
        self._mk_succeeded(db, repo_a, 2, "2026-08-12 10:00:00",
                           "2026-08-12 10:30:00")
        # beta：08-13 完成一条 7200s
        self._mk_succeeded(db, repo_b, 3, "2026-08-13 02:00:00",
                           "2026-08-13 04:00:00")
        body = tc.get("/api/issues/completion-stats").json()
        # 全局：(3600 + 1800 + 7200) / 3 = 4200
        assert body["completed_count"] == 3
        assert body["avg_seconds"] == 4200.0
        assert [r["repo_name"] for r in body["repos"]] == ["alpha", "beta"]
        assert body["repos"][0] == {
            "repo_id": repo_a, "repo_name": "alpha", "completed_count": 2,
            "avg_seconds": 2700.0,
            "trend": [{"date": "2026-08-12", "count": 2,
                       "avg_seconds": 2700.0}]}
        assert body["repos"][1] == {
            "repo_id": repo_b, "repo_name": "beta", "completed_count": 1,
            "avg_seconds": 7200.0,
            "trend": [{"date": "2026-08-13", "count": 1,
                       "avg_seconds": 7200.0}]}

    def test_repos_sorted_by_priority(self, client):
        """repos 排序与 overview 一致：仓库按配置优先级升序（数字小先），
        与入库顺序无关。"""
        tc, stub, db, tmp_path = client
        # 先插 beta（priority 20）再插 alpha（priority 10）
        _add_repo(db, project_id=43, name="beta", priority=20)
        repo_a = _add_repo(db, project_id=42, name="alpha", priority=10)
        self._mk_succeeded(db, repo_a, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")
        body = tc.get("/api/issues/completion-stats").json()
        assert [r["repo_name"] for r in body["repos"]] == ["alpha", "beta"]

    def test_repos_excludes_disabled(self, client):
        """已禁用仓库不出现在 repos 列表；其历史成功任务仍计入全局统计。"""
        tc, stub, db, tmp_path = client
        repo_on = _add_repo(db, project_id=42, name="on")
        repo_off = _add_repo(db, project_id=43, name="off", enabled=False)
        self._mk_succeeded(db, repo_on, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")
        self._mk_succeeded(db, repo_off, 2, "2026-08-12 02:00:00",
                           "2026-08-12 02:30:00")
        body = tc.get("/api/issues/completion-stats").json()
        assert body["completed_count"] == 2, "全局统计应包含禁用仓库历史任务"
        assert [r["repo_name"] for r in body["repos"]] == ["on"]

    def test_repos_enabled_repo_without_tasks(self, client):
        """已启用但无已完成任务的仓库：completed_count=0、avg_seconds=None、
        trend=[]（前端渲染「暂无数据」）。"""
        tc, stub, db, tmp_path = client
        repo_a = _add_repo(db, project_id=42, name="alpha", priority=10)
        repo_b = _add_repo(db, project_id=43, name="beta", priority=20)
        self._mk_succeeded(db, repo_a, 1, "2026-08-12 02:00:00",
                           "2026-08-12 03:00:00")
        body = tc.get("/api/issues/completion-stats").json()
        assert body["repos"][1] == {
            "repo_id": repo_b, "repo_name": "beta", "completed_count": 0,
            "avg_seconds": None, "trend": []}
