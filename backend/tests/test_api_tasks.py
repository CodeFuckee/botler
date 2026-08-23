"""任务 API 测试：列表（过滤/搜索/分页）、详情、统计与失败原因数据契约。

任务列表「失败原因显示」功能依赖 GET /api/tasks 返回的 error_message / error_detail
字段（error_detail 为每次尝试失败详情的结构化对象，供「查看详细原因」按钮使用），
本文件验证该数据契约及其余列表行为。
"""

import json
from datetime import datetime, timedelta, timezone
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


@pytest.fixture
def api_app(tmp_path):
    """最小测试 app：只挂 api 路由，ctx 用临时 config + db（无 gitlab 依赖）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db, gitlab=None, config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db, tmp_path


@pytest.fixture
def client(api_app):
    app, db, tmp_path = api_app
    return TestClient(app), db


def _mk_repo(db, project_id: int = 42, name: str = "demo") -> int:
    """插入一条仓库记录，返回 repo_id。"""
    db.upsert_repo(project_id, name, f"https://gitlab.example.com/group/{name}.git")
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1, title: str = "修复登录问题",
             status: str = "succeeded", error_message: str | None = None,
             error_detail: str | None = None, commit_sha: str | None = None,
             failure_category: str | None = None,
             finished_at: str | None = None) -> int:
    """创建任务并按需更新状态，返回 task_id。

    finished_at（issue #257 水位测试用）：UTC 无时区串，透传给
    set_task_status（tasks.finished_at 白名单字段）。
    """
    task_id = db.create_task(repo_id, 42, issue_iid, title, triggered_by="webhook")
    db.set_task_status(task_id, status, error_message=error_message,
                       error_detail=error_detail, commit_sha=commit_sha,
                       failure_category=failure_category,
                       finished_at=finished_at)
    return task_id


class TestListTasks:
    """GET /api/tasks 列表：字段契约、过滤、搜索、分页。"""

    def test_empty_list(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tasks"] == []
        assert body["total"] == 0
        assert body["stats"] == {}

    def test_list_returns_error_message_field(self, client):
        """列表项必须携带 error_message 字段（失败原因显示的数据契约）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="会失败的任务",
                 status="failed", error_message="重试耗尽（2 次）后仍失败，最后退出码 -1")
        _mk_task(db, repo_id, issue_iid=2, title="成功的任务", status="succeeded")

        body = app_client.get("/api/tasks").json()
        assert body["total"] == 2
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["error_message"] == "重试耗尽（2 次）后仍失败，最后退出码 -1"
        assert by_iid[1]["status"] == "failed"
        # 成功任务无失败原因
        assert by_iid[2]["error_message"] is None
        # 列表项应包含前端所需的全部字段
        for key in ("id", "repo_id", "repo_name", "issue_iid", "issue_title",
                    "status", "attempt_count", "triggered_by", "error_message"):
            assert key in by_iid[1], f"列表项缺少字段 {key}"

    def test_list_returns_resumed_flag(self, client):
        """列表项 resumed 字段：有 claude_session_id（会话恢复过）为 true，否则 false。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="恢复过的任务")
        db.set_task_status(_mk_task(db, repo_id, issue_iid=2, title="全新任务"),
                           "running", claude_session_id="sid-abc")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["resumed"] is False
        assert by_iid[2]["resumed"] is True

    def test_list_returns_dsh_session_id_and_resumed(self, client):
        """issue #281：dsh 会话 id 任务开始即落库，API 需返回该字段供任务
        详情页展示；dsh 恢复过会话的任务 resumed 标记应为 true。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="dsh 普通任务")
        _mk_task(db, repo_id, issue_iid=2, title="dsh 恢复任务")
        db.set_task_status(_mk_task(db, repo_id, issue_iid=3, title="claude 普通任务"),
                           "running", dsh_session_id="botler-7-20260818090101-abcd1234")
        db.set_task_status(_mk_task(db, repo_id, issue_iid=4, title="dsh 已落库"),
                           "running", dsh_session_id="botler-8-20260818090102-ef567890")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        # 未落库 dsh 会话 id → 返回 None，resumed=false
        assert by_iid[1]["dsh_session_id"] is None
        assert by_iid[1]["resumed"] is False
        # 任务 3 有 dsh_session_id（issue #281 前置落库）→ 返回原值，resumed=true
        assert by_iid[3]["dsh_session_id"] == "botler-7-20260818090101-abcd1234"
        assert by_iid[3]["resumed"] is True
        # 任务 4 同样返回原值（详情页展示供人工排查）
        assert by_iid[4]["dsh_session_id"] == "botler-8-20260818090102-ef567890"
        # 列表项字段契约（前端依赖 dsh_session_id）
        for key in ("dsh_session_id", "resumed"):
            assert key in by_iid[1], f"列表项缺少字段 {key}"

    def test_detail_returns_dsh_session_id(self, client):
        """issue #281：任务详情接口同样返回 dsh_session_id。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        tid = _mk_task(db, repo_id, issue_iid=1, title="dsh 任务")
        db.set_task_status(tid, "running", dsh_session_id="botler-9-20260818090103-11223344")
        body = app_client.get(f"/api/tasks/{tid}").json()
        assert body["dsh_session_id"] == "botler-9-20260818090103-11223344"
        assert body["resumed"] is True

    def test_list_returns_error_detail_object(self, client):
        """列表项 error_detail 应解析为结构化对象（「查看详细原因」按钮的数据契约）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        detail_json = json.dumps(
            {"summary": "重试耗尽后仍失败，最后退出码 1",
             "attempts": [{"attempt": 1, "exit_code": 1, "error": "构建超时"},
                          {"attempt": 2, "exit_code": 1, "error": "Traceback: boom"}]})
        _mk_task(db, repo_id, issue_iid=1, title="失败任务",
                 status="failed", error_message="重试耗尽（2 次）后仍失败，最后退出码 1",
                 error_detail=detail_json)
        _mk_task(db, repo_id, issue_iid=2, title="无详情的失败",
                 status="failed", error_message="平台重启导致中断")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["error_detail"]["summary"] == "重试耗尽后仍失败，最后退出码 1"
        assert by_iid[1]["error_detail"]["attempts"][1]["error"] == "Traceback: boom"
        assert by_iid[2]["error_detail"] is None

    def test_invalid_error_detail_returns_none(self, client):
        """error_detail 存了非法 JSON 时 API 返回 None（不 500）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="脏数据任务",
                 status="failed", error_message="原因", error_detail="not-json{{")
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["error_detail"] is None

    def test_status_filter(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, status="failed", error_message="原因 A")
        _mk_task(db, repo_id, issue_iid=2, status="interrupted", error_message="平台重启导致中断")
        _mk_task(db, repo_id, issue_iid=3, status="succeeded")

        failed = app_client.get("/api/tasks", params={"status": "failed"}).json()
        assert [t["issue_iid"] for t in failed["tasks"]] == [1]
        assert failed["total"] == 1

        interrupted = app_client.get("/api/tasks", params={"status": "interrupted"}).json()
        assert [t["issue_iid"] for t in interrupted["tasks"]] == [2]
        assert interrupted["tasks"][0]["error_message"] == "平台重启导致中断"

    def test_invalid_status_returns_400(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks", params={"status": "bogus"})
        assert resp.status_code == 400

    def test_search_by_title_and_iid(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=7, title="数据库连接失败排查")
        _mk_task(db, repo_id, issue_iid=8, title="优化构建速度")

        by_title = app_client.get("/api/tasks", params={"search": "数据库"}).json()
        assert [t["issue_iid"] for t in by_title["tasks"]] == [7]

        by_iid = app_client.get("/api/tasks", params={"search": "8"}).json()
        assert [t["issue_iid"] for t in by_iid["tasks"]] == [8]

    def test_pagination_and_limit_bounds(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        for i in range(5):
            _mk_task(db, repo_id, issue_iid=100 + i, title=f"任务 {i}")

        page = app_client.get("/api/tasks", params={"limit": 2, "offset": 1}).json()
        # ORDER BY id DESC
        assert [t["issue_iid"] for t in page["tasks"]] == [103, 102]

        # limit 上限 200、下限 1（FastAPI Query 约束 → 422）
        assert app_client.get("/api/tasks", params={"limit": 0}).status_code == 422
        assert app_client.get("/api/tasks", params={"limit": 201}).status_code == 422

    # ---- issue #50：翻页组件依赖的 total 契约 ----
    # 任务页面翻页组件按 total 计算总页数；total 必须与当前筛选条件
    # （repo_id / search）一致，否则筛选后总页数偏大、翻页越界。

    def test_total_follows_repo_filter(self, client):
        """repo_id 筛选时 total 只统计该仓库的任务（翻页总页数正确）。"""
        app_client, db = client
        repo_a = _mk_repo(db, project_id=1, name="repo-a")
        repo_b = _mk_repo(db, project_id=2, name="repo-b")
        _mk_task(db, repo_a, issue_iid=1)
        _mk_task(db, repo_a, issue_iid=2)
        _mk_task(db, repo_b, issue_iid=3)

        body = app_client.get("/api/tasks", params={"repo_id": repo_a}).json()
        assert body["total"] == 2
        assert {t["issue_iid"] for t in body["tasks"]} == {1, 2}

    def test_total_follows_search_filter(self, client):
        """search 筛选时 total 只统计匹配的任务（标题或 issue 编号）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=7, title="数据库连接失败排查")
        _mk_task(db, repo_id, issue_iid=8, title="优化构建速度")
        _mk_task(db, repo_id, issue_iid=9, title="数据库迁移脚本")

        body = app_client.get("/api/tasks", params={"search": "数据库"}).json()
        assert body["total"] == 2
        assert {t["issue_iid"] for t in body["tasks"]} == {7, 9}

    def test_total_follows_combined_filters(self, client):
        """status + repo_id + search 组合筛选时 total 与三层过滤条件一致。"""
        app_client, db = client
        repo_a = _mk_repo(db, project_id=1, name="repo-a")
        repo_b = _mk_repo(db, project_id=2, name="repo-b")
        _mk_task(db, repo_a, issue_iid=1, title="部署失败排查", status="failed",
                 error_message="原因")
        _mk_task(db, repo_a, issue_iid=2, title="部署脚本优化", status="succeeded")
        _mk_task(db, repo_b, issue_iid=3, title="部署失败排查", status="failed",
                 error_message="原因")

        body = app_client.get("/api/tasks", params={
            "status": "failed", "repo_id": repo_a, "search": "部署",
        }).json()
        assert body["total"] == 1
        assert [t["issue_iid"] for t in body["tasks"]] == [1]

    def test_total_zero_for_no_match(self, client):
        """筛选无匹配时 total 为 0（翻页组件不渲染、不出现空页）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1)
        body = app_client.get("/api/tasks", params={"search": "不存在的关键字"}).json()
        assert body["total"] == 0
        assert body["tasks"] == []
        # 不存在的仓库 id 同理
        body = app_client.get("/api/tasks", params={"repo_id": 99999}).json()
        assert body["total"] == 0
        assert body["tasks"] == []

    def test_repo_name_resolved(self, client):
        app_client, db = client
        repo_id = _mk_repo(db, project_id=42, name="my-awesome-repo")
        _mk_task(db, repo_id)
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["repo_name"] == "my-awesome-repo"

    def test_repo_missing_shows_null_name(self, client):
        """仓库记录被删后，列表 repo_name 应为 None（前端显示 '—' 不报错）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        db.delete_repo(repo_id)
        body = app_client.get("/api/tasks").json()
        assert body["tasks"][0]["repo_name"] is None
        assert body["tasks"][0]["id"] == task_id


