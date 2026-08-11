"""备份/恢复 API 测试：列表 / 创建 / 下载 / 删除 / 恢复（本地+上传）/ 设置段。

沿用 test_api_settings 的最小 app 模式：SimpleNamespace ctx + 临时 config/db。
重启动作一律 monkeypatch，避免测试进程被 execv 干掉。
"""

import io
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.backup import BotlerBackup
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
backup:
  enabled: true
  retention_days: 30
repos: []
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))

    db_path = tmp_path / "botler.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('seed')")
    conn.commit()
    conn.close()

    backup = BotlerBackup(str(config_path), str(db_path), str(tmp_path / "backups"))
    # 恢复后的进程重启在测试中必须替换为记录调用
    restarted = []
    monkeypatch.setattr(BotlerBackup, "_restart", lambda self, delay=0.0: restarted.append(True))

    ctx = SimpleNamespace(
        config=config,
        db=Database(str(tmp_path / "test.db")),
        backup=backup,
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path, restarted


class TestBackupApi:
    def test_list_empty(self, client):
        tc, tmp_path, _ = client
        resp = tc.get("/api/backups")
        assert resp.status_code == 200
        data = resp.json()
        assert data["backups"] == []
        assert data["config"]["retention_days"] == 30
        assert data["config"]["enabled"] is True

    def test_create_then_list(self, client):
        tc, tmp_path, _ = client
        resp = tc.post("/api/backups")
        assert resp.status_code == 200
        name = resp.json()["name"]
        assert name.startswith("botler-backup-")

        resp = tc.get("/api/backups")
        items = resp.json()["backups"]
        assert len(items) == 1
        assert items[0]["name"] == name

    def test_download(self, client):
        tc, tmp_path, _ = client
        name = tc.post("/api/backups").json()["name"]
        resp = tc.get(f"/api/backups/{name}/download")
        assert resp.status_code == 200
        assert resp.content[:2] == b"\x1f\x8b"  # gzip magic

    def test_download_missing_404(self, client):
        tc, tmp_path, _ = client
        resp = tc.get("/api/backups/no-such.tar.gz/download")
        assert resp.status_code == 404

    def test_download_path_traversal_rejected(self, client):
        tc, tmp_path, _ = client
        resp = tc.get("/api/backups/..%2Fconfig.yaml/download")
        assert resp.status_code in (404, 400)

    def test_delete(self, client):
        tc, tmp_path, _ = client
        name = tc.post("/api/backups").json()["name"]
        resp = tc.delete(f"/api/backups/{name}")
        assert resp.status_code == 200
        assert tc.get("/api/backups").json()["backups"] == []

    def test_delete_missing_404(self, client):
        tc, tmp_path, _ = client
        resp = tc.delete("/api/backups/no-such.tar.gz")
        assert resp.status_code == 404

    def test_restore_local_triggers_restart(self, client):
        tc, tmp_path, restarted = client
        name = tc.post("/api/backups").json()["name"]
        resp = tc.post("/api/backups/restore", json={"name": name})
        assert resp.status_code == 200
        assert restarted == [True]

    def test_restore_local_missing_404(self, client):
        tc, tmp_path, _ = client
        resp = tc.post("/api/backups/restore", json={"name": "no-such.tar.gz"})
        assert resp.status_code == 404

    def test_restore_upload_triggers_restart(self, client):
        tc, tmp_path, restarted = client
        # 先创建一份合法备份作为上传内容
        name = tc.post("/api/backups").json()["name"]
        blob = (tmp_path / "backups" / name).read_bytes()

        resp = tc.post("/api/backups/restore/upload",
                       files={"file": ("backup.tar.gz", io.BytesIO(blob), "application/gzip")})
        assert resp.status_code == 200
        assert restarted == [True]

    def test_restore_upload_rejects_evil_archive(self, client):
        tc, tmp_path, restarted = client
        import tarfile
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tf:
            ti = tarfile.TarInfo("../../etc/passwd")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        resp = tc.post("/api/backups/restore/upload",
                       files={"file": ("evil.tar.gz", evil.getvalue(), "application/gzip")})
        assert resp.status_code == 400
        assert not restarted

    def test_restore_upload_missing_file_422(self, client):
        tc, tmp_path, _ = client
        resp = tc.post("/api/backups/restore/upload")
        assert resp.status_code == 422


class TestBackupSettings:
    def test_settings_include_backup_section(self, client):
        tc, tmp_path, _ = client
        data = tc.get("/api/settings").json()
        assert data["backup"] == {"enabled": True, "retention_days": 30}

    def test_update_retention_days_persists(self, client):
        tc, tmp_path, _ = client
        resp = tc.put("/api/settings", json={"backup": {"retention_days": 7}})
        assert resp.status_code == 200
        assert resp.json()["backup"]["retention_days"] == 7
        # config.yaml 是唯一事实来源，应已落盘
        config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "retention_days: 7" in config_text

    def test_update_backup_enabled(self, client):
        tc, tmp_path, _ = client
        resp = tc.put("/api/settings", json={"backup": {"enabled": False}})
        assert resp.status_code == 200
        assert resp.json()["backup"]["enabled"] is False

    def test_update_rejects_invalid_retention(self, client):
        tc, tmp_path, _ = client
        for bad in (0, -3, 366, "abc"):
            resp = tc.put("/api/settings", json={"backup": {"retention_days": bad}})
            assert resp.status_code == 400

    def test_update_rejects_non_bool_enabled(self, client):
        tc, tmp_path, _ = client
        resp = tc.put("/api/settings", json={"backup": {"enabled": "yes"}})
        assert resp.status_code == 400
