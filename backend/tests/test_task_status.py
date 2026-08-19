"""任务状态原子流转测试（issue #24）：多实例并发领取/收尾不状态错乱。

背景：botler 曾双实例并存（遗留 supervisord 实例 + pm2 实例）同时执行
同一任务，互踩工作区导致任务卡死、队列永久排队，页面状态与实际情况
不一致。修复：claim_task 原子抢占（queued/retrying → running 条件
UPDATE）+ finish_task 条件终态（仅 running/retrying 可流转终态）。
"""


import pytest

from botler.database import (
    Database, STATUS_FAILED, STATUS_RUNNING,
    STATUS_RETRYING, STATUS_SUCCEEDED,
)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _mk_repo_and_task(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    repo_id = db.get_repo_by_project_id(42)["id"]
    return db.create_task(repo_id, 42, 1, "并发测试")


class TestClaimTask:
    """claim_task：原子抢占，防多实例重复执行同一任务。"""

    def test_claim_queued_success(self, db):
        task_id = _mk_repo_and_task(db)
        assert db.claim_task(task_id) is True
        assert db.get_task(task_id)["status"] == STATUS_RUNNING

    def test_claim_retrying_success(self, db):
        task_id = _mk_repo_and_task(db)
        db.set_task_status(task_id, STATUS_RETRYING)
        assert db.claim_task(task_id) is True
        assert db.get_task(task_id)["status"] == STATUS_RUNNING

    def test_claim_already_running_fails(self, db):
        """已 running（其他实例已领取）→ 抢不到。"""
        task_id = _mk_repo_and_task(db)
        db.claim_task(task_id)
        assert db.claim_task(task_id) is False
        assert db.get_task(task_id)["status"] == STATUS_RUNNING

    def test_claim_terminal_fails(self, db):
        """已终态（succeeded/failed）→ 抢不到。"""
        task_id = _mk_repo_and_task(db)
        db.set_task_status(task_id, STATUS_SUCCEEDED)
        assert db.claim_task(task_id) is False
        db.set_task_status(task_id, STATUS_FAILED)
        assert db.claim_task(task_id) is False

    def test_claim_missing_task_fails(self, db):
        assert db.claim_task(9999) is False

    def test_claim_two_connections_only_one_wins(self, db):
        """两个连接（模拟两个实例）同时抢同一任务，只有一个成功。"""
        task_id = _mk_repo_and_task(db)
        db2 = Database(db.path)  # 第二个实例连接同一库
        assert db.claim_task(task_id) is True
        assert db2.claim_task(task_id) is False


class TestFinishTask:
    """finish_task：条件终态，慢实例不覆盖先完成者的结果。"""

    def test_finish_running_success(self, db):
        task_id = _mk_repo_and_task(db)
        db.claim_task(task_id)
        ok = db.finish_task(task_id, STATUS_SUCCEEDED,
                            exit_code=0, finished_at="2026-08-12 16:00:00")
        assert ok is True
        task = db.get_task(task_id)
        assert task["status"] == STATUS_SUCCEEDED
        assert task["exit_code"] == 0
        assert task["finished_at"] == "2026-08-12 16:00:00"

    def test_finish_retrying_success(self, db):
        """重试中（retrying）也允许流转终态（重试耗尽场景）。"""
        task_id = _mk_repo_and_task(db)
        db.set_task_status(task_id, STATUS_RETRYING)
        assert db.finish_task(task_id, STATUS_FAILED,
                              error_message="重试耗尽") is True
        assert db.get_task(task_id)["status"] == STATUS_FAILED

    def test_finish_already_terminal_fails(self, db):
        """已被其他实例先收尾（succeeded）→ 本实例失败收尾被拒绝，不覆盖。"""
        task_id = _mk_repo_and_task(db)
        db.claim_task(task_id)
        assert db.finish_task(task_id, STATUS_SUCCEEDED) is True
        # 慢实例随后想把任务改成 failed → 拒绝，保持 succeeded
        assert db.finish_task(task_id, STATUS_FAILED,
                              error_message="晚到的失败") is False
        task = db.get_task(task_id)
        assert task["status"] == STATUS_SUCCEEDED
        assert task["error_message"] is None

    def test_finish_unknown_fields_ignored(self, db):
        task_id = _mk_repo_and_task(db)
        db.claim_task(task_id)
        assert db.finish_task(task_id, STATUS_SUCCEEDED, not_a_field=1) is True
        assert db.get_task(task_id)["status"] == STATUS_SUCCEEDED
