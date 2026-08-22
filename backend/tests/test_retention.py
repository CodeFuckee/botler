"""数据保留清理测试（issue #204）。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from botler.database import Database, STATUS_FAILED, STATUS_SUCCEEDED
from botler.retention import RetentionManager


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "botler.db"))


def _task(db, iid: int, status: str, finished_at: str) -> int:
    db.upsert_repo(123, "botler", "https://gitlab.example.com/team/botler.git")
    repo_id = db.get_repo_by_project_id(123)["id"]
    task_id = db.create_task(repo_id, 123, iid, f"任务 {iid}")
    db.set_task_status(task_id, status, finished_at=finished_at)
    return task_id


def _settings(task_days=90, notification_days=30, log_days=90):
    return SimpleNamespace(
        retention_enabled=True,
        retention_task_logs_days=task_days,
        retention_notification_events_days=notification_days,
        retention_log_files_days=log_days,
        retention_pm2_max_log_size_mb=10,
    )


class TestRetentionCleanup:
    def test_cleanup_removes_expired_details_preserves_task_summaries_and_recent_data(self, db, tmp_path):
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        old = (now - timedelta(days=91)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (now - timedelta(days=89)).strftime("%Y-%m-%d %H:%M:%S")
        old_task = _task(db, 1, STATUS_SUCCEEDED, old)
        recent_task = _task(db, 2, STATUS_FAILED, recent)
        db.add_log(old_task, "info", "过期明细")
        db.add_log(recent_task, "info", "近期明细")
        old_file = tmp_path / "task_1.log"
        old_file.write_text("old", encoding="utf-8")
        recent_file = tmp_path / "task_2.log"
        recent_file.write_text("recent", encoding="utf-8")
        db.set_task_status(old_task, None, log_path=str(old_file))
        db.set_task_status(recent_task, None, log_path=str(recent_file))
        with db._conn(write=True) as conn:
            conn.execute("INSERT INTO notification_events (type, title, created_at) VALUES (?, ?, ?)",
                         ("old", "过期通知", (now - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")))
            conn.execute("INSERT INTO notification_events (type, title, created_at) VALUES (?, ?, ?)",
                         ("new", "近期通知", (now - timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")))

        manager = RetentionManager(db, config=SimpleNamespace(get=lambda: _settings()), log_dir=tmp_path)
        result = manager.cleanup(now=now)

        assert result == {"task_logs": 1, "notification_events": 1, "log_files": 1}
        assert db.get_task(old_task)["status"] == STATUS_SUCCEEDED
        assert db.get_task(recent_task)["status"] == STATUS_FAILED
        assert db.list_logs(old_task) == []
        assert len(db.list_logs(recent_task)) == 1
        assert not old_file.exists()
        assert recent_file.exists()
        with db._conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == 1

    def test_disabled_retention_is_noop(self, db, tmp_path):
        task_id = _task(db, 3, STATUS_SUCCEEDED, "2020-01-01 00:00:00")
        db.add_log(task_id, "info", "保留")
        manager = RetentionManager(
            db, config=SimpleNamespace(get=lambda: SimpleNamespace(retention_enabled=False)), log_dir=tmp_path)

        assert manager.cleanup(now=datetime(2026, 8, 23, tzinfo=timezone.utc)) == {
            "task_logs": 0, "notification_events": 0, "log_files": 0}
        assert len(db.list_logs(task_id)) == 1

    def test_cleanup_never_unlinks_log_outside_configured_directory(self, db, tmp_path):
        task_id = _task(db, 4, STATUS_SUCCEEDED, "2020-01-01 00:00:00")
        outside = tmp_path.parent / "outside-task.log"
        outside.write_text("must remain", encoding="utf-8")
        db.set_task_status(task_id, None, log_path=str(outside))
        manager = RetentionManager(db, config=SimpleNamespace(get=lambda: _settings()), log_dir=tmp_path)

        result = manager.cleanup(now=datetime(2026, 8, 23, tzinfo=timezone.utc))

        assert result["log_files"] == 0
        assert outside.exists()

class TestRetentionApi:
    def test_settings_retention_is_configurable_and_manual_api_returns_counts(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from botler.api import router as api_router
        from botler.config import ConfigManager

        config_path = tmp_path / "config.yaml"
        config_path.write_text("gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\nrepos: []\n", encoding="utf-8")
        config = ConfigManager(str(config_path))
        retention = RetentionManager(Database(str(tmp_path / "api.db")), config=config, log_dir=tmp_path / "logs")
        app = FastAPI()
        app.state.ctx = SimpleNamespace(config=config, retention=retention, db=retention.db)
        app.include_router(api_router)
        client = TestClient(app)

        assert client.get("/api/settings").json()["retention"]["task_logs_days"] == 90
        response = client.put("/api/settings", json={"retention": {"task_logs_days": 7}})
        assert response.status_code == 200
        assert response.json()["retention"]["task_logs_days"] == 7
        assert client.post("/api/retention/cleanup").json() == {
            "task_logs": 0, "notification_events": 0, "log_files": 0}

    @pytest.mark.parametrize("patch", [
        {"task_logs_days": 0}, {"notification_events_days": True},
        {"log_files_days": 3651}, {"pm2_max_log_size_mb": 0},
    ])
    def test_settings_rejects_invalid_retention_values(self, tmp_path, patch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from botler.api import router as api_router
        from botler.config import ConfigManager

        config_path = tmp_path / "config.yaml"
        config_path.write_text("gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\nrepos: []\n", encoding="utf-8")
        app = FastAPI()
        app.state.ctx = SimpleNamespace(config=ConfigManager(str(config_path)), db=Database(str(tmp_path / "api.db")))
        app.include_router(api_router)
        assert TestClient(app).put("/api/settings", json={"retention": patch}).status_code == 400

class TestPm2LogRotation:
    def test_oversized_pm2_log_is_gzipped_and_active_file_is_truncated(self, db, tmp_path):
        import gzip

        log = tmp_path / "pm2-out.log"
        original = b"x" * (1024 * 1024 + 1)
        log.write_bytes(original)
        manager = RetentionManager(db, config=SimpleNamespace(get=lambda: _settings()), log_dir=tmp_path)

        assert manager._rotate_pm2_logs(max_size_mb=1) == 1
        archives = list(tmp_path.glob("pm2-out.log.*.gz"))
        assert len(archives) == 1
        assert gzip.open(archives[0], "rb").read() == original
        assert log.stat().st_size == 0

    def test_log_at_or_below_limit_is_not_rotated(self, db, tmp_path):
        log = tmp_path / "pm2-error.log"
        log.write_bytes(b"x" * (1024 * 1024))
        manager = RetentionManager(db, config=SimpleNamespace(get=lambda: _settings()), log_dir=tmp_path)

        assert manager._rotate_pm2_logs(max_size_mb=1) == 0
        assert log.stat().st_size == 1024 * 1024

class TestBackupRetentionIntegration:
    def test_backup_runs_injected_retention_cleanup_before_snapshot(self, tmp_path, monkeypatch):
        from botler.backup import BotlerBackup

        config_path = tmp_path / "config.yaml"
        config_path.write_text("gitlab: {}\n", encoding="utf-8")
        db_path = tmp_path / "botler.db"
        db_path.write_bytes(b"")
        backup = BotlerBackup(str(config_path), str(db_path), str(tmp_path / "backups"))
        called = []
        backup.pre_backup_cleanup = lambda: called.append(True)
        monkeypatch.setattr(backup, "prune_backups", lambda days: [])

        backup.create_backup()

        assert called == [True]