class TestTaskDetail:
    """GET /api/tasks/{id} 详情。"""

    def test_detail_includes_error_message_and_logs(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="失败任务",
                           status="failed",
                           error_message="Claude Code 报告无法解决该 issue",
                           failure_category="unsolvable")
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task = resp.json()
        assert task["error_message"] == "Claude Code 报告无法解决该 issue"
        assert task["logs"] == []
        assert task["status"] == "failed"
        # issue #274：详情返回失败分类与处理建议（详情页展示分类徽章 + 建议）
        assert task["failure_category"] == "unsolvable"
        assert "无法解决类" in task["failure_advice"]
        assert "issue" in task["failure_advice"]

    def test_detail_failure_category_defaults_empty(self, client):
        """issue #274：无分类的旧任务返回空分类与空建议，前端不报错。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=10, title="成功任务",
                           status="succeeded")
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task = resp.json()
        assert task["failure_category"] == ""
        assert task["failure_advice"] == ""

    def test_get_missing_task_404(self, client):
        app_client, db = client
        assert app_client.get("/api/tasks/99999").status_code == 404

    def test_logs_endpoint_404_for_missing(self, client):
        app_client, db = client
        assert app_client.get("/api/tasks/99999/logs").status_code == 404

    def test_log_file_tail_decodes_claude_json_lines(self, api_app):
        """log_file_tail：claude JSON 输出行重排为可读文本（转义解码，issue #16）。

        详情页「claude 输出尾部」直接展示日志文件内容，result 内嵌的
        \\n 等转义必须解码为真实换行，而不是按字面量显示。
        """
        app, db, tmp_path = api_app
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="失败任务",
                           status="failed", error_message="原因")
        inner = json.dumps({"tool_name": "Bash",
                            "tool_input": {"command": "echo hi\nraise SystemExit"}})
        log_path = tmp_path / "task_9.log"
        log_path.write_text(json.dumps({"type": "result", "result": inner,
                                        "ttft_ms": 100}) + "\n", encoding="utf-8")
        db.set_task_status(task_id, None, log_path=str(log_path))

        task = TestClient(app).get(f"/api/tasks/{task_id}").json()
        assert "raise SystemExit" in task["log_file_tail"]
        assert "\\n" not in task["log_file_tail"]
        assert "ttft_ms" not in task["log_file_tail"]


class TestStatsAndDedup:
    """task_stats 统计与活跃任务去重。"""

    def test_task_stats_counts_by_status(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, status="succeeded")
        _mk_task(db, repo_id, issue_iid=2, status="failed", error_message="原因")
        _mk_task(db, repo_id, issue_iid=3, status="queued")
        stats = app_client.get("/api/tasks").json()["stats"]
        assert stats == {"succeeded": 1, "failed": 1, "queued": 1}

    def test_dup_active_task_rejected(self, client):
        """同一 (project_id, issue_iid) 已有活跃任务时 create_task 返回 None（去重）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        first = db.create_task(repo_id, 42, 1, "重复任务")
        assert first is not None
        assert db.create_task(repo_id, 42, 1, "重复任务") is None
        # 失败（终态）后允许重新创建
        db.set_task_status(first, "failed", error_message="原因")
        assert db.create_task(repo_id, 42, 1, "重复任务") is not None


