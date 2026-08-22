"""概览页 issue 接口性能回归测试（issue #452：增加前端页面和后端接口性能测试）。

覆盖两个用户报告慢的场景：
1. 添加 issue 对话框（GET /api/issues/form-meta/{repo_id}）——加载成员与标签
   有时需要十几秒；
2. issue 详情右边栏（GET /api/issues/{project_id}/{iid}/detail）——加载
   issue 内容、评论与活动有时需要十几秒。

复现手段：给 StubGitLab 注入可配置的每次调用延迟（sleep），模拟 GitLab 上游
慢响应（网络波动/限流重试等场景），断言接口总耗时在预算内。修复前：
- form-meta 对每个成员串行调用 get_user_id_by_username（N+1 查询），
  N 个成员 = N+2 次串行 GitLab API 调用；
- detail 的 notes 与 label_events 串行调用（2 次串行）；
上游每次 0.5s 时累计 2.5s（3 成员）/ 1.0s，预算断言失败；
修复后成员/标签、notes/label_events 并发拉取且成员 id 直接取顶层 id
（无 N+1），总耗时 ≈ 单次最慢调用，断言通过。
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database



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
    def enqueue(self, task_id: int) -> bool:
        return True


class StubExecutor:
    def clear_stop_request(self, task_id: int) -> None:
        pass


class StubGitLab:
    """慢 GitLab 桩：每次调用注入 delay 秒延迟，记录成员/用户查询次数。

    与 test_api_issues.py 的 StubGitLab 同构，仅增加 delay 注入，用于
    性能预算断言（成员/标签/notes/label_events 的串行 vs 并发差异）。
    """

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.members_by_project: dict[int, list[dict]] = {}
        self.labels_by_project: dict[int, list[dict]] = {}
        self.notes_by_issue: dict[tuple[int, int], list[dict]] = {}
        self.label_events_by_issue: dict[tuple[int, int], list[dict]] = {}
        self.user_id_lookups: list[str] = []
        self.users_by_username: dict[str, int | None] = {}
        self.member_calls: list[int] = []
        self.label_calls: list[int] = []
        self.notes_calls: list[tuple[int, int]] = []
        self.label_event_calls: list[tuple[int, int]] = []

    def _wait(self):
        if self.delay > 0:
            time.sleep(self.delay)

    def list_project_members(self, project_id):
        self._wait()
        self.member_calls.append(project_id)
        return list(self.members_by_project.get(project_id, []))

    def get_user_id_by_username(self, username):
        self._wait()
        self.user_id_lookups.append(username)
        return self.users_by_username.get(username)

    def list_project_labels(self, project_id):
        self._wait()
        self.label_calls.append(project_id)
        return list(self.labels_by_project.get(project_id, []))

    def list_issue_notes(self, project_id, iid, limit=None):
        self._wait()
        self.notes_calls.append((project_id, iid))
        return list(self.notes_by_issue.get((project_id, iid), []))

    def list_issue_label_events(self, project_id, iid, limit=None):
        self._wait()
        self.label_event_calls.append((project_id, iid))
        return list(self.label_events_by_issue.get((project_id, iid), []))

    def find_latest_task(self, project_id, iid):
        return None


@pytest.fixture
def perf_app(tmp_path):
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
    from botler.api import pipelines as pipelines_mod
    pipelines_mod.clear_pipeline_cache()
    from botler.api import issues as issues_mod
    issues_mod.clear_issue_cache()
    return app, stub, db


def _add_repo(db, project_id=42, name="demo", enabled=True) -> int:
    return db.upsert_repo(
        project_id=project_id, name=name,
        url=f"https://gitlab.example.com/{name}.git", enabled=enabled,
        priority=100)


# 3 个成员、user_id 字段缺失（GitLab 19 实测 members/all 仅返回顶层 id
# 与 username，issue #93）——正是 N+1 查询的触发场景
def _three_members_no_user_id() -> list[dict]:
    return [
        {"id": 1, "username": "chenkaidi", "name": "Chenkaidi",
         "access_level": 50},
        {"id": 3, "username": "agent", "name": "Agent",
         "access_level": 40},
        {"id": 11, "username": "project_123_bot_cb5fcbc6b6d6c4c7d8efd11210a3d10f",
         "name": "code01", "access_level": 40},
    ]


class TestFormMetaPerformance:
    """GET /api/issues/form-meta/{repo_id}：添加 issue 对话框加载性能。"""

    def test_form_meta_no_n1_user_id_lookup(self, perf_app):
        """修复验证：成员 id 直接取 members/all 顶层 id（实测即用户 id），
        不再对每个成员串行调用 /users?username= 补齐（N+1 消除）。"""
        app, stub, db = perf_app
        _add_repo(db, project_id=42, name="a")
        stub.members_by_project = {42: _three_members_no_user_id()}
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"}]}

        resp = TestClient(app).get("/api/issues/form-meta/1")

        assert resp.status_code == 200
        data = resp.json()
        # 3 个成员 id 与 members/all 顶层 id 一致（用户 id）
        assert [m["id"] for m in data["members"]] == [1, 3, 11]
        # 不触发任何按 username 补查（修复前这里会记录 3 次查询）
        assert stub.user_id_lookups == []
        assert stub.member_calls == [42]
        assert stub.label_calls == [42]

    def test_form_meta_slow_gitlab_within_budget(self, perf_app):
        """性能预算：上游每次调用 0.5s 时，接口总耗时须 < 1.5s。
        修复前：members + 3×user_id + labels = 5 次串行 = 2.5s（失败）；
        修复后：members/labels 并发 + 无 N+1 ≈ 0.5s（通过）。"""
        app, stub, db = perf_app
        _add_repo(db, project_id=42, name="a")
        stub.delay = 0.5
        stub.members_by_project = {42: _three_members_no_user_id()}
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"},
            {"name": "ui", "color": "69D100", "text_color": "FFFFFF"},
        ]}

        start = time.monotonic()
        resp = TestClient(app).get("/api/issues/form-meta/1")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 1.5, (
            f"form-meta 慢上游耗时 {elapsed:.2f}s 超过预算 1.5s"
            f"（成员 {stub.member_calls}、标签 {stub.label_calls}、"
            f"user_id 补查 {stub.user_id_lookups}）")

    def test_form_meta_members_labels_concurrent(self, perf_app):
        """并发验证：members 与 labels 应并发拉取——labels 单次 0.5s 延迟
        下，总耗时 ≈ 0.5s 而非 members+labels 串行 1.0s。"""
        app, stub, db = perf_app
        _add_repo(db, project_id=42, name="a")
        stub.delay = 0.5
        stub.members_by_project = {42: [
            {"id": 1, "user_id": 1, "username": "chenkaidi", "name": "C"}]}
        stub.labels_by_project = {42: [
            {"name": "bug", "color": "FF0000", "text_color": "FFFFFF"}]}

        start = time.monotonic()
        resp = TestClient(app).get("/api/issues/form-meta/1")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.9, (
            f"form-meta 成员/标签未并发：耗时 {elapsed:.2f}s"
            f"（member_calls={stub.member_calls}, label_calls={stub.label_calls}）")


class TestIssueDetailPerformance:
    """GET /api/issues/{project_id}/{iid}/detail：详情右边栏加载性能。"""

    def test_detail_notes_label_events_concurrent(self, perf_app):
        """并发验证：notes 与 label_events 应并发拉取——各 0.5s 延迟下
        总耗时 ≈ 0.5s 而非串行 1.0s（修复前串行会超预算）。"""
        app, stub, db = perf_app
        _add_repo(db, project_id=42, name="demo")
        stub.delay = 0.5
        stub.notes_by_issue = {(42, 64): [
            {"id": 1, "body": "评论", "system": False, "author": None,
             "created_at": "2026-08-15T10:00:00.000+08:00"}]}
        stub.label_events_by_issue = {(42, 64): [
            {"id": 1, "action": "add", "label": {"name": "bug"},
             "user": None, "created_at": "2026-08-15T10:00:00.000+08:00"}]}

        start = time.monotonic()
        resp = TestClient(app).get("/api/issues/42/64/detail")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.9, (
            f"detail notes/label_events 未并发：耗时 {elapsed:.2f}s"
            f"（notes_calls={stub.notes_calls}, "
            f"label_event_calls={stub.label_event_calls}）")
        assert len(resp.json()["notes"]) == 1
        assert len(resp.json()["label_events"]) == 1

    def test_detail_slow_gitlab_within_budget(self, perf_app):
        """性能预算：notes 与 label_events 各 0.5s，并发后总耗时 < 1.2s
        （修复前串行 1.0s+ 会超预算，为余量取 1.2s 判并发）。"""
        app, stub, db = perf_app
        _add_repo(db, project_id=42, name="demo")
        stub.delay = 0.5
        stub.notes_by_issue = {(42, 64): [
            {"id": 1, "body": "评论", "system": False, "author": None,
             "created_at": "2026-08-15T10:00:00.000+08:00"}]}
        stub.label_events_by_issue = {(42, 64): []}

        start = time.monotonic()
        resp = TestClient(app).get("/api/issues/42/64/detail")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 1.2, (
            f"detail 慢上游耗时 {elapsed:.2f}s 超过预算 1.2s")
