"""任务失败自动创建 GitLab issue 上报测试（issue #347）。

覆盖：
- 标题/正文纯函数：任务 id、原始 issue 链接、失败分类徽章与处理建议、
  超长标题截断（GitLab 255 硬上限）、缺失字段兜底不抛错
- 插件行为（send_task_failed）：未启用跳过 / 启用创建（标题含任务 id、
  标签 bug+bot-failed、负责人解析）/ 负责人解析失败降级不阻塞 / 同一
  任务去重（任务日志标记）/ 缺 project_id 跳过 / 创建失败抛 GitLabError
- 配置：默认值 / 显式配置 / update_section 往返
- 设置 API：GET 返回段 / PUT 保存生效 / 非法类型 400
- executor 集成：_finish_failed 收尾分发到 auto_issue 通道完成创建
"""

from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient, GitLabError
from botler.plugins import PluginKind, get_plugin
from botler.plugins.auto_issue import (
    AUTO_ISSUE_DETAIL_MAX_CHARS,
    AUTO_ISSUE_LABELS,
    AutoIssueNotifierPlugin,
    build_issue_description,
    build_issue_title,
)
from botler.templates import TemplateRenderer

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


def _build_executor(tmp_path, extra: str = "") -> ClaudeExecutor:
    """按 extra 追加的 yaml 文本构造 executor（auto_issue 段可控）。"""
    text = CONFIG_TEXT + extra
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    config.load()
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 347,
             title: str = "任务失败自动提交 issue") -> int:
    return db.create_task(repo_id, 42, issue_iid, title)


# ---- 纯函数：标题 ----

class TestBuildIssueTitle:
    def test_contains_task_id_and_issue_iid(self):
        """标题必须写明失败任务 id，并带上原始 issue 编号。"""
        title = build_issue_title({"id": 12, "issue_iid": 347})
        assert "任务 #12" in title
        assert "issue #347" in title

    def test_appends_issue_title_when_room(self):
        """原始 issue 标题追加到标题末尾（不超过 255 上限）。"""
        title = build_issue_title(
            {"id": 12, "issue_iid": 347, "issue_title": "修复登录 bug"})
        assert title.endswith("：修复登录 bug")
        assert len(title) <= 255

    def test_long_issue_title_truncated(self):
        """超长原始标题截断到 GitLab 255 字符硬上限并加省略号。"""
        long = "很长的标题" * 200
        title = build_issue_title({"id": 12, "issue_iid": 347,
                                   "issue_title": long})
        assert len(title) <= 255
        assert title.endswith("…")

    def test_missing_fields_graceful(self):
        """字段缺失（无 iid/无标题/无 id）不抛错，标题仍含任务 id。"""
        title = build_issue_title({"id": 1})
        assert "任务 #1" in title
        title2 = build_issue_title({})
        assert title2  # 非空，不抛错


# ---- 纯函数：正文 ----

class TestBuildIssueDescription:
    def test_includes_task_id_reason_category_advice(self):
        """正文含任务 id、失败原因、分类徽章与处理建议。"""
        desc = build_issue_description(
            {"id": 12, "issue_iid": 347, "issue_title": "某 issue",
             "failure_category": "env"},
            "网络超时", repo_name="demo",
            repo_url="https://gitlab.example.com/group/demo.git",
            gitlab_url="https://gitlab.example.com")
        assert "任务 ID" in desc and "`12`" in desc
        assert "网络超时" in desc
        assert "环境类" in desc  # 分类徽章
        assert "处理建议" in desc
        assert "https://gitlab.example.com/group/demo/-/issues/347" in desc

    def test_unknown_category_graceful(self):
        """未知分类兜底（正文不崩、不出现分类徽章段）。"""
        desc = build_issue_description(
            {"id": 1, "issue_iid": 1, "issue_title": "t"}, "原因")
        assert "原因" in desc
        assert "失败分类" not in desc  # 无分类 → 无徽章行

    def test_detail_truncated(self):
        """超长错误详情截断到上限并加省略号。"""
        desc = build_issue_description(
            {"id": 1, "issue_iid": 1, "issue_title": "t"}, "原因",
            detail="x" * (AUTO_ISSUE_DETAIL_MAX_CHARS + 500))
        assert len([l for l in desc.splitlines() if l.startswith("x" * 10)][0]) \
            <= AUTO_ISSUE_DETAIL_MAX_CHARS + 1

    def test_empty_task_graceful(self):
        """空任务记录不抛错，仍返回非空正文。"""
        desc = build_issue_description({}, "")
        assert isinstance(desc, str) and desc


# ---- 插件行为 ----

class _FakeGitlab:
    """记录调用的假 GitLab client。"""

    def __init__(self):
        self.created = []
        self.user_id = None
        self.fail_create = None  # 抛出的异常
        self.get_user_fail = None

    def create_issue(self, project_id, title, description=None,
                     assignee_id=None, labels=None):
        if self.fail_create is not None:
            raise self.fail_create
        issue = {"iid": 900 + len(self.created), "project_id": project_id,
                 "title": title, "labels": labels or [],
                 "assignee_id": assignee_id,
                 "description": description or "",
                 "web_url": f"https://gitlab.example.com/x/-/issues/{900 + len(self.created)}"}
        self.created.append(issue)
        return issue

    def get_user_id_by_username(self, username):
        if self.get_user_fail is not None:
            raise self.get_user_fail
        return self.user_id