# ---- issue #19：任务 commit 链接字段契约 ----

class TestTaskCommitFields:
    """列表/详情必须透出 commit_sha 与 commit_url（任务页面 commit 链接的数据契约）。

    commit_sha 由 executor 成功路径从 GitLab commits API 查询落库；
    commit_url 由后端按仓库 URL 拼出（repo url 去 .git 后缀 + /-/commit/<sha>），
    前端零拼接逻辑。
    """

    def test_list_returns_commit_url(self, client):
        """有 commit_sha 的任务，列表返回完整 sha 与可跳转的 commit_url。"""
        app_client, db = client
        repo_id = _mk_repo(db)  # url: https://gitlab.example.com/group/demo.git
        _mk_task(db, repo_id, issue_iid=1, title="成功任务",
                 commit_sha="deadbeef000111222333444555666777888999aa")
        _mk_task(db, repo_id, issue_iid=2, title="无提交任务")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["commit_sha"] == "deadbeef000111222333444555666777888999aa"
        assert by_iid[1]["commit_url"] == (
            "https://gitlab.example.com/group/demo/-/commit/deadbeef000111222333444555666777888999aa")
        # 无提交的任务两个字段都为 None（前端显示占位符）
        assert by_iid[2]["commit_sha"] is None
        assert by_iid[2]["commit_url"] is None

    def test_detail_returns_commit_url(self, client):
        """详情返回 commit_url；仓库 URL 无 .git 后缀时拼接不受影响。"""
        app_client, db = client
        repo_id = db.upsert_repo(43, "plain", "https://gitlab.example.com/group/plain")
        task_id = _mk_task(db, repo_id, issue_iid=3, title="成功任务",
                           commit_sha="abc12345")
        task = app_client.get(f"/api/tasks/{task_id}").json()
        assert task["commit_sha"] == "abc12345"
        assert task["commit_url"] == "https://gitlab.example.com/group/plain/-/commit/abc12345"

    def test_detail_commit_none_when_repo_deleted(self, api_app):
        """仓库记录被删除时（repo_name 显示占位符），commit_url 应为 None 而非 500。"""
        app, db, tmp_path = api_app
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=4, title="成功任务",
                           commit_sha="deadbeef00")
        db.delete_repo(repo_id)
        task = TestClient(app).get(f"/api/tasks/{task_id}").json()
        assert task["repo_name"] is None
        assert task["commit_sha"] == "deadbeef00"
        assert task["commit_url"] is None

    def test_commit_url_helper_edge_cases(self):
        """_commit_url 拼接函数边界：空 URL / 空 sha → None。"""
        from botler.api.tasks import _commit_url
        assert _commit_url("https://x.example/a/b.git", "abc") == "https://x.example/a/b/-/commit/abc"
        assert _commit_url("https://x.example/a/b", "abc") == "https://x.example/a/b/-/commit/abc"
        assert _commit_url("", "abc") is None
        assert _commit_url("https://x.example/a/b.git", "") is None
        assert _commit_url(None, "abc") is None


