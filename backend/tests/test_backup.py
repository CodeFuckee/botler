"""备份与恢复模块测试：创建 / 列表 / 删除 / 保留清理 / 恢复 / 安全校验。

覆盖边界：空目录、sqlite WAL 连接中备份、路径穿越成员、缺 manifest、
校验和失败、非法文件名、恢复后数据还原。
"""

import io
import os
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from botler.backup import ALLOWED_FILES, BotlerBackup, BackupError


@pytest.fixture
def backup_obj(tmp_path, monkeypatch):
    """临时 config.yaml + botler.db + 独立备份目录。

    _restart 一律 mock（真实实现会 os.execv 重启进程，会连 pytest 一起重启）。
    """
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "botler.db"
    backup_dir = tmp_path / "backups"

    config_path.write_text("gitlab:\n  url: https://gitlab.example.com\n", encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(BotlerBackup, "_restart", lambda self, delay=0.0: None)
    return BotlerBackup(str(config_path), str(db_path), str(backup_dir))


def _read_archive(bak: BotlerBackup, name: str) -> dict[str, bytes]:
    """解包 tar.gz，返回 {成员名: 内容}。"""
    with tarfile.open(os.path.join(bak.backup_dir, name), "r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


class TestCreateBackup:
    def test_create_creates_tarball_with_all_files(self, backup_obj):
        """备份包应包含 manifest.json / config.yaml / botler.db 三个成员。"""
        info = backup_obj.create_backup()
        assert info["name"].startswith("botler-backup-")
        assert info["name"].endswith(".tar.gz")

        members = _read_archive(backup_obj, info["name"])
        assert set(members) == {"manifest.json", "config.yaml", "botler.db"}

    def test_manifest_checksums_match(self, backup_obj):
        """manifest 中记录的 sha256 应与实际文件一致。"""
        import hashlib
        info = backup_obj.create_backup()
        members = _read_archive(backup_obj, info["name"])
        manifest = __import__("json").loads(members["manifest.json"])
        for fname in ("config.yaml", "botler.db"):
            actual = hashlib.sha256(members[fname]).hexdigest()
            assert manifest["files"][fname]["sha256"] == actual

    def test_manifest_records_trigger(self, backup_obj):
        import json
        info = backup_obj.create_backup(trigger="scheduled")
        members = _read_archive(backup_obj, info["name"])
        assert json.loads(members["manifest.json"])["trigger"] == "scheduled"

    def test_create_with_wal_connection_open(self, backup_obj):
        """db 被 WAL 连接占用时仍能创建一致备份（sqlite backup API）。"""
        conn = sqlite3.connect(backup_obj.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS wal_t (id INTEGER)")
        conn.execute("INSERT INTO wal_t VALUES (42)")
        conn.commit()
        try:
            info = backup_obj.create_backup()
            # 校验备份内容包含未 checkpoint 的数据（sqlite 连接不支持内存 DB，落盘验证）
            with tarfile.open(os.path.join(backup_obj.backup_dir, info["name"]), "r:gz") as tf:
                db_bytes = tf.extractfile("botler.db").read()
            restore_path = os.path.join(os.path.dirname(backup_obj.backup_dir), "wal-restore.db")
            with open(restore_path, "wb") as f:
                f.write(db_bytes)
            restore_conn = sqlite3.connect(restore_path)
            assert restore_conn.execute("SELECT id FROM wal_t").fetchall() == [(42,)]
            restore_conn.close()
        finally:
            conn.close()


class TestListBackups:
    def test_list_empty(self, backup_obj):
        assert backup_obj.list_backups() == []

    def test_list_after_create(self, backup_obj):
        info = backup_obj.create_backup()
        items = backup_obj.list_backups()
        assert len(items) == 1
        assert items[0]["name"] == info["name"]
        assert items[0]["size"] > 0
        assert items[0]["created_at"]

    def test_list_sorted_newest_first(self, backup_obj):
        backup_obj.create_backup()
        time.sleep(1.1)  # 文件名秒级精度
        second = backup_obj.create_backup()
        items = backup_obj.list_backups()
        assert items[0]["name"] == second["name"]
        assert len(items) == 2


class TestDeleteBackup:
    def test_delete_existing(self, backup_obj):
        info = backup_obj.create_backup()
        backup_obj.delete_backup(info["name"])
        assert backup_obj.list_backups() == []

    def test_delete_missing_raises(self, backup_obj):
        with pytest.raises(BackupError):
            backup_obj.delete_backup("no-such.tar.gz")

    def test_delete_rejects_path_traversal(self, backup_obj):
        with pytest.raises(BackupError):
            backup_obj.delete_backup("../config.yaml")


class TestPrune:
    def test_prune_keeps_recent_deletes_old(self, backup_obj):
        old = backup_obj.create_backup()
        time.sleep(1.1)
        new = backup_obj.create_backup()
        # 把 old 的 mtime 拨到 10 天前
        old_path = os.path.join(backup_obj.backup_dir, old["name"])
        ten_days_ago = time.time() - 10 * 24 * 3600
        os.utime(old_path, (ten_days_ago, ten_days_ago))

        removed = backup_obj.prune_backups(retention_days=7)
        assert removed == [old["name"]]
        remaining = [b["name"] for b in backup_obj.list_backups()]
        assert remaining == [new["name"]]

    def test_prune_retention_zero_removes_all(self, backup_obj):
        backup_obj.create_backup()
        time.sleep(1.1)
        backup_obj.create_backup()
        backup_obj.prune_backups(retention_days=0)
        assert backup_obj.list_backups() == []


class TestRestore:
    def test_restore_roundtrip(self, backup_obj):
        """备份 → 修改数据 → 恢复 → 数据还原。"""
        info = backup_obj.create_backup()

        # 修改 config 与 db
        Path(backup_obj.config_path).write_text("gitlab:\n  url: https://changed.example.com\n", encoding="utf-8")
        conn = sqlite3.connect(backup_obj.db_path)
        conn.execute("INSERT INTO t (v) VALUES ('dirty')")
        conn.commit()
        conn.close()

        backup_obj.restore_backup(info["name"])

        assert "changed.example.com" not in Path(backup_obj.config_path).read_text(encoding="utf-8")
        conn = sqlite3.connect(backup_obj.db_path)
        values = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert values == ["hello"]

    def test_restore_upload_flow(self, backup_obj):
        """上传恢复：任意路径的合法备份包同样能恢复。"""
        info = backup_obj.create_backup()
        import shutil
        copy_path = os.path.join(os.path.dirname(backup_obj.backup_dir), "upload-copy.tar.gz")
        shutil.copy(os.path.join(backup_obj.backup_dir, info["name"]), copy_path)

        Path(backup_obj.config_path).write_text("changed: true\n", encoding="utf-8")
        backup_obj.restore_upload(copy_path)
        assert "changed: true" not in Path(backup_obj.config_path).read_text(encoding="utf-8")


class TestSecurity:
    @staticmethod
    def _make_archive(backup_obj, members: dict[str, bytes], name="evil.tar.gz"):
        """构造自定义备份包（可放恶意成员 / 缺文件 / 错校验和）。"""
        path = os.path.join(os.path.dirname(backup_obj.backup_dir), name)
        with tarfile.open(path, "w:gz") as tf:
            for mname, content in members.items():
                data = io.BytesIO(content)
                ti = tarfile.TarInfo(mname)
                ti.size = len(content)
                tf.addfile(ti, data)
        return str(path)

    def test_rejects_path_traversal_members(self, backup_obj):
        """成员名含 ../ 或绝对路径必须拒绝。"""
        path = self._make_archive(backup_obj, {"../../etc/evil": b"x"})
        with pytest.raises(BackupError, match="非法成员名"):
            backup_obj.restore_upload(path)

    def test_rejects_non_whitelist_members(self, backup_obj):
        """白名单外的成员（如 .env）必须拒绝。"""
        import json
        manifest = json.dumps({"app": "botler", "files": {}})
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            ".env": b"SECRET=1",
        })
        with pytest.raises(BackupError, match="非法成员名"):
            backup_obj.restore_upload(path)

    def test_rejects_missing_manifest(self, backup_obj):
        path = self._make_archive(backup_obj, {"config.yaml": b"x"})
        with pytest.raises(BackupError, match="manifest"):
            backup_obj.restore_upload(path)

    def test_rejects_bad_checksum(self, backup_obj):
        """manifest 校验和与实际内容不符必须拒绝。"""
        import hashlib
        import json
        content = b"tampered"
        manifest = json.dumps({
            "app": "botler",
            "files": {
                "config.yaml": {"sha256": hashlib.sha256(b"original").hexdigest()},
            },
        })
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            "config.yaml": content,
        })
        with pytest.raises(BackupError, match="校验和"):
            backup_obj.restore_upload(path)

    def test_restore_does_not_touch_whitelist_absent_files(self, backup_obj):
        """只替换包内白名单文件：包缺 botler.db 时 config.yaml 仍恢复。"""
        import hashlib
        import json
        content = "restored-config\n".encode()
        manifest = json.dumps({
            "app": "botler",
            "files": {"config.yaml": {"sha256": hashlib.sha256(content).hexdigest()}},
        })
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            "config.yaml": content,
        })
        Path(backup_obj.config_path).write_text("old\n", encoding="utf-8")
        backup_obj.restore_upload(path)
        assert Path(backup_obj.config_path).read_text(encoding="utf-8") == "restored-config\n"


class TestBackupDir:
    def test_missing_source_files_raise(self, tmp_path):
        """config / db 不存在时创建备份必须报错而非静默产生坏包。"""
        bak = BotlerBackup(str(tmp_path / "no-config.yaml"), str(tmp_path / "no.db"), str(tmp_path / "backups"))
        with pytest.raises(BackupError):
            bak.create_backup()

    def test_allowed_files_whitelist(self):
        assert set(ALLOWED_FILES) == {"config.yaml", "botler.db"}