class _FakeCtx:
    """最小可用插件上下文（镜像 executor 的 _call_with_fallback 语义）。"""

    def __init__(self, config, gitlab, repo=None, db=None):
        self.config = config
        self.gitlab = gitlab
        self.repo = repo if repo is not None else {
            "id": 1, "name": "demo",
            "url": "https://gitlab.example.com/group/demo.git"}
        self.db = db or _FakeDb(repo=self.repo)

    def _call_with_fallback(self, repo, call):
        # 与 executor 一致：返回 (结果, client) 元组
        return call(self.gitlab), self.gitlab

    def _transient_retry(self, what, call, **kwargs):
        return call()


class _FakeDb:
    def __init__(self, repo=None, logs=None):
        self.repo = repo
        self.logs = logs or []

    def get_repo(self, repo_id):
        return self.repo

    def list_logs(self, task_id, limit=500):
        return [{"message": m} for m in self.logs]  # 与 sqlite3.Row 同语义

    def add_log(self, task_id, level, message):
        self.logs.append(message)


def _plugin() -> AutoIssueNotifierPlugin:
    return get_plugin(PluginKind.NOTIFIER, "auto_issue")


def _task(**over):
    base = {"id": 12, "repo_id": 1, "project_id": 42, "issue_iid": 347,
            "issue_title": "修复登录 bug", "failure_category": "env",
            "engine": "claude", "attempt_count": 3, "finished_at": "2026-08-20 10:00:00",
            "error_detail": '{"summary": "重试耗尽"}', "status": "failed"}
    base.update(over)
    return base


def _make_config(tmp_path, extra="") -> ConfigManager:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT + extra, encoding="utf-8")
    cm = ConfigManager(str(path))
    cm.load()
    return cm


class TestPluginBehavior:
    def test_disabled_skips(self, tmp_path):
        """未启用（enabled=false）返回 None，不创建 issue。"""
        cm = _make_config(tmp_path, "\nauto_issue:\n  enabled: false\n")
        gl = _FakeGitlab()
        ctx = _FakeCtx(cm, gl)
        result = _plugin().send_task_failed(ctx, _task(), "原因")
        assert result is None
        assert gl.created == []

    def test_enabled_creates_issue_with_labels_and_assignee(self, tmp_path):
        """启用后创建 issue：标题含任务 id、标签 bug+bot-failed、负责人解析。"""
        cm = _make_config(tmp_path)  # 默认 enabled=true, assignee=agent
        gl = _FakeGitlab()
        gl.user_id = 3  # agent 用户 id
        ctx = _FakeCtx(cm, gl)
        result = _plugin().send_task_failed(ctx, _task(), "网络超时")
        assert result is not None
        assert len(gl.created) == 1
        issue = gl.created[0]
        assert "任务 #12" in issue["title"]
        assert issue["labels"] == list(AUTO_ISSUE_LABELS)
        assert issue["labels"] == ["bug", "bot-failed"]
        assert issue["assignee_id"] == 3
        assert "网络超时" in issue.get("description", "")

    def test_assignee_resolve_failure_does_not_block(self, tmp_path):
        """负责人解析失败（用户不存在/API 故障）降级不指定，仍创建。"""
        cm = _make_config(tmp_path)
        gl = _FakeGitlab()
        gl.user_id = None  # 用户不存在
        ctx = _FakeCtx(cm, gl)
        result = _plugin().send_task_failed(ctx, _task(), "原因")
        assert result is not None
        assert gl.created[0]["assignee_id"] is None
        # API 故障场景
        gl2 = _FakeGitlab()
        gl2.get_user_fail = GitLabError("boom", 500)
        ctx2 = _FakeCtx(cm, gl2)
        result2 = _plugin().send_task_failed(ctx2, _task(), "原因")
        assert result2 is not None
        assert gl2.created[0]["assignee_id"] is None

    def test_same_task_dedup_by_log_marker(self, tmp_path):
        """同一任务已有上报标记时跳过，不重复创建。"""
        cm = _make_config(tmp_path)
        gl = _FakeGitlab()
        ctx = _FakeCtx(cm, gl)
        # 第一次创建成功落标记
        _plugin().send_task_failed(ctx, _task(), "原因")
        assert len(gl.created) == 1
        # 第二次（重复分发）跳过
        result = _plugin().send_task_failed(ctx, _task(), "原因")
        assert result is None
        assert len(gl.created) == 1
        assert any("已自动提交失败上报 issue" in m for m in ctx.db.logs)

    def test_missing_project_id_skips(self, tmp_path):
        """缺 project_id 跳过（记日志，不创建）。"""
        cm = _make_config(tmp_path)
        gl = _FakeGitlab()
        ctx = _FakeCtx(cm, gl)
        result = _plugin().send_task_failed(ctx, _task(project_id=0), "原因")
        assert result is None
        assert gl.created == []

    def test_create_failure_raises(self, tmp_path):
        """创建失败抛 GitLabError（调用方统一容错），并落错误日志。"""
        cm = _make_config(tmp_path)
        gl = _FakeGitlab()
        gl.fail_create = GitLabError("title too long", 400)
        ctx = _FakeCtx(cm, gl)
        with pytest.raises(GitLabError):
            _plugin().send_task_failed(ctx, _task(), "原因")
        assert any("创建失败上报 issue 失败" in m for m in ctx.db.logs)

    def test_repo_none_falls_back_global_client(self, tmp_path):
        """仓库查询失败（repo=None）降级用全局 client，不阻塞创建。"""
        cm = _make_config(tmp_path)
        gl = _FakeGitlab()
        gl.user_id = 3
        ctx = _FakeCtx(cm, gl, repo=None, db=_FakeDb(repo=None))
        result = _plugin().send_task_failed(ctx, _task(), "原因")
        assert result is not None
        assert len(gl.created) == 1