class TestTaskExecution:
    """GET /api/tasks/{id}/execution：实时查看任务执行的日志增量与聊天记录（issue #20）。"""

    def _mk_session(self, tmp_path, sid: str, lines: list[str]) -> None:
        """在 ~/.claude/projects/<proj>/ 下写一个 session jsonl 文件。"""
        proj = tmp_path / ".claude" / "projects" / "proj-a"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _mk_running_task(self, db, tmp_path, log_text: str = "",
                         session_id: str | None = None) -> int:
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="实时查看任务",
                           status="running")
        log_path = tmp_path / "logs" / f"task_{task_id}.log"
        log_path.parent.mkdir(exist_ok=True)
        if log_text:
            log_path.write_text(log_text, encoding="utf-8")
        db.set_task_status(task_id, None, log_path=str(log_path),
                           claude_session_id=session_id)
        return task_id

    def test_execution_basic_contract(self, api_app, monkeypatch):
        """无日志文件、无会话时返回空增量与空 transcript（不 500）。"""
        app, db, tmp_path = api_app
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        task_id = self._mk_running_task(db, tmp_path)
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["status"] == "running"
        assert body["session_id"] is None
        assert body["log_offset"] == 0
        assert body["log_delta"] == []
        assert body["transcript"] == []
        assert body["transcript_truncated"] is False

    def test_execution_log_delta_and_offset(self, api_app):
        """日志增量按 after_byte 读取，claude JSON 行解码为可读文本。"""
        app, db, tmp_path = api_app
        line1 = '{"type":"result","session_id":"sid-1","result":"开始执行","exit_code":null}'
        line2 = '{"type":"result","session_id":"sid-1","result":"完成任务"}'
        task_id = self._mk_running_task(db, tmp_path, log_text=line1 + "\n" + line2 + "\n")
        c = TestClient(app)

        body = c.get(f"/api/tasks/{task_id}/execution").json()
        assert len(body["log_delta"]) == 2
        assert "开始执行" in body["log_delta"][0]  # result 嵌套转义已解码
        assert "完成任务" in body["log_delta"][1]
        assert body["log_offset"] == len((line1 + "\n" + line2 + "\n").encode("utf-8"))

        # 增量续读：after_byte 传上次 offset → 无新增
        body2 = c.get(f"/api/tasks/{task_id}/execution",
                      params={"after_byte": body["log_offset"]}).json()
        assert body2["log_delta"] == []
        assert body2["log_offset"] == body["log_offset"]

    def test_execution_log_half_line_rewind(self, api_app, tmp_path):
        """日志文件尾部未写完的半行不返回，offset 回退到行首。"""
        app, db, tmp_path = api_app
        task_id = self._mk_running_task(db, tmp_path, log_text="line1\nline2")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["log_delta"] == ["line1"]
        assert body["log_offset"] == len("line1\n".encode("utf-8"))

    def test_execution_transcript_from_session_file(self, api_app, monkeypatch):
        """有 claude_session_id 且会话文件存在 → 返回解析后的聊天消息。"""
        app, db, tmp_path = api_app
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        session_lines = [
            json.dumps({"type": "user", "message": {"role": "user",
                        "content": [{"type": "text", "text": "请修复 bug"}],
                        "timestamp": "2026-08-12T10:00:00Z"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_1",
                                     "name": "Bash", "input": {"command": "git status"}}],
                        "timestamp": "2026-08-12T10:00:01Z"}}),
        ]
        self._mk_session(tmp_path, "sid-live", session_lines)
        task_id = self._mk_running_task(db, tmp_path, session_id="sid-live")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["session_id"] == "sid-live"
        assert [m["role"] for m in body["transcript"]] == ["user", "tool"]
        assert body["transcript"][1]["tool"] == "Bash"
        assert body["transcript_truncated"] is False

    def test_execution_prompt_from_session_file(self, api_app, monkeypatch):
        """execution 响应带 prompt 字段：首条 user 消息全文（issue #90）。

        「查看提示词」按钮数据源——聊天记录首条 user 完整保留的同时，
        单独提供 prompt 字段供前端展示，与全局模版逐字节比对。
        """
        app, db, tmp_path = api_app
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        prompt = "渲染后的完整提示词" + "p" * 6000
        session_lines = [
            json.dumps({"type": "user", "message": {"role": "user",
                        "content": [{"type": "text", "text": prompt}],
                        "timestamp": "2026-08-12T10:00:00Z"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "开始执行"}],
                        "timestamp": "2026-08-12T10:00:01Z"}}),
        ]
        self._mk_session(tmp_path, "sid-prompt", session_lines)
        task_id = self._mk_running_task(db, tmp_path, session_id="sid-prompt")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["prompt"] == prompt                    # 提示词全文
        assert body["transcript"][0]["text"] == prompt     # 聊天记录同样完整

    def test_execution_prompt_none_when_file_missing(self, api_app, monkeypatch):
        """会话文件丢失 → prompt 为 None（前端回退占位文案），不 500。"""
        app, db, tmp_path = api_app
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        task_id = self._mk_running_task(db, tmp_path, session_id="lost-sid")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["prompt"] is None

    def test_execution_transcript_empty_when_file_missing(self, api_app, monkeypatch):
        """session_id 有值但会话文件已丢失 → transcript 空，不 500。"""
        app, db, tmp_path = api_app
        monkeypatch.setattr("botler.executor.Path.home", lambda: tmp_path)
        task_id = self._mk_running_task(db, tmp_path, session_id="lost-sid")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["session_id"] == "lost-sid"
        assert body["transcript"] == []

    def test_execution_404_and_param_bounds(self, api_app):
        app, db, tmp_path = api_app
        c = TestClient(app)
        assert c.get("/api/tasks/9999/execution").status_code == 404
        task_id = self._mk_running_task(db, tmp_path)
        # after_byte 参数约束（ge=0）在业务逻辑前校验 → 422
        assert c.get(f"/api/tasks/{task_id}/execution",
                     params={"after_byte": -1}).status_code == 422


