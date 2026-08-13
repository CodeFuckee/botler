"""任务「用时」计算测试（issue #49）。

bug 现象：任务页「用时」显示的是执行时长（started_at → finished_at），
任务排队等待的时间不计入；用户期望「用时」= 系统接收到该问题的时间
（任务入队 created_at）→ 系统给 issue 打上 bot-done 标记的时间，
动态计算，不写入数据库。

修复约定：
- finished_at 语义 = bot-done 打标时间：_finish_succeeded 打标签成功后
  把 finished_at 更新为打标时刻（打标失败时保留收尾时刻兜底）；
- 用时由 created_at 与 finished_at 动态计算（前端 fmtDuration），
  不新增数据库字段。
"""

import time
from datetime import timedelta
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


def _parse_utc(ts: str):
    """按 UTC 无后缀格式解析时间串（与前端 fmtDuration 同规则）。"""
    from datetime import datetime
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


class TestFinishSucceededBotDoneTimestamp:
    """_finish_succeeded：finished_at 应反映 bot-done 打标时刻（issue #49）。"""

    def test_finished_at_reflects_bot_done_label_time(self, executor, tmp_path):
        """打标成功后 finished_at 更新为打标时刻（修复前：finished_at 先于打标写入）。

        复现方式：add_labels 模拟 1.2s 网络耗时并记录打标完成时刻；
        修复前 finished_at 在打标前落库（早于打标完成 ≥1s），
        修复后 finished_at 在打标完成后落库（与打标完成时刻差 <1s）。
        """
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        label_done: list[str] = []

        def fake_add_labels(pid, iid, labels):
            time.sleep(1.2)  # 模拟打标 API 耗时
            label_done.append(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))

        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=fake_add_labels)
        db.claim_task(task_id)  # 模拟执行中状态（finish 仅接受 running/retrying）

        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        finished = _parse_utc(task["finished_at"])
        label_ts = _parse_utc(label_done[0])
        # 修复后：finished_at 在打标完成后落库，不早于打标完成时刻
        # （同秒或晚 1 秒，秒级截断）；修复前：finished_at 先于打标
        # 写入（sleep 1.2s），早于打标完成 1~2 秒 → 断言失败（复现 bug）
        assert finished - label_ts >= timedelta(0), (
            f"finished_at {task['finished_at']} 不应早于打标时刻 {label_done[0]}，"
            f"当前早 {label_ts - finished}"
        )

    def test_label_failure_keeps_finished_at(self, executor, tmp_path):
        """打标失败不阻塞成功收尾，finished_at 保留收尾时刻（回归保护）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor._log_file = lambda tid: tmp_path / f"task_{tid}.log"
        executor.gitlab = SimpleNamespace(
            find_commit_for_issue=lambda pid, iid: None,
            add_labels=lambda *a, **k: (_ for _ in ()).throw(
                GitLabError("GitLab API 错误 500: boom", 500)))
        db.claim_task(task_id)

        executor._finish_succeeded(task_id, "ok")

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["finished_at"], "打标失败时 finished_at 应保留收尾时刻"
        logs = [l["message"] for l in db.list_logs(task_id)]
        assert any("打 bot-done 标签失败" in m for m in logs)