# ---- 配置 ----

class TestAutoIssueConfig:
    def test_defaults(self, tmp_path):
        """未配置 auto_issue 段时默认 enabled=true、assignee=agent。"""
        s = _make_config(tmp_path).get()
        assert s.auto_issue_enabled is True
        assert s.auto_issue_assignee == "agent"

    def test_explicit_config(self, tmp_path):
        """显式配置生效。"""
        s = _make_config(
            tmp_path, "\nauto_issue:\n  enabled: false\n  assignee: bob\n").get()
        assert s.auto_issue_enabled is False
        assert s.auto_issue_assignee == "bob"

    def test_update_section_roundtrip(self, tmp_path):
        """update_section 写回 auto_issue 段（磁盘+内存同步）。"""
        cm = _make_config(tmp_path)
        s = cm.update_section("auto_issue", {"enabled": False, "assignee": "x"})
        assert s.auto_issue_enabled is False
        assert s.auto_issue_assignee == "x"
        assert cm.get().auto_issue_enabled is False

    def test_unknown_field_ignored(self, tmp_path):
        """白名单外字段不写入。"""
        cm = _make_config(tmp_path)
        cm.update_section("auto_issue", {"enabled": False, "hacker": 1})
        assert cm.get().auto_issue_enabled is False


# ---- 设置 API ----

def _api_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from botler.api import router as api_router

    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), config


class TestAutoIssueApi:
    def test_get_returns_defaults(self, tmp_path):
        """GET /api/settings 返回 auto_issue 段默认值。"""
        tc, _ = _api_client(tmp_path)
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["auto_issue"] == {"enabled": True, "assignee": "agent"}

    def test_put_saves_and_get_reflects(self, tmp_path):
        """PUT /api/settings 保存 auto_issue 段并即时生效。"""
        tc, _ = _api_client(tmp_path)
        resp = tc.put("/api/settings",
                      json={"auto_issue": {"enabled": False, "assignee": "bob"}})
        assert resp.status_code == 200
        assert resp.json()["auto_issue"] == {"enabled": False, "assignee": "bob"}

    def test_put_invalid_types_400(self, tmp_path):
        """非法类型 400：enabled 非布尔 / assignee 非字符串。"""
        tc, _ = _api_client(tmp_path)
        r1 = tc.put("/api/settings", json={"auto_issue": {"enabled": "yes"}})
        assert r1.status_code == 400
        r2 = tc.put("/api/settings", json={"auto_issue": {"assignee": 3}})
        assert r2.status_code == 400


# ---- executor 集成 ----

class TestExecutorDispatch:
    def test_finish_failed_dispatches_to_auto_issue(self, tmp_path, monkeypatch):
        """_finish_failed 收尾：task_failed 事件分发到 auto_issue 创建 issue。"""
        executor = _build_executor(tmp_path)  # auto_issue 默认启用
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id, issue_iid=347)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"  # type: ignore[method-assign]
        # 原 issue 评论/标签打桩
        executor.gitlab.add_comment = lambda *a, **k: None  # type: ignore[method-assign]
        executor.gitlab.add_labels = lambda *a, **k: None  # type: ignore[method-assign]
        created = {}

        def fake_create(project_id, title, description=None,
                        assignee_id=None, labels=None):
            created["title"] = title
            created["labels"] = labels
            created["assignee_id"] = assignee_id
            created["desc"] = description
            return {"iid": 500, "web_url": "https://x/-/issues/500"}

        monkeypatch.setattr(executor.gitlab, "create_issue", fake_create)
        monkeypatch.setattr(executor.gitlab, "get_user_id_by_username",
                            lambda u: 3)
        db.claim_task(task_id)
        executor._finish_failed(task_id, "网络超时，重试耗尽")
        assert created["title"], "任务失败收尾应自动创建上报 issue"
        assert "任务 #" in created["title"]
        assert created["labels"] == ["bug", "bot-failed"]
        assert created["assignee_id"] == 3
        # 去重标记落库
        logs = db.list_logs(task_id)
        assert any("已自动提交失败上报 issue" in (l["message"] or "") for l in logs)