# ---- issue #32：概览页数据源 ----

    def test_execution_dsh_transcript_returns_prompt_and_messages(self, api_app):
        """dsh 引擎：从 dsh_transcript 返回提示词与聊天记录（issue #146）。

        复现：execution 接口此前只读 claude 会话文件（claude_session_id），
        dsh 引擎会话 id 存 dsh_session_id、提示词/消息落库 dsh_transcript
        字段 → dsh 任务 prompt 返回 null、transcript 为空（前端显示
        「提示词未持久化」「暂无聊天记录」）。
        """
        app, db, tmp_path = api_app
        prompt = "渲染后的 dsh 提示词（issue #146）"
        dsh_transcript = json.dumps({
            "prompt": prompt,
            "messages": [
                {"role": "user", "text": prompt,
                 "ts": "2026-08-17T08:00:00Z", "truncated": False},
                {"role": "assistant", "text": "正在处理…",
                 "ts": "2026-08-17T08:01:00Z", "truncated": False},
                {"role": "tool", "tool": "Bash",
                 "input": {"command": "ls"}, "ts": "2026-08-17T08:02:00Z"},
            ],
            "truncated": False,
        }, ensure_ascii=False)
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="dsh 聊天记录",
                           status="running")
        db.set_task_status(task_id, None, dsh_session_id="dsh-sess-1",
                           dsh_transcript=dsh_transcript)

        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["session_id"] == "dsh-sess-1"          # dsh 会话 id
        assert body["prompt"] == prompt                     # 提示词已持久化
        assert body["transcript_truncated"] is False
        assert [m["role"] for m in body["transcript"]] == ["user", "assistant", "tool"]
        assert body["transcript"][0]["text"] == prompt      # 聊天记录首条 = 提示词
        assert body["transcript"][1]["text"] == "正在处理…"
        assert body["transcript"][2]["tool"] == "Bash"

    def test_execution_dsh_transcript_invalid_json_returns_empty(self, api_app):
        """dsh_transcript 非 JSON（脏数据）→ 返回空 transcript 不 500。"""
        app, db, tmp_path = api_app
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="脏数据",
                           status="running")
        db.set_task_status(task_id, None, dsh_session_id="dsh-sess-1",
                           dsh_transcript="not-json{{")
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["prompt"] is None
        assert body["transcript"] == []

    def test_execution_dsh_truncated_flag_returned(self, api_app):
        """dsh_transcript 落库时截断过 → truncated 标记透传给前端。"""
        app, db, tmp_path = api_app
        dsh_transcript = json.dumps({
            "prompt": "p",
            "messages": [{"role": "user", "text": "p",
                          "ts": "t", "truncated": False}],
            "truncated": True,
        }, ensure_ascii=False)
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=9, title="截断",
                           status="running")
        db.set_task_status(task_id, None, dsh_session_id="dsh-sess-1",
                           dsh_transcript=dsh_transcript)
        body = TestClient(app).get(f"/api/tasks/{task_id}/execution").json()
        assert body["transcript_truncated"] is True


