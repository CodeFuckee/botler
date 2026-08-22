"""GitLab token 到期预警测试（issue #279）。"""
from datetime import date
from botler.token_expiry import evaluate_expiry


def test_evaluate_expiry_has_all_threshold_levels():
    today = date(2026, 8, 22)
    assert evaluate_expiry("2026-10-01", today=today)["level"] == "healthy"
    assert evaluate_expiry("2026-09-21", today=today)["level"] == "warning"
    assert evaluate_expiry("2026-08-29", today=today)["level"] == "critical"
    assert evaluate_expiry("2026-08-25", today=today)["level"] == "urgent"
    assert evaluate_expiry("2026-08-21", today=today)["level"] == "expired"


def test_evaluate_expiry_rejects_invalid_and_empty_dates():
    today = date(2026, 8, 22)
    assert evaluate_expiry(None, today=today)["level"] == "unknown"
    assert evaluate_expiry("2026-02-30", today=today)["level"] == "unknown"


def test_checker_notifies_each_crossed_threshold_once(tmp_path):
    from botler.database import Database
    from botler.notifier import Notifier
    from botler.token_expiry import TokenExpiryChecker

    db = Database(str(tmp_path / "token-expiry.db"))
    repo_id = db.upsert_repo(42, "demo", "https://gitlab.example.com/demo.git")
    db.set_repo_token_expiry(repo_id, "2026-08-25")
    checker = TokenExpiryChecker(db, Notifier(db))

    assert checker.check(today=date(2026, 8, 22)) == ["alert_token_expiry_3d_repo_1"]
    assert checker.check(today=date(2026, 8, 22)) == []
    events = db.list_notifications()
    assert len(events) == 1
    assert "3 天" in events[0]["body"]


def test_database_persists_repo_expiry_and_api_shape(tmp_path):
    from botler.database import Database

    db = Database(str(tmp_path / "expiry.db"))
    repo_id = db.upsert_repo(8, "demo", "https://gitlab.example.com/demo.git")
    db.set_repo_token_expiry(repo_id, "2026-09-01")
    row = db.get_repo(repo_id)
    assert row["token_expires_at"] == "2026-09-01"
