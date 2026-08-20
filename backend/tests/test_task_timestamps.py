"""任务时间戳 UTC 一致性测试（issue #42）。

bug 现象：任务页面「用时」显示不正确——#64 任务创建于 13:52:43（本地），
14:56 查看时页面显示用时 8 小时 24 分钟（真实执行约 24 分钟，多 8 小时）。

根因：容器 TZ=Asia/Shanghai，tasks 表三个时间字段时区混合存储——
created_at 由 SQLite datetime('now') 写 UTC 串，started_at/finished_at
由 executor time.strftime 写本地 CST 串；前端 fmtDuration 统一按 UTC
解析，当 started_at 为 NULL 回退 created_at 时 UTC/CST 串混算，时长
多 8 小时。

修复约定：executor 时间戳统一写 UTC（gmtime），与 created_at 及前端
解析契约一致。
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database, STATUS_RUNNING
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {precheck_enabled: false}
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


if hasattr(time, "tzset"):
    @pytest.fixture
    def cst_env(monkeypatch):
        """临时把进程时区切到 Asia/Shanghai（复现容器部署时区）。"""
        old = os.environ.get("TZ")
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        time.tzset()
        yield
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()
else:  # 平台不支持 tzset（如 Windows）：跳过时区相关用例
    @pytest.fixture
    def cst_env():
        pytest.skip("平台不支持 time.tzset，跳过时区复现测试")


def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "失败任务")


def _parse_utc(ts: str) -> datetime:
    """按 UTC 无后缀格式解析任务时间串（与前端 fmtTime/fmtDuration 同规则）。"""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _assert_utc(ts: str):
    """断言时间串为 UTC（与当前 UTC 时刻相差 1 分钟内，而非偏移 8 小时）。"""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = _parse_utc(ts) - now_utc
    assert abs(delta) < timedelta(minutes=1), f"{ts} 与 UTC 当前时刻相差 {delta}"


def _stub_gitlab(executor):
    """隔离所有 GitLab 网络调用。"""
    executor.gitlab = SimpleNamespace(
        get_issue=lambda pid, iid: {"state": "opened"},
        add_comment=lambda *a, **k: None,
        add_labels=lambda *a, **k: None,
        find_commit_for_issue=lambda pid, iid: None,
        last_note_author_id=lambda pid, iid: None,
    )


def _running_task(executor):
    repo_id = _mk_repo(executor.db)
    task_id = _mk_task(executor.db, repo_id)
    executor.db.set_task_status(task_id, STATUS_RUNNING)
    return task_id


class TestFinishTimestampsUtc:
    """收尾路径的 finished_at 必须写 UTC（strftime 未指定 gmtime 时写本地时区）。"""

    def test_finish_failed_writes_utc_finished_at(self, executor, monkeypatch,
                                                  tmp_path, cst_env):
        """_finish_failed 的 finished_at 应为 UTC（修复前：CST 串，偏 +8h）。"""
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        _stub_gitlab(executor)
        task_id = _running_task(executor)

        executor._finish_failed(task_id, "获取 issue 失败")

        finished_at = executor.db.get_task(task_id)["finished_at"]
        _assert_utc(finished_at)

    def test_finish_stopped_writes_utc_finished_at(self, executor, cst_env):
        """_finish_stopped 的 finished_at 应为 UTC（修复前：CST 串，偏 +8h）。"""
        task_id = _running_task(executor)

        executor._finish_stopped(task_id)

        finished_at = executor.db.get_task(task_id)["finished_at"]
        _assert_utc(finished_at)


class TestRunTaskTimestampsUtc:
    """run_task 全流程的 started_at / finished_at 必须写 UTC。"""

    def test_run_task_writes_utc_started_and_finished(self, executor, monkeypatch,
                                                      tmp_path, cst_env):
        """成功执行路径：started_at 与 finished_at 均应为 UTC 串。"""
        repo_id = _mk_repo(executor.db)
        task_id = _mk_task(executor.db, repo_id)
        _stub_gitlab(executor)
        monkeypatch.setattr(executor, "_run_once",
                            lambda *a: (0, json.dumps({"result": "完成"})))
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

        executor.run_task(task_id)

        task = executor.db.get_task(task_id)
        assert task["status"] == "succeeded"
        _assert_utc(task["started_at"])
        _assert_utc(task["finished_at"])

    def test_run_task_mixed_parse_matches_frontend(self, executor, monkeypatch,
                                                   tmp_path, cst_env):
        """前端视角回归：created_at 与 finished_at 按 UTC 解析的差值即真实执行时长。

        模拟 started_at 为 NULL（领取后立即失败的场景），验证前端
        fmtDuration(created_at, finished_at) 不再多算 8 小时。
        """
        repo_id = _mk_repo(executor.db)
        task_id = _mk_task(executor.db, repo_id)
        _stub_gitlab(executor)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        executor.db.set_task_status(task_id, STATUS_RUNNING, started_at=None)

        executor._finish_failed(task_id, "获取 issue 失败")

        task = executor.db.get_task(task_id)
        elapsed = _parse_utc(task["finished_at"]) - _parse_utc(task["created_at"])
        # 修复前：finished_at 为 CST 串（本地时刻被前端当 UTC 解析），
        # 差值 ≈ 8 小时 + 实际执行时长
        assert elapsed < timedelta(minutes=1)
