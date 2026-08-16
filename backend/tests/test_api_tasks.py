"""任务 API 测试：列表（过滤/搜索/分页）、详情、统计与失败原因数据契约。

任务列表「失败原因显示」功能依赖 GET /api/tasks 返回的 error_message / error_detail
字段（error_detail 为每次尝试失败详情的结构化对象，供「查看详细原因」按钮使用），
本文件验证该数据契约及其余列表行为。
"""

import json
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
             error_detail: str | None = None, commit_sha: str | None = None) -> int:
    """创建任务并按需更新状态，返回 task_id。"""
    task_id = db.create_task(repo_id, 42, issue_iid, title, triggered_by="webhook")
    db.set_task_status(task_id, status, error_message=error_message,
                       error_detail=error_detail, commit_sha=commit_sha)
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
                           status="failed", error_message="Claude Code 报告无法解决该 issue")
        resp = app_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task = resp.json()
        assert task["error_message"] == "Claude Code 报告无法解决该 issue"
        assert task["logs"] == []
        assert task["status"] == "failed"

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