class TestListMultiStatus:
    """GET /api/tasks 多值 status 过滤（概览页一次拉取全部正在执行的任务）。

    概览页需要同时展示 running（执行中）与 retrying（重试中）两类活跃任务，
    status 参数支持逗号分隔多值（如 status=running,retrying），
    单值调用行为保持不变（向后兼容）。
    """

    def test_multi_status_returns_both(self, client):
        """status=running,retrying 返回两种状态的任务，不含其他状态。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="执行中任务", status="running")
        _mk_task(db, repo_id, issue_iid=2, title="重试中任务", status="retrying")
        _mk_task(db, repo_id, issue_iid=3, title="排队任务", status="queued")
        _mk_task(db, repo_id, issue_iid=4, title="成功任务", status="succeeded")

        body = app_client.get("/api/tasks", params={"status": "running,retrying"}).json()
        got = {t["issue_iid"] for t in body["tasks"]}
        assert got == {1, 2}
        assert body["total"] == 2

    def test_multi_status_count_matches_tasks(self, client):
        """多值过滤时 total 与返回数量一致（count_tasks 同步支持多值）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        for iid, status in [(1, "running"), (2, "running"), (3, "retrying"),
                            (4, "succeeded"), (5, "failed")]:
            _mk_task(db, repo_id, issue_iid=iid, title=f"任务{iid}", status=status)

        body = app_client.get("/api/tasks", params={"status": "running,retrying"}).json()
        assert len(body["tasks"]) == 3
        assert body["total"] == 3

    def test_single_status_unchanged(self, client):
        """单值 status 过滤行为不变（向后兼容）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, issue_iid=1, title="执行中任务", status="running")
        _mk_task(db, repo_id, issue_iid=2, title="重试中任务", status="retrying")

        body = app_client.get("/api/tasks", params={"status": "running"}).json()
        assert [t["issue_iid"] for t in body["tasks"]] == [1]
        assert body["total"] == 1

    def test_multi_status_unknown_value_400(self, client):
        """多值中混入未知状态 → 400（与单值校验一致）。"""
        app_client, db = client
        resp = app_client.get("/api/tasks", params={"status": "running,bogus"})
        assert resp.status_code == 400

    def test_multi_status_empty_element_treated_as_unknown_400(self, client):
        """边界：逗号分隔产生空元素（如 running,）→ 400 而非静默忽略。"""
        app_client, db = client
        assert app_client.get("/api/tasks", params={"status": "running,"}).status_code == 400
        assert app_client.get("/api/tasks", params={"status": ",running"}).status_code == 400


class TestTaskIssueUrl:
    """列表/详情透出 issue_url（概览页 issue 链接的数据契约）。

    issue_url 由后端按仓库 URL 拼出（去 .git 后缀 + /-/issues/<iid>），
    仓库缺失或 URL 无则返回 None，前端零拼接逻辑。
    """

    def test_list_returns_issue_url(self, client):
        """列表项返回可跳转的 issue_url。"""
        app_client, db = client
        repo_id = _mk_repo(db)  # url: https://gitlab.example.com/group/demo.git
        _mk_task(db, repo_id, issue_iid=7, title="任务A", status="running")
        _mk_task(db, repo_id, issue_iid=8, title="任务B", status="succeeded")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[7]["issue_url"] == "https://gitlab.example.com/group/demo/-/issues/7"
        assert by_iid[8]["issue_url"] == "https://gitlab.example.com/group/demo/-/issues/8"

    def test_detail_issue_url_without_dot_git(self, client):
        """仓库 URL 无 .git 后缀时拼接不受影响。"""
        app_client, db = client
        repo_id = db.upsert_repo(44, "plain", "https://gitlab.example.com/group/plain")
        task_id = _mk_task(db, repo_id, issue_iid=3, title="任务", status="running")
        task = app_client.get(f"/api/tasks/{task_id}").json()
        assert task["issue_url"] == "https://gitlab.example.com/group/plain/-/issues/3"

    def test_issue_url_none_when_repo_deleted(self, api_app):
        """仓库记录被删除时 issue_url 为 None 而非 500。"""
        app, db, tmp_path = api_app
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=4, title="任务", status="running")
        db.delete_repo(repo_id)
        task = TestClient(app).get(f"/api/tasks/{task_id}").json()
        assert task["repo_name"] is None
        assert task["issue_url"] is None


class TestTaskEngineField:
    """任务列表/详情透出 engine（issue #120）：执行引擎按任务落库后，
    任务页与概览页右边栏都可拿到该 issue 实际执行的引擎。"""

    def test_list_returns_engine_field(self, client):
        """列表项携带 engine；未落库（新任务未执行）为空串。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=1, title="dsh 任务")
        db.set_task_status(task_id, "succeeded", engine="dsh")
        _mk_task(db, repo_id, issue_iid=2, title="未执行任务")

        body = app_client.get("/api/tasks").json()
        by_iid = {t["issue_iid"]: t for t in body["tasks"]}
        assert by_iid[1]["engine"] == "dsh"
        assert by_iid[2]["engine"] == ""

    def test_detail_returns_engine_field(self, client):
        """详情接口同样透出 engine（任务页数据契约一致）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=3, title="hermes 任务")
        db.set_task_status(task_id, "succeeded", engine="hermes")

        task = app_client.get(f"/api/tasks/{task_id}").json()

        assert task["engine"] == "hermes"



class TestTaskEnvironment:
    """任务执行环境快照（issue #276）：API 返回解析后的 environment 对象。"""

    def _mk_env_task(self, db, repo_id):
        """创建带环境快照 JSON 的任务并返回 task_id。"""
        task_id = db.create_task(repo_id, 42, 99, "环境快照任务")
        db.set_task_status(
            task_id, "succeeded",
            environment=json.dumps({
                "engine": {"name": "claude", "version": "2.1.226"},
                "model": {"name": "deepseek-v4-pro"},
                "git": {"branch": "main", "commit_sha": "a" * 40},
                "platform": {"version": "1.0.289"},
                "config_hash": "abc123",
                "captured_at": "2026-08-18T00:00:00+08:00",
            }, ensure_ascii=False))
        return task_id

    def test_get_task_returns_environment(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = self._mk_env_task(db, repo_id)
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        env = resp.json()["environment"]
        assert env["engine"] == {"name": "claude", "version": "2.1.226"}
        assert env["model"]["name"] == "deepseek-v4-pro"
        assert env["git"]["branch"] == "main"
        assert env["git"]["commit_sha"] == "a" * 40
        assert env["platform"]["version"] == "1.0.289"
        assert env["config_hash"] == "abc123"

    def test_get_task_environment_none_without_snapshot(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=100)
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["environment"] is None

    def test_list_returns_environment(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        self._mk_env_task(db, repo_id)
        resp = app_client.get("/api/tasks")
        assert resp.status_code == 200
        env = resp.json()["tasks"][0]["environment"]
        assert env["engine"]["name"] == "claude"

    def test_get_task_environment_error_marker(self, client):
        """采集失败落库的 error 标记应透出（前端显示「环境快照获取失败」）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = db.create_task(repo_id, 42, 101, "采集失败任务")
        db.set_task_status(task_id, "succeeded",
                           environment=json.dumps({"error": "环境快照获取失败"},
                                                  ensure_ascii=False))
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["environment"]["error"] == "环境快照获取失败"

    def test_get_task_environment_invalid_json_returns_none(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        task_id = db.create_task(repo_id, 42, 102, "脏数据任务")
        db.set_task_status(task_id, "succeeded", environment="{broken")
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["environment"] is None


class TestExportTasks:
    """GET /api/tasks/export：CSV（UTF-8 BOM，Excel 中文兼容）/ JSON 导出。

    验收标准（issue #228）：导出 CSV/JSON 可下载且字段完整、Excel 打开
    中文正常、测试覆盖。过滤条件与任务列表一致（status/repo_id/search），
    另支持创建时间范围 date_from/date_to；CSV 必须带 \ufeff BOM 前缀。
    """

    def _mk_export_task(self, db, repo_id, issue_iid, title="导出任务",
                        status="succeeded", engine="claude",
                        error_message=None, created_at="2026-08-01 10:00:00",
                        finished_at="2026-08-01 10:05:30"):
        """建任务并补齐导出相关字段（engine/时间），返回 task_id。"""
        task_id = db.create_task(repo_id, 42, issue_iid, title, triggered_by="webhook")
        db.set_task_status(task_id, status, engine=engine,
                           error_message=error_message,
                           started_at=created_at,
                           finished_at=finished_at)
        # created_at 由 create_task 落库（datetime('now')），测试里直接改
        # 以固定时间范围过滤的预期（与 test_api_issues 同法）
        with db._conn() as conn:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                         (created_at, task_id))
        return task_id

    def test_export_csv_has_bom_and_chinese_headers(self, client):
        """CSV 必须带 UTF-8 BOM 且表头为中文，中文内容不乱码。"""
        app_client, db = client
        repo_id = _mk_repo(db, name="demo中文")
        self._mk_export_task(db, repo_id, 1, title="修复登录问题",
                             error_message="登录失败")
        resp = app_client.get("/api/tasks/export?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert 'filename="tasks_export_' in resp.headers["content-disposition"]
        body = resp.content.decode("utf-8")
        assert body.startswith("\ufeff"), "CSV 必须以 UTF-8 BOM 开头（Excel 中文兼容）"
        # 表头中文列
        for col in ("id", "仓库", "Issue编号", "Issue标题", "状态", "引擎",
                    "用时(秒)", "错误信息", "创建时间"):
            assert col in body.splitlines()[0], f"CSV 表头缺少列 {col}"
        # 数据行含任务数据与中文内容
        assert "修复登录问题" in body
        assert "demo中文" in body
        assert "登录失败" in body

    def test_export_csv_default_format_and_lineterminator(self, client):
        """format 缺省为 csv；行终止为 \\r\\n（Excel 兼容）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        self._mk_export_task(db, repo_id, 1)
        resp = app_client.get("/api/tasks/export")
        body = resp.content.decode("utf-8")
        assert body.startswith("\ufeff")
        assert "\r\n" in body

    def test_export_json_fields_and_chinese(self, client):
        """JSON 导出为扁平对象数组，字段完整且中文原样保留。"""
        app_client, db = client
        repo_id = _mk_repo(db, name="demo")
        self._mk_export_task(db, repo_id, 7, title="中文标题任务",
                             error_message="失败原因：超时")
        resp = app_client.get("/api/tasks/export?format=json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert isinstance(data, list) and len(data) == 1
        row = data[0]
        assert row["id"] == 1
        assert row["repo_name"] == "demo"
        assert row["issue_iid"] == 7
        assert row["issue_title"] == "中文标题任务"
        assert row["status"] == "succeeded"
        assert row["engine"] == "claude"
        assert row["error_message"] == "失败原因：超时"
        # 字段完整（issue #228 验收：id/仓库/issue/状态/引擎/耗时/错误/时间等）
        for key in ("id", "repo_id", "repo_name", "project_id", "issue_iid",
                    "issue_title", "issue_url", "status", "engine",
                    "triggered_by", "attempt_count", "exit_code",
                    "error_message", "failure_category", "commit_sha",
                    "commit_url", "created_at", "started_at", "finished_at",
                    "duration_seconds"):
            assert key in row, f"JSON 导出缺少字段 {key}"

    def test_export_duration_seconds(self, client):
        """用时 = finished_at - created_at（秒）；缺时间字段为 null。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        self._mk_export_task(db, repo_id, 1, created_at="2026-08-01 10:00:00",
                             finished_at="2026-08-01 10:05:30")
        task_id = db.create_task(repo_id, 42, 2, "无结束时间")
        db.set_task_status(task_id, "running", engine="dsh")
        resp = app_client.get("/api/tasks/export?format=json")
        by_iid = {r["issue_iid"]: r for r in resp.json()}
        assert by_iid[1]["duration_seconds"] == 330
        assert by_iid[2]["duration_seconds"] is None

    def test_export_empty(self, client):
        """无任务时 CSV 仅表头、JSON 为空数组。"""
        app_client, db = client
        resp = app_client.get("/api/tasks/export?format=csv")
        body = resp.content.decode("utf-8")
        lines = body.lstrip("\ufeff").splitlines()
        assert len(lines) == 1 and "仓库" in lines[0]
        resp = app_client.get("/api/tasks/export?format=json")
        assert resp.json() == []

    def test_export_invalid_format(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks/export?format=xlsx")
        assert resp.status_code == 422

    def test_export_unknown_status_400(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks/export?status=bogus")
        assert resp.status_code == 400

    def test_export_filters_status_repo_search(self, client):
        """导出过滤与任务列表一致：status（含多值）/ repo_id / search。"""
        app_client, db = client
        repo_a = _mk_repo(db, name="repoA")
        repo_b = _mk_repo(db, project_id=43, name="repoB")
        self._mk_export_task(db, repo_a, 1, title="登录问题", status="succeeded")
        self._mk_export_task(db, repo_a, 2, title="部署问题", status="failed")
        self._mk_export_task(db, repo_b, 3, title="登录问题", status="succeeded")

        data = app_client.get("/api/tasks/export?format=json&status=succeeded").json()
        assert {r["issue_iid"] for r in data} == {1, 3}
        data = app_client.get(
            "/api/tasks/export?format=json&status=succeeded,failed").json()
        assert len(data) == 3
        data = app_client.get(f"/api/tasks/export?format=json&repo_id={repo_a}").json()
        assert {r["issue_iid"] for r in data} == {1, 2}
        data = app_client.get("/api/tasks/export?format=json&search=登录").json()
        assert {r["issue_iid"] for r in data} == {1, 3}

    def test_export_date_range_inclusive(self, client):
        """date_from/date_to 按创建时间过滤，日期串补齐当日边界。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        self._mk_export_task(db, repo_id, 1, created_at="2026-08-01 10:00:00")
        self._mk_export_task(db, repo_id, 2, created_at="2026-08-10 10:00:00")
        self._mk_export_task(db, repo_id, 3, created_at="2026-08-20 10:00:00")

        data = app_client.get(
            "/api/tasks/export?format=json&date_from=2026-08-10&date_to=2026-08-10").json()
        assert [r["issue_iid"] for r in data] == [2], "当日 00:00:00~23:59:59 应全含"
        data = app_client.get(
            "/api/tasks/export?format=json&date_from=2026-08-10 00:00:00"
            "&date_to=2026-08-10 23:59:59").json()
        assert [r["issue_iid"] for r in data] == [2]
        data = app_client.get(
            "/api/tasks/export?format=json&date_from=2026-08-01&date_to=2026-08-10").json()
        assert {r["issue_iid"] for r in data} == {1, 2}
        # 仅 date_from / 仅 date_to
        data = app_client.get(
            "/api/tasks/export?format=json&date_from=2026-08-10").json()
        assert {r["issue_iid"] for r in data} == {2, 3}
        data = app_client.get(
            "/api/tasks/export?format=json&date_to=2026-08-10").json()
        assert {r["issue_iid"] for r in data} == {1, 2}

    def test_export_invalid_date_400(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks/export?format=json&date_from=2026/08/01")
        assert resp.status_code == 400
        resp = app_client.get("/api/tasks/export?format=json&date_to=乱来")
        assert resp.status_code == 400
        resp = app_client.get(
            "/api/tasks/export?format=json&date_from=2026-08-10&date_to=2026-08-01")
        assert resp.status_code == 400

    def test_export_csv_quotes_newline_in_error(self, client):
        """含逗号/换行的错误信息在 CSV 中应被正确引用（Excel 不破列）。"""
        app_client, db = client
        repo_id = _mk_repo(db)
        self._mk_export_task(db, repo_id, 1, title="多行错误",
                             error_message="第一行失败\n第二行,带逗号")
        resp = app_client.get("/api/tasks/export?format=csv")
        body = resp.content.decode("utf-8")
        assert '"第一行失败\n第二行,带逗号"' in body, "多行/逗号内容应按 CSV 规则引用"

    def test_export_deleted_repo_still_exports(self, client):
        """仓库软删除后任务历史仍可导出（仓库名/链接为空不报错）。"""
        app_client, db = client
        repo_id = _mk_repo(db, name="已删仓库")
        self._mk_export_task(db, repo_id, 1, title="历史任务")
        db.delete_repo(repo_id)
        resp = app_client.get("/api/tasks/export?format=json")
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["repo_name"] is None
        assert row["issue_url"] is None


class TestEffectiveTaskParamsInTaskList:
    """任务列表/详情返回实际生效参数与来源（issue #237）。

    timeout_seconds 恒为 null（执行引擎不限时）；max_retries / effective_engine
    按「仓库级 > 全局」解析，来源字段供前端展示「继承 or 覆盖」。
    """

    def test_repo_override_shown_in_list(self, client):
        """仓库配置了覆盖 → 列表返回仓库级生效值与来源 repo。"""
        tc, db = client
        repo_id = _mk_repo(db)
        db.update_repo(repo_id, max_retries=5, engine="dsh")
        task_id = _mk_task(db, repo_id)
        resp = tc.get("/api/tasks")
        assert resp.status_code == 200
        task = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
        assert task["timeout_seconds"] is None and task["timeout_source"] is None
        assert task["max_retries"] == 5 and task["max_retries_source"] == "repo"
        assert task["effective_engine"] == "dsh" and task["engine_source"] == "repo"

    def test_no_override_inherits_global(self, client):
        """仓库未配置 → 引擎不限时，重试/引擎继承全局。"""
        tc, db = client
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        resp = tc.get("/api/tasks")
        task = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
        assert task["timeout_seconds"] is None and task["timeout_source"] is None
        assert task["max_retries"] == 2 and task["max_retries_source"] == "global"
        assert task["effective_engine"] == "claude" and task["engine_source"] == "global"

    def test_partial_override_mixes_sources(self, client):
        """历史仓库超时不影响任务，其他参数仍继承全局。"""
        tc, db = client
        repo_id = _mk_repo(db)
        db.update_repo(repo_id, timeout_seconds=300)
        task_id = _mk_task(db, repo_id)
        resp = tc.get("/api/tasks")
        task = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
        assert task["timeout_seconds"] is None and task["timeout_source"] is None
        assert task["max_retries_source"] == "global"
        assert task["engine_source"] == "global"

    def test_cleared_params_inherit_global(self, client):
        """清空（NULL）后 → 继承全局（与未配置一致）。"""
        tc, db = client
        repo_id = _mk_repo(db)
        db.update_repo(repo_id, max_retries=5, engine="dsh")
        db.update_repo(repo_id, timeout_seconds=None, max_retries=None, engine=None)
        task_id = _mk_task(db, repo_id)
        resp = tc.get("/api/tasks")
        task = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
        assert task["timeout_source"] is None
        assert task["max_retries_source"] == "global"
        assert task["engine_source"] == "global"

    def test_detail_includes_effective_params(self, client):
        """任务详情同样返回生效参数与来源。"""
        tc, db = client
        repo_id = _mk_repo(db)
        db.update_repo(repo_id, max_retries=3, engine="hermes")
        task_id = _mk_task(db, repo_id)
        resp = tc.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timeout_seconds"] is None and data["timeout_source"] is None
        assert data["max_retries"] == 3 and data["max_retries_source"] == "repo"
        assert data["effective_engine"] == "hermes" and data["engine_source"] == "repo"

    def test_global_custom_worker_config_used(self, tmp_path):
        """全局 worker 自定义（非默认）时继承值为自定义值。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT.replace("worker: {}", """worker:
  max_retries: 4
  engine: dsh"""), encoding="utf-8")
        config = ConfigManager(str(config_path))
        db = Database(str(tmp_path / "t2.db"))
        ctx = SimpleNamespace(config=config, db=db, gitlab=None, config_path=str(config_path))
        app = FastAPI()
        app.state.ctx = ctx
        app.include_router(api_router)
        tc = TestClient(app)
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        resp = tc.get("/api/tasks")
        task = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
        assert task["timeout_seconds"] is None and task["timeout_source"] is None
        assert task["max_retries"] == 4 and task["max_retries_source"] == "global"
        assert task["effective_engine"] == "dsh" and task["engine_source"] == "global"


class TestTasksWatermark:
    """GET /api/tasks/watermark（issue #257）：导航栏并发水位数据契约。

    验收标准 1「水位徽章数字与任务列表实际一致」：水位各状态计数与
    GET /api/tasks 的 stats 同表同口径（task_stats 分组），本类验证
    一致性；今日完成/最近完成时间边界单独验证。
    """

    def test_empty_db(self, client):
        app_client, db = client
        resp = app_client.get("/api/tasks/watermark")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("queued", "running", "retrying", "succeeded", "failed",
                    "interrupted", "canceled_by_user", "total", "completed_today"):
            assert body[key] == 0, f"{key} 应为 0"
        assert body["last_completed_at"] is None

    def test_counts_match_task_list_stats(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        # 同 issue 仅允许一条活跃任务（去重索引），各任务用不同 issue_iid
        _mk_task(db, repo_id, issue_iid=1, status="queued")
        _mk_task(db, repo_id, issue_iid=2, status="queued")
        _mk_task(db, repo_id, issue_iid=3, status="running")
        _mk_task(db, repo_id, issue_iid=4, status="retrying")
        _mk_task(db, repo_id, issue_iid=5, status="succeeded")
        _mk_task(db, repo_id, issue_iid=6, status="failed")
        _mk_task(db, repo_id, issue_iid=7, status="interrupted")
        _mk_task(db, repo_id, issue_iid=8, status="canceled_by_user")

        body = app_client.get("/api/tasks/watermark").json()
        assert body["queued"] == 2
        assert body["running"] == 1
        assert body["retrying"] == 1
        assert body["succeeded"] == 1
        assert body["failed"] == 1
        assert body["interrupted"] == 1
        assert body["canceled_by_user"] == 1
        assert body["total"] == 8

        # 与任务列表 stats（task_stats 分组）口径一致
        stats = app_client.get("/api/tasks").json()["stats"]
        for key in ("queued", "running", "retrying", "succeeded", "failed",
                    "interrupted", "canceled_by_user"):
            assert body[key] == stats.get(key, 0), f"{key} 与任务列表 stats 不一致"

    def test_completed_today_only_succeeded_finished_today(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d %H:%M:%S")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        _mk_task(db, repo_id, status="succeeded", finished_at=today)
        _mk_task(db, repo_id, status="succeeded", finished_at=yesterday)
        _mk_task(db, repo_id, status="failed", finished_at=today)
        # 无 finished_at 的排队任务不影响今日完成
        _mk_task(db, repo_id, status="queued")

        body = app_client.get("/api/tasks/watermark").json()
        assert body["succeeded"] == 2
        assert body["completed_today"] == 1, "仅 UTC 今日成功终态计入今日完成"
        assert body["total"] == 4

    def test_last_completed_at_is_max_finished(self, client):
        app_client, db = client
        repo_id = _mk_repo(db)
        _mk_task(db, repo_id, status="succeeded", finished_at="2026-08-20 10:00:00")
        _mk_task(db, repo_id, status="failed", finished_at="2026-08-21 09:00:00")
        _mk_task(db, repo_id, status="queued")  # 无完成时间，不参与
        body = app_client.get("/api/tasks/watermark").json()
        assert body["last_completed_at"] == "2026-08-21 09:00:00"
