"""ClaudeExecutor autoclose 恢复测试（issue #109）。

背景：GitLab 实例开启了 autoclose_referenced_issues——提交信息命中
默认关闭模式（fix: #NN / closes #NN 等）且推送到默认主分支时，issue
被 GitLab 系统自动关闭（closed_by 为该项目的 project bot，非任何真人
用户）。graph2plan 任务的提交信息「fix: #24 …」曾反复触发，用户侧
表现为「agent 自己 close issue」（其实 agent 从未调用关闭 API）。

平台兜底：任务成功收尾时检测 issue 是否被 autoclose 误关，是则
reopen + 补说明评论；人工关闭（closed_by 为真实用户）不干预。
"""

import json
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient, GitLabError
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


@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "失败任务")


# autoclose 特征：closed_by 为该项目的 project bot（GitLab 系统自动关闭）
_AUTOCLOSE_BOT = "project_42_bot_073f85e85be02b26721f65fa91666a86"


def _mock_gitlab(executor, monkeypatch, tmp_path, issue: dict):
    """替换 executor.gitlab 为记录调用序列的 mock（沿用 test_executor 模式）。"""
    calls = []
    executor.gitlab = SimpleNamespace(
        get_issue=lambda pid, iid: dict(issue),
        reopen_issue=lambda pid, iid: calls.append(("reopen", (pid, iid))),
        add_comment=lambda pid, iid, body: calls.append(("comment", (pid, iid, body))),
        add_labels=lambda *a, **k: calls.append(("labels", a)),
        find_commit_for_issue=lambda pid, iid: None,
        last_note_author_id=lambda pid, iid: None,
    )
    monkeypatch.setattr(executor, "_run_once",
                        lambda *a: (0, json.dumps({"result": "完成"}, ensure_ascii=False)))
    monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
    monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
    return calls


def _logs(db, task_id: int) -> list[str]:
    return [r["message"] for r in db.list_logs(task_id)]


class TestRestoreAutoclosedIssue:
    """_restore_autoclosed_issue：autoclose 误关检测与恢复（issue #109）。"""

    def test_autoclosed_by_project_bot_reopened_and_commented(
            self, executor, monkeypatch, tmp_path):
        """closed + closed_by 是 project bot（autoclose 特征）→ reopen + 说明评论。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {
            "state": "closed",
            "closed_by": {"username": _AUTOCLOSE_BOT},
        })

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert calls.count(("reopen", (42, 1))) == 1
        comments = [a for kind, a in calls if kind == "comment"]
        assert any("autoclose" in a[2] for a in comments), \
            "补充说明评论应解释 autoclose 误关"
        assert any("autoclose" in m for m in _logs(db, task_id))

    def test_open_issue_not_touched(self, executor, monkeypatch, tmp_path):
        """issue 正常 opened → 不调用 reopen / 不补评论。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {"state": "opened"})

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert calls.count(("reopen", (42, 1))) == 0

    def test_closed_by_human_not_touched(self, executor, monkeypatch, tmp_path):
        """closed_by 是真实用户（人工关闭）→ 尊重人工决策，不 reopen。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {
            "state": "closed",
            "closed_by": {"username": "chenkaidi"},
        })

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert calls.count(("reopen", (42, 1))) == 0

    def test_closed_without_closed_by_not_touched(self, executor, monkeypatch, tmp_path):
        """closed 但无 closed_by 字段 → 无法确认 autoclose，不干预（容错）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {"state": "closed"})

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert calls.count(("reopen", (42, 1))) == 0

    def test_other_project_bot_username_not_matched(
            self, executor, monkeypatch, tmp_path):
        """closed_by 形似 bot 但不是本项目 bot → 不误判为 autoclose。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {
            "state": "closed",
            "closed_by": {"username": "project_99_bot_abc"},
        })

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert calls.count(("reopen", (42, 1))) == 0

    def test_detection_failure_does_not_block_success(
            self, executor, monkeypatch, tmp_path):
        """get_issue 查询失败（GitLabError）→ 仅记 warn，不抛异常（收尾不被阻塞）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls = _mock_gitlab(executor, monkeypatch, tmp_path, {"state": "closed"})

        def boom(pid, iid):
            raise GitLabError("401 Unauthorized")

        executor.gitlab.get_issue = boom

        # 直接调用恢复方法（run_task 的 _issue_state 同样会查 issue，
        # 若从 run_task 进入，查询失败发生在收尾之前，无法覆盖本场景）
        executor._restore_autoclosed_issue(db.get_task(task_id),
                                           db.get_repo(repo_id))

        assert calls.count(("reopen", (42, 1))) == 0
        assert any("autoclose" in m for m in _logs(db, task_id))

    def test_reopen_failure_does_not_block_success(
            self, executor, monkeypatch, tmp_path):
        """reopen API 失败 → 记 warn，任务仍成功（不阻塞收尾）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        _mock_gitlab(executor, monkeypatch, tmp_path, {
            "state": "closed",
            "closed_by": {"username": _AUTOCLOSE_BOT},
        })

        def boom(pid, iid):
            raise GitLabError("500 Internal Server Error")

        executor.gitlab.reopen_issue = boom

        executor.run_task(task_id)

        assert db.get_task(task_id)["status"] == "succeeded"
        assert any("autoclose" in m for m in _logs(db, task_id))
