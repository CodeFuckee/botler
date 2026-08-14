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
        # (project_id, kwargs)：记录每次 list_open_issues 调用参数
        self.calls: list[tuple[int, dict]] = []

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


def make_issue(iid: int, title: str,
               updated_at: str = "2026-08-14T10:00:00.000+08:00") -> dict:
    return {
        "iid": iid, "title": title, "state": "opened",
        "updated_at": updated_at,
        "web_url": f"https://gitlab.example.com/group/proj/-/issues/{iid}",
    }


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
        """issue 精简字段透传：iid/title/updated_at/web_url（丢弃冗余字段）；
        updated_at 转 UTC 无后缀（前端 fmtAgo 解析约定，与流水线
        commit_time 一致）。"""
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
            "updated_at": "2026-08-14 02:00:00",
            "web_url": "https://gitlab.example.com/group/proj/-/issues/7",
        }

    def test_repo_entry_carries_priority(self, client):
        """每条仓库结果带 priority 字段供前端展示优先级徽章。"""
        tc, stub, db, tmp_path = client
        _add_repo(db, project_id=42, name="p", priority=5)

        resp = tc.get("/api/issues/overview")

        data = resp.json()
        assert data["repos"][0]["priority"] == 5


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
